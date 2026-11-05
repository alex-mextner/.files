#!/usr/bin/env bash
# 3d-cli profile module (user level, never root): puts the `3d` command of a local 3d-cli checkout on PATH and
# reports whether this machine has its Spoolman / PrintOps client configured.
#
#   install.sh           make every fact below true (never deploys anything)
#   install.sh --plan    read-only: what install is going to do
#   install.sh --check   read-only: exit 0 = all facts hold, 3 = drift (one line each), 1 = the check broke
#
# Exit codes (the ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual step
# (the last output line says which), anything else is a failure.
#
# Facts, in order (each one is skipped, not failed, when what it needs is not there):
#   1. a 3d-cli checkout exists at <dir> (default ~/xp/3d-cli). None = nothing to install here, the module is inert.
#      This module never clones it: the checkout is the user's working copy.
#   2. ~/.local/bin/3d is a symlink to <dir>/bin/3d (an existing file that is not that symlink is never replaced)
#   3. the checkout knows `3d init --env`: <dir>/lib/commands/init.py (the `3d init` implementation) names the flag.
#      It is read, never run: `3d` bootstraps itself (config migration, library clones) before it prints even
#      its help, and --check / --plan must change nothing. An older checkout is reported and skipped; update it
#      and the next apply picks the step up.
#   4. the Spoolman / PrintOps client is configured, resolved like the client does: `api` from $PRINTOPS_API, else
#      from ~/.config/3d-cli/printops.yaml ($XDG_CONFIG_HOME/3d-cli/ when set); an empty, null or unparsable value
#      does not count. The token is optional (a plain Spoolman server has none). Read from files and the environment only: no
#      network, so a machine that is offline, or whose server is down, still counts as configured.
#      When it is not, install runs ONE command and nothing else:
#          3d init --env --no-input --spoolman auto
#      which never prompts and installs nothing: it reports the status and prints the exact command that
#      installs Spoolman (on a printer, on this computer, over ssh, or as a script). Whether to install, and where,
#      stays the user's decision, so install then ends with exit 10 (needs manual) until it is done.
#
# Options ([modules.3d-cli] of profile.toml / machine.toml, JSON in UBUNTU_TWEAK_MODULE_CONFIG):
#   dir        where the 3d-cli checkout lives (default ~/xp/3d-cli)
#   spoolman   "auto" (default) reports an unconfigured client; "skip" declines it on this machine (fact 4 is dropped)
set -euo pipefail

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; }

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  --finish-check) mode=finish-check ;;
  --finish-run) mode=finish-run ;;
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) echo "usage: $0 [--plan|--check|--finish-check|--finish-run]" >&2; exit 2 ;;
esac

py=$(command -v python3 || true)   # one interpreter for every call below, resolved before PATH is extended
if [ -z "$py" ]; then
  case "$mode" in check|finish-check) echo "python3 is not installed: the check cannot decide" >&2; exit 1 ;; esac
  echo "needs manual: python3 is not installed" >&2; [ "$mode" = plan ] && exit 0; exit 10
fi

opt() { # opt KEY DEFAULT: one option of UBUNTU_TWEAK_MODULE_CONFIG
  "$py" -B -c 'import json, os, sys
print(json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}").get(sys.argv[1], sys.argv[2]))' "$1" "$2"
}

bad=$("$py" -B -c 'import json, os
table = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}")
problems = [f"unknown option {key}" for key in sorted(set(table) - {"dir", "spoolman"})]
problems += [f"{key} must be a string" for key in ("dir", "spoolman") if key in table and not isinstance(table[key], str)]
if table.get("spoolman", "auto") not in ("auto", "skip"):
    problems.append("spoolman must be \"auto\" or \"skip\"")
print("; ".join(problems))')
[ -z "$bad" ] || { echo "[modules.3d-cli] $bad" >&2; exit 1; }

