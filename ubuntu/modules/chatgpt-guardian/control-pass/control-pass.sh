#!/usr/bin/env bash
# 30-minute proactive control pass (systemd user timer guardian-proactive-control).
# 1. control-precheck.py: deterministic checks, starts an inactive sender, sends the standing Go
#    when the Guardian did not, verifies earlier Go requests, writes control-passes/latest.json.
# 2. The model (CONTROL_PASS_BRIEF.md) runs ONLY when the pre-check reports an anomaly it cannot
#    resolve (exit 10); a healthy pass never calls it (ticket #1126). No Telegram/Slack/email.
# Usage: control-pass.sh [--dry-run]   (dry run: never starts, posts or requests; no model)
# The timer runs the copy in $P (guardian-recovery state dir), not this directory: after a change here,
# install with `cp control-pass.sh control-precheck.py CONTROL_PASS_BRIEF.md "$P"/` (the units are not in this repo).
set -u
P=/home/ultra/.local/state/guardian-recovery-20261101-1653
VENV=/home/ultra/.local/share/agent-tools/chatgpt-guardian-venv
MODEL="${GUARDIAN_CONTROL_MODEL:-anthropic/claude-sonnet-5-5}"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"
mkdir -p "$P/control-passes"
exec 9>"$P/control-passes/.lock"
if ! flock -n 9; then
  echo "$(date -Is) previous pass still running; skipping" >> "$P/control-passes/skipped.log"
  echo "previous pass still running; skipped"
  exit 0
fi
LOG="$P/control-passes/$(date +%Y%m%dT%H%M%S)${DRY:+.dry-run}.log"
exec >>"$LOG" 2>&1
echo "== pass start $(date -Is) ${DRY:-live} model=$MODEL"
cd "$P" || exit 1
nice -n 15 ionice -c3 timeout 600 "$VENV/bin/python" "$P/control-precheck.py" $DRY
rc=$?
echo "== precheck rc=$rc"
if [ "$rc" -eq 10 ]; then
  if [ -n "$DRY" ]; then
    echo "dry-run: anomalies need the model; would run $MODEL"
    rc=0
  else
    # Quiet by construction: tg/slack/mail resolve to refusing stubs for the model's process only.
    PATH="$P/bin-quiet:$PATH" nice -n 15 ionice -c3 timeout 1500 omp -p --no-title --no-pty --no-lsp \
      --no-session --model "$MODEL" --max-time 25m "@$P/CONTROL_PASS_BRIEF.md" \
      "Pass files: status JSON $P/control-passes/latest.json (its summary_file is the markdown to append to)."
    rc=$?
    echo "== model rc=$rc"
    outcome=ok; [ "$rc" -eq 0 ] || outcome=failed  # a failed run is asked again after 2 h, not every pass
    "$VENV/bin/python" "$P/control-precheck.py" --record-model "$outcome"
    echo "== model recorded $outcome rc=$?"
  fi
fi
echo "== pass end $(date -Is) rc=$rc"
exit $rc
