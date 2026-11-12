"""The event-driven trigger: the layout hooks tl_assert_hooks installs (against a real tmux on a THROWAWAY socket) and the
debounced save they run (against a stub tmux-save-now)."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests.stubs import MODULE, write_exec

HOOKS = "session-created session-closed window-linked window-unlinked after-new-window after-split-window after-kill-pane".split()
WINDOW_HOOKS = ["pane-exited"]  # window-renamed is not hooked: automatic-rename fires it after almost every command


class Hooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.socket = f"tmux-hooks-test-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        real = shutil.which("tmux")
        if not real:
            self.skipTest("tmux is not installed")
        self.shim = os.path.join(self.tmp, "shim")
        os.makedirs(self.shim)
        write_exec(os.path.join(self.shim, "tmux"), f'#!/bin/sh\nexec {real} -L {self.socket} -f /dev/null "$@"\n')
        self.addCleanup(self.stop_server)
        self.tmux("new-session", "-d", "-s", "t", "sleep 600")

    def stop_server(self):
        # the throwaway server only: killed through its own socket name, never by process search
        subprocess.run([shutil.which("tmux"), "-L", self.socket, "kill-server"], capture_output=True, check=False)

    def env(self):
        return {**os.environ, "PATH": f"{self.shim}:/usr/bin:/bin", "HOME": self.tmp}

    def tmux(self, *args):
        return subprocess.run(["tmux", *args], env=self.env(), capture_output=True, text=True, check=False)

    def assert_hooks(self):
        script = f'. "{MODULE}/tmux-lib"; tl_assert_hooks'
        return subprocess.run(["bash", "-c", script], env=self.env(), capture_output=True, text=True, check=False)

    def hook_lines(self, hook, scope="-g"):
        return [line for line in self.tmux("show-hooks", scope, hook).stdout.splitlines() if "[" in line]

    def test_every_event_hook_runs_the_debouncer_once(self):
        res = self.assert_hooks()
        self.assertEqual(res.returncode, 0, res.stderr)
        for hook in HOOKS:
            lines = self.hook_lines(hook)
            self.assertEqual(len(lines), 1, (hook, lines))
            self.assertIn("tmux-save-debounce", lines[0])
            self.assertIn("run-shell -b", lines[0])
        for hook in WINDOW_HOOKS:
            lines = self.hook_lines(hook, "-gw")
            self.assertEqual(len(lines), 1, (hook, lines))
            self.assertIn("tmux-save-debounce", lines[0])

    def test_asserting_again_changes_nothing_and_never_duplicates(self):
        self.assert_hooks()
        before = self.tmux("show-hooks", "-g").stdout
        res = self.assert_hooks()
        self.assertEqual(res.stdout, "", "a second run must report no change")
        self.assertEqual(self.tmux("show-hooks", "-g").stdout, before)

    def test_a_wiped_or_stale_hook_is_put_back(self):
        self.assert_hooks()
        self.tmux("set-hook", "-gu", "window-linked")  # the whole hook array gone
        self.tmux("set-hook", "-g", "session-created[940]", "run-shell -b /old/path/tmux-save-debounce")  # stale path
        res = self.assert_hooks()
        self.assertIn("window-linked", res.stdout)
        self.assertIn(MODULE, self.hook_lines("window-linked")[0])
        self.assertIn(MODULE, self.hook_lines("session-created")[0])
        self.assertEqual(len(self.hook_lines("session-created")), 1)

    def test_hooks_that_are_not_ours_are_never_touched(self):
        self.tmux("set-hook", "-g", "window-linked[0]", "display-message mine")
        self.tmux("set-hook", "-g", "window-linked[5]", "display-message also-mine")
        self.tmux("set-hook", "-g", "session-created[940]", "display-message squatter")  # in OUR range, not ours
        self.assert_hooks()
        linked = "\n".join(self.hook_lines("window-linked"))
        self.assertIn("window-linked[0] display-message mine", linked)
        self.assertIn("window-linked[5] display-message also-mine", linked)
        created = "\n".join(self.hook_lines("session-created"))
        self.assertIn("session-created[940] display-message squatter", created)
        self.assertEqual(created.count("tmux-save-debounce"), 1)
        # and ours again is idempotent next to the squatter
        self.assertEqual(self.assert_hooks().stdout, "")

    def test_a_duplicate_of_our_entry_is_removed(self):
        self.assert_hooks()
        self.tmux("set-hook", "-g", "window-unlinked[945]", f"run-shell -b {MODULE}/tmux-save-debounce")
        res = self.assert_hooks()
        self.assertIn("duplicate", res.stdout)
        self.assertEqual(len(self.hook_lines("window-unlinked")), 1)

    def test_a_real_event_fires_the_hook(self):
        marker = os.path.join(self.tmp, "fired")
        # point the hook at a recorder instead of the debouncer: the hook machinery itself is what this proves
        self.tmux("set-hook", "-g", "after-new-window[940]", f"run-shell -b 'touch {marker}; # tmux-save-debounce'")
        self.tmux("new-window", "-t", "t:", "sleep 600")
        deadline = time.time() + 5
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(marker))


class Debounce(unittest.TestCase):
    """tmux-save-debounce with a stub tmux-save-now that records when it ran."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.bin)
        for name in ("tmux-lib", "tmux-save-debounce"):
            shutil.copy2(os.path.join(MODULE, name), os.path.join(self.bin, name))
        self.log = os.path.join(self.tmp, "saves.log")
        write_exec(
            os.path.join(self.bin, "tmux-save-now"),
            f'#!/bin/sh\necho "save $* $(date +%s.%N)" >> "{self.log}"\nsleep "${{SAVE_SLEEP:-0}}"\n',
        )

    def env(self, **extra):
        return {**os.environ, "HOME": self.tmp, "TMUX_BOOT_STATE_DIR": self.state, "TMUX_SAVE_DEBOUNCE": "1", "PATH": "/usr/bin:/bin", **extra}

    def event(self, **extra):
        return subprocess.Popen(["bash", os.path.join(self.bin, "tmux-save-debounce")], env=self.env(**extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def saves(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return fh.read().splitlines()

    def lock_free(self):
        return subprocess.run(["flock", "-n", os.path.join(self.state, "debounce.lock"), "true"], check=False).returncode == 0

    def dirty_mtime(self):
        return os.stat(os.path.join(self.state, "debounce.dirty")).st_mtime_ns

    def test_a_burst_of_events_costs_one_save_after_the_window(self):
        started = time.time()
        procs = [self.event() for _ in range(6)]
        for p in procs:
            p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0)
        self.assertEqual(len(self.saves()), 1, self.saves())
        self.assertIn("--reason event", self.saves()[0])
        self.assertGreaterEqual(time.time() - started, 1.0)  # the trailing edge: after the window, not at the first event
        self.assertTrue(self.lock_free())

    def test_an_event_during_the_save_earns_one_more_save_after_another_window(self):
        first = self.event(SAVE_SLEEP="1.5")
        time.sleep(1.6)  # the worker is inside its save now
        second = self.event(SAVE_SLEEP="1.5")
        for p in (first, second):
            p.communicate(timeout=20)
        self.assertEqual(len(self.saves()), 2, self.saves())
        t = [float(line.rsplit(" ", 1)[1]) for line in self.saves()]
        self.assertGreaterEqual(t[1] - t[0], 1.0)  # saves are at least one window apart

    def test_an_event_in_the_gap_before_the_unlock_is_not_lost(self):
        # the injected event runs while the worker still holds the lock: it stamps `dirty` and finds the lock busy
        inject = f'touch "{self.state}/debounce.dirty"; bash "{self.bin}/tmux-save-debounce"'
        # only the FIRST pass injects, or every pass would find a new event
        inject_once = f'[ -e "{self.tmp}/injected" ] || {{ : > "{self.tmp}/injected"; {inject}; }}'
        env = {"TMUX_SAVE_DEBOUNCE_BEFORE_UNLOCK": inject_once}
        p = self.event(**env)
        p.communicate(timeout=30)
        self.assertEqual(len(self.saves()), 2, self.saves())
        # handoff complete: nothing left running, the lock is free, the last save covers the last event
        self.assertTrue(self.lock_free())
        self.assertEqual(subprocess.run(["pgrep", "-f", f"{self.bin}/tmux-save-debounce"], capture_output=True, check=False).returncode, 1)
        self.assertLessEqual(self.dirty_mtime(), self.last_save_started_ns())

    def test_an_event_after_the_last_release_becomes_the_worker(self):
        inject = f'[ -e "{self.tmp}/injected" ] || {{ : > "{self.tmp}/injected"; bash "{self.bin}/tmux-save-debounce"; }}'
        p = self.event(TMUX_SAVE_DEBOUNCE_BEFORE_EXIT=inject)
        p.communicate(timeout=30)
        self.assertEqual(len(self.saves()), 2, self.saves())
        self.assertTrue(self.lock_free())

    def last_save_started_ns(self):
        return int(float(self.saves()[-1].rsplit(" ", 1)[1]) * 1e9)

    def test_the_debouncer_is_silent_so_run_shell_never_opens_view_mode_over_a_pane(self):
        # tmux shows a finished `run-shell -b` job's output over the current pane: the debouncer must print nothing
        # (the stub tmux-save-now shouts to stdout and stderr on purpose)
        proc = self.event()
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out + err, "")
        self.assertEqual(len(self.saves()), 1)

    def test_a_bad_window_is_a_usage_error(self):
        p = self.event(TMUX_SAVE_DEBOUNCE="soon")
        p.communicate(timeout=10)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