dir=$(opt dir "$HOME/xp/3d-cli")
spoolman=$(opt spoolman auto)
case "$dir" in \~/*) dir="$HOME/${dir#\~/}" ;; esac
case "$dir" in /*) ;; *) echo "[modules.3d-cli] dir must be absolute or start with ~/: $dir" >&2; exit 1 ;; esac

# 3d is a Python dispatcher (uv resolves its dependencies): the login shell's PATH, whichever shell runs this.
export PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"

bin="$HOME/.local/bin"
tool="$dir/bin/3d"
config="${XDG_CONFIG_HOME:-$HOME/.config}/3d-cli/printops.yaml"
init_cmd=("$tool" init --env --no-input --spoolman auto)

has_checkout() { [ -f "$tool" ] && [ -e "$dir/.git" ]; }
link_ok() { [ -L "$bin/3d" ] && [ "$(readlink -f "$bin/3d")" = "$(readlink -f "$tool")" ]; }
link_foreign() { [ -e "$bin/3d" ] || [ -L "$bin/3d" ]; }   # something is at the link path, and link_ok said it is not ours
# A checkout that predates `3d init --env` would read the flag as a project scaffold, so the flag is looked up in the
# source of `3d init` instead of running 3d (see fact 3: running it is not read-only).
# The same checkout must also let `--env` skip the first-run OpenSCAD library bootstrap (clones BOSL2 and NopSCADlib into
# the checkout before the command runs), which lib/cli/dispatch.py names `_wants_openscad_bootstrap`.
has_env_init() {
  grep -qF -e '--env' "$dir/lib/commands/init.py" 2>/dev/null &&
    grep -qF -e '_wants_openscad_bootstrap' "$dir/lib/cli/dispatch.py" 2>/dev/null
}
# the api resolved the way 3d's printops client loads it (environment first, then printops.yaml); the token is optional
# state: 0 configured, 1 no api, 2 the file does not parse (it is then never handed to 3d, whose parse error
# can quote the token it choked on)
state=1
configured() { "$py" -B "$(dirname "$(readlink -f "$0")")/printops_configured.py" "$config"; state=$?; [ "$state" -eq 0 ]; }
unparsable() { echo "$config does not parse as a mapping with api: (fix or remove it, then run \`3d init --env\`; a valid file the built-in reader cannot judge needs python3-yaml)"; }

# -- inert without a checkout --------------------------------------------------------------------
if ! has_checkout; then
  echo "no 3d-cli checkout at $dir: nothing to install (set [modules.3d-cli] dir, or clone alex-mextner/3d-cli there)"
  exit 0
fi

# -- finish-setup step: the one decision this module leaves to the user ----------------------------
# --finish-check: exit 0 when nothing is left to decide (configured, declined, or a checkout that cannot do it yet), 1 otherwise.
# --finish-run: the interactive command itself (UBUNTU_TWEAK_FINISH_DRY_RUN=1 prints it and changes nothing).
if [ "$mode" = finish-check ]; then
  [ "$spoolman" = auto ] && has_env_init || exit 0
  # a launcher that is not executable or a file that does not parse keeps the step pending: --finish-run says what to fix
  configured && exit 0
  exit 1
fi
if [ "$mode" = finish-run ]; then
  # same gate as --finish-check: an old checkout would read `--env` as a project scaffold, and a declined machine has nothing to decide
  if [ "$spoolman" != auto ] || ! has_env_init; then echo "nothing to do: spoolman is declined or this checkout has no \`3d init --env\`"; exit 0; fi
  [ -x "$tool" ] || { echo "$tool is not executable" >&2; exit 1; }
  configured && exit 0
  # a file that does not parse is never handed to 3d: its parse error quotes the line, and the line holds the token
  if [ "$state" -eq 2 ]; then unparsable >&2; exit 1; fi
  # the engine runs this inside the module directory: a script that `3d init --env` writes for a server (spoolman-setup.sh)
  # belongs in the user's home, where the command it prints to copy it will find it
  if [ "${UBUNTU_TWEAK_FINISH_DRY_RUN:-}" = 1 ]; then echo "dry run: would run \`3d init --env\` in $HOME"; exit 0; fi
  cd "$HOME" || exit 1
  exec "$tool" init --env
fi

# -- --check: read-only ------------------------------------------------------------------------
if [ "$mode" = check ]; then
  problems=()
  [ -x "$tool" ] || problems+=("$tool is not executable (chmod +x it)")
  if ! link_ok; then
    if link_foreign; then problems+=("$bin/3d exists and is not a link to $tool (install will ask you to move it)")
    else problems+=("$bin/3d is missing"); fi
  fi
  if [ "$spoolman" = auto ] && ! configured && has_env_init; then
    if [ "$state" -eq 2 ]; then problems+=("Spoolman / PrintOps client is not configured: $(unparsable)")
    else problems+=("Spoolman / PrintOps client is not configured ($config has no api:)"); fi
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'drift: %s\n' "${problems[@]}"
    exit 3
  fi
  if [ "$spoolman" = skip ]; then echo "3d is on PATH from $dir; its Spoolman client is declined on this machine"
  elif ! has_env_init; then echo "3d is on PATH from $dir; its Spoolman step is skipped (this checkout has no \`3d init --env\` yet)"
  else echo "3d is on PATH from $dir and its Spoolman client is configured"; fi
  exit 0
fi

# -- --plan: read-only -------------------------------------------------------------------------
if [ "$mode" = plan ]; then
  [ -x "$tool" ] || echo "plan: $tool is not executable: install will ask you to chmod +x it (needs manual)"
  if link_ok; then echo "link: $bin/3d already points at $tool"
  elif link_foreign; then echo "link: $bin/3d exists and is not a link to $tool: needs manual"
  else echo "ln -s $tool $bin/3d"; fi
  if [ "$spoolman" = skip ]; then echo "spoolman: declined on this machine ([modules.3d-cli] spoolman = \"skip\")"
  elif configured; then echo "spoolman: client configured in $config (nothing to do)"
  elif ! has_env_init; then echo "spoolman: this checkout has no \`3d init --env\` yet (update it); skipped"
  elif [ "$state" -eq 2 ]; then echo "spoolman: $(unparsable): needs manual"
  else
    echo "${init_cmd[*]}   # reports only: never prompts, never installs or deploys"
    echo "spoolman: still unconfigured afterwards: needs manual (run: 3d init --env)"
  fi
  exit 0
fi

# -- install -----------------------------------------------------------------------------------
[ -x "$tool" ] || { echo "needs manual: $tool is not executable; chmod +x it" >&2; exit 10; }
if ! link_ok; then
  # another apply may create the very same link between the checks and the `ln`: a correct link is always success
  if link_foreign && ! link_ok; then echo "needs manual: $bin/3d exists and is not a link to $tool; move it away" >&2; exit 10; fi
  mkdir -p "$bin"
  if ln -sT "$tool" "$bin/3d" 2>/dev/null; then echo "linked $bin/3d -> $tool"
  elif ! link_ok; then echo "needs manual: cannot link $bin/3d to $tool (something else is there); move it away" >&2; exit 10; fi
fi

[ "$spoolman" = auto ] || { echo "spoolman: declined on this machine"; exit 0; }
configured && { echo "spoolman: client already configured in $config"; exit 0; }
if ! has_env_init; then
  echo "spoolman: $dir has no \`3d init --env\` yet; update that checkout and run apply again (skipped)"
  exit 0
fi

if [ "$state" -eq 2 ]; then echo "needs manual: $(unparsable)" >&2; exit 10; fi

# Report only, and never let it fail the run: an offline machine or a crash in the tool is not this module's failure.
rc=0
# run from $HOME: the engine's working directory is the module directory inside the dotfiles repository
(cd "$HOME" && timeout 120 "${init_cmd[@]}" </dev/null) || rc=$?
[ "$rc" -eq 0 ] || echo "warning: \`3d ${init_cmd[*]:1}\` exited $rc (not fatal)" >&2

if configured; then exit 0; fi
if [ "$state" -eq 2 ]; then echo "needs manual: $(unparsable)" >&2; exit 10; fi
echo "needs manual: Spoolman is not set up for 3d-cli on this machine: run \`3d init --env\` and pick where it should live" >&2
exit 10
