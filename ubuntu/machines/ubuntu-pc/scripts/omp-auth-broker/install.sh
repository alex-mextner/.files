#!/usr/bin/env bash
# Runs on the broker PC (ubuntu-pc). Starts omp-auth-broker.service and one omp-auth-broker-tunnel@<alias>.service per
# consumer, and points each consumer's omp at the tunnel. Idempotent: every step checks first and only changes what
# differs. Consumers are ssh Host aliases of this PC's ~/.ssh/config, so no address or user is ever written here.
# The bearer token is read once into a shell variable and handed to the consumer on ssh's standard input, where a
# small Python step sets auth.broker.url and auth.broker.token in one write of the omp config file (mode 0600; only
# those two lines change, comments and other keys stay as they are). It is never in a command line on either side
# (`omp config set <key> <token>` would put it in the consumer's /proc/<pid>/cmdline and, through the omp wrapper's
# systemd-run scope description, in its journal), never printed, and only sent when the consumer's copy differs.
# Usage: install.sh [--dry-run] [alias ...]   (default: the enabled tunnel instances, plus clvm while the old unit serves it)
set -euo pipefail

PORT=18765
URL="http://127.0.0.1:$PORT"
UNIT_DIR="$HOME/.config/systemd/user"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../files/.config/systemd/user" && pwd -P)"
OMP="$HOME/.local/bin/omp"
BROKER=omp-auth-broker.service
# The pre-template unit, pinned to the clvm box on the same port. It is retired when the clvm instance replaces it.
OLD_UNIT=clvm-auth-broker-tunnel.service
OLD_UNIT_ALIAS=clvm
PC_HOST="$(uname -n)"   # systemd's %H, which keys the tunnel's record file on the consumer
OLD_RECORD="clvm-auth-broker-tunnel.$PC_HOST.pids"   # the old unit's record on clvm
# The consumer's omp is called by path: a non-interactive ssh command does not read the shell profile that puts
# ~/.local/bin on PATH. The tilde is expanded by the consumer's shell, not here. OMP_NO_SCOPE keeps these short calls
# out of the wrapper's systemd-run scope.
# shellcheck disable=SC2088
REMOTE_OMP='OMP_NO_SCOPE=1 ~/.local/bin/omp'
# Reads the token from stdin and sets auth.broker.url (argv[2]) and auth.broker.token in the file named by argv[1]
# (<omp config path>/config.yml) in one atomic write, so the consumer never ends up with one and not the other. Only
# those two lines change (the blocks are added when missing), so comments and every other value stay byte for byte:
# a YAML load and dump would drop the comments and could retype values. A layout it cannot edit line by line (a
# flow-style map, a value over several lines) is refused with the file untouched. A symlinked config is written
# through to its target; the result is always mode 0600.
SET_BROKER_PY='import json, os, re, sys, tempfile
import yaml
path = os.path.realpath(sys.argv[1]); url = sys.argv[2]; token = sys.stdin.readline().strip()
if not token: sys.exit("empty token on stdin")
if os.path.dirname(path) != os.path.realpath(os.path.dirname(sys.argv[1])):
    sys.exit(f"{sys.argv[1]} links to {path} outside its directory (a tracked dotfile?); the token is not written there")
lines = open(path).read().splitlines(keepends=True) if os.path.exists(path) else []
if lines and not lines[-1].endswith("\n"): lines[-1] += "\n"
KEY = re.compile(r"( *)([^\s#][^:]*):(.*)")
def child_block(start, end, key):
    """(index, indent, end of its block) of key: among the direct children of lines[start:end], or None."""
    indent = None
    for i in range(start, end):
        m = KEY.match(lines[i])
        if not m: continue
        if indent is None: indent = len(m.group(1))
        if len(m.group(1)) < indent: break
        if len(m.group(1)) == indent and m.group(2).strip().strip("\"").strip("\x27") == key:
            j = i + 1
            while j < end and (not lines[j].strip() or lines[j].lstrip().startswith("#") or len(lines[j]) - len(lines[j].lstrip(" ")) > indent): j += 1
            return i, indent, j
    return None
