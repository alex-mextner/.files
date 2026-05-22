#!/usr/bin/env bash
# install-worktree-deps.sh
# Claude Code PostToolUse hook — runs after EnterWorktree tool
# Detects the repo's package manager and installs deps in the new worktree.
# stdin: JSON { tool_name, tool_input, tool_response }

set -euo pipefail

input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // empty')

[[ "$tool_name" == "EnterWorktree" ]] || exit 0

worktree_path=$(echo "$input" | jq -r '.tool_response.worktreePath // empty')
[[ -n "$worktree_path" && -d "$worktree_path" ]] || exit 0

cd "$worktree_path"

[[ -f "package.json" ]] || exit 0

if [[ -f "bun.lockb" || -f "bunfig.toml" ]]; then
  echo "[worktree-deps] bun install in $worktree_path" >&2
  /Users/ultra/.bun/bin/bun install --frozen-lockfile 2>&1 || true
elif [[ -f "pnpm-lock.yaml" ]]; then
  echo "[worktree-deps] pnpm install in $worktree_path" >&2
  pnpm install --frozen-lockfile 2>&1 || true
elif [[ -f "yarn.lock" ]]; then
  echo "[worktree-deps] yarn install in $worktree_path" >&2
  yarn install --frozen-lockfile 2>&1 || true
elif [[ -f "package-lock.json" ]]; then
  echo "[worktree-deps] npm ci in $worktree_path" >&2
  npm ci 2>&1 || true
fi

exit 0
