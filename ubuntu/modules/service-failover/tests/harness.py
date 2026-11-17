"""Shared stand-ins of the service-failover tests: stub systemctl/journalctl/ssh/docker/launcher/notifier scripts and Base.

Nothing touches the real systemd, the real $HOME or a real omp: every command is a stub on PATH that keeps its state in
files under a temp dir, and the omp launcher is a stub that only records its arguments.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
INSTALL = MODULE / "install.sh"
REPAIR = MODULE / "bin" / "service-failover-repair"
JOURNAL = MODULE / "bin" / "service-failover-journal"
SCAN = MODULE / "bin" / "service-failover-scan"

# systemctl keeps a unit's properties in $STUB/state (KEY=VALUE lines), logs every call, and `restart` brings the unit
# up when $STUB/heals exists.
SYSTEMCTL = r"""#!/bin/bash
echo "systemctl $*" >> "$STUB/calls"
scope=system
[ "$1" = --user ] && { scope=user; shift; }
[ "$1" = --no-ask-password ] && shift
case "$1" in
  daemon-reload) exit 0 ;;
  show-environment) [ -e "$STUB/env" ] && cat "$STUB/env"; exit 0 ;;
  unset-environment) rm -f "$STUB/env" ;;
  is-enabled)
    # real systemd no longer reports a timer whose unit file is gone as enabled
    [ -n "${SERVICE_FAILOVER_UNIT_DIR:-}" ] && [ ! -e "$SERVICE_FAILOVER_UNIT_DIR/service-failover-scan.timer" ] && { echo not-found; exit 1; }
    [ -e "$STUB/timer-enabled" ] && { echo enabled; exit 0; }; echo disabled; exit 1 ;;
  is-active) [ -e "$STUB/timer-enabled" ] && [ ! -e "$STUB/timer-stopped" ] && { echo active; exit 0; }; echo inactive; exit 3 ;;
  enable)
    : > "$STUB/timer-enabled"
    [ -e "$STUB/enable-fails" ] && { : > "$STUB/timer-stopped"; echo "Failed to start the timer" >&2; exit 1; }
    rm -f "$STUB/timer-stopped" ;;
  disable) rm -f "$STUB/timer-enabled" ;;
  reset-failed)
    [ -e "$STUB/reset-refused" ] && { echo "Access denied" >&2; exit 1; }
    rm -f "$STUB/start-limited"
    # real systemd: a failed unit becomes inactive with Result=success and NRestarts=0
    printf 'ActiveState=inactive\nSubState=dead\nResult=success\nNRestarts=0\nInvocationID=old\nFragmentPath=%s\nTransient=no\n%s' "$(sed -n 's/^FragmentPath=//p' "$STUB/state")" "$(grep '^Type=' "$STUB/state")" > "$STUB/state.new"
    mv "$STUB/state.new" "$STUB/state" ;;
  restart)
    # a refused restart prints $STUB/restart-refused if it has text, else a polkit-style refusal
    [ -e "$STUB/restart-refused" ] && { if [ -s "$STUB/restart-refused" ]; then cat "$STUB/restart-refused" >&2; else echo "Interactive authentication required" >&2; fi; exit 1; }
    [ -e "$STUB/show-fails-after-restart" ] && : > "$STUB/show-fails"
    # $STUB/start-limited: the unit's start limit is still in force, so `restart --no-block` exits 0 and starts nothing
    # (same invocation, still failed) until reset-failed. Otherwise a restart is a new invocation.
    # $STUB/dep-refused: a failed dependency refused the start job: also exit 0, nothing started, no limit in the journal
    # (each refusal adds its own journal line, as systemd does; an old limit line stays in the journal)
    [ -e "$STUB/start-limited" ] && echo "2026-11-11T02:00:00+01:00 pc systemd[2103]: ${3}: Start request repeated too quickly." >> "$STUB/journal"
    [ -e "$STUB/dep-refused" ] && echo "2026-11-11T02:00:00+01:00 pc systemd[2103]: Dependency failed for ${3}." >> "$STUB/journal"
    # $STUB/restart-logs: more journal lines the restart writes (a unit's own output that talks about a limit, say)
    [ -s "$STUB/restart-logs" ] && cat "$STUB/restart-logs" >> "$STUB/journal"
    if [ ! -e "$STUB/start-limited" ] && [ ! -e "$STUB/dep-refused" ]; then
      sed -i "s/^InvocationID=.*/InvocationID=run-$RANDOM/" "$STUB/state"
      [ -e "$STUB/heals" ] && printf 'ActiveState=active\nSubState=running\nResult=success\nNRestarts=0\n' > "$STUB/state"
      [ -e "$STUB/after-restart" ] && cp "$STUB/after-restart" "$STUB/state"
    fi ;;
  start) rm -f "$STUB/timer-stopped" ;;
  show)
    [ -e "$STUB/show-fails" ] && { echo "Connection timed out" >&2; exit 124; }
    if [ "$2" = -p ] && [ "$3" = MainPID ]; then echo 2103; exit 0; fi       # user@UID.service: the user manager
    if [ "$2" = -p ] && [ "$3" = ActiveState ] && [ "$4" = --value ]; then
      if [ -e "$STUB/instance-state" ]; then cat "$STUB/instance-state"; fi; exit 0
    fi
    if [ "$2" = -p ] && [ "$3" = NRestarts ]; then cat "$STUB/nrestarts-${5}" 2>/dev/null || echo 0; exit 0; fi
    cat "$STUB/state" ;;
  list-units)
    [ -e "$STUB/list-fails" ] && { echo "Failed to connect to user scope bus" >&2; exit 1; }
    for a in "$@"; do case "$a" in --state=failed) cat "$STUB/failed-$scope" 2>/dev/null ;; --state=activating) cat "$STUB/activating-$scope" 2>/dev/null ;; esac; done ;;
