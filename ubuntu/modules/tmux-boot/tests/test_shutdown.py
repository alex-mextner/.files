"""Shutdown/reboot save of the tmux-boot module: tmux-shutdown-save, tmux-shutdown-watch, tmux-boot-selfcheck, the units.

Stub-based and fast (a few seconds): the system bus, logind and the journal are replaced by stubs injected through the
scripts' env overrides, the tmux server is a REAL throwaway server on its own socket (a PATH shim pins `tmux` to it) so
that "the server was still reachable when the save ran" is observed, not assumed. Nothing touches the live tmux
server, the real systemd, the real journal or the real $HOME.

    cd ubuntu/modules/tmux-boot && python3 -m unittest tests.test_shutdown
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_TMUX = shutil.which("tmux")

TRUE_EVENT = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForShutdown (true,)"
FALSE_EVENT = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForShutdown (false,)"

# the forced-save stand-in: records what it saw. alive=1 only if the tmux server answered at that moment.
# FAKE_SAVE: ok (default) | fail | hang
SAVE_NOW = """#!/bin/sh
echo "save-start $*" >> "$STUB/events.log"
if tmux list-sessions >/dev/null 2>&1; then echo "alive=1" >> "$STUB/events.log"; else echo "alive=0" >> "$STUB/events.log"; fi
case "${FAKE_SAVE:-ok}" in
  fail) echo "save-end rc=1" >> "$STUB/events.log"; exit 1 ;;
  hang) sleep 60 ;;
esac
echo "save-end rc=0" >> "$STUB/events.log"
"""

# systemd-inhibit stand-in: records the lock, then runs the command after its options like the real one does (exec
# keeps the "lock" in the sleeper process); on TERM it records the release first
INHIBIT = """#!/bin/sh
echo "lock-take $*" >> "$STUB/events.log"
trap 'echo lock-release >> "$STUB/events.log"; exit 0' TERM
while [ "$1" != sleep ]; do shift; done
"$@" &
wait $!
"""

# the real tmux, pinned to the throwaway socket
TMUX_SHIM = """#!/bin/sh
exec "%s" -L "$TMUX_TEST_SOCKET" "$@"
"""

# a save step that records the call, so watcher tests need no tmux
SAVE_STEP = """#!/bin/sh
echo "step $*" >> "$STUB/events.log"
case "${FAKE_STEP:-ok}" in
  slow) sleep 1 ;;
