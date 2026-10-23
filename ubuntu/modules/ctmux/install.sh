#!/usr/bin/env bash
# ctmux module installer (user level, never root): clones ctmux-cli and runs its install.sh, which
# links ~/.local/bin/ctmux, writes the ctmux.desktop terminal entry and advertises the agent skill.
#
#   install.sh           clone (when missing) + ctmux-cli/install.sh
#   install.sh --plan    read-only: the steps this would take
#   install.sh --check   read-only: exit 0 = installed correctly, 3 = drift
#
# Exit codes (the ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual step,
# 75 transient failure (the engine retries), anything else is a failure. Options come from [modules.ctmux]
# as JSON in UBUNTU_TWEAK_MODULE_CONFIG:
#
#   repo   git URL of ctmux-cli (default: the module's [source] repo)
#   dir    where the checkout lives (default ~/xp/ctmux-cli, ctmux's own README default)
set -euo pipefail

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

opt() { # opt KEY DEFAULT: one option of UBUNTU_TWEAK_MODULE_CONFIG
  python3 -B -c 'import json, os, sys
print(json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}").get(sys.argv[1], sys.argv[2]))' "$1" "$2"
}

repo=$(opt repo "https://github.com/alex-mextner/ctmux-cli.git")
dir=$(opt dir "$HOME/xp/ctmux-cli")
case "$dir" in \~/*) dir="$HOME/${dir#\~/}" ;; esac
case "$dir" in /*) ;; *) echo "[modules.ctmux] dir must be absolute or start with ~/: $dir" >&2; exit 2 ;; esac
case "$repo" in -*) echo "[modules.ctmux] repo must not start with '-': $repo" >&2; exit 2 ;; esac

# a ctmux checkout: a git work tree with both entry points; any other project that merely has an install.sh is not
has_checkout() { [ -e "$dir/.git" ] && [ -f "$dir/install.sh" ] && [ -f "$dir/bin/ctmux" ] && [ -f "$dir/linux/ctmux" ]; }

foreign() { # a non-empty directory that is not a ctmux checkout is the user's: never run its scripts
  if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ] && ! has_checkout; then
    echo "needs manual: $dir exists and is not a ctmux checkout; move it or set [modules.ctmux] dir" >&2
    exit 10
  fi
}

if [ "$mode" = check ]; then
  foreign
  if ! has_checkout; then echo "drift: no ctmux checkout at $dir"; exit 3; fi
  rc=0
  bash "$dir/install.sh" --check || rc=$?
  case "$rc" in
    0) exit 0 ;;
    1) exit 3 ;;   # ctmux's own "not installed correctly"
    *) echo "ctmux install.sh --check failed with exit $rc (a broken checker is not drift)" >&2; exit 1 ;;
  esac
fi

if [ "$mode" = plan ]; then
  foreign
  has_checkout || echo "git clone $repo $dir"
  echo "$dir/install.sh   (tmux + fzf if missing, ~/.local/bin/ctmux, ctmux.desktop, agent skill)"
  exit 0
fi

foreign
if ! has_checkout; then
  command -v git >/dev/null || { echo "needs manual: git is not installed" >&2; exit 10; }
  mkdir -p "$(dirname "$dir")"
  log=$(mktemp)
  trap 'rm -f "$log"' EXIT
  if ! git clone --quiet "$repo" "$dir" 2>"$log"; then
    cat "$log" >&2
    # 75 = retry with backoff: a network cause; anything else (bad URL, no access) is final
    if grep -qiE 'could not resolve|unable to access|timed out|connection (reset|refused)|temporary failure' "$log"; then exit 75; fi
    exit 1
  fi
fi
rc=0
bash "$dir/install.sh" || rc=$?
case "$rc" in
  0) exit 0 ;;
  3|10|75) echo "ctmux install.sh exited $rc, which the engine would read as drift/manual/retry; reporting a failure instead" >&2; exit 1 ;;
  *) exit "$rc" ;;
esac
