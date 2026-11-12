#!/usr/bin/env python3
"""End-to-end proof of the hardening layers on a THROWAWAY tmux server (its own socket, resurrect dir, state dir and session
maps), with the real tmux-resurrect and tmux-continuum plugins and a fake agent (`omp` is a symlink to sleep, the rig hooks
are small scripts that write the map from the panes). It proves, in this order:

  1. the real Moshi attach command line (`tmux -u set -g set-titles on \\; set -g mouse on \\; set -g status-right '' \\;
     attach-session -t =work`) empties status-right, and saves STILL happen: by the timer path (tmux-autosave) and by the
     event path (a layout hook runs the debounced tmux-save-now), with no client attached,
  2. tmux-watchdog notices a removed layout hook and a wiped @resurrect-save-script-path, heals both and alerts (stub
     sender), and notices a missing session map while an agent pane runs (heals it by a forced save; when the hook is
     broken it reports the problem and alerts),
  3. two tmux-save-now at once serialize (one runs, one waits, the third is covered),
  4. a degenerate snapshot (every window but one closed, no agent left) never replaces `last`; it is kept under
     degenerate/ and alerted,
  5. after the throwaway server is killed (through its own socket, by python), tmux-watchdog with recovery ON starts it
     again through `rig omp watchdog` -> tmux-recover -> tmux-boot, restored from the newest non-degenerate snapshot, and
     the rig restore hook ran; with recovery OFF (the default) nothing is started.

Not a unittest (real tmux, real plugins, minutes): named e2e_*, so discovery skips it:  python3 tests/e2e_hardening.py
It never touches the default tmux server or any other socket: every tmux call goes through a shim that pins `-L <socket>`,
systemctl is a stub, and rig's identity state of the recovery step lives under a temp HOME. Exit 0 = all checks passed,
1 = a check failed, 2 = a prerequisite is missing.
"""

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent
HOME = Path.home()
PLUGINS = HOME / ".tmux/plugins"

ROOT = Path(tempfile.mkdtemp(prefix="tmux-hardening-"))
SOCKET = f"tmux-hardening-{os.getpid()}"
SHIM = ROOT / "shim"
RESURRECT = ROOT / "resurrect"
STATE = ROOT / "state"
RIG = ROOT / "rig"
FAKE_HOME = ROOT / "home"
FAILED = []
DEBOUNCE = 2


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


def env(**extra):
    return {
        **os.environ,
        "PATH": f"{SHIM}:{os.environ['PATH']}",
        "TMUX_BOOT_STATE_DIR": str(STATE),
        "TMUX_SAVE_NOW_MAP_DIR": str(RIG),
        "TMUX_ALERT_CMD": str(ROOT / "alert-sender"),
        "TMUX_SAVE_DEBOUNCE": str(DEBOUNCE),
        "TMUX_BOOT_RESURRECT_DIR": str(RESURRECT),
        "TMUX_AUTOSAVE_TMUX": str(SHIM / "tmux"),
        "TMUX_WATCHDOG_RIG_CONF": str(RIG / "rig.tmux.conf"),
        "TMUX_WATCHDOG_UNIT_DIR": str(ROOT / "units"),
        "TMUX_WATCHDOG_SOCKET": f"/tmp/tmux-{os.getuid()}/{SOCKET}",
        **extra,
    }


def sh(*args, **kw):
    return subprocess.run(list(args), env=kw.pop("env", env()), capture_output=True, text=True, check=False, **kw)


def tmux(*args):
    return sh("tmux", *args)


def option(name):
    return tmux("show-option", "-gqv", name).stdout.strip()


def wait_for(cond, secs, step=0.5):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def snapshots():
    return sorted(p.name for p in RESURRECT.glob("tmux_resurrect_*.txt"))


def last_name():
    link = RESURRECT / "last"
    return os.path.basename(os.readlink(link)) if link.is_symlink() else None


def map_size():
    path = RIG / "omp-sessions.map"
    return path.stat().st_size if path.exists() else -1


def alerts():
    path = ROOT / "alerts.log"
    return path.read_text().splitlines() if path.exists() else []


