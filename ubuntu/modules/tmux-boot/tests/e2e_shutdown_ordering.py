#!/usr/bin/env python3
"""Ordering proof for the shutdown save, on the REAL user systemd and a REAL throwaway tmux server (about 40 s).

What it proves, with the shipped unit files, scripts and drop-ins (nothing is installed: units live in the runtime
dir /run/user/UID/systemd/user under a unique name and are removed again; no real shutdown is triggered):

  1. stop-alone:   `systemctl --user stop` of the tmux-shutdown-save unit runs the real ExecStop (tmux-shutdown-save ->
                   tmux-save-now stand-in) while the throwaway tmux server answers, and a snapshot is written.
  2. teardown WITHOUT the scope drop-ins: a transaction that stops the unit together with the pane scope and an omp
                   scope (a throwaway target with Conflicts= does what shutdown.target does at user-manager exit) kills
                   the panes BEFORE the ExecStop runs. This is why a plain After=tmux-boot.service is not enough.
  3. teardown WITH the shipped systemd-dropins/: ExecStop runs first, the server answers, the pane scope and the omp
                   scope are still active, the snapshot is written; only then do the scopes stop.
  4. watcher:      the real tmux-shutdown-watch under a transient unit holds a REAL logind delay lock (visible in
                   `systemd-inhibit --list`), an injected PrepareForShutdown(true) line (no real shutdown) makes it save
                   with the server alive, then it releases the lock (gone from the list) and keeps running; stopping
                   the unit ends it cleanly.

The live default tmux server is never touched: every tmux call goes through a shim pinned to a throwaway socket, and the
only units that conflict with the test target are the throwaway scopes created here, by exact name.

    python3 tests/e2e_shutdown_ordering.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = os.getuid()
RUNTIME = "/run/user/%d/systemd/user" % UID
TMUX = shutil.which("tmux", path="/usr/bin:/bin") or shutil.which("tmux")
P = "zzsd%d" % os.getpid()
DOWN = P + "-down.target"
SAVE = P + "-save.service"
WHO = P + "-lock"  # the watcher's lock identity: an installed tmux-shutdown-watch must never be mistaken for ours
failures = []


def sh(*argv, **kw):
    return subprocess.run(list(argv), capture_output=True, text=True, **kw)


def ctl(*argv):
    return sh("systemctl", "--user", *argv)


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def wait_for(pred, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def caller_leaf():
    with open("/proc/self/cgroup") as f:
        return f.read().strip().rsplit("/", 1)[-1]


def journal(unit, since):
    return sh("journalctl", "--user", "-u", unit, "--no-pager", "-o", "cat", "--since", since).stdout


class Rig:
    def __init__(self, tag):
        self.tag = tag
        self.t0 = time.strftime("%Y-%m-%d %H:%M:%S")
        self.dir = tempfile.mkdtemp(prefix="tmux-shutdown-e2e-")
        self.socket = "%s-%s" % (P, tag)
        self.shim = os.path.join(self.dir, "tmux")
        self.log = os.path.join(self.dir, "save.log")
        self.rdir = os.path.join(self.dir, "resurrect")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.rdir)
        os.makedirs(self.state)
        self.omp_scope = "omp-%s-%s.scope" % (P, tag)
        self.server_scope = "%s-%s-server.scope" % (P, tag)
        self.omp_proc = None
        self.runtime_files = []
        self.write_exe(self.shim, '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' % (TMUX, self.socket))
        # the tmux-save-now stand-in: records what the save SAW, and writes a snapshot of the panes it could still see
        self.save_now = os.path.join(self.dir, "tmux-save-now")
        self.write_exe(self.save_now, """#!/bin/sh
