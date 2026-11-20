#!/usr/bin/env bash
# service-failover module installer: restart backoff for every systemd service, a repair session when a
# service stays down, and (optionally) docker autoheal. See README.md for the design.
#
#   install.sh [--plan|--check]       user level (never root): drop-ins and units in ~/.config/systemd/user, scripts in
#                                     ~/.local/bin, scan timer enabled, daemon-reload. Restarts NOTHING.
#   install.sh --restart-only         the same without the repair stack: only the restart backoff (hosts without omp,
#                                     such as odroidn2: a PC scans them over ssh)
#   install.sh --autoheal             also bring up the docker autoheal container (needs docker access)
#   install.sh --system [UNIT[=POLICY]...]
#                                     root level, run once with sudo: /etc/systemd/system/service.d/10-failover.conf, and
#                                     a per-unit drop-in for each listed unit. UNIT alone: Restart=on-failure for a unit
#                                     that has no Restart= of its own. UNIT=always or UNIT=on-failure (always is refused
#                                     for Type=oneshot): never give up, StartLimitIntervalSec=0, Restart=POLICY,
#                                     RestartSec=2, RestartSteps=8, RestartMaxDelaySec=5min, all set explicitly.
#                                     Combine with --plan / --check (those do not need root). Restarts nothing.
#
# Exit codes (ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual step, others = failure.
#
# Options ([modules.service-failover] of profile.toml / machine.toml, JSON in UBUNTU_TWEAK_MODULE_CONFIG):
#   repair         bool     install the repair stack (OnFailure=, repair template, scan timer, scripts)   (default true)
#   hosts          [string] ssh hosts the scan watches besides this one; written to ~/.config/service-failover/hosts
#   restart_units  [string] user units that need Restart=on-failure from this module because they have none
#   autoheal       bool     docker autoheal container; false removes the one this module created          (default false)
#
# Test hooks: SERVICE_FAILOVER_UNIT_DIR / _BIN_DIR / _CONFIG_DIR / _ETC_DIR move the install locations; SERVICE_FAILOVER_TG
# names the tg command --check looks for (default tg, on the units' PATH).
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
    -h|--help) sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "usage: $0 [--plan|--check] [--restart-only] [--autoheal] [--system [UNIT[=always|on-failure]...]]" >&2; exit 2 ;;
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
assert isinstance(t, dict), "options must be a table"
p = [f"unknown option {k}" for k in sorted(set(t) - {"repair", "hosts", "restart_units", "autoheal"})]
p += [f"{k} must be a boolean" for k in ("repair", "autoheal") if k in t and not isinstance(t[k], bool)]
for k, pat in (("hosts", r"[A-Za-z0-9._][A-Za-z0-9._-]*"), ("restart_units", r"[A-Za-z0-9:_.@-]+\.service")):
    if k in t and not (isinstance(t[k], list) and all(isinstance(x, str) and re.fullmatch(pat, x) for x in t[k])):
        p.append(f"{k} must be a list of plain names")
print("; ".join(p))') || { echo "[modules.service-failover] the options are not valid JSON; nothing was changed" >&2; exit 1; }
[ -z "$bad" ] || { echo "[modules.service-failover] $bad" >&2; exit 1; }
[ "$(cfg repair true)" = true ] && repair=1 || repair=0
[ "$cli_restart_only" = 1 ] && repair=0
[ "$(cfg autoheal false)" = true ] && autoheal=1 || autoheal=0
[ "$cli_autoheal" = 1 ] && autoheal=1
# Only an explicit `autoheal = false` removes the container: a host that never configured the option (the odroid, run by
# hand with --autoheal) is not told to drop it by leaving the flag out.
autoheal_off=0; [ "$autoheal" = 0 ] && [ "$(cfg autoheal '"absent"')" = false ] && autoheal_off=1
mapfile -t hosts < <(cfg hosts '[]' | sed '/^$/d')
mapfile -t user_units < <(cfg restart_units '[]' | sed '/^$/d')