esac
exit 0
"""
# journalctl: the whole stub journal; --show-cursor prints "-- cursor: N" (N = the line count) and --after-cursor N prints
# the lines after the Nth, like the real one; -n N the last N. `-o json` writes what journald would: every line of the stub
# journal (`TIME HOST IDENT[PID]: MESSAGE`; any other text is the message of an entry of ident `raw`) becomes an entry whose
# _COMM, _EXE and _PID are the ones of the program IDENT[PID] (systemd's own binary for `systemd`); a line that starts with
# `{` is written as it is, to build entries a real journal could hold that this mapping cannot (a forged _EXE, a byte-array
# MESSAGE). $STUB/journal-fails: the options of the real query are refused (an old journalctl).
JOURNALCTL = r"""#!/bin/bash
echo "journalctl $*" >> "$STUB/calls"
# a hung journal: a child in its own session survives the timeout and must not hold the repair lock
[ -e "$STUB/journal-hangs" ] && { setsid sleep 6 >/dev/null 2>&1 & exec sleep 6; }
# a journal that answers until the unit has been restarted and then hangs longer than the test waits: the run is stopped from
# outside (its unit's start deadline); $STUB/hung says the run got that far
[ -e "$STUB/journal-hangs-after-restart" ] && grep -q ' restart ' "$STUB/calls" && { : > "$STUB/hung"; exec sleep 8; }
after=; count=
for ((i = 1; i <= $#; i++)); do [ "${!i}" = --after-cursor ] && { j=$((i + 1)); after=${!j}; }; [ "${!i}" = -n ] && { j=$((i + 1)); count=${!j}; }; done
case " $* " in *" -o json "*)
  [ -e "$STUB/journal-fails" ] && { echo "journalctl: unrecognized option '--output-fields=MESSAGE'" >&2; exit 1; }
  exec python3 - "$STUB/journal" "$count" "$after" <<'PY'
import json, re, sys
from datetime import datetime
try:
    lines = open(sys.argv[1], encoding='utf-8', errors='surrogateescape', newline='').read().split('\n')
except OSError:
    sys.exit(0)
if sys.argv[3]:
    lines = lines[int(sys.argv[3]):]
lines = [line for line in lines if line and not line.startswith('-- ')]      # journalctl's own markers are no entries
if sys.argv[2] and not sys.argv[3]:
    lines = lines[-int(sys.argv[2]):]
for line in lines:
    if line.startswith('{'):
        print(line)
        continue
    m = re.fullmatch(r'(\d{4}-\d\d-\d\dT[0-9:]+[+-]\d\d:\d\d) \S+ ([^\s\[:]+)(?:\[(\d+)\])?: (.*)', line)
    entry = {'MESSAGE': line, '_COMM': 'raw', '_EXE': '/usr/bin/raw', '_PID': '999', 'PRIORITY': '6'}
    if m:
        when, ident, pid, text = m.groups()
        entry = {'__REALTIME_TIMESTAMP': str(int(datetime.fromisoformat(when).timestamp() * 1e6)), 'MESSAGE': text, '_PID': pid or '4242',
                 '_COMM': ident, '_EXE': '/usr/lib/systemd/systemd' if ident == 'systemd' else '/usr/bin/' + ident, 'PRIORITY': '6'}
    print(json.dumps(entry))
PY
  ;;
esac
if [ -n "$after" ]; then tail -n "+$((after + 1))" "$STUB/journal" 2>/dev/null; exit 0; fi
cat "$STUB/journal" 2>/dev/null
case " $* " in *" --show-cursor "*) echo "-- cursor: $(wc -l < "$STUB/journal" 2>/dev/null || echo 0)" ;; esac
exit 0
"""
# ssh: log, then run the remote command string locally (the stubs above stand in for the remote systemctl).
SSH = r"""#!/bin/bash
while [ "$#" -gt 0 ]; do case "$1" in -o) shift 2 ;; -n) shift ;; *) break ;; esac; done
host=$1; shift
echo "ssh $host $*" >> "$STUB/calls"
exec bash -c "$*"
"""
LAUNCHER = r"""#!/bin/bash
echo "launcher $1 $3" >> "$STUB/calls"
cp "$2" "$STUB/brief-sent"
"""
# docker keeps each container in $STUB/containers/NAME as `id|project|compose file|image|running` (absent = no container).
# $STUB/no-docker: the daemon is not reachable; $STUB/up-fails a `compose up` that fails, $STUB/wait-fails one whose
# container did not stay up under --wait (it is left stopped); $STUB/rm-disconnects: the daemon is lost during `rm`;
# $STUB/inspect-denied: `inspect` is refused while the daemon still lists containers; COMPOSE_REMOVE_ORPHANS=true as in compose.
DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$STUB/calls"
C=$STUB/containers
find_container() { # NAME|ID -> its file, like `docker inspect` / `docker rm`
  local f
  for f in "$C"/*; do [ -e "$f" ] || continue; { [ "$(basename "$f")" = "$1" ] || [ "$(cut -d'|' -f1 "$f")" = "$1" ]; } && { echo "$f"; return 0; }; done
  return 1
}
[ "$1" = container ] && shift
case "$1" in
  info) [ -e "$STUB/no-docker" ] && exit 1; exit 0 ;;
  inspect) [ -e "$STUB/no-docker" ] || [ -e "$STUB/inspect-denied" ] && exit 1; f=$(find_container "${!#}") || exit 1; cat "$f" ;;
  ls) [ -e "$STUB/no-docker" ] && exit 1
      for a in "$@"; do case "$a" in
        name=*) n=${a#name=^/}; n=${n%\$}; [ -e "$C/$n" ] && cut -d'|' -f1 "$C/$n" ;;
        id=*) for f in "$C"/*; do [ -e "$f" ] && [ "$(cut -d'|' -f1 "$f")" = "${a#id=}" ] && echo "${a#id=}"; done ;;
      esac; done ;;
  rm) [ -e "$STUB/rm-disconnects" ] && { : > "$STUB/no-docker"; exit 1; }  # the daemon goes away during the removal
      f=$(find_container "${!#}") && rm -f "$f" ;;
  compose)
    while [ "$#" -gt 0 ]; do case "$1" in -f) file=$2; shift 2 ;; -p) project=$2; shift 2 ;; up) action=$1; break ;; *) shift ;; esac; done
    wait=0; case " $* " in *" --wait "*) wait=1 ;; esac
    orphans=${COMPOSE_REMOVE_ORPHANS:-false}; case " $* " in *" --remove-orphans=false "*) orphans=false ;; *" --remove-orphans "*) orphans=true ;; esac
    if [ "$action" = up ]; then
      name=$(sed -n 's/^ *container_name: *//p' "$file"); image=$(sed -n 's/^ *image: *//p' "$file"); mkdir -p "$C"
      # compose removes the orphans of the project (containers of services the file no longer has) before it creates anything
      [ "$orphans" = true ] && for f in "$C"/*; do [ -e "$f" ] && [ "$(basename "$f")" != "$name" ] && [ "$(cut -d'|' -f2 "$f")" = "$project" ] && rm -f "$f"; done
      [ -e "$STUB/up-fails" ] && exit 1
      running=true; [ "$wait" = 1 ] && [ -e "$STUB/wait-fails" ] && running=false
      printf 'id-%s|%s|%s|%s|%s\n' "$RANDOM" "$project" "$file" "$image" "$running" > "$C/$name"
      [ "$running" = true ] || exit 1
    fi ;;
esac
exit 0
"""
# notify-send: records "title | body" per call; $STUB/notify-down makes it fail the way it does while no notification
# daemon is running yet (early in the login).
NOTIFIER = r"""#!/bin/bash
[ -e "$STUB/notify-down" ] && { echo "Could not connect: No such file or directory" >&2; exit 1; }
args=("$@"); n=${#args[@]}
# like GLib's option parser: a body that is not valid UTF-8 is refused
printf '%s' "${args[n-1]}" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1 || { echo "Invalid UTF-8 in the message" >&2; exit 1; }
printf '%s | %s\n' "${args[n-2]}" "${args[n-1]}" >> "$STUB/notifications"
"""
# wireplumber.service on 2026-11-11 01:36:49: a distribution unit, refused its next start after five fast restarts.
WIREPLUMBER_STATE = ("InvocationID=old\nActiveState=failed\nSubState=failed\nResult=exit-code\nNRestarts=5\nUnitFileState=enabled\n"
                     "FragmentPath=/usr/lib/systemd/user/wireplumber.service\nTransient=no\nExecMainStatus=78\n")
