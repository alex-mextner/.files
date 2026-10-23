"""modules/tmux-boot against a throwaway HOME and stub `systemctl`, `loginctl` and `tmux` on PATH.

Nothing touches the real systemd, the real tmux server or the real $HOME: the three install locations are moved
into a temp dir with TMUX_BOOT_*_DIR, and every stub keeps its state in files under that dir.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYSTEMCTL = """#!/bin/sh
# usage: systemctl --user VERB ...
echo "systemctl $*" >> "$STUB/calls.log"
shift
[ -e "$STUB/no-bus" ] && exit 1
case "$1" in
  show-environment) exit 0 ;;
  daemon-reload) exit 0 ;;
  is-enabled) if [ -e "$STUB/enabled" ]; then echo enabled; exit 0; fi; echo disabled; exit 1 ;;
  enable) : > "$STUB/enabled"; exit 0 ;;
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
  new-session) : > "$STUB/server"; echo "${FAKE_WINDOWS_AFTER_START:-1}" > "$STUB/windows" ;;
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

    def test_without_a_user_bus_install_writes_the_autostart_entry_and_the_check_accepts_it(self):
        self.touch("no-bus")
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 3)
        self.assertIn("autostart entry", self.run_script("install.sh", "--plan").stdout)

        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("no user systemd bus", res.stdout)
        self.assertTrue(os.access(os.path.join(self.bin_dir, "tmux-boot"), os.X_OK))
        with open(os.path.join(self.autostart_dir, "tmux-boot.desktop")) as fh:
            self.assertIn(f'Exec="{self.bin_dir}/tmux-boot"', fh.read())
        self.assertFalse(os.path.exists(os.path.join(self.unit_dir, "tmux-boot.service")))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

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


if __name__ == "__main__":
    unittest.main()
