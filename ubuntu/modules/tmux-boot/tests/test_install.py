"""modules/tmux-boot against a throwaway HOME and stub `systemctl`, `loginctl` and `tmux` on PATH.

Nothing touches the real systemd, the real tmux server or the real $HOME: the three install locations are moved
into a temp dir with TMUX_BOOT_*_DIR, and every stub keeps its state in files under that dir.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYSTEMCTL = """#!/bin/sh
# usage: systemctl --user VERB ...; tmux-boot.service's state is the file `enabled`, tmux-autosave.timer's `timer-enabled`
# and `timer-active`
echo "systemctl $*" >> "$STUB/calls.log"
shift
[ -e "$STUB/no-bus" ] && exit 1
case "$1" in
  show-environment) exit 0 ;;
  daemon-reload) exit 0 ;;
  is-enabled)
    case "$2" in tmux-autosave.timer) f=timer-enabled ;; *) f=enabled ;; esac
    if [ -e "$STUB/$f" ]; then echo enabled; exit 0; fi; echo disabled; exit 1 ;;
  is-active) [ -e "$STUB/timer-active" ]; exit $? ;;
  enable)
    for a in "$@"; do
      case "$a" in
        tmux-boot.service) : > "$STUB/enabled" ;;
        tmux-autosave.timer) : > "$STUB/timer-enabled" ;;
        --now) : > "$STUB/timer-active" ;;
      esac
    done
    exit 0 ;;
esac
exit 0
"""

LOGINCTL = """#!/bin/sh
echo "loginctl $*" >> "$STUB/calls.log"
case "$1" in
  show-user) if [ -e "$STUB/linger" ]; then echo yes; else echo no; fi ;;
  enable-linger) [ -e "$STUB/linger-denied" ] && exit 1; : > "$STUB/linger" ;;
esac
exit 0
"""

# A stateful tmux: `new-session -d` starts the server with FAKE_WINDOWS_AFTER_START windows (3 = continuum restored).
TMUX = """#!/bin/sh
echo "tmux $*" >> "$STUB/calls.log"
case "$1" in
  list-sessions) [ -e "$STUB/server" ]; exit $? ;;
  new-session)
    # FAKE_NEW_SESSION=duplicate: continuum restored first, the server is up and `main` exists; =fail: no server comes up
    case "${FAKE_NEW_SESSION:-}" in
      fail) echo "no server" >&2; exit 1 ;;
      duplicate) : > "$STUB/server"; echo "${FAKE_WINDOWS_AFTER_START:-1}" > "$STUB/windows"; echo "duplicate session: $4" >&2; exit 1 ;;
    esac
    : > "$STUB/server"; echo "${FAKE_WINDOWS_AFTER_START:-1}" > "$STUB/windows" ;;
  has-session) [ -e "$STUB/server" ]; exit $? ;;
  list-windows) n=$(cat "$STUB/windows" 2>/dev/null || echo 0); i=0; while [ "$i" -lt "$n" ]; do echo "w$i"; i=$((i+1)); done ;;
  run-shell) "$2" ;;
  show-option)
    case "$3" in
      @resurrect-dir) echo "$FAKE_RESURRECT_DIR" ;;
      @resurrect-restore-script-path) echo "$FAKE_RESTORE_SCRIPT" ;;
    esac ;;