unit_dir=${SERVICE_FAILOVER_UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}
bin_dir=${SERVICE_FAILOVER_BIN_DIR:-$HOME/.local/bin}
cfg_dir=${SERVICE_FAILOVER_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/service-failover}
etc_dir=${SERVICE_FAILOVER_ETC_DIR:-/etc/systemd/system}
launcher=$HOME/.agents/skills/rig-detached-omp/rig-detached-omp
# the repair stack's scripts (service-failover-notify-journal is the repair script's default notifier: journal and tg, no desktop pop-up)
bins=(service-failover-repair service-failover-scan service-failover-journal service-failover-notify-journal)
# The default notifier reaches the owner only through tg (a Bun script), run with the PATH the module's units set (read from
# the scan unit, so the two cannot drift apart). Found means: tg resolves on that PATH, and so does the interpreter its `#!`
# line names through env (bun), which is what fails first on a host whose bun is elsewhere.
tg_cmd=${SERVICE_FAILOVER_TG:-tg}; tg_cmd=${tg_cmd%% *}
units_path=$(sed -n 's/^Environment=PATH=//p' "$here/files/units/service-failover-scan.service" | head -n1); units_path=${units_path//%h/$HOME}
tg_found() {
  local path interp
  path=$(PATH=$units_path command -v "$tg_cmd") || return 1
  interp=$(head -n1 "$path" 2>/dev/null | sed -n 's|^#! *[^ ]*/env  *\([^ ]*\).*|\1|p')
  [ -z "$interp" ] || PATH=$units_path command -v "$interp" >/dev/null
}
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
# One install at a time (two `ubuntu-tweak apply` runs, or one by hand beside it): what is inspected below is what is changed.
# The lock path is the same for every caller: XDG_RUNTIME_DIR exists in a login session and not in an ssh one, so a path
# that followed it would give those two installs two locks. A lock that cannot be created or opened is an error, not a
# reason to go on without one.
if [ "$mode" = install ] && command -v flock >/dev/null; then
  lock=${SERVICE_FAILOVER_INSTALL_LOCK:-$HOME/.cache/service-failover-install.lock}
  { mkdir -p "$(dirname "$lock")" && exec 8>"$lock"; } || { echo "cannot take the install lock $lock (its directory or the file cannot be created); nothing was changed" >&2; exit 1; }
  flock -w "${SERVICE_FAILOVER_INSTALL_LOCK_WAIT:-120}" 8 || { echo "another install of this module holds $lock; nothing was changed" >&2; exit 1; }
fi

# The wanted files: "SRC|DEST|MODE" (SRC is a module file or a generated one under $tmp).
want=()
restart_dropin() { # restart_dropin UNIT -> path of a generated drop-in
  local f; f="$tmp/restart-$1.conf"
  printf '# service-failover (module option restart_units / install.sh --system): this unit has no Restart= of its own.\n[Service]\nRestart=on-failure\n' >"$f"
  echo "$f"
}
# A unit that must come back whatever happens: every value is set explicitly (a unit file's own Restart=, RestartSec= and
# StartLimit*= lose to a drop-in). StartLimitIntervalSec=0 switches the rate limit off; `infinity` would never forget a
# start, so the unit would be refused for good after StartLimitBurst starts in its whole life (2026-11-09), and the
# default 5 starts in 10 s refuses a crash loop or a manual restart burst and leaves the unit `failed` for good.
never_give_up_dropin() { # never_give_up_dropin UNIT POLICY -> path of a generated drop-in
  local f; f="$tmp/never-give-up-$1.conf"
  printf '# service-failover (install.sh --system UNIT=%s): restart without a start limit, backoff 2 s growing to 5 min.\n[Unit]\nStartLimitIntervalSec=0\n[Service]\nRestart=%s\nRestartSec=2\nRestartSteps=8\nRestartMaxDelaySec=5min\n' "$2" "$2" >"$f"
  echo "$f"
}
if [ "$level" = system ]; then
  want+=("$here/files/service.d/10-failover.conf|$etc_dir/service.d/10-failover.conf|644")
  declare -A listed=()
  for spec in "${sys_units[@]}"; do
    u=${spec%%=*}; policy=; plain=1
    case "$spec" in *=*) policy=${spec#*=}; plain=0 ;; esac
    case "$u" in *[!A-Za-z0-9:_.@-]*|"") echo "bad unit name (a plain NAME.service): $u" >&2; exit 2 ;; esac
    case "$u" in *.service) ;; *) echo "bad unit name (a plain NAME.service): $u" >&2; exit 2 ;; esac
    # one drop-in file per unit: two different specs for it would fight over the file (--check would report drift forever)
    if [ -n "${listed[$u]+x}" ]; then
      [ "${listed[$u]}" = "$spec" ] && continue
      echo "unit listed twice with different policies: $u (${listed[$u]} and $spec)" >&2; exit 2
    fi
    listed[$u]=$spec
    case "$plain:$policy" in
      1:) want+=("$(restart_dropin "$u")|$etc_dir/$u.d/30-failover-restart.conf|644") ;;
      0:always|0:on-failure) want+=("$(never_give_up_dropin "$u" "$policy")|$etc_dir/$u.d/30-failover-restart.conf|644") ;;
      *) echo "bad restart policy for $u (UNIT=always or UNIT=on-failure): $policy" >&2; exit 2 ;;
    esac
  done