def clear_alerts():
    (ROOT / "alerts.log").write_text("")
    for f in (STATE / "alerts").glob("*"):
        f.unlink()


def snapshot_text(name):
    return (RESURRECT / name).read_text()


def windows_in(name):
    return sum(1 for line in snapshot_text(name).splitlines() if line.startswith("window\t"))


def agents_in(name):
    return sum(1 for line in snapshot_text(name).splitlines() if line.startswith("pane\t") and line.split("\t")[9] == "omp")


def fixtures():
    for d in (SHIM, RESURRECT, STATE, RIG, FAKE_HOME, ROOT / "units"):
        d.mkdir(exist_ok=True)
    # hides the real tmux servers from continuum's "another server runs" count; ps's own exit status is kept (rig's identity probe reads it)
    write_exec(SHIM / "ps", f'#!/usr/bin/env bash\nout=$({shutil.which("ps")} "$@"); rc=$?\n[ -z "$out" ] || {{ printf "%s\\n" "$out" | {{ grep -v "^tmux" || true; }}; }}\nexit $rc\n')
    write_exec(SHIM / "tmux", f'#!/bin/sh\nexec {shutil.which("tmux")} -L {SOCKET} -f {ROOT}/tmux.conf "$@"\n')
    # a symlink named omp to python: the process is called `omp` (coreutils' sleep is a multi-call binary that would refuse the name)
    os.symlink(shutil.which("python3"), SHIM / "omp")
    write_exec(
        SHIM / "systemctl",
        '#!/bin/sh\nshift\ncase "$1" in is-enabled) echo enabled;; show) echo loaded;; esac\nexit 0\n',
    )
    write_exec(ROOT / "alert-sender", f'#!/bin/sh\nprintf "%s\\n" "$1" >> {ROOT}/alerts.log\n')
    # the fake rig hooks: the map holds one line per pane that runs omp, unless the hook is switched off
    write_exec(
        RIG / "omp-save.sh",
        f'#!/bin/sh\n[ -e {ROOT}/hook-broken ] && exit 0\n'
        f'tmux list-panes -a -F "#{{session_name}}:#{{window_index}}.#{{pane_index}}\t#{{pane_current_path}}\t#{{pane_current_command}}" '
        f"| awk -F'\\t' '$3 == \"omp\"' > {RIG}/omp-sessions.map\n",
    )
    write_exec(RIG / "omp-restore.sh", f'#!/bin/sh\ncat {RIG}/omp-sessions.map >> {ROOT}/restored.log 2>/dev/null\necho restored >> {ROOT}/restored.log\n')
    (RIG / "rig.tmux.conf").write_text(
        f'set -g @resurrect-hook-post-save-all "{RIG}/omp-save.sh"\n'
        f'set -g @resurrect-hook-post-restore-all "{RIG}/omp-restore.sh"\n'
        "set -g @continuum-save-interval '1'\n"
    )
    (ROOT / "tmux.conf").write_text(
        "set -g status-right '#{?#{e|>=:#{client_width},100},#{=/45/…:pane_title},}'\n"
        f"set -g @resurrect-dir {RESURRECT}\n"
        "set -g @resurrect-capture-pane-contents 'on'\n"
        "set -g @continuum-restore 'on'\n"
        "set -g @continuum-save-interval '1'\n"
        f'set -g @resurrect-hook-post-save-all "{RIG}/omp-save.sh"\n'
        f'set -g @resurrect-hook-post-restore-all "{RIG}/omp-restore.sh"\n'
        f"run-shell {PLUGINS}/tmux-resurrect/resurrect.tmux\n"
        f"run-shell {PLUGINS}/tmux-continuum/continuum.tmux\n"
    )


