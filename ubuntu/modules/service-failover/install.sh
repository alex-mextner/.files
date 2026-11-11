#!/usr/bin/env bash
# service-failover module installer: restart backoff for every systemd service, a repair session when a
# service stays down, and (optionally) docker autoheal. See README.md for the design.
#
#   install.sh [--plan|--check]       user level (never root): drop-ins and units in ~/.config/systemd/user, scripts in
#                                     ~/.local/bin, scan timer enabled, daemon-reload. Restarts NOTHING.
#   install.sh --restart-only         the same without the repair stack: only the restart backoff (hosts without omp,
#                                     such as odroidn2: a PC scans them over ssh)
#   install.sh --autoheal             also bring up the docker autoheal container (needs docker access)
#   install.sh --system [UNIT...]     root level, run once with sudo: /etc/systemd/system/service.d/10-failover.conf, and
#                                     Restart=on-failure drop-ins for the listed units that have no Restart= of their own.
#                                     Combine with --plan / --check (those do not need root). Restarts nothing.
#
# Exit codes (ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual step, others = failure.
#
# Options ([modules.service-failover] of profile.toml / machine.toml, JSON in UBUNTU_TWEAK_MODULE_CONFIG):
#   repair         bool     install the repair stack (OnFailure=, repair template, scan timer, scripts)   (default true)
#   hosts          [string] ssh hosts the scan watches besides this one; written to ~/.config/service-failover/hosts
#   restart_units  [string] user units that need Restart=on-failure from this module because they have none
#   autoheal       bool     docker autoheal container                                                    (default false)
#
# Test hooks: SERVICE_FAILOVER_UNIT_DIR / _BIN_DIR / _CONFIG_DIR / _ETC_DIR move the install locations.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
mode=install; level=user; repair=; autoheal=; sys_units=()
cli_restart_only=0; cli_autoheal=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) mode=check ;;
    --plan) mode=plan ;;
    --restart-only) cli_restart_only=1 ;;
    --autoheal) cli_autoheal=1 ;;
    --system) level=system ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "usage: $0 [--plan|--check] [--restart-only] [--autoheal] [--system [UNIT...]]" >&2; exit 2 ;;
    *) sys_units+=("$1") ;;
  esac
  shift
done
if [ "$level" = user ] && [ "${#sys_units[@]}" -gt 0 ]; then echo "unit names are for --system only" >&2; exit 2; fi

py=$(command -v python3 || true)
[ -n "$py" ] || { echo "needs manual: python3 is not installed" >&2; exit 10; }
cfg() { # cfg KEY DEFAULT: one option, lists as one word per line
  "$py" -B -c 'import json, os, sys
v = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}").get(sys.argv[1], json.loads(sys.argv[2]))
print("\n".join(map(str, v)) if isinstance(v, list) else str(v).lower() if isinstance(v, bool) else v)' "$1" "$2"
}
bad=$("$py" -B -c 'import json, os, re
t = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}")
p = [f"unknown option {k}" for k in sorted(set(t) - {"repair", "hosts", "restart_units", "autoheal"})]
p += [f"{k} must be a boolean" for k in ("repair", "autoheal") if k in t and not isinstance(t[k], bool)]
for k, pat in (("hosts", r"[A-Za-z0-9._@-]+"), ("restart_units", r"[A-Za-z0-9:_.@-]+\.service")):
    if k in t and not (isinstance(t[k], list) and all(isinstance(x, str) and re.fullmatch(pat, x) for x in t[k])):
        p.append(f"{k} must be a list of plain names")
print("; ".join(p))')
[ -z "$bad" ] || { echo "[modules.service-failover] $bad" >&2; exit 1; }
[ "$(cfg repair true)" = true ] && repair=1 || repair=0
[ "$cli_restart_only" = 1 ] && repair=0
[ "$(cfg autoheal false)" = true ] && autoheal=1 || autoheal=0
[ "$cli_autoheal" = 1 ] && autoheal=1
mapfile -t hosts < <(cfg hosts '[]' | sed '/^$/d')
mapfile -t user_units < <(cfg restart_units '[]' | sed '/^$/d')