if "$TMUX_AUTOSAVE_TMUX" list-sessions >/dev/null 2>&1; then alive=yes; else alive=NO; fi
echo "tmux-save-now $* server_reachable=$alive pane_scope=$(systemctl --user is-active "$PANE_SCOPE") omp_scope=$(systemctl --user is-active "$OMP_SCOPE")" >> "$SAVE_LOG"
"$TMUX_AUTOSAVE_TMUX" list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} pid=#{pane_pid}' > "$RESURRECT_DIR/tmux_resurrect_$(date +%s%N).txt"
""")
        self.pane_scope = None  # set only after the pane really moved into a tmux-spawn-* scope

    @staticmethod
    def write_exe(path, text):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def start_server_and_omp(self):
        # The throwaway server runs in its OWN transient scope, never in the caller's (an agent session's) scope.
        r = sh("systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit=" + self.server_scope,
               "--", TMUX, "-L", self.socket, "-f", "/dev/null", "new-session", "-d", "-s", "main", "sleep 600")
        assert r.returncode == 0, r.stderr
        pid = sh(TMUX, "-L", self.socket, "display-message", "-p", "-t", "main", "#{pane_pid}").stdout.strip()

        def pane_leaf():
            # tmux moves the pane into its own tmux-spawn-*.scope a moment after new-session returns; until then the
            # pane still shows the cgroup it was forked in
            try:
                with open("/proc/%s/cgroup" % pid) as f:
                    return f.read().strip().rsplit("/", 1)[-1]
            except OSError:
                return ""

        assert wait_for(lambda: pane_leaf().startswith("tmux-spawn-") and pane_leaf().endswith(".scope")), pane_leaf()
        self.pane_scope = pane_leaf()
        assert self.pane_scope != caller_leaf(), "pane scope is the caller's own scope: " + self.pane_scope
        # an omp session stand-in: its own scope in omp.slice, exactly like ~/.local/bin/omp creates
        self.omp_proc = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", "--unit=" + self.omp_scope,
                                          "--slice=omp.slice", "--", "sleep", "600"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert wait_for(lambda: ctl("is-active", self.omp_scope).stdout.strip() == "active")

    def env_lines(self):
        return {
            "TMUX_AUTOSAVE_TMUX": self.shim,
            "TMUX_SAVE_NOW": self.save_now,
            "TMUX_BOOT_STATE_DIR": self.state,
            "SAVE_LOG": self.log,
            "RESURRECT_DIR": self.rdir,
            "PANE_SCOPE": self.pane_scope,
            "OMP_SCOPE": self.omp_scope,
        }

    def runtime(self, relpath, text):
        path = os.path.join(RUNTIME, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        self.runtime_files.append(path)

    def install_save_unit(self):
        with open(os.path.join(MODULE, "tmux-shutdown-save.service")) as f:
            text = f.read()
        text = text.replace("%h/.local/bin/tmux-shutdown-save", os.path.join(MODULE, "tmux-shutdown-save"))
        text = text.split("[Install]")[0]
        envs = "".join('Environment="%s=%s"\n' % kv for kv in self.env_lines().items())
        text = text.replace("[Unit]\n", "[Unit]\nConflicts=%s\nBefore=%s\n" % (DOWN, DOWN), 1)
        text = text.replace("[Service]\n", "[Service]\n" + envs, 1)
        self.runtime(SAVE, text)
        self.runtime(DOWN, "[Unit]\nDescription=throwaway stand-in for shutdown.target\n")
        # the throwaway scopes (by exact name only) join the teardown transaction
        for scope in (self.pane_scope, self.omp_scope):
            self.runtime("%s.d/10-%s-teardown.conf" % (scope, P), "[Unit]\nConflicts=%s\nBefore=%s\n" % (DOWN, DOWN))

    def install_shipped_dropins(self):
        # the SHIPPED drop-ins, matched by unit-name prefix, pointed at the throwaway save unit
        for d in ("tmux-spawn-.scope.d", "omp-.scope.d"):
            with open(os.path.join(MODULE, "systemd-dropins", d, "10-tmux-shutdown-save.conf")) as f:
                text = f.read().replace("tmux-shutdown-save.service", SAVE)
            self.runtime("%s/10-%s.conf" % (d, P), text)

    def save_log(self):
        try:
            with open(self.log) as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def snapshots(self):
        return [n for n in os.listdir(self.rdir) if n.startswith("tmux_resurrect_")]

    def owned(self, unit):
        """Only units this test created may be stopped: the unique-prefix names (zzsd<pid>...), plus the one pane scope that was read
        from the pane of OUR throwaway server (a tmux-spawn-* scope that is not the caller's own scope)."""
        # the omp stand-in must start with "omp-" (the shipped drop-in matches that prefix), so its unique part is infixed
        if unit.startswith(P) or (unit.startswith("omp-" + P) and unit.endswith(".scope")):
            return True
        return unit == self.pane_scope and unit.startswith("tmux-spawn-") and unit != caller_leaf()

    def cleanup(self):
        for unit in (SAVE, DOWN, self.pane_scope, self.omp_scope, self.server_scope, P + "-watch.service"):
            if unit and self.owned(unit):
                ctl("stop", unit)
            elif unit:
                print("REFUSING to stop %s: it does not carry the test prefix %s" % (unit, P))
        sh(TMUX, "-L", self.socket, "kill-server")
        if self.omp_proc:
            self.omp_proc.kill()
            self.omp_proc.wait()
        for path in self.runtime_files:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            parent = os.path.dirname(path)
            try:
                os.rmdir(parent)
            except OSError:
                pass
        ctl("daemon-reload")
        shutil.rmtree(self.dir, ignore_errors=True)


def teardown_scenario(tag, with_dropins):
    rig = Rig(tag)
    try:
        rig.start_server_and_omp()
        rig.install_save_unit()
        if with_dropins:
            rig.install_shipped_dropins()
        ctl("daemon-reload")
        check(ctl("start", SAVE).returncode == 0, "[%s] throwaway save unit started" % tag)
        if with_dropins:
            before = ctl("show", rig.pane_scope, "-p", "Before").stdout
            before_omp = ctl("show", rig.omp_scope, "-p", "Before").stdout
            check(SAVE in before, "[%s] shipped tmux-spawn-.scope.d drop-in matched the live tmux pane scope (Before=%s)" % (tag, SAVE))
            check(SAVE in before_omp, "[%s] shipped omp-.scope.d drop-in matched the omp scope in omp.slice" % tag)
        r = ctl("start", DOWN)  # one transaction: stops the save unit and both scopes, ordered by Before=/After=
        check(r.returncode == 0, "[%s] teardown transaction ran (%s)" % (tag, r.stderr.strip() or "ok"))
        wait_for(lambda: bool(rig.save_log()), 20)
        print("  save log: " + rig.save_log())
        print("  journal : " + " | ".join(l for l in journal(SAVE, rig.t0).splitlines() if l.startswith("tmux-shutdown-save")))
        return rig.save_log(), rig.snapshots()
    finally:
        rig.cleanup()


def stop_alone():
    rig = Rig("alone")
    try:
        rig.start_server_and_omp()
        rig.install_save_unit()
        ctl("daemon-reload")
        ctl("start", SAVE)
        check(rig.save_log() == "", "[alone] starting the unit saves nothing (ExecStart is /bin/true)")
        r = ctl("stop", SAVE)
        check(r.returncode == 0, "[alone] systemctl --user stop returned 0")
        log = rig.save_log()
        print("  save log: " + log)
        print("  journal : " + " | ".join(l for l in journal(SAVE, rig.t0).splitlines() if l.startswith("tmux-shutdown-save")))
        check("tmux-save-now --reason shutdown" in log or "--reason user-manager-stop" in log, "[alone] ExecStop ran tmux-save-now")
        check("server_reachable=yes" in log, "[alone] the tmux server was reachable inside ExecStop")
        check(len(rig.snapshots()) == 1, "[alone] a snapshot file was written")
    finally:
        rig.cleanup()


def watcher():
    rig = Rig("watch")
    unit = P + "-watch.service"
    events = os.path.join(rig.dir, "events.in")
    open(events, "w").close()
    try:
        rig.start_server_and_omp()
        env = rig.env_lines()
        env.update({
            "TMUX_SHUTDOWN_EVENT_CMD": "exec tail -n 0 -F %s" % events,
            "TMUX_SHUTDOWN_SAVE": os.path.join(MODULE, "tmux-shutdown-save"),
            "TMUX_SHUTDOWN_WHO": WHO,
        })
        args = ["systemd-run", "--user", "--unit=" + unit, "--collect", "--quiet"]
        for k, v in env.items():
            args += ["-E", "%s=%s" % (k, v)]
        r = sh(*args, "--", os.path.join(MODULE, "tmux-shutdown-watch"))
        check(r.returncode == 0, "[watch] watcher started as a transient unit (%s)" % (r.stderr.strip() or "ok"))

        def lock_listed():
            return WHO in sh("systemd-inhibit", "--list", "--no-pager").stdout

        check(wait_for(lock_listed), "[watch] a logind delay lock is held while the watcher runs")
        listing = [l for l in sh("systemd-inhibit", "--list", "--no-pager").stdout.splitlines() if WHO in l]
        print("  inhibit : " + " ".join(listing[0].split()) if listing else "  inhibit : (none)")
        check(bool(listing) and "shutdown" in listing[0] and "delay" in listing[0], "[watch] the lock is a shutdown/delay lock")
        time.sleep(1)
        check(rig.save_log() == "", "[watch] no event, no save")
        with open(events, "a") as f:  # the injected PrepareForShutdown(true): no real shutdown happens
            f.write("/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForShutdown (true,)\n")
        check(wait_for(lambda: bool(rig.save_log()), 20), "[watch] the injected PrepareForShutdown(true) triggered the save")
        check(wait_for(lambda: not lock_listed()), "[watch] the lock was released after the save (gone from systemd-inhibit --list)")
        print("  save log: " + rig.save_log())
        print("  journal : " + " | ".join(journal(unit, rig.t0).splitlines()))
        check("server_reachable=yes" in rig.save_log(), "[watch] the tmux server was reachable during the save")
        check("pane_scope=active omp_scope=active" in rig.save_log(), "[watch] the pane scope and the omp scope were still active during the save")
        check(len(rig.snapshots()) == 1, "[watch] a snapshot file was written")
        check(ctl("is-active", unit).stdout.strip() == "active", "[watch] the watcher keeps running (re-armed) after the save")
        ctl("stop", unit)
        check(ctl("show", unit, "-p", "ActiveState").stdout.strip() in ("ActiveState=inactive", "ActiveState="), "[watch] stopping the unit ended it")
        check(not lock_listed(), "[watch] no lock left after the unit stopped")
    finally:
        rig.cleanup()


def main():
    if not TMUX:
        print("tmux is not installed")
        return 2
    if ctl("show-environment").returncode != 0:
        print("no user systemd bus")
        return 2
    stop_alone()
    log, snaps = teardown_scenario("nodrop", with_dropins=False)
    check("pane_scope=inactive" in log or "omp_scope=inactive" in log or "server_reachable=NO" in log or log == "",
          "[nodrop] WITHOUT the scope drop-ins the panes are already gone when ExecStop runs (the problem the drop-ins solve)")
    log, snaps = teardown_scenario("drop", with_dropins=True)
    check("server_reachable=yes pane_scope=active omp_scope=active" in log,
          "[drop] WITH the shipped drop-ins ExecStop sees the server, the pane scope and the omp scope alive")
    check(len(snaps) == 1, "[drop] a snapshot file was written before the scopes stopped")
    watcher()
    print("\n%s" % ("ALL PASS" if not failures else "FAILURES: %d" % len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