def value(i): return KEY.match(lines[i]).group(3).split(" #")[0].strip()
def content_end(start, end):
    """end, moved back over trailing blank and comment lines, so an insert stays above a comment that leads the next key."""
    while end > start and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")): end -= 1
    return end
def child_pad(start, end, parent_indent):
    """Indent of the existing children of lines[start:end], or two more than the parent when it has none."""
    kids = [len(m.group(1)) for k in range(start, end) if (m := KEY.match(lines[k]))]
    return " " * (kids[0] if kids else parent_indent + 2)
def refuse(what): sys.exit(f"{what} in {path} cannot be edited line by line; set it by hand once")
def mapping(found, name):
    if value(found[0]): refuse(name)
    return found
def scalar(found, name):
    i, _, end = found
    if value(i) in ("", "|", ">", "|-", ">-", "|+", ">+") or any(lines[k].strip() and not lines[k].lstrip().startswith("#") for k in range(i + 1, end)): refuse(name)
for line in lines:
    if not line.strip() or line.lstrip().startswith("#") or line.startswith("---"): continue
    if not KEY.match(line) or line[0] == " " or line.startswith("{"): refuse("the top level")
    break
auth = child_block(0, len(lines), "auth")
if not auth:
    lines += ["auth:\n", "  broker:\n", f"    url: {json.dumps(url)}\n", f"    token: {json.dumps(token)}\n"]
else:
    a, a_indent, a_end = mapping(auth, "auth")
    broker = child_block(a + 1, a_end, "broker")
    if not broker:
        pad = child_pad(a + 1, a_end, a_indent)
        at = content_end(a + 1, a_end)
        lines[at:at] = [f"{pad}broker:\n", f"{pad}  url: {json.dumps(url)}\n", f"{pad}  token: {json.dumps(token)}\n"]
    else:
        b, b_indent, b_end = mapping(broker, "auth.broker")
        pad = child_pad(b + 1, b_end, b_indent)
        for key, val in (("url", url), ("token", token)):
            found = child_block(b + 1, b_end, key)
            if found:
                scalar(found, f"auth.broker.{key}")
                if value(found[0]).strip("\"").strip("\x27") != val: lines[found[0]] = " " * found[1] + f"{key}: {json.dumps(val)}\n"
            else:
                at = content_end(b + 1, b_end)
                lines.insert(at, f"{pad}{key}: {json.dumps(val)}\n"); b_end += 1
try: broker = (yaml.safe_load("".join(lines)) or {}).get("auth", {}).get("broker", {})
except yaml.YAMLError: broker = None
if not isinstance(broker, dict) or broker.get("url") != url or broker.get("token") != token: refuse("the result")
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.yml.")
try:
    with os.fdopen(fd, "w") as f: f.writelines(lines)
    os.replace(tmp, path)
except BaseException:
    os.unlink(tmp); raise'

usage() { printf 'usage: %s [--dry-run] [alias ...]   (default: the enabled omp-auth-broker-tunnel@ instances)\n' "$0"; }
dry=0
aliases=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; exit 2 ;;
    *) aliases+=("$arg") ;;
  esac