WIREPLUMBER_JOURNAL = (
    "2026-11-11T01:36:44+01:00 pc wireplumber[8331]: failed to load components: failed to load component 'support.xdg-desktop-portal "
    "[module: libxdg-desktop-portal]' (required by policy.xdg-desktop-portal [virtual]): Failed to locate module libxdg-desktop-portal\n"
    "2026-11-11T01:36:44+01:00 pc systemd[2103]: wireplumber.service: Main process exited, code=exited, status=78/CONFIG\n"
    "2026-11-11T01:36:49+01:00 pc systemd[2103]: wireplumber.service: Start request repeated too quickly.\n"
    "2026-11-11T01:36:49+01:00 pc systemd[2103]: Failed to start wireplumber.service - Multimedia Service Session Manager.\n")
FAILED_STATE = "InvocationID=old\nActiveState=failed\nSubState=failed\nResult=exit-code\nNRestarts=9\nUnitFileState=enabled\nFragmentPath=/home/u/.config/systemd/user/foo.service\nTransient=no\n"

# Fake secrets, assembled at run time so no secret-shaped literal sits in the source.
HEADER_VALUE = "abcd1234" + "valuevalue"
PASSWORD_VALUE = "hunter" + "2"
LONG_VALUE = "A" * 40
HEX_VALUE = "0123456789abcdef" * 3
LONG_PATH = "/home/u/.local/share/some-very-long-script-name-over-thirty-two-chars.sh"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stub = self.root / "stub"
        self.bin = self.root / "bin"
        for d in (self.stub, self.bin):
            d.mkdir()
        for name, text in (("systemctl", SYSTEMCTL), ("journalctl", JOURNALCTL), ("ssh", SSH), ("docker", DOCKER)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        launcher = self.root / "launcher"
        launcher.write_text(LAUNCHER)
        launcher.chmod(0o755)
        notifier = self.root / "notify-send"
        notifier.write_text(NOTIFIER)
        notifier.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "STUB": str(self.stub), "HOME": str(self.root / "home"),
                    "SERVICE_FAILOVER_STATE": str(self.root / "state"), "SERVICE_FAILOVER_LAUNCHER": str(launcher),
                    "SERVICE_FAILOVER_NOTIFY": str(notifier),
                    "SERVICE_FAILOVER_SETTLE_S": "0", "MANAGERPID": "2103", "RIG_AGENT_LOG_DIR": str(self.root / "agent-logs")}
        self.env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
        (self.root / "home").mkdir()

    def calls(self):
        path = self.stub / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def launches(self):
        return [c for c in self.calls() if c.startswith("launcher ")]

    def notifications(self):
        path = self.stub / "notifications"
        return path.read_text().splitlines() if path.exists() else []

    def set_state(self, text=FAILED_STATE):
        (self.stub / "state").write_text(text)

    def recheck_marker(self, instance):
        # the repair script names it after the sanitized instance plus the checksum of the exact instance
        key = re.sub(r"[^A-Za-z0-9._-]", "_", instance)
        if key.startswith("."):  # a hidden file would be skipped by every glob over the state directory
            key = "@" + key
        crc = subprocess.run(["cksum"], input=instance.encode(), capture_output=True, check=True).stdout.split()[0].decode()
        return self.root / "state" / "recheck" / f"{key}.{crc}"
