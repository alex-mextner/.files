"""tmux-save-now, tmux-resurrect-save and tmux-alert against stub tmux / resurrect / alert channel (see stubs.py)."""

import os
import subprocess
import time
import unittest

from tests.stubs import MODULE, StubBase, snapshot, write_exec  # noqa: F401  (MODULE: for the installed-layout test)

RICH = snapshot(windows=6, panes_per_window=2, harness=4)  # 12 panes, 6 windows, 4 omp
BARE = snapshot(windows=1, panes_per_window=1)  # a fresh `main`


class SaveNow(StubBase):
    def save_state(self, body, stamp):
        self.next_body(body)
        self.set_stamp(stamp)

    def first_good_save(self):
        self.save_state(RICH, "20261108T100000")
        res = self.save_now("--reason", "test")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res

    # ── the accepted path ──────────────────────────────────────────────────────────────────────────────────────────
    def test_every_outcome_is_kept_in_a_bounded_history_with_its_reason(self):
        self.first_good_save()
        self.save_state(BARE, "20261108T100500")
        self.save_now("--reason", "timer")
        lines = self.read(self.state, "save-history").splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("result=ok reason=test", lines[0])
        self.assertIn("result=degenerate reason=timer", lines[1])
        with open(os.path.join(self.state, "save-history"), "w") as fh:
            fh.write("epoch=1 result=ok reason=old detail=x\n" * 450)
        self.set_stamp("20261108T101000")
        self.save_now("--reason", "again")
        lines = self.read(self.state, "save-history").splitlines()
        self.assertLessEqual(len(lines), 201)
        self.assertIn("reason=again", lines[-1])

    def test_a_good_save_runs_the_real_saver_and_keeps_the_archive_maps_and_harness_link(self):
        res = self.first_good_save()
        self.assertIn("saved tmux_resurrect_20261108T100000.txt (test, 4 agent panes)", res.stdout)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")
        self.assertEqual(os.readlink(os.path.join(self.resurrect, "last-with-harness")), "tmux_resurrect_20261108T100000.txt")
        self.assertTrue(os.path.exists(os.path.join(self.resurrect, "pane_contents_20261108T100000.tar.gz")))
        self.assertEqual(self.read(self.resurrect, "maps", "20261108T100000", "omp-sessions.map"), "omp-panes 4\n")
        self.assertIn("result=ok", self.read(self.state, "save-status"))
        # the real script was run, not told it is being called from inside a save
        self.assertIn("saver ran quiet [ACTIVE=1]", self.calls())
        # resurrect's own prune is pushed out of the way
        self.assertEqual(self.get_option("@resurrect-delete-backup-after"), "3650")

    def test_a_snapshot_without_agents_does_not_move_the_harness_link(self):
        self.first_good_save()
        self.save_state(snapshot(windows=6, panes_per_window=2, harness=0), "20261108T100500")
        res = self.save_now()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")  # 4 omp -> 0 omp is degenerate
        self.save_state(snapshot(windows=6, panes_per_window=2, harness=0), "20261108T100500")
        self.save_now("--force")
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100500.txt")
        self.assertEqual(os.readlink(os.path.join(self.resurrect, "last-with-harness")), "tmux_resurrect_20261108T100000.txt")

    def test_an_identical_snapshot_is_reported_unchanged(self):
        self.first_good_save()
        self.set_stamp("20261108T100500")
        res = self.save_now()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("nothing changed", res.stdout)
        self.assertEqual(self.snapshots(), ["tmux_resurrect_20261108T100000.txt"])

    def test_a_legitimate_small_change_is_accepted(self):
        self.first_good_save()
        self.save_state(snapshot(windows=5, panes_per_window=2, harness=4), "20261108T100500")  # 12 -> 10 panes
        res = self.save_now()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100500.txt")
        self.assertEqual(self.alerts(), [])

    def test_a_save_into_a_directory_without_a_previous_snapshot_is_accepted(self):
        self.save_state(BARE, "20261108T100000")
        res = self.save_now()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")
        self.assertFalse(os.path.exists(os.path.join(self.resurrect, "last-with-harness")))

    # ── the degenerate path ────────────────────────────────────────────────────────────────────────────────────────
    def test_a_bare_main_after_a_rich_layout_never_replaces_last(self):
        self.first_good_save()
        # what the maps and the archive were before the bad save
        before_archive = self.read(self.resurrect, "pane_contents.tar.gz")
        before_map = self.read(self.maps, "omp-sessions.map")
        self.save_state(BARE, "20261108T100500")
        res = self.save_now("--reason", "timer")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("DEGENERATE snapshot tmux_resurrect_20261108T100500.txt rejected", res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")
        self.assertEqual(self.snapshots(), ["tmux_resurrect_20261108T100000.txt"])
        self.assertEqual(self.read(self.resurrect, "degenerate", "20261108T100500.txt"), BARE)
        # the pane archive and rig's session map belong to `last`, not to the rejected save
        self.assertEqual(self.read(self.resurrect, "pane_contents.tar.gz"), before_archive)
        self.assertEqual(self.read(self.maps, "omp-sessions.map"), before_map)
        self.assertEqual(self.read(self.resurrect, "last"), RICH)
        self.assertIn("result=degenerate", self.read(self.state, "save-status"))
        self.assertEqual(len(self.alerts()), 1)
        self.assertIn("degenerate snapshot", self.alerts()[0])

    def test_plain_shell_panes_where_the_previous_snapshot_had_agents_are_degenerate(self):
        self.first_good_save()
        self.save_state(snapshot(windows=6, panes_per_window=2, harness=0), "20261108T100500")
        res = self.save_now()
        self.assertIn("no omp/claude/codex pane", res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")

    def test_fewer_panes_or_windows_than_the_tolerance_allows_are_degenerate_and_the_tolerance_is_configurable(self):
        self.first_good_save()
        small = snapshot(windows=2, panes_per_window=2, harness=4)  # 12 -> 4 panes, 6 -> 2 windows
        self.save_state(small, "20261108T100500")
        res = self.save_now()
        self.assertIn("panes fell from 12 to 4", res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")
        self.save_state(small, "20261108T100500")
        res = self.save_now(TMUX_SAVE_NOW_TOLERANCE="80")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100500.txt")

    def test_a_save_in_the_second_of_the_previous_snapshot_does_not_lose_it(self):
        # tmux-resurrect overwrites the file `last` names, finds it equal to `last` (the same file) and deletes it
        self.first_good_save()
        self.save_state(BARE, "20261108T100000")  # same stamp as the previous snapshot
        res = self.save_now()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("put back", res.stdout)
        self.assertEqual(self.read(self.resurrect, "tmux_resurrect_20261108T100000.txt"), RICH)
        self.assertEqual(self.read(self.resurrect, "last"), RICH)

    def test_a_real_save_in_the_second_of_the_previous_one_waits_for_the_next_second(self):
        # a saver that names its snapshot after the clock, as tmux-resurrect does
        write_exec(
            self.saver,
            "#!/bin/sh\nd=$FAKE_RESURRECT_DIR\nf=\"$d/tmux_resurrect_$(date +%Y%m%dT%H%M%S).txt\"\n"
            "cp \"$STUB/body\" \"$f\"\nif ! cmp -s \"$f\" \"$d/last\"; then ln -fs \"$(basename \"$f\")\" \"$d/last\"; else rm \"$f\"; fi\n",
        )
        self.next_body(RICH)
        self.assertEqual(self.save_now().returncode, 0)
        newer = snapshot(windows=5, panes_per_window=2, harness=3)
        self.next_body(newer)
        res = self.save_now()  # within the same second as the first, unless the clock just ticked
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(len(self.snapshots()), 2, (self.snapshots(), res.stdout))
        self.assertEqual(self.read(self.resurrect, "last"), newer)


    def test_the_alert_is_rate_limited_but_every_rejection_is_kept(self):
        self.first_good_save()
        for stamp in ("20261108T100500", "20261108T101000"):
            self.save_state(BARE, stamp)
            self.assertEqual(self.save_now().returncode, 0)
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(sorted(os.listdir(os.path.join(self.resurrect, "degenerate"))), ["20261108T100500.txt", "20261108T101000.txt"])

    def test_a_degenerate_state_that_persists_is_accepted_after_the_configured_time(self):
        self.first_good_save()
        self.save_state(BARE, "20261108T100500")
        self.assertEqual(self.save_now().returncode, 0)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100000.txt")
        res = self.save_now(TMUX_SAVE_NOW_ACCEPT_AFTER="0")
        self.assertIn("accepting it as the new baseline", res.stdout)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100500.txt")
        self.assertFalse(os.path.exists(os.path.join(self.state, "degenerate-since")))

    def test_force_accepts_a_degenerate_snapshot(self):
        self.first_good_save()
        self.save_state(BARE, "20261108T100500")
        self.assertEqual(self.save_now("--force", "--reason", "operator").returncode, 0)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T100500.txt")

    # ── skips and failures ─────────────────────────────────────────────────────────────────────────────────────────
    def test_without_a_server_there_is_nothing_to_save(self):
        self.drop("server")
        res = self.save_now()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no tmux server", res.stdout)
        self.assertNotIn("saver ran", " ".join(self.calls()))

    def test_a_missing_saver_is_a_loud_failure(self):
        res = self.save_now(TMUX_SAVE_NOW_SAVER=os.path.join(self.tmp, "gone.sh"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("save script is missing", res.stderr)
        self.assertIn("result=failed", self.read(self.state, "save-status"))

    def test_a_saver_that_fails_is_a_failure_but_what_it_wrote_is_still_judged(self):
        self.first_good_save()
        self.touch("saver-fail")
        res = self.save_now()
        self.assertEqual(res.returncode, 1)
        self.assertIn("exited 3", res.stderr)

    def test_a_saver_that_leaves_no_snapshot_is_a_failure(self):
        write_exec(self.saver, "#!/bin/sh\nexit 0\n")
        res = self.save_now()
        self.assertEqual(res.returncode, 1)
        self.assertIn("left no snapshot behind", res.stderr)

    def test_a_calls_from_inside_the_save_do_nothing(self):
        res = self.save_now(TMUX_SAVE_NOW_ACTIVE="1")
        self.assertEqual(res.returncode, 0)
        self.assertIn("inside a running save", res.stdout)
        self.assertNotIn("saver ran", " ".join(self.calls()))

    def test_bad_numbers_and_arguments_are_usage_errors(self):
        self.assertEqual(self.save_now(TMUX_SAVE_NOW_KEEP="many").returncode, 2)
        self.assertEqual(self.save_now("--nope").returncode, 2)
        self.assertEqual(self.save_now("--reason").returncode, 2)

    def test_the_callers_tmux_variable_never_redirects_it_to_another_server(self):
        self.first_good_save()
        self.set_stamp("20261108T100500")
        self.next_body(snapshot(windows=5, panes_per_window=2, harness=3))
        res = self.save_now(TMUX="/tmp/other-server,1,0")
        self.assertEqual(res.returncode, 0, res.stderr)
        tmux_calls = [c for c in self.calls() if c.startswith("tmux ")]
        self.assertTrue(tmux_calls)
        self.assertTrue(all(c.endswith("[TMUX=unset]") for c in tmux_calls), tmux_calls)

    # ── single flight ──────────────────────────────────────────────────────────────────────────────────────────────
    def test_concurrent_saves_serialize_one_waits_and_the_rest_are_covered(self):
        self.first_good_save()
        self.put("saver-sleep", "1.5")
        self.save_state(snapshot(windows=6, panes_per_window=2, harness=4), "20261108T100500")
        a = self.popen("tmux-save-now", "--reason", "a")
        deadline = time.time() + 5
        while self.calls().count("saver ran quiet [ACTIVE=1]") < 2 and time.time() < deadline:  # the first run plus A
            time.sleep(0.05)
        b = self.popen("tmux-save-now", "--reason", "b")
        time.sleep(0.5)
        c = self.popen("tmux-save-now", "--reason", "c")
        out = {name: p.communicate(timeout=30) for name, p in (("a", a), ("b", b), ("c", c))}
        self.assertEqual([p.returncode for p in (a, b, c)], [0, 0, 0], out)
        self.assertIn("waits for it", out["b"][0])
        self.assertIn("covered by it", out["c"][0])
        # the saver ran for the first save, A and B: never two at a time
        times = [line.split() for line in self.read(self.stub, "saver-times.log").splitlines()]
        kinds = [k for k, _ in times]
        self.assertEqual(kinds, ["start", "end"] * 3, times)
        stamps = [float(t) for _, t in times]
        self.assertEqual(stamps, sorted(stamps))

    def test_the_lock_is_not_inherited_by_what_the_saver_leaves_behind(self):
        write_exec(
            self.saver,
            "#!/bin/sh\n(sleep 5 >/dev/null 2>&1 </dev/null &) \nd=$FAKE_RESURRECT_DIR\ncp \"$STUB/body\" \"$d/tmux_resurrect_$(cat \"$STUB/stamp\").txt\"\n"
            "ln -fs \"tmux_resurrect_$(cat \"$STUB/stamp\").txt\" \"$d/last\"\n",
        )
        self.save_state(RICH, "20261108T100000")
        self.assertEqual(self.save_now().returncode, 0)
        started = time.time()
        self.save_state(RICH, "20261108T100100")
        res = self.save_now()  # would wait for the lingering `sleep 5` if it held the lock
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLess(time.time() - started, 4)

    # ── retention ──────────────────────────────────────────────────────────────────────────────────────────────────
    def write_old(self, stamp, body=RICH):
        for name, text in ((f"tmux_resurrect_{stamp}.txt", body), (f"pane_contents_{stamp}.tar.gz", "x\n")):
            with open(os.path.join(self.resurrect, name), "w") as fh:
                fh.write(text)
        os.makedirs(os.path.join(self.resurrect, "maps", stamp))

    def seed_retention_history(self, today, new_stamp):
        """20 days, 8 snapshots a day (160 files), oldest first; the oldest carries the ancient harness link."""
        import datetime

        stamps = []
        for back in range(19, -1, -1):
            day = (today - datetime.timedelta(days=back)).strftime("%Y%m%d")
            for hour in range(8):
                stamps.append(f"{day}T{hour * 3:02d}0000")
        plain = snapshot(windows=6, panes_per_window=2, harness=0)
        for s in stamps:
            self.write_old(s, body=RICH if s == stamps[0] else plain)
        os.symlink(f"tmux_resurrect_{stamps[0]}.txt", os.path.join(self.resurrect, "last-with-harness"))  # an ancient harness link
        os.symlink(f"tmux_resurrect_{stamps[-1]}.txt", os.path.join(self.resurrect, "last"))
        self.save_state(snapshot(windows=5, panes_per_window=2, harness=0), new_stamp)  # no agent: the link must stay put
        return stamps

    def assert_retention_policy(self, today, all_stamps, kept, ancient):
        import datetime

        # the newest 48 are all there
        self.assertTrue(set(all_stamps[-48:]) <= set(kept))
        # one per day for the last 14 days (the newest of each such day)
        for back in range(14):
            day = (today - datetime.timedelta(days=back)).strftime("%Y%m%d")
            newest = max(s for s in all_stamps if s.startswith(day))
            self.assertIn(newest, kept, day)
        # the ancient harness link target survives, nothing else outside the policy does
        self.assertIn(ancient, kept)
        cutoff = (today - datetime.timedelta(days=14)).strftime("%Y%m%d")
        extras = [s for s in kept if s not in all_stamps[-48:] and s[:8] < cutoff and s != ancient]
        self.assertEqual(extras, [])

    def test_retention_keeps_the_newest_n_and_one_per_day_and_the_protected_links(self):
        import datetime

        today = datetime.date.today()
        new_stamp = today.strftime("%Y%m%d") + "T235959"
        stamps = self.seed_retention_history(today, new_stamp)
        env = {"TMUX_SAVE_NOW_KEEP": "48", "TMUX_SAVE_NOW_KEEP_DAYS": "14"}
        self.assertEqual(self.save_now(**env).returncode, 0)
        kept = [n[len("tmux_resurrect_") : -len(".txt")] for n in self.snapshots()]
        all_stamps = stamps + [new_stamp]
        self.assert_retention_policy(today, all_stamps, kept, stamps[0])
        # the archive and the maps go with their snapshot
        gone = next(s for s in all_stamps if s not in kept)
        self.assertFalse(os.path.exists(os.path.join(self.resurrect, f"pane_contents_{gone}.tar.gz")))
        self.assertFalse(os.path.exists(os.path.join(self.resurrect, "maps", gone)))
        self.assertTrue(os.path.exists(os.path.join(self.resurrect, f"pane_contents_{stamps[0]}.tar.gz")))
        self.assertEqual(self.last_name(), f"tmux_resurrect_{new_stamp}.txt")

    def test_a_save_that_changes_nothing_clears_the_degenerate_clock(self):
        # back at the good layout (identical to `last`): the persistence clock must start over, or a later loss
        # would be accepted at once on the strength of a stamp from an old, recovered episode
        self.save_state(RICH, "20261108T100000")
        self.assertEqual(self.save_now().returncode, 0)
        with open(os.path.join(self.state, "degenerate-since"), "w") as fh:
            fh.write(str(int(time.time()) - 7 * 3600) + "\n")
        res = self.save_now()  # the stub saver writes the same body: unchanged
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "degenerate-since")))

    # ── the wrapper ────────────────────────────────────────────────────────────────────────────────────────────────
    def test_the_resurrect_wrapper_goes_through_the_guard_and_names_its_reason(self):
        self.save_state(RICH, "20261108T100000")
        res = subprocess.run(
            ["bash", os.path.join(MODULE, "tmux-resurrect-save"), "quiet"], env=self.env(), capture_output=True, text=True, check=False
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("saved tmux_resurrect_20261108T100000.txt (continuum,", res.stdout)

    def test_the_real_saver_is_found_through_the_stash_when_the_option_names_the_wrapper(self):
        self.save_state(RICH, "20261108T100000")
        wrapper = os.path.join(MODULE, "tmux-resurrect-save")
        self.option("@resurrect-save-script-path", wrapper)
        self.option("@tmux-boot-resurrect-save-script", self.saver)
        env = self.env()
        del env["TMUX_SAVE_NOW_SAVER"]
        res = subprocess.run(["bash", os.path.join(MODULE, "tmux-save-now")], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("saver ran quiet", " ".join(self.calls()))


class Alert(StubBase):
    def alert(self, *args, **extra):
        return self.run_script("tmux-alert", *args, **extra)

    def test_the_first_alert_is_sent_and_a_repeat_with_the_same_key_is_suppressed(self):
        res = self.alert("autosave is dead", "--key", "k1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("sent (k1)", res.stdout)
        self.assertEqual(len(self.alerts()), 1)
        self.assertIn("autosave is dead", self.alerts()[0])
        res = self.alert("autosave is dead again", "--key", "k1")
        self.assertIn("suppressed", res.stdout)
        self.assertEqual(len(self.alerts()), 1)

    def test_another_key_is_not_suppressed_and_the_interval_expires(self):
        self.alert("a", "--key", "k1")
        self.alert("b", "--key", "k2")
        self.assertEqual(len(self.alerts()), 2)
        stamp = os.path.join(self.state, "alerts", "k1")
        self.age(stamp, 7 * 3600)
        with open(stamp, "w") as fh:
            fh.write(str(int(time.time()) - 7 * 3600) + "\n")
        self.alert("a again", "--key", "k1")
        self.assertEqual(len(self.alerts()), 3)

    def test_the_interval_is_configurable(self):
        self.alert("a", "--key", "k1")
        self.alert("a", "--key", "k1", TMUX_ALERT_INTERVAL="0")
        self.assertEqual(len(self.alerts()), 2)

    def test_a_failing_sender_is_an_error_and_is_retried_next_time(self):
        res = self.alert("down", "--key", "k1", ALERT_EXIT="1")
        self.assertEqual(res.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.state, "alerts", "k1")))
        res = self.alert("down", "--key", "k1")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(len(self.alerts()), 2)

    def test_the_message_is_the_key_when_none_is_given_and_usage_errors_are_exit_2(self):
        self.assertEqual(self.alert("same message").returncode, 0)
        self.assertIn("suppressed", self.alert("same message").stdout)
        self.assertEqual(self.alert().returncode, 2)
        self.assertEqual(self.alert("a", "b").returncode, 2)
        self.assertEqual(self.alert("a", "--key").returncode, 2)
        self.assertEqual(self.alert("a", TMUX_ALERT_INTERVAL="soon").returncode, 2)

    def test_the_senders_own_output_is_never_shown(self):
        write_exec(self.alert_cmd, "#!/bin/sh\necho 'token=SECRET123'\necho 'token=SECRET123' >&2\nexit 0\n")
        res = self.alert("x", "--key", "k")
        self.assertNotIn("SECRET123", res.stdout + res.stderr)

    def steer_tools(self, sessions="unset"):
        """tg-ctl/omp-steer stubs on PATH: omp-live status answers $STUB/live-sessions, deliveries are logged."""
        write_exec(
            os.path.join(self.fakebin, "tg-ctl"),
            '#!/bin/sh\necho "tg-ctl $*" >> "$STUB/tg-ctl.log"\n'
            'case "$1 $2" in "omp-live status") [ -f "$STUB/live-sessions" ] && cat "$STUB/live-sessions" || exit 1 ;; esac\n'
            'exit 0\n',
        )
        write_exec(os.path.join(self.fakebin, "omp-steer"), '#!/bin/sh\necho "omp-steer $*" >> "$STUB/omp-steer.log"\nexit 0\n')
        if sessions != "unset":
            self.put("live-sessions", sessions)

    def bare_env(self):
        env = self.env()
        del env["TMUX_ALERT_CMD"]  # the default delivery: mem-sentinel's steer channel, no recorder
        return env

    def test_the_default_delivery_steers_into_a_live_omp_session(self):
        # mem-sentinel's channel: `tg-ctl omp-live send <pid> --session <sessionId> -- <text>`, prefixed [tmux-boot]
        self.steer_tools('[{"pid": 4242, "sessionId": "01a1-4242", "cwd": "/tmp"}]\n')
        res = subprocess.run(["bash", os.path.join(MODULE, "tmux-alert"), "boom", "--key", "k"], env=self.bare_env(), capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        log = self.read(self.stub, "tg-ctl.log")
        self.assertIn("tg-ctl omp-live status --json", log)
        self.assertIn("tg-ctl omp-live send 4242 --request-id tmux-alert-", log)
        self.assertIn("--session 01a1-4242 -- [tmux-boot]", log)
        self.assertIn("boom", log)
        self.assertFalse(os.path.exists(os.path.join(self.stub, "omp-steer.log")), "a live session means no broadcast")

    def test_with_no_live_session_it_broadcasts_through_omp_steer(self):
        self.steer_tools("[]\n")
        res = subprocess.run(["bash", os.path.join(MODULE, "tmux-alert"), "boom", "--key", "k"], env=self.bare_env(), capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        log = self.read(self.stub, "omp-steer.log")
        self.assertIn("omp-steer all [tmux-boot]", log)
        self.assertIn("boom", log)
        self.assertNotIn(" omp-live send ", self.read(self.stub, "tg-ctl.log"))

    def test_a_dead_tg_ctl_reads_as_no_session_and_the_broadcast_carries_the_alert(self):
        self.steer_tools()  # no live-sessions file: tg-ctl fails the status round trip, the way an outage does
        res = subprocess.run(["bash", os.path.join(MODULE, "tmux-alert"), "boom", "--key", "k"], env=self.bare_env(), capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("omp-steer all [tmux-boot]", self.read(self.stub, "omp-steer.log"))

    def test_without_any_channel_installed_it_fails_and_is_retried_next_time(self):
        res = subprocess.run(["bash", os.path.join(MODULE, "tmux-alert"), "boom", "--key", "k"], env=self.bare_env(), capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("cannot send", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "alerts", "k")))


if __name__ == "__main__":
    unittest.main()
