#!/usr/bin/env bash
# dotfiles-link module: runs the dotfiles repository's own Ubuntu installer (../../../install-ubuntu.sh),
# then converges the machine to the rig global config that installer merged (`rig apply commit`; with
# rig-cli#746 merged this becomes rig's own `rig activate commit`). rig itself is installed by the
# `rig` module (order 100, before this one); without it the apply path ends in a manual step.
#
#   install.sh           apply
#   install.sh --plan    read-only: what it would change
#   install.sh --check   read-only: 0 = nothing pending, 3 = drift (the installer's 1, or rig status), 10 = manual step
set -u

repo=$(cd "$(dirname "$0")/../../.." && pwd -P)
script="$repo/install-ubuntu.sh"
[ -x "$script" ] || { echo "needs manual: $script is missing or not executable" >&2; exit 10; }

have_rig() { command -v rig >/dev/null 2>&1; }
# in_repo <command...>: rig reads the repository's rig.yaml on top of the global config
in_repo() { (cd "$repo" && "$@"); }

case "${1:-}" in
  --plan)
    "$script" --plan; rc=$?
    if have_rig; then echo "rig apply (preview):"; in_repo rig apply 2>&1 | sed 's/^/  /'; fi
    exit "$rc" ;;
  --check)
    "$script" --check; rc=$?
    [ "$rc" -eq 1 ] && rc=3
    # rig drift counts even while the installer still has a manual step (10), but not after a usage error
    if [ "$rc" -ne 2 ] && have_rig && ! in_repo rig status >/dev/null 2>&1; then
      echo "pending: rig status reports drift (rig apply commit)"
      rc=3
    fi
    exit "$rc" ;;
  "")
    "$script"; rc=$?
    # a failed or manual installer (rig/config.yaml possibly not merged) must not be followed by a rig apply
    [ "$rc" -eq 0 ] || exit "$rc"
    have_rig || { echo "needs manual: rig is not installed; install it (module rig), then re-run" >&2; exit 10; }
    in_repo rig apply commit || { echo "needs manual: rig apply commit failed (see above)" >&2; exit 10; }
    exit 0 ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac
