#!/bin/bash
# ralphex external review via DeepSeek API (deepseek-reasoner)
# receives: $1 = path to prompt file (review instructions + diff)
# outputs: review findings to stdout + RALPHEX:CODEX_REVIEW_DONE signal
set -euo pipefail

PROMPT_FILE="$1"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "ERROR: prompt file not found: $PROMPT_FILE" >&2
  exit 1
fi

AUTH_FILE="${HOME}/.local/share/opencode/auth.json"
API_KEY=$(jq -r '.deepseek.key' "$AUTH_FILE")
PROMPT_CONTENT=$(cat "$PROMPT_FILE")

RESPONSE=$(curl -s "https://api.deepseek.com/chat/completions" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg content "$PROMPT_CONTENT" \
    '{
      model: "deepseek-reasoner",
      messages: [{"role": "user", "content": $content}],
      max_tokens: 16384
    }')")

ERROR=$(echo "$RESPONSE" | jq -r '.error.message // empty')
if [[ -n "$ERROR" ]]; then
  echo "ERROR from DeepSeek API: $ERROR" >&2
  exit 1
fi

echo "$RESPONSE" | jq -r '.choices[0].message.content'

echo ""
echo "<<<RALPHEX:CODEX_REVIEW_DONE>>>"