def attach_like_moshi(seconds):
    """A real client on a pty that runs the Moshi iOS client's attach command line."""
    code = (
        "import fcntl,os,pty,select,signal,struct,sys,termios,time\n"
        "pid,fd=pty.fork()\n"
        "if pid==0:\n"
        " os.environ['TERM']='xterm-256color'\n"
        f" os.execv({str(SHIM / 'tmux')!r},['tmux','-u','set','-g','set-titles','on',';','set','-g','mouse','on',';','set','-g','status-right','',';','attach-session','-t','=work'])\n"
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


def stop_throwaway_server():
    # only the throwaway server, through its own socket name (python, so no pattern-based kill is involved)
    subprocess.run([shutil.which("tmux"), "-L", SOCKET, "kill-server"], capture_output=True, check=False)


def watchdog(**extra):
    return sh(str(MODULE / "tmux-watchdog"), env=env(HOME=str(FAKE_HOME), **extra))


def phase0_boot():
    boot = sh(str(MODULE / "tmux-boot"), env=env(TMUX_BOOT_WAIT="5"))
    check(boot.returncode == 0, f"tmux-boot starts the throwaway server ({boot.stdout.strip()} {boot.stderr.strip()})")
    tmux("new-session", "-d", "-s", "work", "-n", "w1", "-x", "200", "-y", "50")
    for name in ("w2", "w3", "w4"):
        tmux("new-window", "-t", "work", "-n", name)
    for w in ("w1", "w2", "w3", "w4"):
        tmux("split-window", "-t", f"work:{w}")
    tmux("respawn-pane", "-k", "-t", "work:w1.0", "omp -c 'import time; time.sleep(3600)'")
    time.sleep(1)
    check("omp" in tmux("list-panes", "-a", "-F", "#{pane_current_command}").stdout.split(), "an omp-named pane runs")
    # the layout hooks are asserted by the timer's script: this first run is what installs them
    run = sh(str(MODULE / "tmux-autosave"), env=env())
    say(f"tmux-autosave: exit {run.returncode}: {(run.stdout + run.stderr).strip()[:300]}")
    saved = sh(str(MODULE / "tmux-save-now"), "--reason", "e2e-first", env=env())
    check(saved.returncode == 0 and last_name() is not None, f"a first snapshot exists: {last_name()} ({saved.stdout.strip()})")
    check(agents_in(last_name()) == 1 and windows_in(last_name()) == 5, "it holds the 5 windows (main + w1..w4) and the agent pane")
    check((RESURRECT / "last-with-harness").is_symlink(), "last-with-harness points at it")
    check(option("@resurrect-save-script-path").endswith("tmux-resurrect-save"), "@resurrect-save-script-path is the guarding wrapper")
    check("tmux-save-debounce" in tmux("show-hooks", "-g", "window-linked").stdout, "the layout hooks are in the server")
    check(map_size() > 0, "the post-save hook wrote the omp map")


def phase1_moshi_attach():
    """The Moshi attach wipes status-right; saves still happen."""
    client = attach_like_moshi(6)
    check(wait_for(lambda: option("status-right") == "", 10), "the Moshi attach command line emptied status-right")
    client.wait()
    check("continuum_save.sh" not in option("status-right"), "continuum's status-right trigger is gone for good")
    check(not tmux("list-clients").stdout.strip(), "no client is attached")
    tmux("new-window", "-t", "work", "-n", "timer-w")
    time.sleep(DEBOUNCE + 4)  # the event path saves this window: let it settle, so the timer path is measured on its own
    before = last_name()      # ... and only THEN take the baseline, or the control below compares against the pre-event state
    (STATE / "debounce.dirty").unlink(missing_ok=True)
    return before


def phase2_timer_and_event_paths(before):
    for hook in ("session-created", "session-closed", "window-linked", "window-unlinked", "after-new-window", "after-split-window", "after-kill-pane"):
        tmux("set-hook", "-gu", hook)  # no event path for this step ...
    for hook in ("window-renamed", "pane-exited"):
        tmux("set-hook", "-gwu", hook)  # ... a new window is renamed by tmux itself, which would fire its hook too
    tmux("set-option", "-g", "@continuum-save-last-timestamp", str(int(time.time()) - 20 * 60))
    tmux("new-window", "-t", "work", "-n", "timer-w2")
    time.sleep(DEBOUNCE + 4)
    check(last_name() == before, "CONTROL: with status-right wiped and no hook and no timer run, a new window is NOT saved (nothing else triggers a save)")
    run_timer_path(before)
    run_event_path()


def run_timer_path(before):
    run = sh(str(MODULE / "tmux-autosave"), env=env(TMUX_AUTOSAVE_SAVE_WAIT="40"))
    say(f"tmux-autosave: exit {run.returncode}: {(run.stdout + run.stderr).strip()[:300]}")
    check(run.returncode == 0, "tmux-autosave (the timer path) exits 0 with status-right wiped and no client")
    check(wait_for(lambda: last_name() != before and "timer-w2" in snapshot_text(last_name()), 20), f"the timer path saved the new window: {last_name()}")
    # tmux-save-now writes its history line after the snapshot is complete (archive, maps, retention)
    check(wait_for(lambda: "reason=continuum" in (STATE / "save-history").read_text(), 15), "it went through the guard (save-history has a reason=continuum save)")
    check("tmux-save-debounce" in tmux("show-hooks", "-g", "window-linked").stdout, "tmux-autosave re-asserted the layout hooks it just found missing")


def run_event_path():
    before = last_name()
    tmux("new-window", "-t", "work", "-n", "event-w")
    check(wait_for(lambda: last_name() != before and "event-w" in snapshot_text(last_name()), DEBOUNCE + 25), f"the event path saved the new window with no timer: {last_name()}")
    check(wait_for(lambda: "reason=event" in (STATE / "save-history").read_text(), 15), "it went through the guard (save-history has a reason=event save)")


def phase3_watchdog_heal():
    """The watchdog heals a removed hook and a wiped saver option, and alerts."""
    sh(str(MODULE / "tmux-watchdog"), "recovery", "on", env=env(HOME=str(FAKE_HOME)))
    clear_alerts()
    tmux("set-hook", "-gu", "session-closed")
    tmux("set-option", "-g", "@resurrect-save-script-path", str(PLUGINS / "tmux-resurrect/scripts/save.sh"))
    res = watchdog()
    say(f"tmux-watchdog: exit {res.returncode}: {(res.stdout + res.stderr).strip()[:600]}")
    check(res.returncode == 0, "tmux-watchdog exits 0 after healing")
    check("tmux-save-debounce" in tmux("show-hooks", "-g", "session-closed").stdout, "the removed hook is back")
    check(option("@resurrect-save-script-path").endswith("tmux-resurrect-save"), "the wiped @resurrect-save-script-path points at the wrapper again")
    check(option("@tmux-boot-resurrect-save-script").endswith("tmux-resurrect/scripts/save.sh"), "resurrect's own path is kept discoverable")
    check(any("repaired" in a for a in alerts()), f"it alerted: {alerts()[:2]}")
    res = watchdog()
    check(res.returncode == 0 and "healed" not in res.stdout, "a second run finds nothing to do")


def phase4_missing_map():
    """A missing map while an agent pane runs."""
    (RIG / "omp-sessions.map").unlink(missing_ok=True)
    clear_alerts()
    res = watchdog()
    check(res.returncode == 0 and map_size() > 0, f"a missing map is detected and a forced save rewrites it ({res.stdout.strip()[-200:]})")
    (ROOT / "hook-broken").write_text("")
    (RIG / "omp-sessions.map").unlink(missing_ok=True)
    clear_alerts()
    res = watchdog()
    check(res.returncode == 1 and "omp-sessions.map is missing or empty" in res.stderr, "with a broken hook the watchdog reports the problem (exit 1)")
    check(any("omp-sessions.map" in a for a in alerts()), "and alerts")
    (ROOT / "hook-broken").unlink()
    watchdog()


def phase5_serialized_saves():
    """Two saves at once serialize."""
    slow = ROOT / "slow-saver"
    write_exec(slow, f'#!/bin/sh\necho "start $(date +%s.%N)" >> {ROOT}/saver-times.log\nsleep 2\n{PLUGINS}/tmux-resurrect/scripts/save.sh "$@"\necho "end $(date +%s.%N)" >> {ROOT}/saver-times.log\n')
    procs = [subprocess.Popen([str(MODULE / "tmux-save-now"), "--reason", name], env=env(TMUX_SAVE_NOW_SAVER=str(slow)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("c1", "c2", "c3")]
    outs = [p.communicate(timeout=60) for p in procs]
    text = "\n".join(o[0] + o[1] for o in outs)
    times = [line.split()[0] for line in (ROOT / "saver-times.log").read_text().splitlines()]
    check(all(p.returncode == 0 for p in procs), "all three requests exit 0")
    check(times == ["start", "end"] * (len(times) // 2) and len(times) >= 4, f"the real saver never ran twice at once: {times}")
    check("waits for it" in text and "covered by it" in text or len(times) == 4, "one request waited, the others were covered")


def phase6_degenerate():
    """A degenerate snapshot never replaces `last`."""
    time.sleep(DEBOUNCE + 3)
    rich = last_name()
    clear_alerts()
    for w in ("w4", "w3", "w2", "timer-w", "timer-w2", "event-w", "w1"):
        tmux("kill-window", "-t", f"work:{w}")
    time.sleep(DEBOUNCE + 6)  # the event path fires into the guard
    check(len(tmux("list-windows", "-a").stdout.splitlines()) <= 2, "the throwaway server is down to a bare main")
    res = sh(str(MODULE / "tmux-save-now"), "--reason", "e2e-degenerate", env=env())
    check(res.returncode == 0 and "DEGENERATE" in res.stderr, f"the guard rejected it ({res.stderr.strip()[:200]})")
    check(agents_in(last_name()) == 1 and windows_in(last_name()) >= 3, f"`last` ({last_name()}) still holds the agent pane and a rich layout (was {rich})")
    check(any(RESURRECT.joinpath("degenerate").glob("*.txt")), "the rejected file is kept under degenerate/")
    check(len([a for a in alerts() if "degenerate" in a]) == 1, f"exactly one alert: {alerts()}")
    return last_name()


def phase7_recovery(good):
    """Server death: recovery off does nothing, recovery on restores the newest good snapshot."""
    check(watchdog().returncode == 0, "a healthy run records the server identity for recovery")
    stop_throwaway_server()
    check(wait_for(lambda: tmux("list-sessions").returncode != 0, 10), "the throwaway server is dead")
    sh(str(MODULE / "tmux-watchdog"), "recovery", "off", env=env(HOME=str(FAKE_HOME)))
    res = watchdog()
    check(res.returncode == 1 and tmux("list-sessions").returncode != 0, "recovery OFF (the default): reported, nothing started")
    sh(str(MODULE / "tmux-watchdog"), "recovery", "on", env=env(HOME=str(FAKE_HOME)))
    clear_alerts()
    res = watchdog()
    say(f"tmux-watchdog (recovery): exit {res.returncode}: {(res.stdout + res.stderr).strip()[:800]}")
    check(res.returncode == 0 and tmux("list-sessions").returncode == 0, "recovery ON: a server is up again")
    check(
        wait_for(lambda: len(tmux("list-windows", "-a").stdout.splitlines()) >= windows_in(good), 60),
        f"the layout of {good} is back ({windows_in(good)} windows)",
    )
    check(wait_for(lambda: (ROOT / "restored.log").exists() and "restored" in (ROOT / "restored.log").read_text(), 30), "rig's post-restore hook ran")
    check(last_name() == good, "`last` still points at the good snapshot after the recovery")
    check(any("had died" in a for a in alerts()), f"the recovery was reported: {alerts()[:1]}")


def main():
    for cmd in ("tmux", "rig", "lsof", "flock", "setsid"):
        need(cmd)
    for plugin in ("tmux-resurrect", "tmux-continuum"):
        if not (PLUGINS / plugin).is_dir():
            say(f"missing prerequisite: ~/.tmux/plugins/{plugin}")
            sys.exit(2)
    fixtures()
    say(f"throwaway server: socket {SOCKET}, root {ROOT}")
    try:
        phase0_boot()
        phase2_timer_and_event_paths(phase1_moshi_attach())
        phase3_watchdog_heal()
        phase4_missing_map()
        phase5_serialized_saves()
        phase7_recovery(phase6_degenerate())
    finally:
        stop_throwaway_server()
        subprocess.run(["sleep", "1"], check=False)
        shutil.rmtree(ROOT, ignore_errors=True)
    print("\nRESULT:", "FAILED: " + "; ".join(FAILED) if FAILED else "all checks passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