unit_dir=${SERVICE_FAILOVER_UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}
bin_dir=${SERVICE_FAILOVER_BIN_DIR:-$HOME/.local/bin}
cfg_dir=${SERVICE_FAILOVER_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/service-failover}
etc_dir=${SERVICE_FAILOVER_ETC_DIR:-/etc/systemd/system}
launcher=$HOME/.agents/skills/rig-detached-omp/rig-detached-omp
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# The wanted files: "SRC|DEST|MODE" (SRC is a module file or a generated one under $tmp).
want=()
restart_dropin() { # restart_dropin UNIT -> path of a generated drop-in
  local f; f="$tmp/restart-$1.conf"
  printf '# service-failover (module option restart_units / install.sh --system): this unit has no Restart= of its own.\n[Service]\nRestart=on-failure\n' >"$f"
  echo "$f"
}
if [ "$level" = system ]; then
  want+=("$here/files/service.d/10-failover.conf|$etc_dir/service.d/10-failover.conf|644")
  for u in "${sys_units[@]}"; do
    case "$u" in *[!A-Za-z0-9:_.@-]*|"") echo "bad unit name (a plain NAME.service): $u" >&2; exit 2 ;; esac
    case "$u" in *.service) ;; *) echo "bad unit name (a plain NAME.service): $u" >&2; exit 2 ;; esac
    want+=("$(restart_dropin "$u")|$etc_dir/$u.d/30-failover-restart.conf|644")
  done
else
  want+=("$here/files/service.d/10-failover.conf|$unit_dir/service.d/10-failover.conf|644")
  for u in "${user_units[@]}"; do want+=("$(restart_dropin "$u")|$unit_dir/$u.d/30-failover-restart.conf|644"); done
  if [ "$repair" = 1 ]; then
    want+=("$here/files/service.d/15-failover-repair.conf|$unit_dir/service.d/15-failover-repair.conf|644")
    while IFS= read -r rel; do want+=("$here/files/units/$rel|$unit_dir/$rel|644"); done < <(cd "$here/files/units" && find . -type f -printf '%P\n' | sort)
    for s in service-failover-repair service-failover-scan; do want+=("$here/bin/$s|$bin_dir/$s|755"); done
    if [ "${#hosts[@]}" -gt 0 ]; then
      { echo "# written by the service-failover module (option hosts); 'local' is this host's user manager"; echo local; printf '%s\n' "${hosts[@]}"; } >"$tmp/hosts"
      want+=("$tmp/hosts|$cfg_dir/hosts|644")
    fi
  fi
fi

# What an earlier run of this module wrote but the current options no longer want (repair turned off, a restart_units
# entry or the hosts file removed): files with the module's marker that are not in $want. --check reports them, an
# install removes them, so turning a feature off really turns it off.
stale=()
marked() { [ -f "$1" ] && grep -q 'service-failover' "$1"; }
find_stale() { # find_stale PATH: stale when it exists, carries the marker and is not wanted
  local f=$1 w found=0
  for w in "${want[@]}"; do case "$w" in *"|$f|"*) found=1 ;; esac; done
  [ "$found" = 1 ] || { marked "$f" && stale+=("$f"); }
}
if [ "$level" = user ]; then
  find_stale "$unit_dir/service.d/15-failover-repair.conf"
  find_stale "$cfg_dir/hosts"
  for rel in $(cd "$here/files/units" && find . -type f -printf '%P\n'); do find_stale "$unit_dir/$rel"; done
  for s in service-failover-repair service-failover-scan; do find_stale "$bin_dir/$s"; done
fi
restart_root=$unit_dir; [ "$level" = system ] && restart_root=$etc_dir
if [ -d "$restart_root" ]; then
  while IFS= read -r f; do find_stale "$f"; done < <(find "$restart_root" -maxdepth 2 -name 30-failover-restart.conf 2>/dev/null)
fi

state_of() { # state_of SRC DEST MODE -> missing | differs | mode | (empty = in place)
  if [ ! -e "$2" ]; then echo missing
  elif ! cmp -s "$1" "$2"; then echo differs
  elif [ "$(stat -c %a "$2")" != "$3" ]; then echo mode; fi
}

drift=()
for w in "${want[@]}"; do
  IFS='|' read -r src dest m <<<"$w"
  st=$(state_of "$src" "$dest" "$m"); [ -z "$st" ] || drift+=("$st: $dest")