else
  want+=("$here/files/service.d/10-failover.conf|$unit_dir/service.d/10-failover.conf|644")
  for u in "${user_units[@]}"; do want+=("$(restart_dropin "$u")|$unit_dir/$u.d/30-failover-restart.conf|644"); done
  if [ "$repair" = 1 ]; then
    want+=("$here/files/service.d/15-failover-repair.conf|$unit_dir/service.d/15-failover-repair.conf|644")
    while IFS= read -r rel; do want+=("$here/files/units/$rel|$unit_dir/$rel|644"); done < <(cd "$here/files/units" && find . -type f -printf '%P\n' | sort)
    for s in "${bins[@]}"; do want+=("$here/bin/$s|$bin_dir/$s|755"); done
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
find_unowned() { # find_unowned PATH: stale when it sits in a directory the module owns outright and nothing wants it
  local f=$1 w s
  for w in "${want[@]}"; do case "$w" in *"|$f|"*) return 0 ;; esac; done
  for s in "${stale[@]}"; do [ "$s" = "$f" ] && return 0; done
  stale+=("$f")
}
if [ "$level" = user ]; then
  find_stale "$unit_dir/service.d/15-failover-repair.conf"
  find_stale "$cfg_dir/hosts"
  for rel in $(cd "$here/files/units" && find . -type f -printf '%P\n'); do find_stale "$unit_dir/$rel"; done
  for s in "${bins[@]}"; do find_stale "$bin_dir/$s"; done
  # The module's own units are the module's to declare, so every drop-in of the directories named after them is its
  # own: the repair template's, the scan's, one for a single instance. One nobody declared (a smoke-test drop-in set
  # SERVICE_FAILOVER_DRY_RUN=1 and made every repair on the PC a dry run) is drift. Symlinks count: systemd loads them.
  if [ -d "$unit_dir" ]; then
    while IFS= read -r f; do find_unowned "$f"; done < <(find "$unit_dir" -mindepth 2 -maxdepth 2 \( -type f -o -type l \) -path "$unit_dir/service-failover-*.d/*")
  fi
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

# A SERVICE_FAILOVER_* variable in the user manager's environment (a smoke test's `systemctl --user set-environment
# SERVICE_FAILOVER_DRY_RUN=1`) reaches every repair run, whatever the files say: it is drift too, and install unsets it.
leftover_env=()
if [ "$level" = user ] && command -v systemctl >/dev/null; then
  mapfile -t leftover_env < <(systemctl --user show-environment 2>/dev/null | sed -n 's/^\(SERVICE_FAILOVER_[A-Za-z0-9_]*\)=.*/\1/p')