esac
exit 0
"""

RESTORE = """#!/bin/sh
echo "restore.sh ran" >> "$STUB/calls.log"
"""


def write_exec(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stub = os.path.join(self.tmp, "stub")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        for d in (self.home, self.stub, self.fakebin):
            os.makedirs(d)
        write_exec(os.path.join(self.fakebin, "systemctl"), SYSTEMCTL)
        write_exec(os.path.join(self.fakebin, "loginctl"), LOGINCTL)
        write_exec(os.path.join(self.fakebin, "tmux"), TMUX)
        self.resurrect = os.path.join(self.tmp, "resurrect")
        os.makedirs(self.resurrect)
        self.restore = os.path.join(self.tmp, "restore.sh")
        write_exec(self.restore, RESTORE)
        self.bin_dir = os.path.join(self.home, ".local", "bin")
        self.unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        self.autostart_dir = os.path.join(self.home, ".config", "autostart")

    def env(self, **extra):
        return {
            **os.environ,
            "HOME": self.home,
            "STUB": self.stub,
            # the stubs come first; /usr/bin and /bin stay for coreutils, cmp, wc, sleep
            "PATH": f"{self.fakebin}:/usr/bin:/bin",
            "TMUX_BOOT_BIN_DIR": self.bin_dir,
            "TMUX_BOOT_UNIT_DIR": self.unit_dir,
            "TMUX_BOOT_AUTOSTART_DIR": self.autostart_dir,
            "FAKE_RESURRECT_DIR": self.resurrect,
            "FAKE_RESTORE_SCRIPT": self.restore,
            **extra,
        }

    def run_script(self, script, *args, **extra):
        return subprocess.run(
            ["bash", os.path.join(MODULE, script), *args],
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self):
        path = os.path.join(self.stub, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def touch(self, name):
        with open(os.path.join(self.stub, name), "w"):
            pass


class Installer(Base):
    def install_ok(self):
        self.touch("linger")
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_check_before_install_is_drift_and_writes_nothing(self):
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("tmux-boot is missing", res.stdout)
        self.assertIn("tmux-boot.service is missing", res.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_plan_lists_the_steps_and_writes_nothing(self):
        res = self.run_script("install.sh", "--plan")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("tmux-boot.service", res.stdout)
        self.assertIn("systemctl --user daemon-reload", res.stdout)
        self.assertIn("systemctl --user enable tmux-boot.service", res.stdout)
        self.assertIn("loginctl enable-linger", res.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_the_autosave_script_and_timer_are_installed_enabled_and_started(self):
        self.install_ok()
        self.assertTrue(os.access(os.path.join(self.bin_dir, "tmux-autosave"), os.X_OK))
        with open(os.path.join(self.unit_dir, "tmux-autosave.service")) as fh:
            self.assertIn("ExecStart=%h/.local/bin/tmux-autosave", fh.read())
        with open(os.path.join(self.unit_dir, "tmux-autosave.timer")) as fh:
            self.assertIn("OnUnitActiveSec=5min", fh.read())
        self.assertIn("systemctl --user enable --now tmux-autosave.timer", self.calls())
        # only the timer is started; tmux-boot.service stays unstarted so a running server is never touched
        self.assertNotIn("systemctl --user start tmux-boot.service", self.calls())
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_stopped_or_missing_autosave_timer_is_drift_and_install_repairs_it(self):
        self.install_ok()
        os.remove(os.path.join(self.stub, "timer-active"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-autosave.timer is not enabled and running", res.stdout)
        self.assertIn("enable --now tmux-autosave.timer", self.run_script("install.sh", "--plan").stdout)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

        os.remove(os.path.join(self.unit_dir, "tmux-autosave.timer"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-autosave.service or .timer is missing", res.stdout)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_modified_autosave_script_is_drift(self):
        self.install_ok()
        with open(os.path.join(self.bin_dir, "tmux-autosave"), "a") as fh:
            fh.write("# local edit\n")
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-autosave is missing or differs", res.stdout)

    def test_install_puts_script_and_unit_in_place_and_enables_without_starting(self):
        self.install_ok()
        self.assertTrue(os.access(os.path.join(self.bin_dir, "tmux-boot"), os.X_OK))
        with open(os.path.join(self.unit_dir, "tmux-boot.service")) as fh:
            self.assertIn("ExecStart=%h/.local/bin/tmux-boot", fh.read())
        self.assertTrue(os.path.exists(os.path.join(self.stub, "enabled")))
        self.assertNotIn("systemctl --user start tmux-boot.service", self.calls())
        self.assertNotIn("systemctl --user enable --now tmux-boot.service", self.calls())
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_second_install_changes_nothing(self):
        self.install_ok()
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        changed = [c for c in self.calls()[before:] if " enable " in c or "daemon-reload" in c or "enable-linger" in c]
        self.assertEqual(changed, [])

    def test_a_modified_script_is_drift_and_install_repairs_it(self):
        self.install_ok()
        with open(os.path.join(self.bin_dir, "tmux-boot"), "a") as fh:
            fh.write("# local edit\n")
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("differs from the module copy", res.stdout)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_linger_off_is_drift_and_a_refused_enable_is_a_manual_step(self):
        self.install_ok()
        os.remove(os.path.join(self.stub, "linger"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("linger is off", res.stdout)

        self.touch("linger-denied")
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 10)
        self.assertIn("needs manual: systemd linger is off", res.stderr)
        self.assertIn("sudo loginctl enable-linger", res.stderr)

    def test_without_tmux_install_is_a_manual_step(self):
        tools = os.path.join(self.tmp, "tools")  # the system tools the installer needs, and no tmux
        os.makedirs(tools)
        for name in ("dirname", "cmp", "id", "mkdir", "install"):
            os.symlink(shutil.which(name), os.path.join(tools, name))
        for name in ("systemctl", "loginctl"):
            os.symlink(os.path.join(self.fakebin, name), os.path.join(tools, name))
        res = subprocess.run(
            [shutil.which("bash"), os.path.join(MODULE, "install.sh")],
            env=self.env(PATH=tools),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 10)
        self.assertIn("needs manual: tmux is not installed", res.stderr)

    def test_without_a_user_bus_install_writes_the_autostart_entry_and_reports_the_missing_autosave_timer(self):
        self.touch("no-bus")
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 3)
        plan = self.run_script("install.sh", "--plan").stdout
        self.assertIn("autostart entry", plan)
        self.assertIn("tmux-autosave.timer cannot be installed here", plan)

        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("no user systemd bus", res.stdout)
        self.assertIn("tmux-autosave.timer was NOT installed", res.stderr)
        self.assertTrue(os.access(os.path.join(self.bin_dir, "tmux-boot"), os.X_OK))
        with open(os.path.join(self.autostart_dir, "tmux-boot.desktop")) as fh:
            self.assertIn(f'Exec="{self.bin_dir}/tmux-boot"', fh.read())
        self.assertFalse(os.path.exists(os.path.join(self.unit_dir, "tmux-boot.service")))
        # the autostart entry is in place, but nothing triggers the autosave: that stays visible as drift
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-autosave.timer cannot be installed", res.stdout)
        self.assertNotIn("autostart entry", res.stdout)

    def test_when_the_bus_comes_back_the_stale_autostart_entry_is_drift_and_install_removes_it(self):
        self.touch("no-bus")
        self.touch("linger")
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        os.remove(os.path.join(self.stub, "no-bus"))  # the next apply runs from the desktop session

        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-boot.service is missing", res.stdout)
        self.assertIn("stale", res.stdout)
        plan = self.run_script("install.sh", "--plan").stdout
        self.assertIn("systemctl --user enable tmux-boot.service", plan)
        self.assertIn("remove the stale", plan)

        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.autostart_dir, "tmux-boot.desktop")))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_an_installed_but_disabled_unit_is_drift(self):
        self.install_ok()
        os.remove(os.path.join(self.stub, "enabled"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-boot.service is not enabled", res.stdout)
        self.assertNotIn("is missing", res.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root can write into a read-only directory")
    def test_a_failing_unit_install_is_a_failure_and_never_falls_back_to_autostart(self):
        self.touch("linger")
        os.makedirs(self.unit_dir)
        os.chmod(self.unit_dir, 0o500)  # a unit directory that cannot be written
        self.addCleanup(os.chmod, self.unit_dir, 0o700)
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.autostart_dir, "tmux-boot.desktop")))

    def test_a_usage_error_is_exit_2(self):
        self.assertEqual(self.run_script("install.sh", "--nope").returncode, 2)


class Runtime(Base):
    def boot(self, **extra):
        return self.run_script("tmux-boot", **{"TMUX_BOOT_WAIT": "1", **extra})

    def snapshot(self, windows=3, directory=None):
        directory = directory or self.resurrect
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "tmux_resurrect_x.txt"), "w") as fh:
            fh.writelines(f"window\tmain\t{i}\tw{i}\t1\t*\tlayout\toff\n" for i in range(windows))
            fh.write("state\tmain\n")
        link = os.path.join(directory, "last")
        if not os.path.lexists(link):
            os.symlink("tmux_resurrect_x.txt", link)

    def reset_stubs(self):
        """A new boot: no server, an empty call log."""
        with open(os.path.join(self.stub, "calls.log"), "w"):
            pass
        if os.path.exists(os.path.join(self.stub, "server")):
            os.remove(os.path.join(self.stub, "server"))

    def test_a_running_server_is_left_alone(self):
        self.touch("server")
        res = self.boot()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("already running", res.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("tmux new-session")])

    def test_a_main_session_that_continuum_restored_first_is_not_a_failure(self):
        self.snapshot(windows=3)
        res = self.boot(FAKE_NEW_SESSION="duplicate", FAKE_WINDOWS_AFTER_START="3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("continuum restored", res.stdout)

    def test_a_new_session_that_fails_without_a_server_is_still_a_failure(self):
        self.snapshot(windows=3)
        res = self.boot(FAKE_NEW_SESSION="fail")
        self.assertEqual(res.returncode, 1)
        self.assertIn("tmux new-session failed", res.stderr)

    def test_a_fresh_server_is_started_with_the_canonical_session_and_continuum_restores(self):
        self.snapshot(windows=3)
        res = self.boot(FAKE_WINDOWS_AFTER_START="3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("tmux new-session -d -s main", self.calls())
        self.assertIn("continuum restored", res.stdout)
        self.assertNotIn("restore.sh ran", self.calls())

    def test_when_continuum_is_too_late_the_snapshot_is_restored_once_by_hand(self):
        self.snapshot(windows=3)
        res = self.boot(TMUX_BOOT_SESSION="work")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("tmux new-session -d -s work", self.calls())
        self.assertEqual(self.calls().count("restore.sh ran"), 1)
        self.assertIn("did not restore the 3 windows", res.stdout)

    def test_a_snapshot_of_one_window_is_never_restored_on_top_of_the_fresh_session(self):
        self.snapshot(windows=1)
        res = self.boot()  # one live window too: continuum may have restored it, or nothing was to restore
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("nothing to restore", res.stdout)
        self.assertNotIn("restore.sh ran", self.calls())

    def test_without_a_snapshot_nothing_is_restored_and_nothing_waits(self):
        res = self.boot(TMUX_BOOT_WAIT="600")  # would hang the test if the loop ran before the snapshot check
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no saved snapshot", res.stdout)
        self.assertNotIn("restore.sh ran", self.calls())

    def test_a_snapshot_with_tmux_resurrect_not_loaded_fails_loudly(self):
        self.snapshot(windows=3)
        res = self.boot(FAKE_RESTORE_SCRIPT="")
        self.assertEqual(res.returncode, 1)
        self.assertIn("tmux-resurrect is not loaded", res.stderr)

    def test_the_snapshot_directory_follows_resurrects_rules(self):
        home_dir = os.path.join(self.home, "r")
        self.snapshot(windows=3, directory=home_dir)
        for configured in ("$HOME/r", "~/r"):
            with self.subTest(configured=configured):
                self.reset_stubs()
                res = self.boot(FAKE_RESURRECT_DIR=configured)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(self.calls().count("restore.sh ran"), 1, res.stdout)

        # no @resurrect-dir: ~/.tmux/resurrect when it exists, else $XDG_DATA_HOME/tmux/resurrect
        for where, extra in (
            (os.path.join(self.home, ".tmux", "resurrect"), {}),
            (os.path.join(self.tmp, "xdg", "tmux", "resurrect"), {"XDG_DATA_HOME": os.path.join(self.tmp, "xdg")}),
        ):
            with self.subTest(default=where):
                shutil.rmtree(os.path.join(self.home, ".tmux"), ignore_errors=True)
                self.reset_stubs()
                self.snapshot(windows=3, directory=where)
                res = self.boot(FAKE_RESURRECT_DIR="", **extra)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(self.calls().count("restore.sh ran"), 1, res.stdout)

    def test_a_wait_that_is_not_a_number_is_a_usage_error(self):
        res = self.boot(TMUX_BOOT_WAIT="soon")
        self.assertEqual(res.returncode, 2)
        self.assertIn("TMUX_BOOT_WAIT", res.stderr)
        self.assertFalse(self.calls())


class Autosave(Base):
    """tmux-autosave against a stub tmux whose options live in files and whose run-shell runs the stub continuum script.

    The stub continuum saves only when the test touches `continuum-saves` (it then stamps the last-save timestamp, as the
    real one does when it launches resurrect) and the stub resurrect writes only when it also touches `saver-writes`.
    """

    CONTINUUM = """#!/bin/sh
