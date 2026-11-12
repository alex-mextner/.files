"""modules/tmux-boot against a throwaway HOME and stub `systemctl`, `loginctl` and `tmux` on PATH.

Nothing touches the real systemd, the real tmux server or the real $HOME: the three install locations are moved
into a temp dir with TMUX_BOOT_*_DIR, and every stub keeps its state in files under that dir.
"""

import os
import re
import glob
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYSTEMCTL = """#!/bin/sh
# usage: systemctl --user VERB ...; a unit's state is the files `enabled-UNIT` and `active-UNIT` in $STUB
echo "systemctl $*" >> "$STUB/calls.log"
shift
[ -e "$STUB/no-bus" ] && exit 1
case "$1" in
  daemon-reload) [ -e "$STUB/fail-reload" ] && exit 1 ;;
  try-restart) : > "$STUB/active-$2" 2>/dev/null ;;
  is-enabled)
    if [ -e "$STUB/enabled-$2" ]; then echo enabled; exit 0; fi; echo disabled; exit 1 ;;
  is-active) for last; do :; done; [ -e "$STUB/active-$last" ]; exit $? ;;
  enable)
    now=0
    for a in "$@"; do [ "$a" = --now ] && now=1; done
    for a in "$@"; do
      case "$a" in
        tmux-*) [ -e "$STUB/fail-enable-$a" ] && exit 1; : > "$STUB/enabled-$a"; [ "$now" = 1 ] && : > "$STUB/active-$a" ;;
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
            # the installer writes state (retry/guard markers): keep it inside the throwaway HOME even when the
            # runner exports XDG_STATE_HOME or TMUX_BOOT_STATE_DIR
            "TMUX_BOOT_STATE_DIR": os.path.join(self.home, ".local", "state", "tmux-boot"),
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
        os.remove(os.path.join(self.stub, "active-tmux-autosave.timer"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-autosave.timer is not enabled and running", res.stdout)
        self.assertIn("enable --now tmux-autosave.timer", self.run_script("install.sh", "--plan").stdout)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

        os.remove(os.path.join(self.unit_dir, "tmux-autosave.timer"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-autosave.timer is missing", res.stdout)
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
        self.assertTrue(os.path.exists(os.path.join(self.stub, "enabled-tmux-boot.service")))
        self.assertNotIn("systemctl --user start tmux-boot.service", self.calls())
        self.assertNotIn("systemctl --user enable --now tmux-boot.service", self.calls())
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_second_install_changes_nothing(self):
        self.install_ok()
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        changed = [c for c in self.calls()[before:] if " enable " in c or "daemon-reload" in c or "enable-linger" in c or "try-restart" in c]
        self.assertEqual(changed, [], "a second install must not enable, reload, linger or restart anything")

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
        os.remove(os.path.join(self.stub, "enabled-tmux-boot.service"))
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

    def test_an_install_section_is_recognised_as_the_first_line_and_with_blanks(self):
        module = os.path.join(self.tmp, "module")
        shutil.copytree(MODULE, module, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        for name, text in (
            ("tmux-first.timer", "[Install]\nWantedBy=timers.target\n\n[Timer]\nOnBootSec=1min\n"),
            ("tmux-indented.timer", "[Timer]\nOnBootSec=1min\n\n  [Install]  \nWantedBy=timers.target\n"),
            ("tmux-nonewline.timer", "[Timer]\nOnBootSec=1min\n[Install]"),
            ("tmux-comment.timer", "[Timer]\nOnBootSec=1min\n# [Install] is for documentation only\n"),
            ("tmux-quoted.timer", "[Timer]\nDescription=see [Install]\n"),
        ):
            with open(os.path.join(module, name), "w") as fh:
                fh.write(text)
        self.touch("linger")
        res = subprocess.run(["bash", os.path.join(module, "install.sh")], env=self.env(), capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for name in ("tmux-first.timer", "tmux-indented.timer", "tmux-nonewline.timer"):
            self.assertIn(f"systemctl --user enable --now {name}", self.calls(), name)
        for name in ("tmux-comment.timer", "tmux-quoted.timer"):
            self.assertNotIn(f"systemctl --user enable --now {name}", self.calls(), name)
        check = subprocess.run(["bash", os.path.join(module, "install.sh"), "--check"], env=self.env(), capture_output=True, text=True, check=False)
        self.assertEqual(check.returncode, 0, check.stdout)
        os.remove(os.path.join(self.stub, "enabled-tmux-first.timer"))
        check = subprocess.run(["bash", os.path.join(module, "install.sh"), "--check"], env=self.env(), capture_output=True, text=True, check=False)
        self.assertEqual(check.returncode, 3)
        self.assertIn("tmux-first.timer is not enabled", check.stdout)

    def test_every_layer_of_the_module_is_installed_and_the_timers_are_started(self):
        self.install_ok()
        for script in ("tmux-boot", "tmux-autosave", "tmux-lib", "tmux-save-now", "tmux-resurrect-save", "tmux-alert", "tmux-save-debounce", "tmux-watchdog", "tmux-recover"):
            self.assertTrue(os.access(os.path.join(self.bin_dir, script), os.X_OK), script)
        for unit in ("tmux-autosave.timer", "tmux-watchdog.timer"):
            self.assertIn(f"systemctl --user enable --now {unit}", self.calls())
        for unit in ("tmux-autosave.service", "tmux-watchdog.service"):  # oneshots fired by their timers: copied, never enabled
            self.assertTrue(os.path.exists(os.path.join(self.unit_dir, unit)), unit)
            self.assertNotIn(f"systemctl --user enable {unit}", self.calls())
        with open(os.path.join(self.unit_dir, "tmux-watchdog.service")) as fh:
            self.assertIn("ExecStart=%h/.local/bin/tmux-watchdog", fh.read())
        with open(os.path.join(self.unit_dir, "tmux-watchdog.service")) as fh:
            # recovery starts the server inside this unit's cgroup: its end must not kill the restored server
            self.assertIn("KillMode=process", fh.read())
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_the_library_is_installed_first_and_a_failed_copy_of_it_leaves_the_scripts_alone(self):
        self.install_ok()
        real_install = shutil.which("install")
        write_exec(
            os.path.join(self.fakebin, "install"),
            f'#!/bin/sh\nfor last; do :; done\necho "install $last" >> "$STUB/install.log"\n'
            f'case "$last" in */tmux-lib) [ -e "$STUB/lib-fails" ] && exit 1 ;; esac\nexec {real_install} "$@"\n',
        )
        module = os.path.join(self.tmp, "module")
        shutil.copytree(MODULE, module, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        for name in ("tmux-lib", "tmux-boot", "tmux-autosave"):  # a new version of everything that is installed
            with open(os.path.join(module, name), "a") as fh:
                fh.write("# newer\n")
        self.touch("lib-fails")
        run = lambda: subprocess.run(["bash", os.path.join(module, "install.sh")], env=self.env(), capture_output=True, text=True, check=False)
        res = run()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        with open(os.path.join(self.stub, "install.log")) as fh:
            copied = fh.read().splitlines()
        self.assertEqual(copied, [f"install {self.bin_dir}/tmux-lib"], "the failed library copy is the only attempt")
        for name in ("tmux-boot", "tmux-autosave"):
            with open(os.path.join(self.bin_dir, name)) as fh:
                self.assertNotIn("# newer", fh.read(), name)
        os.remove(os.path.join(self.stub, "lib-fails"))
        os.remove(os.path.join(self.stub, "install.log"))
        self.assertEqual(run().returncode, 0)
        with open(os.path.join(self.stub, "install.log")) as fh:
            order = [line.split("/")[-1] for line in fh.read().splitlines() if "/tmux-" in line and "." not in line.split("/")[-1]]
        self.assertEqual(order[0], "tmux-lib", order)

    def test_the_shared_library_is_installed_next_to_the_scripts_it_serves(self):
        self.install_ok()
        self.assertTrue(os.access(os.path.join(self.bin_dir, "tmux-lib"), os.X_OK))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_new_scripts_and_units_dropped_into_the_module_are_found_by_glob(self):
        module = os.path.join(self.tmp, "module")
        shutil.copytree(MODULE, module, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        write_exec(os.path.join(module, "tmux-extra"), "#!/bin/sh\n")
        with open(os.path.join(module, "tmux-noexec"), "w") as fh:  # committed without the exec bit: a script too
            fh.write("#!/bin/sh\n")
        with open(os.path.join(module, "tmux-extra.timer"), "w") as fh:
            fh.write("[Timer]\nOnBootSec=1min\n\n[Install]\nWantedBy=timers.target\n")
        # a service that must run (what the planned tmux-shutdown-* units look like): enabled AND started
        with open(os.path.join(module, "tmux-extra.service"), "w") as fh:
            fh.write("[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/true\n\n[Install]\nWantedBy=default.target\n")
        with open(os.path.join(module, "tmux-helper.service"), "w") as fh:  # a oneshot fired by a timer: copied, not enabled
            fh.write("[Service]\nType=oneshot\nExecStart=/bin/true\n")
        self.touch("linger")
        run = lambda *a: subprocess.run(["bash", os.path.join(module, "install.sh"), *a], env=self.env(), capture_output=True, text=True, check=False)
        self.assertEqual(run("--check").returncode, 3)
        plan = run("--plan").stdout
        self.assertIn("tmux-extra ->", plan)
        self.assertIn("tmux-noexec ->", plan)
        self.assertIn("systemctl --user enable --now tmux-extra.timer", plan)
        self.assertIn("systemctl --user enable --now tmux-extra.service", plan)
        res = run()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.access(os.path.join(self.bin_dir, "tmux-extra"), os.X_OK))
        self.assertTrue(os.access(os.path.join(self.bin_dir, "tmux-noexec"), os.X_OK), "install -m 755 sets the mode")
        for unit in ("tmux-extra.timer", "tmux-extra.service", "tmux-helper.service"):
            self.assertTrue(os.path.exists(os.path.join(self.unit_dir, unit)), unit)
        self.assertIn("systemctl --user enable --now tmux-extra.timer", self.calls())
        self.assertIn("systemctl --user enable --now tmux-extra.service", self.calls())
        self.assertNotIn("systemctl --user enable tmux-extra.service", self.calls())
        self.assertNotIn("systemctl --user enable tmux-helper.service", self.calls())
        self.assertEqual(run("--check").returncode, 0)
        before = len(self.calls())
        self.assertEqual(run().returncode, 0)
        self.assertEqual([c for c in self.calls()[before:] if " enable " in c or "daemon-reload" in c], [])

    def test_an_unrecognised_module_file_is_never_silently_skipped(self):
        module = os.path.join(self.tmp, "module")
        shutil.copytree(MODULE, module, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        with open(os.path.join(module, "tmux-stray.path"), "w") as fh:  # a unit kind the installer has no home for
            fh.write("[Path]\nPathExists=/tmp\n")
        run = lambda *a: subprocess.run(["bash", os.path.join(module, "install.sh"), *a], env=self.env(), capture_output=True, text=True, check=False)
        check = run("--check")
        self.assertEqual(check.returncode, 3, check.stdout + check.stderr)
        self.assertIn("tmux-stray.path", check.stdout)
        plan = run("--plan")
        self.assertIn("tmux-stray.path", plan.stdout + plan.stderr)
        res = run()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("tmux-stray.path", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.unit_dir, "tmux-stray.path")))

    def test_a_failing_auxiliary_unit_does_not_block_the_boot_unit(self):
        # glob order enables tmux-autosave.timer before tmux-boot.service: a broken timer must not cost the boot path
        self.touch("linger")
        self.touch("fail-enable-tmux-autosave.timer")
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.stub, "enabled-tmux-boot.service")))

    def test_module_toml_claims_exactly_the_assets_the_module_ships(self):
        # every coverage block names its assets explicitly (a glob would also claim leftovers and unrelated tmux-*
        # files as tracked), so the lists must grow with the module: this is the test that fails when they do not
        with open(os.path.join(MODULE, "module.toml")) as fh:
            text = fh.read()
        claimed = set(re.findall(r'"((?:unit|file):[^"]+)"', text))
        expected = {"file:~/.config/autostart/tmux-boot.desktop"}  # the failover entry (not found by the globs)
        for name in os.listdir(MODULE):
            if not name.startswith("tmux-"):
                continue
            if name.endswith((".service", ".timer")):
                expected.add(f"unit:{name}")
            elif "." not in name:
                expected.add(f"file:~/.local/bin/{name}")
        # one level only, exactly like the installer's own glob (install.sh: systemd-dropins/*/*.conf)
        for path in glob.glob(os.path.join(MODULE, "systemd-dropins", "*", "*.conf")):
            rel = os.path.relpath(path, MODULE).replace("systemd-dropins/", "", 1)
            expected.add("file:~/.config/systemd/user/" + rel)
        self.assertEqual(claimed, expected)

    def test_a_script_whose_library_is_missing_fails_loudly(self):
        # tmux-lib is a hard dependency of both: a partial install must not pass silently at boot time
        lonely = os.path.join(self.tmp, "lonely")
        os.makedirs(lonely)
        for script in ("tmux-boot", "tmux-autosave"):
            shutil.copy(os.path.join(MODULE, script), lonely)
            res = subprocess.run(["bash", os.path.join(lonely, script)], env=self.env(), capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 1, script)
            self.assertIn("cannot load", res.stderr)


class ShutdownWiring(Base):
    """The shutdown save assets (watcher, save unit, selfcheck, scope drop-ins) come out of the installer."""

    SHUTDOWN_UNITS = ("tmux-shutdown-save.service", "tmux-shutdown-watch.service")
    DROPINS = ("tmux-spawn-.scope.d/10-tmux-shutdown-save.conf", "omp-.scope.d/10-tmux-shutdown-save.conf")

    def install_ok(self):
        self.touch("linger")
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_scripts_units_and_dropins_are_deployed(self):
        self.install_ok()
        for s in ("tmux-shutdown-save", "tmux-shutdown-watch", "tmux-boot-selfcheck"):
            self.assertTrue(os.access(os.path.join(self.bin_dir, s), os.X_OK), s)
        for u in self.SHUTDOWN_UNITS + ("tmux-boot-selfcheck.service", "tmux-boot-selfcheck.timer"):
            self.assertTrue(os.path.exists(os.path.join(self.unit_dir, u)), u)
        for d in self.DROPINS:
            with open(os.path.join(MODULE, "systemd-dropins", d)) as want, open(os.path.join(self.unit_dir, d)) as got:
                self.assertEqual(got.read(), want.read(), d)
        self.assertIn("systemctl --user daemon-reload", self.calls())
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_the_shutdown_services_are_started_the_boot_service_is_not(self):
        self.install_ok()
        calls = self.calls()
        for u in self.SHUTDOWN_UNITS:
            self.assertIn("systemctl --user enable --now " + u, calls)
        self.assertIn("systemctl --user enable --now tmux-boot-selfcheck.timer", calls)
        self.assertIn("systemctl --user enable tmux-boot.service", calls)
        self.assertNotIn("systemctl --user enable --now tmux-boot.service", calls)
        # nothing is stopped or restarted, and installing never talks to tmux (a running server stays untouched)
        self.assertEqual([c for c in calls if " stop " in c or " restart " in c or " kill " in c], [])
        self.assertEqual([c for c in calls if c.startswith("tmux ")], [])

    def test_a_disabled_shutdown_service_is_drift_and_an_enabled_active_one_is_ok(self):
        self.install_ok()
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)
        for u in self.SHUTDOWN_UNITS:
            os.remove(os.path.join(self.stub, "enabled-" + u))
            os.remove(os.path.join(self.stub, "active-" + u))
            res = self.run_script("install.sh", "--check")
            self.assertEqual(res.returncode, 3, u)
            self.assertIn("%s is not enabled and running (systemctl --user enable --now %s)" % (u, u), res.stdout)
            self.assertIn("enable --now " + u, self.run_script("install.sh", "--plan").stdout)
            self.assertEqual(self.run_script("install.sh").returncode, 0)
            self.assertEqual(self.run_script("install.sh", "--check").returncode, 0, u)

    def test_an_enabled_but_stopped_shutdown_service_is_drift(self):
        # enabled without running protects nothing until the next boot
        self.install_ok()
        os.remove(os.path.join(self.stub, "active-tmux-shutdown-watch.service"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("tmux-shutdown-watch.service is not enabled and running", res.stdout)

    def test_a_missing_or_modified_dropin_is_drift_and_install_repairs_it(self):
        self.install_ok()
        for d in self.DROPINS:
            path = os.path.join(self.unit_dir, d)
            os.remove(path)
            res = self.run_script("install.sh", "--check")
            self.assertEqual(res.returncode, 3, d)
            self.assertIn(d + " is missing or differs", res.stdout)
            before = len(self.calls())
            self.assertEqual(self.run_script("install.sh").returncode, 0)
            self.assertIn("systemctl --user daemon-reload", self.calls()[before:])
            with open(path, "a") as fh:
                fh.write("# local edit\n")
            self.assertEqual(self.run_script("install.sh", "--check").returncode, 3)
            self.assertEqual(self.run_script("install.sh").returncode, 0)
            self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_second_install_changes_nothing(self):
        self.install_ok()
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual([c for c in self.calls()[before:] if " enable " in c or "daemon-reload" in c or "try-restart" in c], [])

    def test_plan_names_the_dropins_and_writes_nothing(self):
        res = self.run_script("install.sh", "--plan")
        self.assertEqual(res.returncode, 0, res.stderr)
        for d in self.DROPINS:
            self.assertIn(os.path.join(self.unit_dir, d), res.stdout)
        self.assertEqual(os.listdir(self.home), [])


    def test_without_a_user_bus_plan_and_install_mention_the_dropins_like_check_does(self):
        self.touch("no-bus")
        self.touch("linger")
        self.assertIn("the systemd drop-ins", self.run_script("install.sh", "--check").stdout)
        self.assertIn("the systemd drop-ins", self.run_script("install.sh", "--plan").stdout)
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("the systemd drop-ins", res.stderr)

    def test_a_failed_daemon_reload_is_retried_by_the_next_run_and_reported_as_drift(self):
        # run 1 writes the drop-ins but the reload fails on a transient bus error; without a marker the next run
        # would find the files identical and the running scopes would never get the ordering
        self.install_ok()
        os.remove(os.path.join(self.unit_dir, self.DROPINS[0]))
        self.touch("fail-reload")
        self.assertEqual(self.run_script("install.sh").returncode, 1)
        marker = os.path.join(self.home, ".local", "state", "tmux-boot", "daemon-reload-pending")
        self.assertTrue(os.path.exists(marker), "a failed reload must leave the retry marker")
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("daemon-reload did not finish", res.stdout)
        self.assertIn("systemctl --user daemon-reload", self.run_script("install.sh", "--plan").stdout)
        os.remove(os.path.join(self.stub, "fail-reload"))
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertIn("systemctl --user daemon-reload", self.calls()[before:])
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_an_update_of_the_watcher_script_restarts_the_running_service(self):
        # `install` unlinks the old script, so a running watcher would keep the old code until a reboot
        self.install_ok()
        with open(os.path.join(self.bin_dir, "tmux-shutdown-watch"), "a") as fh:
            fh.write("# local edit\n")
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertIn("systemctl --user try-restart tmux-shutdown-watch.service", self.calls()[before:])
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_watcher_update_interrupted_by_a_failed_reload_is_restarted_by_the_next_run(self):
        # the restart must survive a run that fails part-way, like the daemon-reload retry does
        self.install_ok()
        with open(os.path.join(self.bin_dir, "tmux-shutdown-watch"), "a") as fh:
            fh.write("# local edit\n")
        os.remove(os.path.join(self.unit_dir, self.DROPINS[0]))  # a drop-in change forces the reload that fails
        self.touch("fail-reload")
        self.assertEqual(self.run_script("install.sh").returncode, 1)
        marker = os.path.join(self.home, ".local", "state", "tmux-boot", "watch-restart-pending")
        self.assertTrue(os.path.exists(marker), "an interrupted watcher update must leave the restart marker")
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("try-restart tmux-shutdown-watch.service", self.run_script("install.sh", "--plan").stdout)
        os.remove(os.path.join(self.stub, "fail-reload"))
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertIn("systemctl --user try-restart tmux-shutdown-watch.service", self.calls()[before:])
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_an_unchanged_watcher_is_never_restarted(self):
        self.install_ok()
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertNotIn("try-restart", " ".join(self.calls()[before:]))

    def test_a_selfcheck_timer_first_started_by_install_is_guarded_for_its_first_fire(self):
        # OnBootSec=10min: `enable --now` can fire the timer at once (a machine up past that), and that fire would
        # judge a previous boot this module never covered. The guard is armed BEFORE the enable, whatever the
        # uptime, and carries the boot id.
        self.install_ok()
        marker = os.path.join(self.home, ".local", "state", "tmux-boot", "selfcheck-first-fire")
        with open("/proc/sys/kernel/random/boot_id") as fh:
            boot_id = fh.read().strip()
        with open(marker) as fh:
            self.assertEqual(fh.read().strip(), boot_id, "the guard names the boot it was armed in")
        os.remove(marker)
        self.assertEqual(self.run_script("install.sh").returncode, 0)  # the timer is already active: no re-arm
        self.assertFalse(os.path.exists(marker))

    def test_without_a_user_bus_the_dropins_are_reported_as_not_installable(self):
        self.touch("no-bus")
        self.touch("linger")
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("the systemd drop-ins", res.stdout)


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
        """A new boot (a new day for the restore-loop guard): no server, an empty call log."""
        shutil.rmtree(os.path.join(self.home, ".local", "state"), ignore_errors=True)
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

    def test_the_snapshot_directory_expands_hostname_like_resurrect(self):
        host = subprocess.run(["hostname"], capture_output=True, text=True, check=True).stdout.strip()
        self.snapshot(windows=3, directory=os.path.join(self.home, "r", host))
        res = self.boot(FAKE_RESURRECT_DIR="~/r/$HOSTNAME")
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

    def test_a_snapshot_directory_created_in_the_same_second_proves_nothing(self):
        # a directory that is new, and whose mtime is not older than this run's start, while the saver writes nothing
        shutil.rmtree(self.resurrect)
        os.makedirs(self.resurrect)
        future = time.time() + 5
        os.utime(self.resurrect, (future, future))
        self.saves(writes=False)
        res = self.autosave(TMUX_AUTOSAVE_SAVE_WAIT="1")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("tmux-resurrect wrote nothing", res.stderr)

    def test_a_symlinked_snapshot_directory_is_judged_by_its_target(self):
        link = os.path.join(self.tmp, "resurrect-link")
        os.symlink(self.resurrect, link)
        past = time.time() - 3600
        os.utime(link, (past, past), follow_symlinks=False)
        self.option("@resurrect-dir", link)
        self.saves()
        res = self.autosave(FAKE_RESURRECT_DIR=link)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.calls().count("continuum_save ran"), 1)
        os.remove(os.path.join(self.stub, "saver-writes"))
        self.option("@continuum-save-last-timestamp", str(int(time.time()) - 20 * 60))
        res = self.autosave(FAKE_RESURRECT_DIR=link, TMUX_AUTOSAVE_SAVE_WAIT="1")
        self.assertEqual(res.returncode, 1, "a save that wrote nothing must still fail behind a symlink")

    def test_the_save_wait_is_the_full_configured_time_whatever_the_fraction_of_the_second(self):
        # the save lands 0.9 s after the trigger and the allowance is 1 s: a whole-second deadline taken at x.9 would
        # expire at (x+1).0 already, 0.1 s into the allowance
        write_exec(
            self.continuum,
            '#!/bin/sh\necho "continuum_save ran" >> "$STUB/calls.log"\ndate +%s > "$STUB/opt-@continuum-save-last-timestamp"\n'
            '( sleep 0.9; : > "$FAKE_RESURRECT_DIR/tmux_resurrect_late.txt" ) >/dev/null 2>&1 </dev/null &\nexit 0\n',
        )
        while time.time() % 1 < 0.85:  # start late in a second
            time.sleep(0.01)
        res = self.autosave(TMUX_AUTOSAVE_SAVE_WAIT="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_the_snapshot_directory_follows_resurrects_expansion_of_home_tilde_and_hostname(self):
        host = subprocess.run(["hostname"], capture_output=True, text=True, check=True).stdout.strip()
        target = os.path.join(self.home, "rr", host)
        for configured in ("~/rr/$HOSTNAME", "$HOME/rr/$HOSTNAME"):
            with self.subTest(configured=configured):
                shutil.rmtree(os.path.join(self.home, "rr"), ignore_errors=True)
                os.makedirs(target)
                past = time.time() - 3600
                os.utime(target, (past, past))
                self.option("@resurrect-dir", configured)
                self.option("@continuum-save-last-timestamp", str(int(time.time()) - 20 * 60))
                self.saves()
                res = self.autosave(FAKE_RESURRECT_DIR=target)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertFalse(os.path.exists(os.path.join(self.home, "rr", "$HOSTNAME")))

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

    def test_numbers_with_a_leading_zero_are_decimal_and_never_skip_the_save_check(self):
        # "08" and "09" are no valid octal numbers: bash arithmetic on them fails, which used to skip the verification
        # and exit 0 ("010" is valid octal and proved nothing, so it is not here)
        for wait in ("08", "09"):
            with self.subTest(wait=wait):
                self.option("@continuum-save-last-timestamp", str(int(time.time()) - 20 * 60))
                self.saves(writes=False)
                res = self.autosave(TMUX_AUTOSAVE_SAVE_WAIT=wait)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertIn("tmux-resurrect wrote nothing", res.stderr)
                self.assertNotIn("invalid", res.stderr)
        os.remove(os.path.join(self.stub, "continuum-saves"))  # continuum does not save this time: the interval has not passed
        self.option("@continuum-save-interval", "08")
        self.option("@continuum-save-last-timestamp", str(int(time.time()) - 600))
        res = self.autosave(TMUX_AUTOSAVE_STALE_MIN="09")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("invalid", res.stderr)

    def test_a_stale_limit_or_save_wait_that_is_not_a_number_is_a_usage_error(self):
        for name in ("TMUX_AUTOSAVE_STALE_MIN", "TMUX_AUTOSAVE_SAVE_WAIT"):
            with self.subTest(name=name):
                res = self.autosave(**{name: "soon"})
                self.assertEqual(res.returncode, 2)
                self.assertIn(name, res.stderr)
                self.assertFalse(self.calls())


if __name__ == "__main__":
    unittest.main()
