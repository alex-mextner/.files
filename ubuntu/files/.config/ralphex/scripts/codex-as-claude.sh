#!/usr/bin/env bash
# codex-as-claude.sh - wraps codex CLI to produce Claude-compatible stream-json output.
#
# Translates Codex JSONL events into the Claude stream-json format expected by
# ralphex's ClaudeExecutor, allowing Codex to be used as a drop-in replacement
# for Claude in task and review phases.
#
# Config example (~/.config/ralphex/config or .ralphex/config):
#   claude_command = /Users/ultra/.config/ralphex/scripts/codex-as-claude.sh
#   claude_args =
#
# Environment variables:
#   CODEX_MODEL   - codex model to use
#   CODEX_SANDBOX - sandbox mode, default danger-full-access
#   CODEX_VERBOSE - set to 1 to include command execution output

set -euo pipefail

command -v jq >/dev/null 2>&1 || {
  echo "error: jq is required but not found" >&2
  exit 1
}

prompt=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p)
      prompt="${2:-}"
      shift
      shift 2>/dev/null || true
      ;;
    *)
      shift
      ;;
  esac
done

if [[ -z "$prompt" && ! -t 0 ]]; then
  prompt=$(cat)
fi

if [[ -z "$prompt" ]]; then
  echo "error: no prompt provided (expected -p flag or stdin)" >&2
  exit 1
fi

CODEX_MODEL="${CODEX_MODEL:-gpt-5.5}"
CODEX_SANDBOX="${CODEX_SANDBOX:-danger-full-access}"
CODEX_VERBOSE="${CODEX_VERBOSE:-0}"

if [[ "$CODEX_VERBOSE" != "0" && "$CODEX_VERBOSE" != "1" ]]; then
  echo "warning: CODEX_VERBOSE must be 0 or 1, got '$CODEX_VERBOSE', defaulting to 0" >&2
  CODEX_VERBOSE=0
fi

is_review_prompt=0
if [[ "$prompt" == *"<<<RALPHEX:REVIEW_DONE>>>"* ]]; then
  is_review_prompt=1
fi

if [[ "$is_review_prompt" == "1" ]]; then
  adapter_text=$'Ralphex review adapter for Codex:\n- Interpret review "Task tool" instructions using codex collaboration tools: spawn_agent, send_input, wait, close_agent.\n- Launch all requested review agents in parallel in one turn.\n- Wait for all spawned review agents before collecting findings and applying fixes.\n- Keep original review workflow and all <<<RALPHEX:...>>> signals unchanged.'
  prompt="$adapter_text"$'\n\n'"$prompt"
fi

codex_args=(exec --json --dangerously-bypass-approvals-and-sandbox -s "$CODEX_SANDBOX")
[[ -n "$CODEX_MODEL" ]] && codex_args+=(-m "$CODEX_MODEL")
if [[ "$is_review_prompt" == "1" ]]; then
  codex_args+=(-c "features.multi_agent=true")
fi

printf '%s' "$prompt" | codex "${codex_args[@]}" 2>/dev/null | while IFS= read -r line; do
  echo "$line" | jq -c --argjson verbose "$CODEX_VERBOSE" '
    if .type == "item.completed" then
      if .item.type == "agent_message" then
        {type: "content_block_delta", delta: {type: "text_delta", text: (.item.text + "\n")}}
      elif .item.type == "command_execution" and $verbose == 1 then
        {type: "content_block_delta", delta: {type: "text_delta",
          text: ("$ " + .item.command + "\n" + (.item.aggregated_output // "") + "\n")}}
      else empty
      end
    elif .type == "turn.completed" then
      {type: "result", result: ""}
    else empty
    end
  ' 2>/dev/null || true
done || true

echo '{"type":"result","result":""}'