done
# No alias given: the consumers this PC already serves (its enabled tunnel instances, plus clvm while the old unit
# still serves it, so its migration is retried), so a rerun after a token rotation reaches every current consumer
# and never re-adds one that was removed.
if [ ${#aliases[@]} -eq 0 ]; then
  for link in "$UNIT_DIR"/default.target.wants/omp-auth-broker-tunnel@*.service; do
    [ -L "$link" ] || continue
    inst="${link##*/omp-auth-broker-tunnel@}"; aliases+=("${inst%.service}")
  done
  if [ -L "$UNIT_DIR/$OLD_UNIT" ] || [ -L "$UNIT_DIR/default.target.wants/$OLD_UNIT" ] \
     || systemctl --user is-active --quiet "$OLD_UNIT" 2>/dev/null; then
    case " ${aliases[*]} " in *" $OLD_UNIT_ALIAS "*) ;; *) aliases+=("$OLD_UNIT_ALIAS") ;; esac
  fi
  [ ${#aliases[@]} -gt 0 ] || { printf 'error: no tunnel instance is enabled yet; name the consumers, e.g. %s clvm home\n' "$0" >&2; exit 2; }
fi

say() { printf '%s\n' "$*"; }
# Quotes one argument for any POSIX shell: single quotes, with each embedded quote closed, escaped and reopened.
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
run() { if [ "$dry" = 1 ]; then say "would run: $*"; else "$@"; fi; }
# Same host-key policy and the same -T -a -x as the tunnel units (the token travels over these connections: no
# terminal to echo it, no agent or X11 handed to the consumer), no forwards an alias may carry (a RemoteForward of the
# port would make every probe see it held), and no shared ControlMaster connection, so every check sees the consumer
# as the units will.
ssh_c() { ssh -T -a -x -o ClearAllForwardings=yes -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o ControlMaster=no -o ControlPath=none "$@"; }
remote_get() { ssh_c "$1" "$REMOTE_OMP config get $2" 2>/dev/null | tail -n 1 | tr -d '[:space:]' || true; }

[ -x "$OMP" ] || { say "error: $OMP not found; install omp on this PC first" >&2; exit 127; }

# 1. Unit files: `ubuntu-tweak apply` links them; link them here too so the script also works before the first apply.
#    A link into another checkout (a worktree used for a test run) is moved to this one, and said so.
[ -d "$UNIT_DIR" ] || run mkdir -p "$UNIT_DIR"
for unit in "$BROKER" omp-auth-broker-tunnel@.service; do
  current=""
  [ -L "$UNIT_DIR/$unit" ] && current="$(readlink -f "$UNIT_DIR/$unit" || true)"
  if [ "$current" != "$SRC_DIR/$unit" ]; then
    if [ -e "$UNIT_DIR/$unit" ] && [ ! -L "$UNIT_DIR/$unit" ]; then
      say "error: $UNIT_DIR/$unit is a regular file, not the profile's link; move it away and rerun" >&2; exit 2
    fi
    [ -n "$current" ] && say "relinking $unit (was $current)"
    run ln -sfn "$SRC_DIR/$unit" "$UNIT_DIR/$unit"
  fi
done
run systemctl --user daemon-reload

# 2. The broker itself. One started from the older unit file (no OMP_NO_SCOPE=1) was moved by the omp wrapper into an
#    omp.slice scope, where the unit's MemoryMax does not reach it. daemon-reload already shows the new file's
#    properties, so look at the running process: its cgroup must be the unit's own.
if systemctl --user is-active --quiet "$BROKER"; then
  broker_pid="$(systemctl --user show -p MainPID --value "$BROKER")"
  broker_cg="$(systemctl --user show -p ControlGroup --value "$BROKER")"
  if [ "$(sed -n 's/^0:://p' "/proc/$broker_pid/cgroup" 2>/dev/null)" != "$broker_cg" ]; then
    say "restarting $BROKER: it runs outside its own cgroup (started from an older unit file)"
    run systemctl --user restart "$BROKER"
  fi
fi
# `enable` points the wants link at the unit file's resolved path; one enabled from another checkout (or the old shared
# file this setup replaced) is moved to this checkout's file.
# Not `reenable`: its `disable` half also removes the unit link step 1 made for a non-template unit, and the `enable`
# half then finds no unit file. Replacing just the wants link and enabling again is the same result without that.
reenable_if_elsewhere() {
  local wants="$UNIT_DIR/default.target.wants/$1"
  if [ -L "$wants" ] && [ "$(readlink -f "$wants")" != "$SRC_DIR/$2" ]; then
    run rm -f "$wants" && run systemctl --user enable "$1"
  fi
}
reenable_if_elsewhere "$BROKER" "$BROKER"
if ! systemctl --user is-enabled --quiet "$BROKER" || ! systemctl --user is-active --quiet "$BROKER"; then
  run systemctl --user enable --now "$BROKER"
fi
if [ "$dry" = 0 ]; then
  for _ in $(seq 20); do ss -Hltn "sport = :$PORT" | grep -q . && break; sleep 0.5; done
  ss -Hltn "sport = :$PORT" | grep -q . || { say "error: the broker is not listening on 127.0.0.1:$PORT" >&2; exit 1; }
fi
token="$(OMP_NO_SCOPE=1 "$OMP" auth-broker token | tail -n 1 | tr -d '[:space:]')" \
  || { say "error: omp auth-broker token failed; is omp-auth-broker.service running?" >&2; exit 1; }
[ -n "$token" ] || { say "error: omp auth-broker token printed nothing" >&2; exit 1; }
token_sha="$(printf '%s' "$token" | sha256sum | cut -d' ' -f1)"

old_unit_present() {
  old_unit_running || [ -L "$UNIT_DIR/$OLD_UNIT" ] || [ -L "$UNIT_DIR/default.target.wants/$OLD_UNIT" ]
}
old_unit_running() {
  case "$(systemctl --user show -p ActiveState --value "$OLD_UNIT" 2>/dev/null)" in ""|inactive|failed) return 1 ;; esac
}

# The old unit's file left the profile, so its links may point at nothing: look at the links and the running state.
# `stop` only while it is not inactive (stopping an unloaded unit fails); the links go either way. A session the old
# unit left behind on the consumer (#193) is ended through its own record, with the same boot_id and start-time
# checks as the tunnel's ExecStartPre, since the new instance reads only its own record. That clean-up runs first and
# is best effort: if it cannot reach the consumer, the new instance still starts and retries until the port is free,
# which is never worse than no tunnel.
retire_old_unit() {
  # shellcheck disable=SC2016
  if ! run ssh_c "$1" 'f=~/.local/state/'"$(sq "$OLD_RECORD")"'; [ -r "$f" ] || exit 0
    { read -r b; if [ "$b" = "$(cat /proc/sys/kernel/random/boot_id)" ]; then while read -r p s; do
      case "$p" in ""|*[!0-9]*) continue;; esac
      [ -n "$s" ] && [ "$(cut -d" " -f22 "/proc/$p/stat" 2>/dev/null)" = "$s" ] && kill "$p" 2>/dev/null; done; fi; } <"$f"
    rm -f "$f"'; then
    say "warning: could not clear the old tunnel's record on $1; the new instance retries until the port is free"
  fi
  if old_unit_running && ! run systemctl --user stop "$OLD_UNIT"; then return 1; fi
  # ssh exits 255 when stopped or cut off, which leaves the unit failed; service-failover would keep trying to repair
  # a unit whose file is gone.
  run systemctl --user reset-failed "$OLD_UNIT" 2>/dev/null || true
  if [ -L "$UNIT_DIR/$OLD_UNIT" ] || [ -L "$UNIT_DIR/default.target.wants/$OLD_UNIT" ]; then
    run rm -f "$UNIT_DIR/default.target.wants/$OLD_UNIT" "$UNIT_DIR/$OLD_UNIT" || return 1
    run systemctl --user daemon-reload || return 1
  fi
}