fi
# The module's container is the compose file's container_name. It is "ours" when docker compose created it from a copy of
# this module's compose file (its config-file label ends in service-failover/docker/autoheal.compose.yaml: the repository
# checkout or the odroid's copy); a container that merely carries the name is somebody else's and is never touched.
# ok = in place; stale = ours, but stopped or another image than the file declares (docker compose up starts or recreates
# it). Earlier copies of the file named the container `autoheal`: one of those (same label rule) is the module's too and goes
# once its replacement runs; any other container named autoheal is not ours and not our business.
compose_file=$here/docker/autoheal.compose.yaml
compose_project=$(sed -n 's/^name: *//p' "$compose_file" | head -n1); compose_image=$(sed -n 's/^ *image: *//p' "$compose_file" | head -n1)
compose_name=$(sed -n 's/^ *container_name: *//p' "$compose_file" | head -n1)
container_info() { # container_info NAME|ID -> id|project|config file|image|running
  timeout 20 docker container inspect -f '{{.Id}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}|{{.Config.Image}}|{{.State.Running}}' "$1" 2>/dev/null
}
# Absent only when docker answers and lists nothing: an inspect that failed for any other reason (timeout, no permission, a
# daemon that went away) says nothing about whether the container is still there.
container_absent() { # container_absent NAME|ID
  local by_name by_id
  by_name=$(timeout 20 docker container ls -aq --no-trunc --filter "name=^/$1\$") && by_id=$(timeout 20 docker container ls -aq --no-trunc --filter "id=$1") && [ -z "$by_name$by_id" ]
}
autoheal_state() { # -> "nodocker" | "noaccess" | "none" | "foreign" | "ok ID" | "stale ID" (ID: handled by id, not by name)
  command -v docker >/dev/null || { echo nodocker; return; }
  local info id proj files image running
  info=$(container_info "$compose_name") || { container_absent "$compose_name" && echo none || echo noaccess; return; }
  IFS='|' read -r id proj files image running <<<"$info"
  case "$files" in */service-failover/docker/autoheal.compose.yaml) ;; *) echo foreign; return ;; esac
  if [ "$proj" = "$compose_project" ] && [ "$image" = "$compose_image" ] && [ "$running" = true ]; then echo "ok $id"; else echo "stale $id"; fi
}
legacy_autoheal() { # the id of an `autoheal` container that an earlier copy of this module's compose file created, if any; ? when docker cannot say
  local info id proj files rest
  info=$(container_info autoheal) || { container_absent autoheal || echo '?'; return 0; }
  IFS='|' read -r id proj files rest <<<"$info"
  case "$files" in */service-failover/docker/autoheal.compose.yaml) echo "$id" ;; esac
}
remove_container() { # remove_container ID: removed, or confirmed gone (another run was first); a docker that cannot be asked is neither
  docker rm -f "$1" >/dev/null 2>&1 && return 0
  container_absent "$1"
}
legacy_drift() { # legacy_drift TEXT: the drift line for a present earlier `autoheal` container
  case "$lg_id" in "") ;; "?") drift+=("unknown: docker cannot tell whether an earlier autoheal container exists") ;; *) drift+=("$1") ;; esac
}
legacy_remove() { # legacy_remove WHAT: removes the earlier `autoheal` container
  case "$lg_id" in
    "") ;;
    "?") echo "needs manual: docker cannot tell whether an earlier autoheal container exists, so it was not removed" >&2; rc=10 ;;
    *) if remove_container "$lg_id"; then echo "removed: $1"; else echo "cannot remove the earlier autoheal container" >&2; rc=1; fi ;;
  esac
}
# docker is asked only when the option is being switched on or off explicitly: an install that never configured autoheal
# must not wait for (or fail on) a docker daemon it does not use.
# No docker client at all means nothing to remove when the option is off (a host that never had docker must not be
# reported as needing manual work for ever); an unreachable daemon behind an installed client is "unknown", see below.
ah=none; ah_id=; lg_id=
if [ "$level" = user ] && { [ "$autoheal" = 1 ] || [ "$autoheal_off" = 1 ]; }; then
  read -r ah ah_id <<<"$(autoheal_state)"
  case "$ah" in none|ok|stale|foreign) lg_id=$(legacy_autoheal) ;; esac
fi
drift=()
for w in "${want[@]}"; do
  IFS='|' read -r src dest m <<<"$w"
  st=$(state_of "$src" "$dest" "$m"); [ -z "$st" ] || drift+=("$st: $dest")
