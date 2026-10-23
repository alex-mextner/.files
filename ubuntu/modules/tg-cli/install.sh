#!/usr/bin/env bash
# tg-cli profile module (user level, never root): the Telegram bridge `tg` + its inbound daemon `tg-ctl`,
# supervised by exactly ONE systemd user unit.
#
#   install.sh           make every fact below true
#   install.sh --plan    read-only: what install is going to do
#   install.sh --check   read-only: exit 0 = all facts hold, 3 = drift (one line each), 1 = the check broke
#
# Exit codes (the ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual step
# (the last output line says which), anything else is a failure.
#
# Facts, in order:
#   1. the checkout <dir> (default ~/.local/share/tg-cli, cloned from <repo>) holds tg and tg-ctl
#   2. ~/.local/bin/tg and ~/.local/bin/tg-ctl link to it
#   3. ~/.config/tg-cli/config.yaml says control.enabled: false (`tg` never spawns a detached daemon of its
#      own: the unit is the only supervisor; an existing file keeps every other key)
#   4. ~/.config/tg-cli/.env holds TG_BOT_TOKEN (a secret: never written by this module -> needs manual)
#   5. systemd linger is on for the user (the daemon starts at boot, not at login)
#   6. a drop-in pins the login shell's PATH on the unit (systemd's minimal PATH has no tmux from Linuxbrew:
#      every snapshot would see zero panes, tg-cli#767)
#   7. agenttools-tg-ctl-tg-ctl.service is installed (by `tg-ctl enable`), enabled and active
#   8. every running `tg-ctl run` is the unit's MainPID (no detached stray: two pollers = Telegram 409)
#
# Options ([modules.tg-cli] of profile.toml / machine.toml, JSON in UBUNTU_TWEAK_MODULE_CONFIG):
#   repo   git URL of tg-cli   (default https://git.hyperide.ai/ultrabricks/tg-cli.git)
#   dir    where the checkout lives (default ~/.local/share/tg-cli)
set -euo pipefail

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

if ! command -v python3 >/dev/null; then
  [ "$mode" = check ] && { echo "python3 is not installed: the check cannot decide" >&2; exit 1; }
  echo "needs manual: python3 is not installed" >&2; exit 10
fi

opt() { # opt KEY DEFAULT: one option of UBUNTU_TWEAK_MODULE_CONFIG
  python3 -B -c 'import json, os, sys
value = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}").get(sys.argv[1], sys.argv[2])
print(value)' "$1" "$2"
}

bad=$(python3 -B -c 'import json, os
table = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}")
problems = [f"unknown option {key}" for key in sorted(set(table) - {"repo", "dir"})]
problems += [f"{key} must be a string" for key in ("repo", "dir") if key in table and not isinstance(table[key], str)]
print("; ".join(problems))')
[ -z "$bad" ] || { echo "[modules.tg-cli] $bad" >&2; exit 1; }

repo_url=$(opt repo "https://git.hyperide.ai/ultrabricks/tg-cli.git")
dir=$(opt dir "$HOME/.local/share/tg-cli")
case "$dir" in \~/*) dir="$HOME/${dir#\~/}" ;; esac
case "$dir" in /*) ;; *) echo "[modules.tg-cli] dir must be absolute or start with ~/: $dir" >&2; exit 1 ;; esac
case "$repo_url" in -*) echo "[modules.tg-cli] repo must not start with '-': $repo_url" >&2; exit 1 ;; esac

# tg-ctl is a Bun script: it needs bun (and, for pane routing, tmux) on PATH whichever shell runs this.
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"

unit=agenttools-tg-ctl-tg-ctl.service
bin="$HOME/.local/bin"
cfg_dir="$HOME/.config/tg-cli"   # the daemon's default config dir; the unit pins no other
config="$cfg_dir/config.yaml"
dropin_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit.d"
dropin="$dropin_dir/path.conf"
unit_path="$HOME/.local/bin:$HOME/.bun/bin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:$HOME/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
dropin_text="# Managed by ubuntu-tweaks (profile module tg-cli).
# systemd --user hands the daemon a minimal PATH without Linuxbrew, so tg-ctl's bare \`tmux\` spawn fails
# (ENOENT), every snapshot sees zero panes and tmux-routed replies are refused (tg-cli#767). Pin the
# login shell's PATH.
[Service]
Environment=PATH=$unit_path
"

manual() { echo "needs manual: $1" >&2; exit 10; }
sc() { systemctl --user "$@"; }

has_checkout() { [ -f "$dir/tg" ] && [ -f "$dir/tg-ctl" ] && [ -e "$dir/.git" ]; }
link_ok() { [ -L "$bin/$1" ] && [ "$(readlink -f "$bin/$1")" = "$(readlink -f "$dir/$1")" ]; }
creds_ok() { grep -q '^TG_BOT_TOKEN=.' "$cfg_dir/.env" 2>/dev/null; }
linger_ok() { [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" = yes ]; }
dropin_ok() { [ -f "$dropin" ] && [ "$(cat "$dropin")" = "${dropin_text%$'\n'}" ]; }
unit_installed() { [ "$(sc show -p LoadState --value "$unit" 2>/dev/null)" = loaded ]; }
# A running daemon is `bun <checkout>/tg-ctl run` (what the unit's ExecStart and a hand start both exec).
daemons() { # pgrep narrows by cmdline; the exact shape `<bun> <checkout>/tg-ctl run` is confirmed per pid
  local pid
  for pid in $(pgrep -u "$(id -u)" -f '/tg-ctl run( |$)' || true); do
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qE '^[^ ]*bun [^ ]*/tg-ctl run ?$' && echo "$pid"
  done
  return 0
}
live_path_ok() { # the loaded unit pins the PATH AND the daemon started after the drop-in was written
  local entered started
  sc show -p Environment --value "$unit" 2>/dev/null | grep -qF "PATH=$unit_path" || return 1
  entered=$(sc show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null)
  [ -n "$entered" ] && started=$(date -d "$entered" +%s 2>/dev/null) && [ "$started" -ge "$(stat -c %Y "$dropin" 2>/dev/null || echo 0)" ]
}
in_unit() { grep -q "/$unit\$" "/proc/$1/cgroup" 2>/dev/null; }   # the unit's cgroup, whatever its process shape
strays() { # daemons outside the unit
  local pid
  while read -r pid; do [ -z "$pid" ] || in_unit "$pid" || echo "$pid"; done < <(daemons)
}

