"""tmux-boot's restore choices: the newest NON-degenerate snapshot is what a restore uses, and a server that keeps dying is not
restored again and again. Stub tmux (see stubs.py); nothing touches a real server."""

import os
import time
import unittest

from tests.stubs import MODULE, StubBase, snapshot, write_exec  # noqa: F401

RICH = snapshot(windows=6, panes_per_window=2, harness=3)
BARE = snapshot(windows=1, panes_per_window=1)


class BootGuard(StubBase):
    def setUp(self):
        super().setUp()
        self.drop("server")  # a boot starts with no server
        self.restore = os.path.join(self.tmp, "restore.sh")
        write_exec(self.restore, '#!/bin/sh\necho "restore.sh ran" >> "$STUB/calls.log"\n')
        self.option("@resurrect-restore-script-path", self.restore)
        self.boot_id = os.path.join(self.tmp, "boot_id")
        with open(self.boot_id, "w") as fh:
            fh.write("boot-A\n")
        self.put("windows", "w0\n")  # the fresh session's one window: continuum never restores in the stub

    def write_snapshot(self, stamp, body, age=0, last=False, harness_link=False):
        path = os.path.join(self.resurrect, f"tmux_resurrect_{stamp}.txt")
        with open(path, "w") as fh:
            fh.write(body)
        if age:
            self.age(path, age)
        for name, flag in (("last", last), ("last-with-harness", harness_link)):
            link = os.path.join(self.resurrect, name)
            if flag:
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink(os.path.basename(path), link)
        return path

    def boot(self, **extra):
        return self.run_script(
            "tmux-boot", TMUX_BOOT_WAIT="1", TMUX_BOOT_RESURRECT_DIR=self.resurrect, TMUX_BOOT_ID_FILE=self.boot_id, TMUX_BOOT_HOLD="1", **extra
        )

    def restores(self):
        return self.calls().count("restore.sh ran")

    # ── the newest non-degenerate snapshot ─────────────────────────────────────────────────────────────────────────────
    def test_a_degenerate_last_is_replaced_by_the_newest_good_snapshot_before_the_server_starts(self):
        self.write_snapshot("20261108T100000", RICH, age=7200)
        self.write_snapshot("20261108T110000", BARE, age=3600, last=True)  # something wrote it behind the guard's back
        res = self.boot()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")
        self.assertIn("restoring tmux_resurrect_20261108T100000.txt", res.stdout)
        self.assertEqual(self.restores(), 1)
        self.assertEqual(len(self.alerts()), 1)
        self.assertIn("fell back", self.alerts()[0])

    def test_an_acceptance_the_guard_recorded_is_trusted_at_boot(self):
        # tmux-save-now records every acceptance (--force and the 6-hour rule) in last-guarded: the boot replay
        # must not judge the accepted smaller layout against the older rich one and undo the user's decision
        self.write_snapshot("20261108T100000", RICH, age=7200)
        accepted = self.write_snapshot("20261108T110000", BARE, age=3600, last=True)
        os.symlink(os.path.basename(accepted), os.path.join(self.resurrect, "last-guarded"))
        res = self.boot()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T110000.txt")
        self.assertEqual(self.alerts(), [])

    def on_new_session(self, shell):
        """Make the stub tmux run `shell` when the server starts (what continuum would see at that moment)."""
        path = os.path.join(self.fakebin, "tmux")
        with open(path) as fh:
            text = fh.read()
        write_exec(path, text.replace("new-session) : > \"$STUB/server\"; exit 0 ;;", f'new-session) {shell}; : > "$STUB/server"; exit 0 ;;'))

    def test_the_repoint_is_done_before_new_session(self):
        self.write_snapshot("20261108T100000", RICH, age=7200)
        self.write_snapshot("20261108T110000", BARE, age=3600, last=True)
        self.on_new_session('readlink "$FAKE_RESURRECT_DIR/last" > "$STUB/last-at-start"')
        self.boot()
        self.assertEqual(self.read(self.stub, "last-at-start").strip(), "tmux_resurrect_20261108T100000.txt")

    def test_a_good_last_is_left_alone_and_nobody_is_alerted(self):
        self.write_snapshot("20261108T100000", RICH, age=7200)
        self.write_snapshot("20261108T110000", snapshot(windows=5, panes_per_window=2, harness=3), age=3600, last=True)
        res = self.boot()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T110000.txt")
        self.assertEqual(self.alerts(), [])
        self.assertNotIn("not a snapshot to restore", res.stdout)

    def test_a_snapshot_chain_with_a_lost_last_pointer_gets_its_pointer_back(self):
        self.write_snapshot("20261108T100000", RICH, age=7200)
        self.write_snapshot("20261108T110000", snapshot(windows=5, panes_per_window=2, harness=3), age=3600)
        self.boot()
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T110000.txt")

    def test_a_state_that_persisted_long_enough_is_the_new_reality(self):
        self.write_snapshot("20261108T000000", RICH, age=10 * 3600)
        self.write_snapshot("20261108T010000", BARE, age=9 * 3600)
        self.write_snapshot("20261108T080000", BARE.replace("main", "main"), age=2 * 3600, last=True)  # 7 h of bare main
        self.boot(TMUX_SAVE_NOW_ACCEPT_AFTER="21600")
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T080000.txt")

    def test_the_chosen_snapshots_own_pane_archive_and_maps_come_back_with_it(self):
        good = self.write_snapshot("20261108T100000", RICH, age=7200)
        self.write_snapshot("20261108T110000", BARE, age=3600, last=True)
        with open(os.path.join(self.resurrect, "pane_contents_20261108T100000.tar.gz"), "w") as fh:
            fh.write("archive of the good one\n")
        os.makedirs(os.path.join(self.resurrect, "maps", "20261108T100000"))
        with open(os.path.join(self.resurrect, "maps", "20261108T100000", "omp-sessions.map"), "w") as fh:
            fh.write("the good map\n")
        with open(os.path.join(self.resurrect, "pane_contents.tar.gz"), "w") as fh:
            fh.write("archive of the bare one\n")
        os.makedirs(self.maps)
        self.boot(TMUX_SAVE_NOW_MAP_DIR=self.maps, TMUX_BOOT_MAP_DIR=self.maps)
        self.assertTrue(good)
        self.assertEqual(self.read(self.resurrect, "pane_contents.tar.gz"), "archive of the good one\n")
        self.assertEqual(self.read(self.maps, "omp-sessions.map"), "the good map\n")

    def test_when_nothing_qualifies_last_with_harness_is_used(self):
        self.write_snapshot("20261108T100000", RICH, age=7200, harness_link=True)
        self.write_snapshot("20261108T110000", "", age=3600, last=True)  # an empty file: no panes at all
        self.boot()
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")

    # ── the restore-loop guard ─────────────────────────────────────────────────────────────────────────────────────────
    def attempts(self):
        path = os.path.join(self.state, "restore-attempts")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [line for line in fh.read().splitlines() if line]

    def test_every_restore_is_recorded_and_the_third_in_an_hour_is_the_last(self):
        self.write_snapshot("20261108T100000", RICH, last=True)
        for n in range(3):
            self.boot()
            self.assertEqual(len(self.attempts()), n + 1)
            self.assertEqual(self.restores(), n + 1)
            self.drop("server")
            self.put("windows", "w0\n")
        res = self.boot()  # the fourth boot within the hour
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.restores(), 3, "no restore on the fourth attempt")
        self.assertIn("restore loop", res.stdout + res.stderr)
        self.assertTrue(any("restore loop" in a for a in self.alerts()), self.alerts())
        # the snapshot stays where it was for the next boot: held while the server came up, then put back
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")
        self.assertFalse(os.path.lexists(os.path.join(self.resurrect, "last.held-by-tmux-boot")))
        self.assertTrue(os.path.exists(os.path.join(self.stub, "server")), "the server itself is still started")

    def test_continuum_cannot_see_last_while_the_loop_guard_holds_it(self):
        self.write_snapshot("20261108T100000", RICH, last=True)
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "restore-attempts"), "w") as fh:
            fh.write("".join(f"{int(time.time()) - 60 * n}\n" for n in (1, 2, 3)))
        self.on_new_session('[ -e "$FAKE_RESURRECT_DIR/last" ] && echo visible > "$STUB/last-at-start" || echo hidden > "$STUB/last-at-start"')
        self.boot()
        self.assertEqual(self.read(self.stub, "last-at-start").strip(), "hidden")

    def test_attempts_older_than_an_hour_do_not_count(self):
        self.write_snapshot("20261108T100000", RICH, last=True)
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "restore-attempts"), "w") as fh:
            fh.write("".join(f"{int(time.time()) - 3700 - n}\n" for n in range(5)))
        self.boot()
        self.assertEqual(self.restores(), 1)
        self.assertEqual(len(self.attempts()), 1)

    def test_the_limit_is_configurable_and_validated(self):
        self.write_snapshot("20261108T100000", RICH, last=True)
        self.assertEqual(self.boot(TMUX_BOOT_MAX_RESTORES="soon").returncode, 2)
        self.boot(TMUX_BOOT_MAX_RESTORES="1")
        self.drop("server")
        self.put("windows", "w0\n")
        self.boot(TMUX_BOOT_MAX_RESTORES="1")
        self.assertEqual(self.restores(), 1)

    def test_a_snapshot_of_one_window_is_no_restore_attempt(self):
        self.write_snapshot("20261108T100000", BARE, last=True)
        self.boot()
        self.assertEqual(self.attempts(), [])

    def test_a_running_server_changes_no_state(self):
        self.touch("server")
        self.write_snapshot("20261108T100000", RICH, last=True)
        res = self.boot()
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.attempts(), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "boot-id")))

    def test_boot_records_the_boot_it_started_the_server_in(self):
        self.write_snapshot("20261108T100000", RICH, last=True)
        self.boot()
        self.assertEqual(self.read(self.state, "boot-id").strip(), "boot-A")


if __name__ == "__main__":
    unittest.main()