echo "continuum_save ran" >> "$STUB/calls.log"
if [ -e "$STUB/continuum-saves" ]; then
  date +%s > "$STUB/opt-@continuum-save-last-timestamp"
  [ -e "$STUB/saver-writes" ] && : > "$FAKE_RESURRECT_DIR/tmux_resurrect_$$.txt"
fi
exit 0
"""

    TMUX_STUB = """#!/bin/sh
echo "tmux $* [TMUX=${TMUX-unset}]" >> "$STUB/calls.log"
case "$1" in
  list-sessions) [ -e "$STUB/server" ]; exit $? ;;
  show-option) cat "$STUB/opt-$3" 2>/dev/null ;;
  run-shell) sh -c "$2" ;;
esac
exit 0
"""

    def setUp(self):
        super().setUp()
        write_exec(os.path.join(self.fakebin, "tmux"), self.TMUX_STUB)
        self.continuum = os.path.join(self.tmp, "continuum_save.sh")
        write_exec(self.continuum, self.CONTINUUM)
        self.saver = os.path.join(self.tmp, "save.sh")
        write_exec(self.saver, "#!/bin/sh\n")
        self.touch("server")
        self.option("@continuum-save-interval", "15")
        self.option("@continuum-save-last-timestamp", str(int(time.time()) - 20 * 60))  # a save is due
        self.option("@resurrect-hook-post-save-all", "/x/omp-save.sh")
        self.option("@resurrect-save-script-path", self.saver)
        self.option("@resurrect-dir", self.resurrect)
        past = time.time() - 3600
        os.utime(self.resurrect, (past, past))  # last written an hour ago, so only a save in this run can touch it

    def option(self, name, value):
        with open(os.path.join(self.stub, f"opt-{name}"), "w") as fh:
            fh.write(value + "\n")

    def saves(self, writes=True):
        self.touch("continuum-saves")
        if writes:
            self.touch("saver-writes")

    def autosave(self, **extra):
        return self.run_script("tmux-autosave", TMUX_AUTOSAVE_CONTINUUM_SAVE=self.continuum, **extra)

    def test_without_a_server_there_is_nothing_to_save_and_nothing_fails(self):
        os.remove(os.path.join(self.stub, "server"))
        res = self.autosave()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no tmux server", res.stdout)
        self.assertNotIn("continuum_save ran", self.calls())

    def test_it_runs_continuums_save_entry_point_inside_the_server_and_reports_a_current_save(self):
        self.saves()
        res = self.autosave()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.calls().count("continuum_save ran"), 1)
        self.assertTrue([c for c in self.calls() if c.startswith("tmux run-shell ")])
        self.assertIn("ok, last autosave 0 min ago (every 15 min)", res.stdout)

    def test_a_save_that_continuum_skipped_because_the_interval_has_not_passed_is_fine(self):
        self.option("@continuum-save-last-timestamp", str(int(time.time()) - 600))
        res = self.autosave()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("last autosave 10 min ago", res.stdout)

    def test_a_server_where_continuum_is_not_active_is_a_loud_failure_and_continuum_is_not_run(self):
        os.remove(os.path.join(self.stub, "opt-@continuum-save-last-timestamp"))
        self.saves()
        res = self.autosave()
        self.assertEqual(res.returncode, 1)
        self.assertIn("tmux-continuum is not active in this server", res.stderr)
        # continuum's own guard against competing servers (no timestamp when another server was running at load) is kept
        self.assertNotIn("continuum_save ran", self.calls())

    def test_a_save_that_resurrect_never_wrote_is_a_loud_failure(self):
        self.saves(writes=False)  # continuum stamps its timestamp, the saver it launched writes nothing
        res = self.autosave(TMUX_AUTOSAVE_SAVE_WAIT="1")
        self.assertEqual(res.returncode, 1)
        self.assertIn("tmux-resurrect wrote nothing", res.stderr)

    def test_a_missing_resurrect_save_script_is_a_loud_failure_before_anything_runs(self):
        self.option("@resurrect-save-script-path", os.path.join(self.tmp, "gone.sh"))
        self.saves()
        res = self.autosave()
        self.assertEqual(res.returncode, 1)
        self.assertIn("save script is missing", res.stderr)
        self.assertNotIn("continuum_save ran", self.calls())

    @unittest.skipIf(os.geteuid() == 0, "root can write into a read-only directory")
    def test_an_unwritable_snapshot_directory_is_a_loud_failure_before_anything_runs(self):
        os.chmod(self.resurrect, 0o500)
        self.addCleanup(os.chmod, self.resurrect, 0o700)
        self.saves()
        res = self.autosave()
        self.assertEqual(res.returncode, 1)
        self.assertIn("cannot be written", res.stderr)
        self.assertNotIn("continuum_save ran", self.calls())

    def test_no_save_for_longer_than_the_stale_limit_is_a_loud_failure(self):
        self.option("@continuum-save-last-timestamp", str(int(time.time()) - 3600))
        res = self.autosave()
        self.assertEqual(res.returncode, 1)
        self.assertIn("the last autosave was 60 minutes ago (> 45)", res.stderr)
        self.assertEqual(self.autosave(TMUX_AUTOSAVE_STALE_MIN="90").returncode, 0)

    def test_a_longer_save_interval_raises_the_stale_limit_to_three_of_its_intervals(self):
        self.option("@continuum-save-interval", "60")
        self.option("@continuum-save-last-timestamp", str(int(time.time()) - 90 * 60))
        res = self.autosave()  # 90 minutes without a save is normal for a 60-minute interval
        self.assertEqual(res.returncode, 0, res.stderr)
        self.option("@continuum-save-last-timestamp", str(int(time.time()) - 200 * 60))
        res = self.autosave()
        self.assertEqual(res.returncode, 1)
        self.assertIn("(> 180)", res.stderr)

    def test_an_interval_that_is_not_a_number_is_a_loud_failure(self):
        self.option("@continuum-save-interval", "often")
        res = self.autosave()
        self.assertEqual(res.returncode, 1)
        self.assertIn("whole number of minutes", res.stderr)
        self.assertNotIn("continuum_save ran", self.calls())

    def test_an_interval_of_zero_means_autosave_is_off_on_purpose(self):
        self.option("@continuum-save-interval", "0")
        res = self.autosave()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("switched off on purpose", res.stdout)
        self.assertNotIn("continuum_save ran", self.calls())

    def test_a_server_that_never_loaded_rigs_hooks_warns_that_no_session_map_is_written(self):
        self.saves()
        self.option("@resurrect-hook-post-save-all", "")
        res = self.autosave()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no @resurrect-hook-post-save-all", res.stderr)

    def test_a_missing_continuum_script_fails(self):
        res = self.run_script("tmux-autosave", TMUX_AUTOSAVE_CONTINUUM_SAVE=os.path.join(self.tmp, "nope.sh"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("tmux-continuum is not installed", res.stderr)

    def test_the_caller_s_own_tmux_variable_never_redirects_it_to_another_server(self):
        self.saves()
        res = self.autosave(TMUX="/tmp/other-server,1,0")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.calls().count("continuum_save ran"), 1)
        tmux_calls = [c for c in self.calls() if c.startswith("tmux ")]
        self.assertTrue(tmux_calls)
        self.assertTrue(all(c.endswith("[TMUX=unset]") for c in tmux_calls), tmux_calls)

    def test_a_stale_limit_or_save_wait_that_is_not_a_number_is_a_usage_error(self):
        for name in ("TMUX_AUTOSAVE_STALE_MIN", "TMUX_AUTOSAVE_SAVE_WAIT"):
            with self.subTest(name=name):
                res = self.autosave(**{name: "soon"})
                self.assertEqual(res.returncode, 2)
                self.assertIn(name, res.stderr)
                self.assertFalse(self.calls())


if __name__ == "__main__":
    unittest.main()