done
for f in "${stale[@]}"; do drift+=("stale: $f (not part of what the module installs with these options)"); done
if [ "$level" = user ]; then
  for v in "${leftover_env[@]}"; do drift+=("set: $v in the user manager's environment (a leftover test setting that changes every repair run)"); done
  if [ "$repair" = 1 ]; then
    [ -x "$launcher" ] || drift+=("missing: $launcher (the sanctioned omp launcher, installed by rig apply; the repair stack cannot start a session without it)")
    tg_found || drift+=("missing: $tg_cmd on the units' PATH, or the interpreter it needs (rig installs tg, a Bun script; without it nobody is told of a unit that no repair session is acting on)")
    command -v systemctl >/dev/null && [ "$(systemctl --user is-enabled service-failover-scan.timer 2>/dev/null)" = enabled ] || drift+=("not enabled: service-failover-scan.timer")
    # enabled is not running: a stopped timer (a failed first start, a manual stop) leaves the hosts unwatched
    if command -v systemctl >/dev/null && [ "$(systemctl --user is-enabled service-failover-scan.timer 2>/dev/null)" = enabled ] \
       && [ "$(systemctl --user is-active service-failover-scan.timer 2>/dev/null)" != active ]; then drift+=("not active: service-failover-scan.timer"); fi
  fi
  if [ "$repair" = 0 ] && command -v systemctl >/dev/null && [ "$(systemctl --user is-enabled service-failover-scan.timer 2>/dev/null)" = enabled ]; then
    drift+=("enabled: service-failover-scan.timer (the repair stack is off)")
  fi
  if [ "$autoheal" = 1 ]; then
    case "$ah" in
      ok) ;;
      foreign) drift+=("needs manual: a container named $compose_name exists that this module did not create") ;;
      stale) drift+=("stale: docker container $compose_name (stopped, or not the image of docker/autoheal.compose.yaml)") ;;
      *) drift+=("not running: docker container $compose_name") ;;
    esac
    legacy_drift "stale: docker container autoheal (an earlier copy of the compose file named it so; $compose_name replaces it)"
  elif [ "$autoheal_off" = 1 ]; then
    case "$ah" in
      ok|stale) drift+=("running: docker container $compose_name (the autoheal option is off)") ;;
      noaccess) drift+=("unknown: docker is not reachable, so the autoheal container cannot be checked (the autoheal option is off)") ;;
    esac
    legacy_drift "running: docker container autoheal (the autoheal option is off)"
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
for v in "${leftover_env[@]}"; do systemctl --user unset-environment "$v" && echo "unset: $v (user manager environment)" || rc=1; done
if [ "$repair" = 1 ]; then
  if [ "$(systemctl --user is-enabled service-failover-scan.timer 2>/dev/null)" != enabled ]; then
    systemctl --user enable --now service-failover-scan.timer >/dev/null && echo "enabled: service-failover-scan.timer" || rc=1
  elif [ "$(systemctl --user is-active service-failover-scan.timer 2>/dev/null)" != active ]; then
    systemctl --user start service-failover-scan.timer && echo "started: service-failover-scan.timer" || rc=1
  fi
  if [ ! -x "$launcher" ]; then echo "needs manual: $launcher is missing (rig apply installs the rig-detached-omp skill); the repair stack is in place but cannot start a session" >&2; rc=10; fi
  if ! tg_found; then echo "needs manual: $tg_cmd, or the interpreter it needs, is not on the units' PATH (rig installs tg, a Bun script); the repair stack is in place but cannot tell you of a unit that no repair session is acting on" >&2; rc=10; fi
fi
if [ "$autoheal" = 1 ]; then
  up_ok=0
  case "$ah" in
    ok) up_ok=1 ;;
    foreign) echo "needs manual: a container named $compose_name exists that this module did not create; remove it, then run this again" >&2; rc=10 ;;
    nodocker|noaccess) echo "needs manual: docker is missing or not accessible" >&2; rc=10 ;;
    *)
      # stale: docker compose starts it, or recreates it for another image. The earlier `autoheal` container is an orphan of this
      # project: COMPOSE_REMOVE_ORPHANS (environment or .env) must not make compose delete it before its replacement runs.
      if docker compose -p "$compose_project" -f "$compose_file" up -d --wait --wait-timeout 60 --remove-orphans=false >/dev/null 2>&1; then echo "started: docker container $compose_name"; up_ok=1
      else echo "needs manual: docker compose up of docker/autoheal.compose.yaml failed, or the container did not stay up (no docker access, or the image cannot be pulled)" >&2; rc=10; fi ;;
  esac
  # the earlier `autoheal` container goes only once its replacement runs: until then it is the one that is working
  if [ "$up_ok" = 1 ]; then legacy_remove "docker container autoheal (replaced by $compose_name)"; fi
elif [ "$autoheal_off" = 1 ]; then
  case "$ah" in
    ok|stale) if remove_container "$ah_id"; then echo "removed: docker container $compose_name"; else echo "cannot remove the $compose_name container" >&2; rc=1; fi ;;
    noaccess) echo "needs manual: docker is not reachable, so the autoheal container could not be checked or removed (the autoheal option is off)" >&2; rc=10 ;;
  esac
  legacy_remove "docker container autoheal"
fi
exit "$rc"