done
for f in "${stale[@]}"; do drift+=("stale: $f (no longer wanted by the options)"); done
if [ "$level" = user ]; then
  if [ "$repair" = 1 ]; then
    [ -x "$launcher" ] || drift+=("missing: $launcher (the sanctioned omp launcher, installed by rig apply; the repair stack cannot start a session without it)")
    command -v systemctl >/dev/null && [ "$(systemctl --user is-enabled service-failover-scan.timer 2>/dev/null)" = enabled ] || drift+=("not enabled: service-failover-scan.timer")
  fi
  if [ "$repair" = 0 ] && command -v systemctl >/dev/null && [ "$(systemctl --user is-enabled service-failover-scan.timer 2>/dev/null)" = enabled ]; then
    drift+=("enabled: service-failover-scan.timer (the repair stack is off)")
  fi
  if [ "$autoheal" = 1 ]; then
    [ "$(docker inspect -f '{{.State.Running}}' autoheal 2>/dev/null)" = true ] || drift+=("not running: docker container autoheal")
  fi
fi

if [ "$mode" != install ]; then
  if [ "${#drift[@]}" -eq 0 ]; then echo "ok: service-failover ($level level) is in place"; exit 0; fi
  printf '%s\n' "${drift[@]}"
  [ "$mode" = plan ] && exit 0
  exit 3
fi

if [ "$level" = system ] && [ "$(id -u)" -ne 0 ] && [ -z "${SERVICE_FAILOVER_ETC_DIR:-}" ]; then
  echo "needs manual: the system level is root-owned; run once: sudo bash $here/install.sh --system ${sys_units[*]-}" >&2
  exit 10
fi

changed=0
for w in "${want[@]}"; do
  IFS='|' read -r src dest m <<<"$w"
  st=$(state_of "$src" "$dest" "$m"); [ -n "$st" ] || continue
  mkdir -p "$(dirname "$dest")" && install -m "$m" "$src" "$dest" || { echo "cannot write $dest" >&2; exit 1; }
  echo "installed: $dest"; [ "$st" = mode ] || changed=1
done

# The timer is disabled BEFORE its unit file is removed: once the file is gone systemd no longer reports it as enabled.
rc=0
if [ "$level" = user ] && [ "$repair" = 0 ] && command -v systemctl >/dev/null \
   && [ "$(systemctl --user is-enabled service-failover-scan.timer 2>/dev/null)" = enabled ]; then
  systemctl --user disable --now service-failover-scan.timer >/dev/null && echo "disabled: service-failover-scan.timer" || rc=1
fi
for f in "${stale[@]}"; do
  rm -f "$f" || { echo "cannot remove $f" >&2; exit 1; }
  echo "removed: $f"; changed=1
done

if [ "$level" = system ]; then
  [ "$changed" = 0 ] || [ -n "${SERVICE_FAILOVER_ETC_DIR:-}" ] || systemctl daemon-reload
  echo "note: no unit was restarted; a unit takes the new backoff at its next start. A unit already in 'failed' state needs:"
  echo "      systemctl reset-failed <unit> && systemctl start <unit>"
  exit 0
fi

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "needs manual: no user systemd session reachable (run from a login session, or enable linger)" >&2; exit 10
fi
# daemon-reload applies the drop-ins to the NEXT start of each unit; it restarts nothing.
systemctl --user daemon-reload || { echo "daemon-reload failed" >&2; exit 1; }
if [ "$repair" = 1 ]; then
  if [ "$(systemctl --user is-enabled service-failover-scan.timer 2>/dev/null)" != enabled ]; then
    systemctl --user enable --now service-failover-scan.timer >/dev/null && echo "enabled: service-failover-scan.timer" || rc=1
  fi
  if [ ! -x "$launcher" ]; then echo "needs manual: $launcher is missing (rig apply installs the rig-detached-omp skill); the repair stack is in place but cannot start a session" >&2; rc=10; fi
fi
if [ "$autoheal" = 1 ] && [ "$(docker inspect -f '{{.State.Running}}' autoheal 2>/dev/null)" != true ]; then
  if docker compose -f "$here/docker/autoheal.compose.yaml" up -d >/dev/null 2>&1; then echo "started: docker container autoheal"
  else echo "needs manual: docker compose up of docker/autoheal.compose.yaml failed (docker missing or no access)" >&2; rc=10; fi
fi
exit "$rc"
