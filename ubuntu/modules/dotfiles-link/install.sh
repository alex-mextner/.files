#!/usr/bin/env bash
# dotfiles-link module: runs the dotfiles repository's own Ubuntu installer (../../../install-ubuntu.sh),
# then converges the machine to the rig global config that installer merged (`rig apply commit`; with
# rig-cli#746 merged this becomes rig's own `rig activate commit`).
#
#   install.sh           apply
#   install.sh --plan    read-only: what it would change
#   install.sh --check   read-only: 0 = nothing pending, 3 = drift (the installer's 1, or rig status), 10 = manual step
set -u

repo=$(cd "$(dirname "$0")/../../.." && pwd -P)
script="$repo/install-ubuntu.sh"
[ -x "$script" ] || { echo "needs manual: $script is missing or not executable" >&2; exit 10; }

case "${1:-}" in
  --plan)
    "$script" --plan; rc=$?
    command -v rig >/dev/null 2>&1 && { echo; (cd "$repo" && rig apply 2>&1 | tail -n +1 | sed 's/^/rig: /'); }
    exit "$rc" ;;
  --check)
    "$script" --check; rc=$?
    [ "$rc" -eq 1 ] && rc=3
    if [ "$rc" -eq 0 ] && command -v rig >/dev/null 2>&1; then
      (cd "$repo" && rig status >/dev/null 2>&1) || { echo "pending: rig status reports drift (rig apply commit)"; rc=3; }
    fi
    exit "$rc" ;;
  "")
    "$script"; rc=$?
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 10 ]; then
      if command -v rig >/dev/null 2>&1; then
        (cd "$repo" && rig apply commit) || { echo "needs manual: rig apply commit failed (see above)" >&2; exit 10; }
      else
        echo "needs manual: rig is not installed; install it (module rig), then re-run" >&2; exit 10
      fi
    fi
    exit "$rc" ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac
