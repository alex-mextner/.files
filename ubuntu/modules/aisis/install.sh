#!/usr/bin/env bash
# aisis profile module (user level, never root): the AISIS voice stack's systemd user units come from the aisis
# repository, not from this one.
#
#   install.sh           make every fact below true
#   install.sh --plan    read-only: what install is going to do
#   install.sh --check   read-only: exit 0 = all facts hold, 3 = drift (one line each), 1 = the check broke
#
# Exit codes (the ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual step
# (the last output line says which), anything else is a failure.
#
# Facts, in order:
#   1. a checkout of aisis exists at ~/xp/aisis (cloned from <repo> when missing; an existing one is the user's working
#      copy and is never pulled or reset). The path is fixed: the units name %h/xp/aisis in WorkingDirectory= and ExecStart=
#   2. ~/xp/aisis/scripts/install-systemd-units.sh exists (a checkout that predates it needs `git pull` by the user)
#   3. that script's own --check holds: every unit and drop-in of services/*/systemd is in ~/.config/systemd/user
#      byte for byte, every unit with an [Install] section is enabled and active. `install` runs the script, which also
#      reloads systemd and starts what is not running; a running unit is never restarted. A unit that does not start
#      (runtime state missing) is reported as needs manual, and stays drift in --check until it runs.
#
# Options ([modules.aisis] of profile.toml / machine.toml, JSON in UBUNTU_TWEAK_MODULE_CONFIG):
#   repo           git URL of aisis (default https://github.com/alex-mextner/aisis.git)
#   require_mount  a path (absolute or ~/...) that must be a mount point before install may create ~/xp or clone into it
#                  (default none). The PC sets ~/xp: it is a bind mount of the backups disk, and without the disk a clone
#                  would land on the nearly full root filesystem. A checkout that already exists never needs it.
set -euo pipefail

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; }

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

py=$(command -v python3 || true)
if [ -z "$py" ]; then
  [ "$mode" = check ] && { echo "python3 is not installed: the check cannot decide" >&2; exit 1; }
  echo "needs manual: python3 is not installed" >&2; exit 10
fi

opt() { # opt KEY DEFAULT: one option of UBUNTU_TWEAK_MODULE_CONFIG
  "$py" -B -c 'import json, os, sys
value = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}").get(sys.argv[1], sys.argv[2])
print(value)' "$1" "$2"
}

bad=$("$py" -B -c 'import json, os
table = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}")
problems = [f"unknown option {key}" for key in sorted(set(table) - {"repo", "require_mount"})]
problems += [f"{key} must be a string" for key in ("repo", "require_mount") if key in table and not isinstance(table[key], str)]
print("; ".join(problems))')
[ -z "$bad" ] || { echo "[modules.aisis] $bad" >&2; exit 1; }

repo_url=$(opt repo "https://github.com/alex-mextner/aisis.git")
require_mount=$(opt require_mount "")
case "$require_mount" in \~/*) require_mount="$HOME/${require_mount#\~/}" ;; esac
case "$require_mount" in ""|/*) ;; *) echo "[modules.aisis] require_mount must be absolute or start with ~/: $require_mount" >&2; exit 1 ;; esac
dir="$HOME/xp/aisis"
# Diagnostics never carry URL credentials (https://user:token@host/...).
redact() { sed -E 's#(://)[^/@[:space:]]*@#\1#g'; }
shown_url=$(printf '%s' "$repo_url" | redact)
case "$repo_url" in -*) echo "[modules.aisis] repo must not start with '-': $shown_url" >&2; exit 1 ;; esac

installer="$dir/scripts/install-systemd-units.sh"
has_checkout() { [ -e "$dir/.git" ]; }

if [ "$mode" != install ]; then
  if ! has_checkout; then
    echo "no aisis checkout at $dir (install clones $shown_url there, then installs the units from it)"
    [ "$mode" = plan ] && exit 0
    exit 3
  fi
  if [ ! -f "$installer" ]; then
    echo "$installer is missing: this checkout predates the unit installer (git pull it)"
    [ "$mode" = plan ] && exit 0
    exit 3
  fi
  rc=0
  bash "$installer" "--$mode" || rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] || { echo "$installer --$mode failed (exit $rc)" >&2; exit 1; }
  exit "$rc"
fi

if ! has_checkout; then
  if [ -n "$require_mount" ] && ! mountpoint -q -- "$require_mount"; then
    echo "needs manual: $require_mount is not mounted; mount it first (a clone would land on the wrong disk), then run apply again" >&2
    exit 10
  fi
  command -v git >/dev/null || { echo "needs manual: git is not installed" >&2; exit 10; }
  mkdir -p "$(dirname "$dir")"
  if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    echo "needs manual: $dir exists, is not empty and is not a git checkout; move it away" >&2
    exit 10
  fi
  clone_err=$(GIT_TERMINAL_PROMPT=0 git clone --quiet -- "$repo_url" "$dir" 2>&1) || { printf '%s\n' "$clone_err" | redact >&2; echo "needs manual: git clone $shown_url failed (network or credentials)" >&2; exit 10; }
  echo "cloned: $shown_url -> $dir"
fi
[ -f "$installer" ] || { echo "needs manual: $installer is missing: the checkout predates the unit installer; git pull it, then run apply again" >&2; exit 10; }
exec bash "$installer"