self=$(readlink -f "$0")   # the verify loop re-runs this very file, whatever $0 looked like
config_py() { python3 -B "$(dirname "$self")/config_control.py" "$config" "$1"; }   # check|apply
config_state() { # sets cfg_state: ok | drift | unsupported; any other exit of the rewriter is a broken check
  local rc=0
  config_py check 2>/dev/null || rc=$?
  case "$rc" in
    0) cfg_state=ok ;;
    3) cfg_state=drift ;;
    4) cfg_state=unsupported ;;
    *) echo "config_control.py check failed (exit $rc) on $config" >&2; exit 1 ;;
  esac
}

# -- --check: read-only ------------------------------------------------------------------------
if [ "$mode" = check ]; then
  problems=()
  has_checkout || problems+=("tg-cli checkout missing at $dir")
  if has_checkout; then
    link_ok tg || problems+=("$bin/tg is missing or points elsewhere")
    link_ok tg-ctl || problems+=("$bin/tg-ctl is missing or points elsewhere")
  fi
  config_state
  case "$cfg_state" in
    ok) ;;
    drift) problems+=("$config does not set control.enabled: false") ;;
    unsupported) problems+=("$config writes control in a style this module does not edit (set control.enabled: false by hand)") ;;
  esac
  creds_ok || problems+=("$cfg_dir/.env has no TG_BOT_TOKEN")
  linger_ok || problems+=("systemd linger is off for $(id -un) (loginctl enable-linger)")
  dropin_ok || problems+=("$dropin is missing or differs (PATH pin)")
  if sc show -p Id --value "$unit" >/dev/null 2>&1; then
    if unit_installed; then
      [ "$(sc is-enabled "$unit" 2>/dev/null || true)" = enabled ] || problems+=("$unit is not enabled")
      [ "$(sc is-active "$unit" 2>/dev/null || true)" = active ] || problems+=("$unit is not active")
      sc show -p ExecStart --value "$unit" | grep -qF -e "$dir/tg-ctl run" -e "$(readlink -f "$dir")/tg-ctl run" || problems+=("$unit runs another checkout than $dir (run $dir/tg-ctl enable; [modules.tg-cli] dir and repo only matter for the first install)")
      live_path_ok || problems+=("the daemon runs without the pinned PATH (systemctl --user restart $unit)")
      stray=$(strays | tr '\n' ' ')
      [ -z "$stray" ] || problems+=("tg-ctl daemon(s) outside the unit: ${stray% }")
    else
      problems+=("$unit is not installed (tg-ctl enable)")
    fi
  else
    echo "systemctl --user is unavailable (no user bus): the check cannot decide" >&2
    exit 1
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'drift: %s\n' "${problems[@]}"
    exit 3
  fi
  echo "tg-cli is installed and its daemon runs under $unit"
  exit 0
fi

