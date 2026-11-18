"""tmux-watchdog: every invariant, its self-heal, its alert, and the (off by default) server recovery, against stub tmux,
systemctl, rig and Telegram (see stubs.py). Nothing here touches a real server, unit or alert channel."""

import os
import subprocess
import time
import unittest

from tests.stubs import MODULE, StubBase, snapshot, write_exec

# a unit exists when $STUB/unit-NAME does; `enable [--now]` creates enabled-NAME (active-NAME with --now) unless
# $STUB/enable-fails; `show -p LoadState --value NAME` answers loaded/not-found
SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$STUB/calls.log"
shift
case "$1" in
  show-environment) [ -e "$STUB/no-bus" ] && exit 1; exit 0 ;;
  is-enabled) if [ -e "$STUB/enabled-$2" ]; then echo enabled; exit 0; fi; echo disabled; exit 1 ;;
  is-active) for last; do :; done; [ -e "$STUB/active-$last" ]; exit $? ;;
  show) for last; do :; done; if [ -e "$STUB/unit-$last" ]; then echo loaded; else echo not-found; fi; exit 0 ;;
  enable)
    [ -e "$STUB/enable-fails" ] && exit 1
    now=0; for a in "$@"; do [ "$a" = --now ] && now=1; done
    for a in "$@"; do case "$a" in tmux-*) : > "$STUB/enabled-$a"; [ "$now" = 1 ] && : > "$STUB/active-$a" ;; esac; done
    exit 0 ;;