esac
echo "step-end" >> "$STUB/events.log"
"""

JOURNALCTL = """#!/bin/sh
echo "journalctl $*" >> "$STUB/events.log"
[ -e "$STUB/no-prev-boot" ] && { echo "Specified boot ID not found" >&2; exit 1; }
printf '{"MESSAGE":"x","__REALTIME_TIMESTAMP":"%s000000","_PID":"1"}\\n' "$(cat "$STUB/prev-end")"
"""

ALERT = """#!/bin/sh
echo "alert $*" >> "$STUB/events.log"
"""


def write_exe(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


class Env:
    """A throwaway HOME, a PATH of stubs and, on demand, a real tmux server on a throwaway socket."""

    def __init__(self, test):
        self.test = test
        self.dir = tempfile.mkdtemp(prefix="tmux-shutdown-test-")
        test.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.home = os.path.join(self.dir, "home")
        self.stub = os.path.join(self.dir, "stub")
        self.bin = os.path.join(self.dir, "bin")
        self.state = os.path.join(self.dir, "state")
        for d in (self.home, self.stub, self.bin, self.state):
            os.makedirs(d)
        self.socket = "shutdown-test-%d-%d" % (os.getpid(), id(self))
        self.events = os.path.join(self.stub, "events.log")
        write_exe(os.path.join(self.stub, "tmux-save-now"), SAVE_NOW)
        write_exe(os.path.join(self.stub, "tmux-shutdown-save-step"), SAVE_STEP)
        write_exe(os.path.join(self.stub, "inhibit"), INHIBIT)
        write_exe(os.path.join(self.bin, "tmux"), TMUX_SHIM % REAL_TMUX)
        self.procs = []
        test.addCleanup(self.cleanup)
        self.vars = {
            "HOME": self.home,
            "PATH": self.bin + ":/usr/bin:/bin",
            "STUB": self.stub,
            "TMUX_TEST_SOCKET": self.socket,
            "TMUX_SAVE_NOW": os.path.join(self.stub, "tmux-save-now"),
            "TMUX_BOOT_STATE_DIR": self.state,
            "TMUX_SHUTDOWN_BOOT_ID": "boot-A",
            "TMUX_SHUTDOWN_SAVE": os.path.join(self.stub, "tmux-shutdown-save-step"),
            "TMUX_SHUTDOWN_INHIBIT": os.path.join(self.stub, "inhibit"),
            "TMUX_SHUTDOWN_DELAY_MAX": "30",
        }

    def env(self, **extra):
        e = dict(self.vars)
        e.update(extra)
        return e

    def tmux(self, *args):
        return subprocess.run([REAL_TMUX, "-L", self.socket, *args], capture_output=True, text=True)

    def start_server(self):
        r = self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "main", "sleep 600")
        self.test.assertEqual(r.returncode, 0, r.stderr)

    def kill_server(self):
        self.tmux("kill-server")

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
            for pipe in (p.stdout, p.stderr):
                if pipe:
                    pipe.close()
        self.kill_server()

    def script(self, name, *args, timeout=60, **extra):
        return subprocess.run([os.path.join(MODULE, name), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=timeout)

    def spawn(self, name, *args, **extra):
        p = subprocess.Popen([os.path.join(MODULE, name), *args], env=self.env(**extra),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def log(self):
        try:
            with open(self.events) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def wait_for(self, predicate, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def wait_log(self, line, seconds=10):
        return self.wait_for(lambda: any(l.startswith(line) for l in self.log()), seconds)


@unittest.skipUnless(REAL_TMUX, "tmux is not installed")
class ShutdownSaveStep(unittest.TestCase):
    """tmux-shutdown-save: the bounded step both paths call."""

    def test_saves_while_the_server_is_alive_and_stamps(self):
        e = Env(self)
        e.start_server()
        r = e.script("tmux-shutdown-save", "--reason", "shutdown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved (shutdown)", r.stdout)
        self.assertEqual(e.log(), ["save-start --reason shutdown", "alive=1", "save-end rc=0"])
        with open(os.path.join(e.state, "shutdown-save")) as f:
            stamp = f.read().split()
        self.assertEqual((stamp[0], stamp[2]), ("boot-A", "shutdown"))

    def test_save_after_the_server_is_gone_is_never_run(self):
        # the regression this module exists to prevent: a save after teardown would see no panes. The step must refuse
        # (and the stub records alive=0 if it were ever called), so a late ExecStop cannot clobber the good snapshot.
        e = Env(self)
        e.start_server()
        e.kill_server()
        r = e.script("tmux-shutdown-save", "--reason", "user-manager-stop")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(e.log(), [], "tmux-save-now ran although the tmux server was gone")
        self.assertIn("no tmux server reachable", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(e.state, "shutdown-save")))

    def test_second_path_skips_after_a_save_in_the_same_boot(self):
        e = Env(self)
        e.start_server()
        self.assertEqual(e.script("tmux-shutdown-save", "--reason", "shutdown").returncode, 0)
        r = e.script("tmux-shutdown-save", "--reason", "user-manager-stop")
        self.assertEqual(r.returncode, 0)
        self.assertIn("skipped (user-manager-stop)", r.stdout)
        self.assertEqual([l for l in e.log() if l.startswith("save-start")], ["save-start --reason shutdown"])

    def test_stamp_of_another_boot_or_an_old_stamp_does_not_skip(self):
        e = Env(self)
        e.start_server()
        with open(os.path.join(e.state, "shutdown-save"), "w") as f:
            f.write("boot-OLD %d shutdown\n" % int(time.time()))
        self.assertIn("saved", e.script("tmux-shutdown-save").stdout)
        with open(os.path.join(e.state, "shutdown-save"), "w") as f:
            f.write("boot-A %d shutdown\n" % (int(time.time()) - 4000))
        self.assertIn("saved", e.script("tmux-shutdown-save").stdout)
        self.assertEqual(len([l for l in e.log() if l.startswith("save-start")]), 2)

    def test_failed_save_never_blocks_and_leaves_no_stamp(self):
        e = Env(self)
        e.start_server()
        r = e.script("tmux-shutdown-save", FAKE_SAVE="fail")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ERROR", r.stderr)
        self.assertIn("failed with exit 1", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(e.state, "shutdown-save")))

    def test_hung_save_is_stopped_within_the_timeout(self):
        e = Env(self)
        e.start_server()
        t0 = time.time()
        r = e.script("tmux-shutdown-save", "--timeout", "1", FAKE_SAVE="hang")
        took = time.time() - t0
        self.assertEqual(r.returncode, 0)
        self.assertLess(took, 5, "the hung save was not bounded by --timeout")
        self.assertIn("did not finish within 1s", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(e.state, "shutdown-save")))

    def test_missing_forced_save_entry_is_loud_but_not_fatal(self):
        e = Env(self)
        e.start_server()
        r = e.script("tmux-shutdown-save", TMUX_SAVE_NOW=os.path.join(e.dir, "nope"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("is not installed, the sessions were NOT saved", r.stderr)

    def test_bad_arguments_are_a_usage_error(self):
        e = Env(self)
        self.assertEqual(e.script("tmux-shutdown-save", "--timeout", "0").returncode, 2)
        self.assertEqual(e.script("tmux-shutdown-save", "--bogus").returncode, 2)
        self.assertEqual(e.script("tmux-shutdown-save", "--reason").returncode, 2)


@unittest.skipUnless(REAL_TMUX, "tmux is not installed")
class ShutdownWatcher(unittest.TestCase):
    """tmux-shutdown-watch with an injected logind event source and a stub inhibitor."""

    def source(self, *lines, then="exec sleep 60"):
        """An event source command: print the lines, then stay alive like a real bus monitor."""
        return "; ".join([*("echo '%s'" % l for l in lines), then])

    def stop(self, p):
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        return p.returncode

    def test_prepare_for_shutdown_runs_the_save_then_releases_the_lock(self):
        e = Env(self)
        w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD=self.source(TRUE_EVENT))
        self.assertTrue(e.wait_log("lock-release"), e.log())
        lines = e.log()
        self.assertEqual(lines[0].split()[0], "lock-take")
        self.assertIn("--what=shutdown", lines[0])
        self.assertIn("--mode=delay", lines[0])
        # the save finished BEFORE the lock was released: logind holds the shutdown back exactly that long
        self.assertEqual(lines[1:], ["step --reason shutdown --timeout 25", "step-end", "lock-release"])
        self.assertIsNone(w.poll(), "the watcher must keep running after a save")
        self.assertEqual(self.stop(w), 0)

    def test_the_save_runs_with_the_server_alive_and_before_the_lock_is_released(self):
        # end to end with the real step and the tmux-save-now stub: the server answers during the save
        e = Env(self)
        e.start_server()
        real_step = os.path.join(MODULE, "tmux-shutdown-save")
        w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD=self.source(TRUE_EVENT), TMUX_SHUTDOWN_SAVE=real_step)
        self.assertTrue(e.wait_log("lock-release"), e.log())
        lines = e.log()
        self.assertEqual(lines[1:], ["save-start --reason shutdown", "alive=1", "save-end rc=0", "lock-release"])
        self.stop(w)

    def test_no_event_does_nothing(self):
        # a detach, a closed terminal, a logout: no PrepareForShutdown, so no save and the lock stays held
        e = Env(self)
        w = e.spawn("tmux-shutdown-watch",
                    TMUX_SHUTDOWN_EVENT_CMD=self.source("[detached (from session main)]", "client-detached", FALSE_EVENT))
        time.sleep(1.5)
        self.assertIsNone(w.poll())
        lines = e.log()
        self.assertTrue(lines[0].startswith("lock-take"))
        self.assertFalse([l for l in lines if l.startswith(("step", "save", "lock-release"))], lines)
        self.assertEqual(self.stop(w), 0)

    def test_failing_save_does_not_block_the_shutdown(self):
        e = Env(self)
        e.start_server()
        w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD=self.source(TRUE_EVENT),
                    TMUX_SHUTDOWN_SAVE=os.path.join(MODULE, "tmux-shutdown-save"), FAKE_SAVE="fail")
        self.assertTrue(e.wait_log("lock-release"), e.log())
        self.assertIsNone(w.poll())
        self.stop(w)

    def test_hung_save_is_bounded_and_the_lock_is_still_released(self):
        # logind waits at most InhibitDelayMaxSec; a save that hangs must be stopped inside it, the lock released
        e = Env(self)
        e.start_server()
        t0 = time.time()
        w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD=self.source(TRUE_EVENT),
                    TMUX_SHUTDOWN_SAVE=os.path.join(MODULE, "tmux-shutdown-save"), FAKE_SAVE="hang",
                    TMUX_SHUTDOWN_DELAY_MAX="3")
        self.assertTrue(e.wait_log("lock-release", seconds=12), e.log())
        self.assertLess(time.time() - t0, 3 + 2, "the lock outlived the shutdown delay")
        self.assertIn("save-start --reason shutdown", e.log())
        self.stop(w)

    def test_save_budget_follows_the_logind_delay(self):
        for delay, budget in (("30", "25"), ("5", "4"), ("10", "9"), ("60", "50"), ("1", "1")):
            e = Env(self)
            w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD=self.source(TRUE_EVENT), TMUX_SHUTDOWN_DELAY_MAX=delay)
            self.assertTrue(e.wait_log("lock-release"), e.log())
            self.assertIn("step --reason shutdown --timeout %s" % budget, e.log(), "delay %s" % delay)
            self.stop(w)

    def test_explicit_save_timeout_wins(self):
        e = Env(self)
        w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD=self.source(TRUE_EVENT), TMUX_SHUTDOWN_SAVE_TIMEOUT="7")
        self.assertTrue(e.wait_log("lock-release"), e.log())
        self.assertIn("step --reason shutdown --timeout 7", e.log())
        self.stop(w)

    def test_cancelled_shutdown_rearms_the_lock_and_drops_the_stamp(self):
        e = Env(self)
        stamp = os.path.join(e.state, "shutdown-save")
        with open(stamp, "w") as f:
            f.write("boot-A %d shutdown\n" % int(time.time()))
        w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD=self.source(TRUE_EVENT, FALSE_EVENT))
        self.assertTrue(e.wait_for(lambda: len([l for l in e.log() if l.startswith("lock-take")]) == 2), e.log())
        self.assertFalse(os.path.exists(stamp), "a cancelled shutdown must not leave a stamp that suppresses the next save")
        self.assertIsNone(w.poll())
        self.stop(w)

    def test_sigterm_releases_the_lock_and_exits_zero(self):
        e = Env(self)
        w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD=self.source())
        self.assertTrue(e.wait_log("lock-take"))
        time.sleep(0.5)
        self.assertEqual(self.stop(w), 0)
        self.assertIn("lock-release", e.log())

    def test_event_source_ending_is_a_failure_so_systemd_restarts_the_unit(self):
        e = Env(self)
        w = e.spawn("tmux-shutdown-watch", TMUX_SHUTDOWN_EVENT_CMD="exit 0")
        w.wait(timeout=10)
        self.assertEqual(w.returncode, 1)
        self.assertIn("lock-release", e.log())

    def test_bad_timeout_env_is_a_usage_error(self):
        e = Env(self)
        r = e.script("tmux-shutdown-watch", TMUX_SHUTDOWN_SAVE_TIMEOUT="soon")
        self.assertEqual(r.returncode, 2)


class BootSelfcheck(unittest.TestCase):
    """tmux-boot-selfcheck against a stub journal, a stub tmux-alert and a throwaway snapshot directory."""

    def setUp(self):
        self.e = Env(self)
        self.now = int(time.time())
        self.boot_start = self.now - 600
        self.prev_end = self.boot_start - 60
        self.rdir = os.path.join(self.e.home, "resurrect")
        os.makedirs(self.rdir)
        self.omp_map = os.path.join(self.e.home, "omp-sessions.map")
        write_exe(os.path.join(self.e.stub, "journalctl"), JOURNALCTL)
        write_exe(os.path.join(self.e.bin, "tmux-alert"), ALERT)
        with open(os.path.join(self.e.stub, "prev-end"), "w") as f:
            f.write(str(self.prev_end))

    def snapshot(self, name, mtime):
        p = os.path.join(self.rdir, "tmux_resurrect_%s.txt" % name)
        open(p, "w").close()
        os.utime(p, (mtime, mtime))

    def touch_map(self, mtime):
        open(self.omp_map, "w").close()
        os.utime(self.omp_map, (mtime, mtime))

    def run_check(self, **extra):
        base = {
            "TMUX_BOOT_SELFCHECK_JOURNALCTL": os.path.join(self.e.stub, "journalctl"),
            "TMUX_BOOT_SELFCHECK_RESURRECT_DIR": self.rdir,
            "TMUX_BOOT_SELFCHECK_OMP_MAP": self.omp_map,
            "TMUX_BOOT_SELFCHECK_BOOT_START": str(self.boot_start),
            "TMUX_AUTOSAVE_TMUX": "false",
        }
        base.update(extra)
        return self.e.script("tmux-boot-selfcheck", **base)

    def alerts(self):
        return [l for l in self.e.log() if l.startswith("alert ")]

    def test_fresh_save_before_poweroff_passes(self):
        self.snapshot("20260101T000000", self.prev_end - 95)
        self.touch_map(self.prev_end - 95)
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tmux-boot-selfcheck: shutdown save landed (95s before poweroff)", r.stdout)
        self.assertEqual(self.alerts(), [])
        # it asked for the LAST entry of the PREVIOUS boot, as json
        self.assertIn("journalctl -b -1 -n 1 -o json", self.e.log())

    def test_stale_snapshot_fails_loudly_and_alerts_once_per_run(self):
        self.snapshot("20260101T000000", self.prev_end - 3 * 3600)
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("tmux-boot-selfcheck: ERROR:", r.stderr)
        self.assertIn("did NOT land", r.stderr)
        calls = self.alerts()
        self.assertEqual(len(calls), 1)
        # the rate limit lives in tmux-alert and is keyed: the key must be passed
        self.assertIn("--key tmux-boot-selfcheck", calls[0])
        self.assertIn("older than the end of the previous boot", calls[0])

    def test_snapshots_of_this_boot_do_not_count(self):
        # autosave of the restored server wrote a fresh file after boot; the shutdown snapshot is the old one
        self.snapshot("old", self.prev_end - 5 * 3600)
        self.snapshot("new", self.now - 60)
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.alerts()), 1)

    def test_threshold_is_configurable(self):
        self.snapshot("20260101T000000", self.prev_end - 30 * 60)
        self.assertEqual(self.run_check().returncode, 1)
        r = self.run_check(TMUX_BOOT_SELFCHECK_MAX_MIN="45")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("shutdown save landed (1800s before poweroff)", r.stdout)

    def test_stale_omp_map_fails_even_with_a_fresh_snapshot(self):
        self.snapshot("20260101T000000", self.prev_end - 10)
        self.touch_map(self.prev_end - 4 * 3600)
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("omp session map", r.stderr)
        self.assertEqual(len(self.alerts()), 1)

    def test_omp_map_rewritten_in_this_boot_is_not_judged(self):
        self.snapshot("20260101T000000", self.prev_end - 10)
        self.touch_map(self.now - 100)
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("omp map was rewritten in this boot", r.stdout)

    def test_no_previous_boot_is_not_an_error(self):
        open(os.path.join(self.e.stub, "no-prev-boot"), "w").close()
        self.snapshot("20260101T000000", self.prev_end - 5 * 3600)
        r = self.run_check()
        self.assertEqual(r.returncode, 0)
        self.assertIn("no journal of a previous boot", r.stdout)
        self.assertEqual(self.alerts(), [])

    def test_no_snapshot_is_not_an_error(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0)
        self.assertIn("no snapshot from before this boot", r.stdout)
        self.assertEqual(self.alerts(), [])

    def test_missing_alert_command_falls_back_to_the_journal(self):
        os.remove(os.path.join(self.e.bin, "tmux-alert"))
        self.snapshot("20260101T000000", self.prev_end - 5 * 3600)
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("tmux-alert is not installed", r.stderr)

    def test_bad_threshold_is_a_usage_error(self):
        self.assertEqual(self.run_check(TMUX_BOOT_SELFCHECK_MAX_MIN="soon").returncode, 2)


class Units(unittest.TestCase):
    """The unit files carry the ordering the design relies on."""

    def unit(self, name):
        sections, cur = {}, None
        with open(os.path.join(MODULE, name)) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("["):
                    cur = sections.setdefault(line.strip("[]"), {})
                elif "=" in line:
                    k, v = line.split("=", 1)
                    cur.setdefault(k, []).append(v)
        return sections

    def test_shutdown_save_stops_before_the_server_and_omp_slice(self):
        u = self.unit("tmux-shutdown-save.service")
        # units stop in reverse start order: After= means this ExecStop runs while those are still up
        self.assertEqual(set(u["Unit"]["After"][0].split()), {"tmux-boot.service", "omp.slice"})
        self.assertEqual(u["Service"]["Type"], ["oneshot"])
        self.assertEqual(u["Service"]["RemainAfterExit"], ["yes"])
        self.assertIn("tmux-shutdown-save --reason user-manager-stop", u["Service"]["ExecStop"][0])
        self.assertGreater(int(u["Service"]["TimeoutStopSec"][0]), 60)
        self.assertEqual(u["Install"]["WantedBy"], ["default.target"])

    def test_watcher_is_a_restarting_default_target_service(self):
        u = self.unit("tmux-shutdown-watch.service")
        self.assertEqual(u["Service"]["Restart"], ["always"])
        self.assertEqual(u["Install"]["WantedBy"], ["default.target"])

    def test_selfcheck_timer_fires_once_ten_minutes_after_boot(self):
        t = self.unit("tmux-boot-selfcheck.timer")
        self.assertEqual(t["Timer"]["OnBootSec"], ["10min"])
        self.assertNotIn("OnUnitActiveSec", t["Timer"])
        self.assertEqual(t["Install"]["WantedBy"], ["timers.target"])

    def test_scope_dropins_order_the_panes_after_the_save(self):
        for d in ("tmux-spawn-.scope.d", "omp-.scope.d"):
            with open(os.path.join(MODULE, "systemd-dropins", d, "10-tmux-shutdown-save.conf")) as f:
                text = f.read()
            self.assertIn("Before=tmux-shutdown-save.service", text, d)


if __name__ == "__main__":
    unittest.main()
