#!/usr/bin/env python3
"""End-to-end proof on a THROWAWAY tmux server (its own socket, resurrect dir and session maps) that

  1. continuum's native autosave works while status-right carries its token and a client is attached,
  2. it is DEAD once a Moshi-style `set -g status-right ''` has wiped the token, client attached or not (the 2026-11 incident),
  3. tmux-autosave (the module's timer-driven trigger) saves a snapshot and the omp session map anyway,
  4. after the server is killed, the module's tmux-boot restores the layout and rig's omp hook relaunches both omp
     sessions, by their exact session files, in the restored panes.

Not a unittest (it starts real omp processes, uses the real ~/.tmux plugins and rig, and takes about 6 minutes), so it is
named e2e_* and unittest discovery skips it:   python3 tests/e2e_isolated_restore.py

It never touches the default tmux server or any other socket: every tmux call goes through a shim that pins
`-L <throwaway socket>`. Exit 0 = all checks passed, 1 = a check failed, 2 = a prerequisite is missing.
"""

import datetime
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_procs  # noqa: E402

MODULE = Path(__file__).resolve().parent.parent
HOME = Path.home()
RIG_TMUX = HOME / ".config/rig/tmux"
PLUGINS = HOME / ".tmux/plugins"
INTERVAL_MIN = 1  # the throwaway server saves every minute instead of every 15

ROOT = Path(tempfile.mkdtemp(prefix="tmux-e2e-"))
SOCKET = f"tmux-e2e-{os.getpid()}"
SHIM = ROOT / "shim"
RIG = ROOT / "rig"
RESURRECT = ROOT / "resurrect"
FAILED = []


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(ok, what):
    say(("PASS " if ok else "FAIL ") + what)
    if not ok:
        FAILED.append(what)
    return ok