esac
exit 0
"""

RIG = """#!/bin/sh
echo "rig $*" >> "$STUB/calls.log"
echo "lsof-for-rig: $(command -v lsof)" >> "$STUB/calls.log"
[ -e "$STUB/rig-starts-server" ] && : > "$STUB/server"
echo '{"action": "start", "reason": "server_confirmed_absent", "pid": null}'
exit 0
"""

RICH = snapshot(windows=4, panes_per_window=2, harness=2)


class Watchdog(StubBase):
    def setUp(self):
        super().setUp()
        write_exec(os.path.join(self.fakebin, "systemctl"), SYSTEMCTL)
        self.rig = os.path.join(self.tmp, "rig")
        write_exec(self.rig, RIG)
        self.units = os.path.join(self.tmp, "units")
        os.makedirs(self.units)
        self.scripts = os.path.join(self.tmp, "rigscripts")
        os.makedirs(self.scripts)
        for name in ("cc-save.sh", "omp-save.sh", "cc-restore.sh", "omp-restore.sh"):
            write_exec(os.path.join(self.scripts, name), "#!/bin/sh\n")
        self.rig_conf = os.path.join(self.tmp, "rig.tmux.conf")
        self.write_rig_conf()
        self.boot_id = os.path.join(self.tmp, "boot_id")
        with open(self.boot_id, "w") as fh:
            fh.write("boot-1\n")
        self.healthy()

    def write_rig_conf(self, save=True, restore=True, interval="15"):
        lines = ["# rig-managed"]
        if save:
            lines.append(f'set -g @resurrect-hook-post-save-all "{self.scripts}/cc-save.sh; {self.scripts}/omp-save.sh"')
        if restore:
            lines.append(f'set -g @resurrect-hook-post-restore-all "{self.scripts}/cc-restore.sh; {self.scripts}/omp-restore.sh"')
        lines.append(f"set -g @continuum-save-interval '{interval}'")
        with open(self.rig_conf, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def unit(self, name, enabled=True, active=True, loaded=True, file=True):
        if file:
            write_exec(os.path.join(self.units, name), "[Unit]\n")
        for prefix, on in (("enabled", enabled), ("active", active), ("unit", loaded)):
            path = os.path.join(self.stub, f"{prefix}-{name}")
            if on:
                open(path, "w").close()
            elif os.path.exists(path):
                os.remove(path)

    def healthy(self):
        """A server in which every invariant holds, with fresh saves and no agent panes."""
        self.put("panes", "100\tzsh\n101\tzsh\n")
        self.put("ps", "100 1 pts/1 -zsh\n101 1 pts/2 -zsh\n")  # pid ppid tty args, the columns tmux-lib asks ps for
        write_exec(os.path.join(self.fakebin, "ps"), '#!/bin/sh\ncat "$STUB/ps" 2>/dev/null\n')
        self.option("@continuum-save-interval", "15")
        self.option("@continuum-save-last-timestamp", str(int(time.time())))
        self.option("@resurrect-hook-post-save-all", f"{self.scripts}/cc-save.sh; {self.scripts}/omp-save.sh")
        self.option("@resurrect-hook-post-restore-all", f"{self.scripts}/cc-restore.sh; {self.scripts}/omp-restore.sh")
        self.option("status-right", "#(continuum_save.sh)")
        self.unit("tmux-autosave.timer")
        self.unit("tmux-boot.service", active=False)
        # the saver option and the hooks as tmux-autosave/watchdog leave them
        subprocess.run(
            ["bash", "-c", f'. "{MODULE}/tmux-lib"; tl_assert_saver; tl_assert_hooks'],
            env=self.env(), capture_output=True, check=True,
        )
        self.next_body(RICH)
        self.set_stamp("20261108T100000")
        self.assertEqual(self.save_now().returncode, 0)
        self.put("alerts.log", "")
        self.put("calls.log", "")

    def watchdog(self, *args, **extra):
        # the agent detection reads argv from TL_PROC_ROOT/PID/cmdline: a fake root keeps a stub pid off a real process
        extra.setdefault("TL_PROC_ROOT", os.path.join(self.tmp, "proc"))
        return self.run_script(
            "tmux-watchdog", *args,
            TMUX_WATCHDOG_RIG_CONF=self.rig_conf, TMUX_WATCHDOG_UNIT_DIR=self.units, TMUX_BOOT_ID_FILE=self.boot_id,
            TMUX_WATCHDOG_RIG=self.rig, TMUX_SAVE_NOW_SAVER=self.saver,
            **extra,
        )

    def saver_calls(self):
        return [c for c in self.calls() if c.startswith("saver ran")]

    # ── nothing wrong ──────────────────────────────────────────────────────────────────────────────────────────────
    def test_a_healthy_server_changes_nothing_and_alerts_nobody(self):
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("tmux-watchdog: ok", res.stdout)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.saver_calls(), [])
        self.assertEqual([c for c in self.calls() if c.startswith("tmux set") or " enable " in c], [])

    # ── the status-right trigger is NOT an invariant (Moshi empties it on every attach) ────────────────────────────────
    def test_an_emptied_status_right_is_a_note_not_a_problem(self):
        self.option("status-right", "")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("status-right carries no continuum token", res.stdout)
        self.assertEqual(self.alerts(), [])

    # ── the pointers and hooks ─────────────────────────────────────────────────────────────────────────────────────────
    def test_a_wiped_saver_option_is_pointed_back_and_reported(self):
        self.option("@resurrect-save-script-path", "/somewhere/tmux-resurrect/scripts/save.sh")  # a reloaded plugin
        write_exec("/somewhere-nonexistent" if False else self.saver, "#!/bin/sh\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.get_option("@resurrect-save-script-path"), os.path.join(MODULE, "tmux-resurrect-save"))
        self.assertEqual(self.get_option("@tmux-boot-resurrect-save-script"), "/somewhere/tmux-resurrect/scripts/save.sh")
        self.assertIn("healed: @resurrect-save-script-path", res.stdout)
        self.assertEqual(len(self.alerts()), 1)
        self.assertIn("repaired", self.alerts()[0])

    def test_a_removed_layout_hook_is_reinstalled_and_reported(self):
        self.put("hooks", "")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        hooks = self.read(self.stub, "hooks")
        for hook in ("session-created", "window-linked", "pane-exited"):  # window-renamed is not hooked (automatic-rename)
            self.assertIn(f"{hook}[940]", hooks)
        self.assertEqual(hooks.count("window-linked["), 1)
        self.assertEqual(len(self.alerts()), 1)
        # and the next run finds nothing to do
        self.put("alerts.log", "")
        self.assertEqual(self.watchdog().returncode, 0)
        self.assertEqual(self.read(self.stub, "hooks"), hooks)

    def test_empty_rig_hooks_in_the_server_are_set_from_rigs_generated_config(self):
        self.option("@resurrect-hook-post-save-all", "")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.get_option("@resurrect-hook-post-save-all"), f"{self.scripts}/cc-save.sh; {self.scripts}/omp-save.sh")
        self.assertEqual(len(self.alerts()), 1)

    def test_empty_rig_hooks_that_rigs_config_does_not_hold_are_a_problem(self):
        self.option("@resurrect-hook-post-save-all", "")
        self.write_rig_conf(save=False)
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("@resurrect-hook-post-save-all is empty", res.stderr)
        self.assertGreaterEqual(len(self.alerts()), 1)

    def test_rig_apply_clobbering_the_config_is_loud(self):
        self.write_rig_conf(save=False, restore=False)  # a rig apply that forgot the hooks
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("no longer sets @resurrect-hook-post-save-all", res.stderr)
        self.assertIn("rewrote it wholesale", res.stderr)
        self.assertTrue(any("no longer sets" in a for a in self.alerts()))
        os.remove(self.rig_conf)
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("is missing", res.stderr)

    def test_a_hook_that_names_a_missing_script_is_a_problem(self):
        os.remove(os.path.join(self.scripts, "omp-save.sh"))
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("omp-save.sh, which does not exist", res.stderr)

    def test_an_interval_of_zero_is_healed_from_rig_or_reported(self):
        self.option("@continuum-save-interval", "0")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.get_option("@continuum-save-interval"), "15")
        self.option("@continuum-save-interval", "0")
        self.write_rig_conf(interval="0")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("switched off", res.stderr)

    def test_a_save_interval_with_a_leading_zero_is_decimal_not_an_octal_crash(self):
        # "08" used to reach the limit arithmetic raw, where bash read it as an invalid octal literal and the
        # unbound limit killed the watchdog under set -u
        self.option("@continuum-save-interval", "08")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("tmux-watchdog: ok", res.stdout)

    # ── units ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def test_an_inactive_autosave_timer_is_enabled_and_started(self):
        self.unit("tmux-autosave.timer", enabled=False, active=False)
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("systemctl --user enable --now tmux-autosave.timer", self.calls())
        self.assertEqual(len(self.alerts()), 1)

    def test_a_timer_that_cannot_be_enabled_is_a_problem(self):
        self.unit("tmux-autosave.timer", enabled=False, active=False)
        self.touch("enable-fails")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("tmux-autosave.timer is not enabled", res.stderr)
        self.assertGreaterEqual(len(self.alerts()), 1)

    def test_shutdown_units_are_checked_only_when_their_files_exist_and_enabled_when_they_do(self):
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse([c for c in self.calls() if "shutdown" in c])
        self.unit("tmux-shutdown-save.service", enabled=False, active=False)
        self.unit("tmux-shutdown-watch.service", enabled=True, active=True)
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("systemctl --user enable --now tmux-shutdown-save.service", self.calls())
        self.assertNotIn("systemctl --user enable --now tmux-shutdown-watch.service", self.calls())

    def test_a_unit_file_that_systemd_does_not_load_is_a_problem(self):
        self.unit("tmux-shutdown-save.service", loaded=False)
        os.remove(os.path.join(self.stub, "enabled-tmux-shutdown-save.service")) if os.path.exists(os.path.join(self.stub, "enabled-tmux-shutdown-save.service")) else None
        self.touch("enable-fails")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("tmux-shutdown-save.service", res.stderr)

    def test_without_a_user_bus_the_unit_checks_are_skipped(self):
        self.touch("no-bus")
        self.unit("tmux-autosave.timer", enabled=False, active=False)
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("no user systemd bus", res.stdout)

    # ── freshness and maps ─────────────────────────────────────────────────────────────────────────────────────────────
    def age_every_sign_of_a_save(self, seconds=3 * 3600):
        for name in os.listdir(self.resurrect):
            path = os.path.join(self.resurrect, name)
            if os.path.isfile(path):
                self.age(path, seconds)
        os.remove(os.path.join(self.state, "save-status"))

    def test_a_stale_snapshot_while_panes_exist_forces_a_save_and_reports(self):
        self.age_every_sign_of_a_save()
        self.next_body(snapshot(windows=4, panes_per_window=2, harness=1))
        self.set_stamp("20261108T110000")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(len(self.saver_calls()), 1)
        self.assertEqual(self.last_name(), "tmux_resurrect_20261108T110000.txt")
        self.assertIn("result=ok reason=watchdog", self.read(self.state, "save-status"))
        self.assertEqual(len(self.alerts()), 1)

    def test_an_identical_workspace_that_writes_no_new_file_is_not_stale(self):
        # tmux-save-now leaves nothing new when the layout did not change, but its status says a save ran
        self.age_every_sign_of_a_save()
        self.put("save-status-fresh", "")
        with open(os.path.join(self.state, "save-status"), "w") as fh:
            fh.write(f"epoch={int(time.time())} result=unchanged reason=timer detail=x\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.saver_calls(), [])
        self.assertEqual(self.alerts(), [])

    def test_a_save_that_cannot_run_is_a_problem(self):
        self.age_every_sign_of_a_save()
        self.touch("saver-fail")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("forced tmux-save-now did not fix it", res.stderr)
        self.assertGreaterEqual(len(self.alerts()), 1)

    def test_no_panes_means_no_freshness_requirement(self):
        self.age_every_sign_of_a_save()
        self.put("panes", "")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.saver_calls(), [])

    def test_the_freshness_limit_follows_the_continuum_interval(self):
        self.age_every_sign_of_a_save(seconds=40 * 60)  # 40 min: stale at 15 min (limit 30), fine at 60 min (limit 120)
        self.option("@continuum-save-interval", "60")
        with open(os.path.join(self.state, "save-status"), "w") as fh:
            fh.write("epoch=0 result=ok reason=x detail=x\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.saver_calls(), [])

    def test_while_the_guard_holds_a_smaller_layout_back_the_maps_are_not_reported_stale(self):
        # during a hold the maps are `last`'s on purpose (put back with cp -p, so their mtime is old): a fresh
        # result=degenerate verdict means the saver ran and decided, which is not rig failing to write maps
        self.put("panes", "100\tzsh\n101\tomp\n")
        self.age(os.path.join(self.maps, "omp-sessions.map"), 40 * 60)  # older than the 30-minute limit
        with open(os.path.join(self.state, "save-status"), "w") as fh:
            fh.write(f"epoch={int(time.time())} result=degenerate tmux_resurrect_x.txt: panes fell\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.saver_calls(), [])

    def test_a_missing_map_while_an_agent_runs_forces_a_save_that_writes_it(self):
        self.put("panes", "100\tzsh\n101\tomp\n")
        self.next_body(snapshot(windows=4, panes_per_window=2, harness=2))
        self.assertTrue(os.path.exists(os.path.join(self.maps, "omp-sessions.map")))
        os.remove(os.path.join(self.maps, "omp-sessions.map"))
        self.set_stamp("20261108T110000")
        self.next_body(snapshot(windows=4, panes_per_window=2, harness=1))
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.getsize(os.path.join(self.maps, "omp-sessions.map")) > 0)
        self.assertEqual(len(self.saver_calls()), 1)
        self.assertEqual(len(self.alerts()), 1)

    def test_a_map_that_stays_empty_after_a_forced_save_is_a_problem(self):
        self.put("panes", "100\tzsh\n101\tomp\n")
        with open(os.path.join(self.maps, "omp-sessions.map"), "w"):
            pass
        write_exec(self.saver, "#!/bin/sh\ncd \"$FAKE_RESURRECT_DIR\" && ln -fs \"$(basename \"$(readlink last)\")\" last\n: > \"$FAKE_MAP_DIR/omp-sessions.map\"\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("omp-sessions.map is missing or empty", res.stderr)
        self.assertGreaterEqual(len(self.alerts()), 1)

    def test_a_present_fresh_map_needs_nothing(self):
        self.put("panes", "100\tzsh\n101\tomp\n")
        res = self.watchdog()  # the setup's save wrote a map with 2 omp panes
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.saver_calls(), [])

    # ── agents that are not called `omp` in #{pane_current_command}: rig's cc-save.sh / codex-save.sh walk the pane's
    # process tree and look at argv[0] only; the watchdog must demand exactly the maps rig would write ────────────────────
    def saver_writing(self, *maps):
        body = "".join(f': > "$FAKE_MAP_DIR/{m}"; echo "x" >> "$FAKE_MAP_DIR/{m}"\n' for m in maps)
        write_exec(
            self.saver,
            "#!/bin/sh\nd=$FAKE_RESURRECT_DIR\nf=\"$d/tmux_resurrect_$(cat \"$STUB/stamp\").txt\"\ncp \"$STUB/body\" \"$f\"\n"
            "ln -fs \"$(basename \"$f\")\" \"$d/last\"\nmkdir -p \"$FAKE_MAP_DIR\"\n" + body,
        )
        self.next_body(snapshot(windows=4, panes_per_window=2, harness=1))
        self.set_stamp("20261108T110000")

    def test_a_claude_pane_that_shows_its_version_demands_the_cc_map_and_the_forced_save_heals_it(self):
        self.put("panes", "100\tzsh\n300\t2.1.178\n")  # pane_current_command is the VERSION, never `claude`
        self.put("ps", "100 1 pts/1 -zsh\n300 1 pts/3 -zsh\n301 300 pts/3 /home/ultra/.local/share/claude/versions/2.1.178 --resume abc\n")
        self.saver_writing("cc-sessions.map")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(len(self.saver_calls()), 0)  # the custom saver does not log; its map is what proves the run
        self.assertTrue(os.path.getsize(os.path.join(self.maps, "cc-sessions.map")) > 0)
        self.assertTrue(any("session maps were stale" in a for a in self.alerts()))

    def test_a_claude_pane_without_a_cc_map_is_a_problem_when_the_save_cannot_write_one(self):
        self.put("panes", "300\t2.1.178\n")
        self.put("ps", "300 1 pts/3 -zsh\n301 300 pts/3 claude --resume abc\n")  # a symlink launch: argv[0] is `claude`
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("cc-sessions.map is missing or empty", res.stderr)

    def test_a_codex_pane_demands_the_codex_map(self):
        self.put("panes", "400\tnode\n")
        self.put("ps", "400 1 pts/4 -zsh\n401 400 pts/4 /usr/local/bin/codex resume abc\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("codex-sessions.map is missing or empty", res.stderr)

    def run_procs(self, pane, procs):
        """Stub the pane and its process table from (pid, ppid, tty, argv) rows: ps shows the flattened argv, the fake
        /proc/PID/cmdline (TL_PROC_ROOT) the exact NUL-separated one, as on Linux."""
        self.put("panes", pane)
        self.put("ps", "".join(f"{pid} {ppid} {tty} {' '.join(argv)}\n" for pid, ppid, tty, argv in procs))
        for pid, _, _, argv in procs:
            os.makedirs(os.path.join(self.tmp, "proc", str(pid)), exist_ok=True)
            with open(os.path.join(self.tmp, "proc", str(pid), "cmdline"), "wb") as f:
                f.write(b"".join(a.encode() + b"\0" for a in argv))
        return self.watchdog()

    def test_one_shot_claude_and_codex_runs_without_a_terminal_demand_no_map(self):
        # an omp in the pane runs a review: its `codex exec` / `claude -p` run detached from the pane's terminal and leave
        # no resumable session, so rig writes no map line for them and the watchdog must not demand one (it failed every
        # pass while such a review ran)
        self.saver_writing("omp-sessions.map")
        res = self.run_procs("100\tomp\n", [
            (100, 1, "pts/1", ["omp"]),
            (110, 100, "?", ["node", "/usr/local/bin/codex", "exec", "-s", "read-only", "--ephemeral", "-"]),
            (111, 110, "?", ["/opt/codex/bin/codex", "exec", "-s", "read-only", "--ephemeral", "-"]),
            (120, 100, "?", ["/home/ultra/.local/share/claude/versions/2.1.178", "-p", "--output-format", "json"]),
            (130, 100, "?", ["/usr/local/bin/codex", "mcp-server"]),
        ])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("cc-sessions.map", res.stdout + res.stderr)
        self.assertNotIn("codex-sessions.map", res.stdout + res.stderr)

    def test_an_interactive_agent_still_demands_its_map_with_one_sign_of_a_one_shot(self):
        for case, kind, tty, argv in (
            # a terminal: whatever the arguments say, the pane runs it
            ("terminal and -p", "cc", "pts/3", ["claude", "-p"]),
            # started through setsid: no controlling terminal, and an interactive invocation
            ("setsid launch", "cc", "?", ["claude", "--resume", "abc"]),
            ("prompt mentions --print", "cc", "?", ["claude", "--system-prompt", "Explain --print usage"]),
            ("prompt is exactly --print", "cc", "?", ["claude", "--system-prompt", "--print"]),
            ("session named --print", "cc", "?", ["claude", "--name", "--print"]),
            ("codex prompt says review", "codex", "?", ["/usr/local/bin/codex", "review this patch"]),
            ("codex profile named review", "codex", "?", ["/usr/local/bin/codex", "--profile", "review"]),
            ("codex prompt after --", "codex", "?", ["/usr/local/bin/codex", "--", "exec"]),
        ):
            with self.subTest(case=case):
                res = self.run_procs("300\t2.1.178\n", [(300, 1, "pts/3", ["-zsh"]), (301, 300, tty, argv)])
                self.assertEqual(res.returncode, 1)
                self.assertIn(f"{kind}-sessions.map is missing or empty", res.stderr)
                for key in os.listdir(os.path.join(self.state, "alerts")):
                    os.remove(os.path.join(self.state, "alerts", key))

    def test_a_detached_agent_whose_argv_cannot_be_read_still_demands_its_map(self):
        self.put("panes", "300\t2.1.178\n")
        self.put("ps", "300 1 pts/3 -zsh\n301 300 ? claude -p\n")  # no /proc entry under TL_PROC_ROOT
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("cc-sessions.map is missing or empty", res.stderr)
        # an agent that exits between ps and the cmdline read is routine: no raw shell error in the journal
        self.assertNotIn("No such file or directory", res.stderr)

    def test_a_nested_agent_in_the_process_tree_is_found(self):
        self.put("panes", "300\tzsh\n")
        self.put("ps", "300 1 pts/3 -zsh\n310 300 pts/3 bash -c wrapper\n311 310 pts/3 /opt/claude\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("cc-sessions.map is missing or empty", res.stderr)

    def test_a_pane_whose_own_process_is_the_agent_demands_its_map(self):
        for exe, kind in (("/home/ultra/.local/share/claude/versions/2.1.178", "cc"), ("/usr/local/bin/codex", "codex")):
            with self.subTest(kind=kind):
                self.put("panes", "300\t2.1.178\n")
                self.put("ps", f"300 1 pts/3 {exe} --resume abc\n")  # no shell between the pane and the agent
                res = self.watchdog()
                self.assertEqual(res.returncode, 1)
                self.assertIn(f"{kind}-sessions.map is missing or empty", res.stderr)
                for key in os.listdir(os.path.join(self.state, "alerts")):
                    os.remove(os.path.join(self.state, "alerts", key))

    def test_several_panes_with_the_same_agent_demand_its_map_once_and_alert_once(self):
        self.put("panes", "300\t2.1.178\n310\t2.1.178\n320\tomp\n330\tomp\n")
        self.put("ps", "300 1 pts/3 -zsh\n301 300 pts/3 claude\n310 1 pts/4 -zsh\n311 310 pts/4 claude\n320 1 pts/5 omp\n330 1 pts/6 omp\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertEqual(res.stderr.count("cc-sessions.map is missing or empty"), 1, res.stderr)
        self.assertEqual(len([a for a in self.alerts() if "cc-sessions.map" in a]), 1, self.alerts())

    def test_source_launched_omp_demands_the_omp_map_and_unrelated_bun_and_node_do_not(self):
        entry = "/home/ultra/xp/oh-my-pi/packages/coding-agent/src/cli.ts"
        omp_map = os.path.join(self.maps, "omp-sessions.map")
        self.saver_writing()  # a forced save writes no omp map: a demanded one stays missing
        for exe, demanded in (
            (f"bun {entry}", True),
            (f"/home/ultra/.bun/bin/bun {entry} --resume x", True),
            (f"node {entry}", True),
            ("bun /x/other/cli.ts", False),
            ("bun run dev", False),
            (f"bun {entry} ps", False),  # a subcommand, not an interactive session
            (f"bun {entry} -p hello", False),
            ("omp --resume x", True),
            ("omp read file", False),
            ("omp --mode rpc", False),
            ("omp -p x", False),
            ("omp --mode json", False),
            ("omp --mode text", True),
            ("omp help", False),
            ("omp __omp_worker_1", False),
        ):
            with self.subTest(exe=exe):
                self.put("panes", "300\tbun\n")  # never `omp`, so only the process tree can say it
                self.put("ps", f"300 1 pts/3 -zsh\n301 300 pts/3 {exe}\n")
                if os.path.exists(omp_map):
                    os.remove(omp_map)
                res = self.watchdog()
                if demanded:
                    self.assertEqual(res.returncode, 1, res.stdout)
                    self.assertIn("omp-sessions.map is missing or empty", res.stderr)
                else:
                    self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                alerts_dir = os.path.join(self.state, "alerts")
                for key in os.listdir(alerts_dir) if os.path.isdir(alerts_dir) else []:
                    os.remove(os.path.join(alerts_dir, key))

    def test_the_pane_command_omp_does_not_count_when_the_process_table_says_it_is_a_one_shot(self):
        self.saver_writing()
        self.put("panes", "300\tomp\n")
        self.put("ps", "300 1 pts/3 omp -p summarize\n")
        os.remove(os.path.join(self.maps, "omp-sessions.map"))
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_a_pane_that_merely_mentions_claude_in_an_argument_demands_nothing(self):
        self.put("panes", "500\tgrep\n600\tvim\n")
        self.put("ps", "500 1 pts/5 grep x /home/ultra/.local/share/claude/versions/\n600 1 pts/6 vim claude.md\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.saver_calls(), [])
        self.assertEqual(self.alerts(), [])

    def test_a_tmux_lib_without_the_agent_helper_is_a_problem_never_a_green_run(self):
        copy = os.path.join(self.tmp, "modcopy")
        os.makedirs(copy)
        for name in os.listdir(MODULE):
            src = os.path.join(MODULE, name)
            if os.path.isfile(src):
                with open(src) as fh:
                    text = fh.read()
                if name == "tmux-lib":
                    start = text.index("tl_pane_agents() {")
                    end = text.index("\n}\n", start) + 3
                    text = text[:start] + text[end:]
                write_exec(os.path.join(copy, name), text)
        res = subprocess.run(
            ["bash", os.path.join(copy, "tmux-watchdog")],
            env=self.env(TMUX_WATCHDOG_RIG_CONF=self.rig_conf, TMUX_WATCHDOG_UNIT_DIR=self.units, TMUX_BOOT_ID_FILE=self.boot_id),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("lacks tl_pane_agents", res.stderr)
        self.assertTrue(any("lacks tl_pane_agents" in a for a in self.alerts()))

    # ── alert rate limiting ────────────────────────────────────────────────────────────────────────────────────────────
    def test_a_persistent_problem_alerts_once_per_interval(self):
        self.write_rig_conf(save=False, restore=False)
        for _ in range(3):
            self.assertEqual(self.watchdog().returncode, 1)
        keys = os.listdir(os.path.join(self.state, "alerts"))
        self.assertTrue(keys)
        # one alert per key, however often the timer fires
        self.assertEqual(len(self.alerts()), len(keys))

    def test_two_runs_at_once_do_not_overlap(self):
        write_exec(self.saver, "#!/bin/sh\nsleep 2\n")
        self.age_every_sign_of_a_save()
        first = self.popen(
            "tmux-watchdog", TMUX_WATCHDOG_RIG_CONF=self.rig_conf, TMUX_WATCHDOG_UNIT_DIR=self.units,
            TMUX_BOOT_ID_FILE=self.boot_id, TMUX_SAVE_NOW_SAVER=self.saver,
        )
        time.sleep(0.7)
        second = self.watchdog()
        self.assertIn("another run is in progress", second.stdout)
        first.communicate(timeout=30)

    # ── server recovery: off by default ────────────────────────────────────────────────────────────────────────────────
    def kill_server(self):
        self.drop("server")

    def test_a_missing_server_is_a_problem_and_nothing_is_started_by_default(self):
        self.kill_server()
        with open(os.path.join(self.state, "boot-id"), "w") as fh:
            fh.write("boot-1\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertIn("automatic recovery is off", res.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("rig ")])
        self.assertFalse([c for c in self.calls() if c.startswith("tmux new-session")])

    def test_recovery_switch_subcommands(self):
        self.assertIn("OFF", self.watchdog("recovery", "status").stdout)
        self.assertIn("ON", self.watchdog("recovery", "on").stdout)
        self.assertIn("ON", self.watchdog("recovery", "status").stdout)
        self.assertIn("OFF", self.watchdog("recovery", "off").stdout)
        self.assertEqual(self.watchdog("recovery", "sideways").returncode, 2)
        self.assertEqual(self.watchdog("restart").returncode, 2)

    def test_with_recovery_on_a_server_that_died_this_boot_goes_through_rig_omp_watchdog(self):
        self.watchdog("recovery", "on")
        self.kill_server()
        with open(os.path.join(self.state, "boot-id"), "w") as fh:
            fh.write("boot-1\n")
        self.touch("rig-starts-server")
        res = self.watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        rig_calls = [c for c in self.calls() if c.startswith("rig omp watchdog")]
        self.assertTrue(rig_calls, self.calls())
        self.assertIn("--boot-script " + os.path.join(MODULE, "tmux-recover"), rig_calls[-1])
        self.assertIn("--socket", rig_calls[-1])
        self.assertIn("healed: the tmux server had died", res.stdout)
        self.assertTrue(any("tmux server had died" in a for a in self.alerts()))

    def test_rig_gets_an_lsof_that_keeps_its_warnings_to_itself(self):
        # rig refuses to recover when its lsof probe writes anything to stderr, and lsof warns about docker overlay mounts
        self.watchdog("recovery", "on")
        self.kill_server()
        with open(os.path.join(self.state, "boot-id"), "w") as fh:
            fh.write("boot-1\n")
        self.watchdog()
        shim = [c for c in self.calls() if c.startswith("lsof-for-rig: ")][-1].split(": ", 1)[1]
        self.assertEqual(shim, os.path.join(self.state, "rig-shim", "lsof"))
        self.assertIn(" -w ", self.read(shim))

    def test_recovery_never_runs_for_a_server_tmux_boot_did_not_start_this_boot(self):
        self.watchdog("recovery", "on")
        self.kill_server()
        with open(os.path.join(self.state, "boot-id"), "w") as fh:
            fh.write("boot-0 (an earlier boot)\n")
        res = self.watchdog()
        self.assertEqual(res.returncode, 1)
        self.assertFalse([c for c in self.calls() if c.startswith("rig omp watchdog")])
        self.assertIn("has not started one this boot", res.stderr)

    def test_recovery_that_does_not_bring_a_server_up_is_a_problem(self):
        self.watchdog("recovery", "on")
        self.kill_server()
        with open(os.path.join(self.state, "boot-id"), "w") as fh:
            fh.write("boot-1\n")
        res = self.watchdog()  # the stub rig starts nothing
        self.assertEqual(res.returncode, 1)
        self.assertIn("did not start one", res.stderr)

    def test_the_environment_switch_turns_recovery_on_without_a_state_file(self):
        self.assertIn("ON", self.watchdog("recovery", "status", TMUX_WATCHDOG_RECOVER="1").stdout)


if __name__ == "__main__":
    unittest.main()