# True when a tunnel record on the consumer (argument 2, under ~/.local/state) names a live sshd-session with the
# recorded start time: the forward that unit opened is the one holding the port (both units write the record only
# after ExitOnForwardFailure let the forward through).
session_on() {
  # shellcheck disable=SC2016
  ssh_c "$1" 'f=~/.local/state/'"$(sq "$2")"'; [ -r "$f" ] || exit 1
    { read -r b; read -r p s; } <"$f"
    case "$p" in ""|*[!0-9]*) exit 1;; esac; [ -n "$s" ] || exit 1
    [ "$b" = "$(cat /proc/sys/kernel/random/boot_id)" ] && [ "$(cut -d" " -f22 "/proc/$p/stat" 2>/dev/null)" = "$s" ]' 2>/dev/null
}

# 3. Each consumer. Every remote read tolerates failure and every change is checked, so one bad host is reported and
#    skipped instead of ending the run under `set -e`.
failed=0
skip() { say "skip $host: $*"; failed=1; }
for host in "${aliases[@]}"; do
  say "== $host"
  unit="omp-auth-broker-tunnel@$host.service"
  if ! ssh_c "$host" true; then skip "ssh $host fails (add a Host alias with key auth to ~/.ssh/config)"; continue; fi
  if ! ssh_c "$host" 'test -x ~/.local/bin/omp && command -v ss >/dev/null && python3 -c "import yaml" 2>/dev/null && test -r /proc/sys/kernel/random/boot_id'; then
    skip "needs Linux, ss, python3 with PyYAML and ~/.local/bin/omp there"; continue
  fi
  # A broker or the old tunnel unit on the consumer would hold or later steal its port; both must be retired there.
  if ssh_c "$host" "for u in $BROKER $OLD_UNIT; do { systemctl --user is-active --quiet \$u || systemctl --user is-enabled --quiet \$u; } && exit 0; done; exit 1" 2>/dev/null; then
    skip "$BROKER or $OLD_UNIT is active or enabled there; disable them there first"; continue
  fi
  # The unit cannot clear an alias's own forwards (that would also clear its -R), so an alias that carries any would
  # break the tunnel after the old clvm unit is already gone; README step 1 forbids them, this enforces it.
  if ssh -G -o ClearAllForwardings=no "$host" 2>/dev/null | grep -viE '^remotecommand none$' | grep -qiE '^(remoteforward|localforward|dynamicforward|remotecommand) '; then
    skip "the ssh alias $host carries a forward or RemoteCommand; give the tunnel a plain alias"; continue
  fi
  own_record="omp-auth-broker-tunnel.$PC_HOST.$host.pids"
  replaces_old=0
  [ "$host" = "$OLD_UNIT_ALIAS" ] && old_unit_present && replaces_old=1
  # Anything else holding the consumer's port (an old tunnel from another machine, a second alias, another account)
  # would receive the bearer token; leave such a consumer untouched. The port counts as this PC's only while a record
  # proves the holder is this instance's session, or on clvm the old unit's session, which is retired below. The check
  # only reads, so a dry run makes it too, and a probe that cannot reach the consumer counts as held.
  if ! session_on "$host" "$own_record" \
     && ! { [ "$replaces_old" = 1 ] && session_on "$host" "$OLD_RECORD"; }; then
    held=1
    for _ in $(seq 10); do
      probe=0
      ssh_c "$host" "ss -Hltn 'sport = :$PORT' | grep -q ." 2>/dev/null || probe=$?
      [ "$probe" = 1 ] && { held=0; break; }
      # A restart of this instance briefly has the port held before its new record is written.
      session_on "$host" "$own_record" && { held=0; break; }
      sleep 1
    done
    if [ "$held" = 1 ]; then
      if [ "$replaces_old" = 1 ] && old_unit_running; then
        skip "127.0.0.1:$PORT there is held, and this PC's $OLD_UNIT runs without a record proving it is the holder; stop it here and rerun"
      else
        skip "127.0.0.1:$PORT there is held by something other than this PC's tunnel"
      fi
      continue
    fi
  fi
  # The tunnel comes first and the consumer's config last: the URL and the token are written only once this
  # instance's own session holds the port, so a failure before that leaves the consumer exactly as it was, and the
  # bearer never goes to a listener that is not proven to be this tunnel.
  if ! reenable_if_elsewhere "$unit" omp-auth-broker-tunnel@.service; then skip "reenabling $unit failed"; continue; fi
  # The units run in the user manager, whose environment (ssh agent, Match exec) can differ from this shell's. Prove it
  # can reach the consumer before anything is enabled, and before the working old clvm tunnel is torn down. Stricter
  # than the unit on purpose: IdentityAgent=none proves the key works without an agent (README step 1), since an
  # agent the user manager sees while someone is logged in is gone after a reboot.
  if [ "$dry" = 0 ] \
     && ! systemd-run --user --wait --pipe --quiet /usr/bin/ssh -T -a -x -o IdentityAgent=none -o BatchMode=yes -o ConnectTimeout=10 \
          -o StrictHostKeyChecking=yes -o ControlMaster=no -o ControlPath=none "$host" true </dev/null >/dev/null 2>&1; then
    if [ "$replaces_old" = 1 ]; then
      skip "the systemd user manager cannot ssh to $host (agent-only key?); $OLD_UNIT was left running, but its unit file is gone from the profile, so it will not come back after a drop or reboot"
    else
      skip "the systemd user manager cannot ssh to $host (agent-only key?)"
    fi
    continue
  fi
  if [ "$replaces_old" = 1 ] && ! retire_old_unit "$host"; then
    skip "retiring $OLD_UNIT failed"; continue
  fi
  was_enabled=0; systemctl --user is-enabled --quiet "$unit" && was_enabled=1
  if { [ "$was_enabled" = 0 ] || ! systemctl --user is-active --quiet "$unit"; } \
     && ! run systemctl --user enable --now "$unit"; then
    # A unit this run enabled would otherwise retry forever (Restart=always) and draw service-failover repairs.
    # Not on clvm after the old unit was retired: there retrying is the only way back to a tunnel.
    if [ "$was_enabled" = 0 ] && [ "$replaces_old" = 0 ]; then run systemctl --user disable --now "$unit" 2>/dev/null || true; fi
    skip "starting $unit failed; see journalctl --user -u $unit"; continue
  fi
  url_differs=0; [ "$(remote_get "$host" auth.broker.url)" = "$URL" ] || url_differs=1
  token_differs=0; [ "$(remote_get "$host" auth.broker.token | sha256sum | cut -d' ' -f1)" = "$token_sha" ] || token_differs=1
  if [ "$dry" = 1 ]; then
    [ "$url_differs" = 1 ] && say "would set auth.broker.url on $host"
    [ "$token_differs" = 1 ] && say "would send the broker token to $host"
    continue
  fi
  session=0
  for _ in $(seq 15); do session_on "$host" "$own_record" && { session=1; break; }; sleep 2; done
  if [ "$session" = 0 ]; then
    # A refused forward still counts as started (Type=simple), so the same clean-up as for a failed start applies.
    if [ "$was_enabled" = 0 ] && [ "$replaces_old" = 0 ]; then run systemctl --user disable --now "$unit" 2>/dev/null || true; fi
    say "FAIL $host: $unit holds no session there; see journalctl --user -u $unit (its config was left unchanged)"; failed=1; continue
  fi
  # Counted from here: a first attempt refused while an old session still held the port is not a failure. NRestarts
  # only ever grows, so it is compared across the check, not against 0.
  restarts_before="$(systemctl --user show -p NRestarts --value "$unit")"
  config_dir="$(ssh_c "$host" "$REMOTE_OMP config path" 2>/dev/null | tail -n 1 | tr -d '\r' || true)"
  case "$config_dir" in /*) ;; *) skip "omp config path printed no absolute directory (its config was left unchanged)"; continue ;; esac
  config_file="$config_dir/config.yml"
  if [ "$url_differs" = 1 ] || [ "$token_differs" = 1 ]; then
    # ssh hands the consumer one command line for its login shell, which may be a plain POSIX sh.
    if ! printf '%s\n' "$token" | ssh_c "$host" "umask 077; python3 -c $(sq "$SET_BROKER_PY") $(sq "$config_file") $(sq "$URL")"; then
      skip "writing auth.broker there failed (its config was left unchanged)"; continue
    fi
  fi
  # The file holds the bearer token, whoever wrote it last (omp, rig, an earlier setup): keep it private on every run.
  ssh_c "$host" "chmod 600 $(sq "$config_file")" || say "warning: could not chmod 600 $config_file on $host"
  ok=0
  for _ in $(seq 15); do
    if ssh_c "$host" "$REMOTE_OMP auth-broker status" 2>/dev/null | grep -q '^OK ' && session_on "$host" "$own_record"; then ok=1; break; fi
    sleep 2
  done
  restarts_after="$(systemctl --user show -p NRestarts --value "$unit")"
  if [ "$ok" = 1 ] && [ "$restarts_after" = "$restarts_before" ]; then say "ok $host: omp auth-broker status reports OK through $unit"
  else say "FAIL $host: status OK=$ok, $unit restarts $restarts_before -> $restarts_after; see journalctl --user -u $unit"; failed=1; fi
done
exit "$failed"