# -- --plan: read-only -------------------------------------------------------------------------
if [ "$mode" = plan ]; then
  if has_checkout; then echo "checkout: $dir present (left as it is; update with tg-cli's scripts/deploy.sh)"
  elif [ -e "$dir" ]; then echo "$dir exists and is not a tg-cli checkout: needs manual"
  else echo "git clone $repo_url $dir"; fi
  if has_checkout && link_ok tg && link_ok tg-ctl; then echo "links: $bin/tg and $bin/tg-ctl already point at $dir"
  else echo "bash $dir/install.sh   # links tg, tg-ctl into $bin and registers the skill"; fi
  config_state
  case "$cfg_state" in
    ok) echo "config: control.enabled is false" ;;
    drift) echo "config: set control.enabled: false in $config" ;;
    unsupported) echo "config: $config needs a manual edit (control.enabled: false)" ;;
  esac
  if creds_ok; then echo "credentials: $cfg_dir/.env has TG_BOT_TOKEN"; else echo "credentials: $cfg_dir/.env has no TG_BOT_TOKEN: needs manual"; fi
  if linger_ok; then echo "linger: on"; else echo "loginctl enable-linger $(id -un)"; fi
  if dropin_ok; then echo "PATH drop-in: $dropin ok"; else echo "write $dropin (PATH pin) and daemon-reload"; fi
  if unit_installed 2>/dev/null; then
    echo "systemctl --user enable --now $unit"
  else
    echo "tg-ctl enable   # writes $unit, enable --now"
  fi
  echo "stop any tg-ctl daemon that is not the unit's before starting it (never two pollers)"
  exit 0
fi

# -- install -----------------------------------------------------------------------------------
command -v git >/dev/null || manual "git is not installed (module base-tools installs it)"
command -v bun >/dev/null || manual "bun is not installed (module brew installs it, or curl -fsSL https://bun.sh/install | bash)"

if [ ! -e "$dir" ]; then
  mkdir -p "$(dirname "$dir")"
  echo "cloning $repo_url -> $dir"
  git clone -- "$repo_url" "$dir" || manual "cannot clone $repo_url (git credentials for that forge, e.g. fj auth login, or the network); then re-run"
fi
has_checkout || manual "$dir exists and is not a tg-cli checkout; move it away or set [modules.tg-cli] dir"

if ! { link_ok tg && link_ok tg-ctl; }; then
  for name in tg tg-ctl; do
    [ ! -e "$bin/$name" ] || [ -L "$bin/$name" ] || manual "$bin/$name exists and is not a symlink; move it away"
  done
  bash "$dir/install.sh"   # tg-cli's own installer (not this file): links tg and tg-ctl, registers the skill
fi

config_state
mkdir -p "$cfg_dir"
case "$cfg_state" in
  ok) ;;
  drift) config_py apply ;;
  unsupported) manual "$config writes \`control\` in a style this module does not edit: set \`control.enabled: false\` there by hand, then run apply again" ;;
esac

creds_ok || manual "$cfg_dir/.env has no TG_BOT_TOKEN: put TG_BOT_TOKEN and TG_CHAT_ID there (migrate the file or run \`tg auth login\`), then run apply again"

if ! linger_ok; then
  loginctl enable-linger "$(id -un)" 2>/dev/null || manual "systemd linger is off: run \`sudo loginctl enable-linger $(id -un)\`, then run apply again"
fi

sc show -p Id --value "$unit" >/dev/null 2>&1 || { echo "systemctl --user is unavailable (no user bus)" >&2; exit 1; }

if ! dropin_ok; then
  mkdir -p "$dropin_dir"
  printf '%s' "$dropin_text" > "$dropin.tmp"
  mv -f "$dropin.tmp" "$dropin"
  sc daemon-reload
fi

# One supervisor: a detached daemon (a hand start, `tg`'s lazy autostart) holds the bot's flock, so the
# unit's own `tg-ctl run` would exit 0 against it and the unit would sit "inactive". SIGTERM exactly those
# pids (graceful: tg-ctl drains in-flight work), wait until every one has exited, and only then let the
# unit start the only daemon: never two pollers at once (Telegram 409).
stray_pids=$(strays | tr '\n' ' ')
if [ -n "$stray_pids" ]; then
  echo "stopping the detached tg-ctl daemon(s) ${stray_pids% } so the unit owns the bot"
  # shellcheck disable=SC2086
  kill $stray_pids
  for _ in $(seq 1 40); do
    [ -z "$(strays)" ] && break
    sleep 1
  done
  [ -z "$(strays)" ] || { echo "tg-ctl daemon(s) $(strays | tr '\n' ' ')still alive 40s after SIGTERM: not starting the unit beside them" >&2; exit 1; }
fi

if unit_installed; then
  sc enable --now "$unit"
  # a running daemon keeps the environment it started with: restart it (stop, then start: never two) until
  # its live PATH is the pinned one
  if [ "$(sc is-active "$unit" 2>/dev/null || true)" = active ] && ! live_path_ok; then sc restart "$unit"; fi
else
  "$dir/tg-ctl" enable
fi

# enable --now returning 0 is not proof: the daemon takes the bot's flock a moment later. Hold the install
# to the same facts the checker states, so exit 0 never means "unit inactive" or "a stray still polls".
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  bash "$self" --check >/dev/null 2>&1 && exit 0
  sleep 1
done
bash "$self" --check >&2 || true
echo "install finished but the facts do not hold (above)" >&2
exit 1