def write_exec(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def need(cmd):
    if not shutil.which(cmd):
        say(f"missing prerequisite: {cmd}")
        sys.exit(2)


def env():
    # the shim dir first: every `tmux` (plugins, hooks, rig, the module scripts) resolves to the throwaway socket and
    # continuum's "another tmux server runs, so no restore" count (`ps`, lines starting with `tmux`) ignores the real ones.
    # The state, the pre-server snapshot dir and the map dir are the throwaway ones too: without them every boot
    # records its restore attempt in the REAL ~/.local/state/tmux-boot and consults the REAL ~/.tmux/resurrect, and
    # three runs inside an hour arm the real restore-loop guard against this test. And the runner's own rig identity
    # (RIG_SESSION/ABDUCO_SOCKET_DIR, set when the test itself runs inside an omp harness session) must not leak into
    # the fixture omp processes: they would register under the runner's durable name, and rig's restore-panes then
    # fail-closes on the identity mismatch instead of relaunching them under their own.
    e = {
        **os.environ,
        "PATH": f"{SHIM}:{os.environ['PATH']}",
        "TMUX_BOOT_STATE_DIR": str(ROOT / "state"),
        "TMUX_BOOT_RESURRECT_DIR": str(RESURRECT),
        "TMUX_BOOT_MAP_DIR": str(RIG),  # tl_prepare_restore copies session maps into the dir it is told, never the real one
    }
    for k in ("RIG_SESSION", "RIG_AGENT_ID", "RIG_SIGNAL_SCOPED", "ABDUCO_SOCKET_DIR"):
        e.pop(k, None)
    return e


def sh(*args, **kw):
    return subprocess.run(list(args), env=kw.pop("env", env()), capture_output=True, text=True, check=False, **kw)


def tmux(*args):
    return sh("tmux", *args)


def option(name):
    return tmux("show-option", "-gqv", name).stdout.strip()


def last_save():
    value = option("@continuum-save-last-timestamp")
    return int(value) if value.isdigit() else 0


def wait_for(cond, secs, step=1.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def live_omp():
    """tg-ctl's records of the live omp processes, as a list: a duplicate must stay visible."""
    out = sh("tg-ctl", "omp-live", "status", "--json").stdout
    try:
        return [s for s in json.loads(out) if s.get("sessionFile")]
    except (ValueError, TypeError):
        return []


def records_for(session_file):
    return [s for s in live_omp() if s["sessionFile"] == session_file]


def durable_name(session_file):
    # rig names the durable session of a relaunch after the session id: "omp-recover-" + sha256(id)[:20]
    session_id = Path(session_file).stem.split("_", 1)[1]
    return "omp-recover-" + hashlib.sha256(session_id.encode()).hexdigest()[:20]


def process_table():
    rows = []
    for line in sh("ps", "-eo", "pid,ppid,args", env=os.environ).stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def owned_pids(session_files):
    """The omp/abduco/harness processes of OUR session files or durable names (ps, not tg-ctl); never an inspector of a fixture."""
    return e2e_procs.owned_pids(process_table(), set(session_files), {durable_name(f) for f in session_files}, os.getpid())


def descendant_args(root_pid):
    children = {}
    args_of = {}
    for pid, ppid, args in process_table():
        children.setdefault(ppid, []).append(pid)
        args_of[pid] = args
    queue, found = [root_pid], []
    while queue:
        pid = queue.pop()
        found.append(args_of.get(pid, ""))
        queue.extend(children.get(pid, []))
    return found


def fixtures():
    for d in (SHIM, RIG, RESURRECT, ROOT / "p1", ROOT / "p2"):
        d.mkdir(exist_ok=True)
    write_exec(SHIM / "ps", f'#!/usr/bin/env bash\n{shutil.which("ps")} "$@" | {{ grep -v "^tmux" || true; }}\n')
    write_exec(SHIM / "tmux", f'#!/bin/sh\nexec {shutil.which("tmux")} -L {SOCKET} -f {ROOT}/tmux.conf "$@"\n')
    # rig's generated scripts and conf, unchanged except for where they keep the maps
    for name in ("cc-save.sh", "omp-save.sh", "codex-save.sh", "cc-restore.sh", "omp-restore.sh", "codex-restore.sh", "rig.tmux.conf"):
        write_exec(RIG / name, (RIG_TMUX / name).read_text().replace(str(RIG_TMUX), str(RIG)))
    (ROOT / "tmux.conf").write_text(
        "# mirrors ~/.tmux.conf: status-right first, then the rig-generated file\n"
        "set -g status-right '#{?#{e|>=:#{client_width},100},#{=/45/…:pane_title},}'\n"
        f"set -g @resurrect-dir {RESURRECT}\n"
        f"source-file '{RIG / 'rig.tmux.conf'}'\n"
        f"set -g @continuum-save-interval '{INTERVAL_MIN}'\n"
    )
    sessions = {}
    for name in ("p1", "p2"):
        cwd = str(ROOT / name)
        directory = HOME / ".omp/agent/sessions" / ("-" + cwd.lstrip("/").replace("/", "-"))
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        sid = str(uuid.uuid4())
        path = directory / f"{stamp.replace(':', '-').replace('.', '-')}_{sid}.jsonl"
        rows = [
            {"type": "title", "v": 1, "title": "", "updatedAt": stamp, "pad": " " * 150},
            {"type": "session", "version": 3, "id": sid, "timestamp": stamp, "cwd": cwd},
            {"type": "thinking_level_change", "id": uuid.uuid4().hex[:8], "parentId": None, "timestamp": stamp, "thinkingLevel": "high", "configured": "auto"},
            {"type": "service_tier_change", "id": uuid.uuid4().hex[:8], "parentId": None, "timestamp": stamp, "serviceTier": None},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        sessions[name] = str(path)
    return sessions


def client(seconds):
    """A real tmux client on a pty, so the status line is drawn exactly as for a terminal."""
    code = (
        "import fcntl,os,pty,select,signal,struct,sys,termios,time\n"
        "pid,fd=pty.fork()\n"
        f"if pid==0:\n os.environ['TERM']='xterm-256color'; os.execv({shutil.which('tmux')!r},['tmux','-L',{SOCKET!r},'attach-session','-t','work'])\n"
        "fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',50,200,0,0))\n"
        f"end=time.time()+{seconds}\n"
        "while time.time()<end:\n"
        " r,_,_=select.select([fd],[],[],0.5)\n"
        " if r:\n"
        "  try: os.read(fd,65536)\n"
        "  except OSError: break\n"
        "os.kill(pid,signal.SIGHUP); os.waitpid(pid,0)\n"
    )
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def snapshots():
    return sorted(p.name for p in RESURRECT.glob("tmux_resurrect_*.txt"))


def map_entries():
    path = RIG / "omp-sessions.map"
    if not path.exists():
        return {}
    rows = [line.split("\t") for line in path.read_text().splitlines() if line]
    return {r[1]: r[2] for r in rows}  # cwd -> session file


def cleanup(session_files):
    files = list(session_files.values())
    sh("tmux", "kill-server")
    # the relaunched omp lives under abduco, which outlives the server by design: stop every process that carries OUR
    # session files or durable names (found with ps, so a failing tg-ctl cannot hide one), and only those
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in owned_pids(files):
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        wait_for(lambda: not owned_pids(files), 10, step=0.5)
    left = owned_pids(files)
    if left:
        say(f"WARNING: processes {left} still run, fixtures stay in place under {ROOT}")
        return
    for path in files:
        shutil.rmtree(Path(path).parent, ignore_errors=True)
    shutil.rmtree(ROOT, ignore_errors=True)


def main():
    for cmd in ("tmux", "rig", "omp", "abduco", "tg-ctl", "lsof"):
        need(cmd)
    for plugin in ("tmux-resurrect", "tmux-continuum", "tpm"):
        if not (PLUGINS / plugin).is_dir():
            say(f"missing prerequisite: ~/.tmux/plugins/{plugin}")
            sys.exit(2)
    if not (RIG_TMUX / "omp-save.sh").exists():
        say("missing prerequisite: rig's generated ~/.config/rig/tmux/*.sh (run `rig apply commit`)")
        sys.exit(2)

    sessions = fixtures()
    say(f"throwaway server: socket {SOCKET}, root {ROOT}")
    try:
        # ── 0. a fresh server the way boot starts it ───────────────────────────────────────────────────────────────────
        boot = sh(str(MODULE / "tmux-boot"), env={**env(), "TMUX_BOOT_WAIT": "5"})
        check("no saved snapshot" in boot.stdout, f"tmux-boot starts the server with no snapshot ({boot.stdout.strip()})")
        tmux("new-session", "-d", "-s", "work", "-n", "w1", "-x", "200", "-y", "50", "-c", str(ROOT / "p1"))
        tmux("new-window", "-t", "work", "-n", "w2", "-c", str(ROOT / "p2"))
        time.sleep(2)
        tmux("send-keys", "-t", "work:w1", f"omp --resume {sessions['p1']}", "Enter")
        tmux("send-keys", "-t", "work:w2", f"omp --resume {sessions['p2']}", "Enter")
        check(wait_for(lambda: all(len(records_for(f)) == 1 for f in sessions.values()), 60), "both omp sessions are live, one process each (tg-ctl omp-live)")
        check("continuum_save.sh" in tmux("show-option", "-gv", "status-right").stdout, "continuum's token is in status-right after the plugin loaded")
        start_save = last_save()
        check(start_save > 0, "continuum set its last-save timestamp when it loaded")

        # ── 1. native autosave works while the token is there and a client draws the status line ─────────────────────────
        c = client(80)
        got = wait_for(lambda: last_save() > start_save, INTERVAL_MIN * 60 + 40)
        check(got, f"native continuum autosave fires within {INTERVAL_MIN} min + one status interval while the token is intact")
        # continuum stamps its timestamp when it LAUNCHES save.sh in the background: wait for what the save writes
        check(wait_for(lambda: bool(snapshots()), 30), f"a snapshot was written: {snapshots()}")
        want = {str(ROOT / "p1"): sessions["p1"], str(ROOT / "p2"): sessions["p2"]}
        check(wait_for(lambda: map_entries() == want, 30), "rig's post-save hook wrote omp-sessions.map with both sessions by exact file")
        c.wait()

        # ── 2. the incident: a Moshi-style attach wipes status-right, and continuum's autosave is dead ─────────────────────
        tmux("set", "-g", "status-right", "")
        check(option("status-right") == "", "status-right wiped like Moshi's `tmux set -g status-right ''`")
        before = last_save()
        c = client(INTERVAL_MIN * 60 + 45)
        time.sleep(INTERVAL_MIN * 60 + 30)
        check(last_save() == before, f"CONTROL: no autosave in {INTERVAL_MIN * 60 + 30}s with a client attached and status-right wiped (the bug)")
        c.wait()

        # ── 3. tmux-autosave: the timer-driven trigger saves anyway, with no client and status-right still wiped ─────────
        check(not tmux("list-clients").stdout.strip(), "no client attached")
        (RIG / "omp-sessions.map").unlink()
        run = sh(str(MODULE / "tmux-autosave"), env={**env(), "TMUX_AUTOSAVE_TMUX": str(SHIM / "tmux")})
        say(f"tmux-autosave: exit {run.returncode}: {(run.stdout + run.stderr).strip()}")
        check(run.returncode == 0, "tmux-autosave exits 0")
        check(wait_for(lambda: last_save() > before, 10), "tmux-autosave advanced continuum's last-save timestamp")
        check(wait_for(lambda: map_entries() == want, 20), "the save it triggered rewrote omp-sessions.map with both sessions")

        # ── 4. reboot: the server dies, boot restores the layout and the omp hook relaunches the sessions ────────────────
        pids_before = owned_pids(sessions.values())
        check(len(pids_before) >= 2, f"the omp processes of the first run are visible to ps: {pids_before}")
        tmux("kill-server")
        check(wait_for(lambda: not set(pids_before) & set(owned_pids(sessions.values())), 20), "the old omp processes ended with the server")
        check(sh("tmux", "list-sessions").returncode != 0, "the throwaway server is gone")
        boot = sh(str(MODULE / "tmux-boot"), env={**env(), "TMUX_BOOT_WAIT": "30"})
        say(f"tmux-boot: {boot.stdout.strip()} {boot.stderr.strip()}")
        check(boot.returncode == 0 and "continuum restored" in boot.stdout, "tmux-boot: continuum restored the layout")
        panes = tmux("list-panes", "-a", "-F", "#{session_name}:#{window_name}").stdout.split()
        check({"work:w1", "work:w2"} <= set(panes), f"both windows are back: {sorted(set(panes))}")
        check(
            wait_for(lambda: all(len(records_for(f)) == 1 for f in sessions.values()), 60),
            "omp was relaunched: exactly one live process per expected session file, by the exact files from the map",
        )
        for window, name in (("work:w1", "p1"), ("work:w2", "p2")):
            durable = durable_name(sessions[name])
            pane_pid = tmux("display-message", "-p", "-t", window, "#{pane_pid}").stdout.strip()
            attached = pane_pid.isdigit() and any(durable in a for a in descendant_args(int(pane_pid)))
            check(attached, f"{window} runs the durable (abduco) client of its own session {durable}")
        check(
            all((records_for(f) or [{}])[0].get("durableSession") == durable_name(f) for f in sessions.values()),
            "the relaunches run under rig's durable launcher, named after their session ids",
        )
    finally:
        cleanup(sessions)
    print("\nRESULT:", "FAILED: " + "; ".join(FAILED) if FAILED else "all checks passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
