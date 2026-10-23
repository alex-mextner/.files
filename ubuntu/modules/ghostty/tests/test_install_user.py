"""modules/ghostty/install-user.sh against a throwaway HOME with stub apt-get / dpkg-deb / dpkg-architecture.

Nothing here touches the real $HOME, the network or /usr: the system-package lookup is pointed at an empty
directory (GHOSTTY_SYSTEM_DIRS) and the archive tools are shell stubs that record their calls.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install-user.sh")

APT_GET = """#!/bin/sh
echo "apt-get $*" >> "$STUB_LOG"
[ "$1" = download ] || exit 1
if [ -n "${APT_FAIL:-}" ]; then echo "$APT_FAIL" >&2; exit 100; fi
shift
for p in "$@"; do : > "${p}_1.0_amd64.deb"; done
"""

DPKG_DEB = """#!/bin/sh
# dpkg-deb -x FILE DEST
echo "dpkg-deb $*" >> "$STUB_LOG"
case "$(basename "$2")" in
  ghostty_*) mkdir -p "$3/usr/bin"; printf '#!/bin/sh\\n[ -z "$STUB_BROKEN_BIN" ] || { echo "error while loading shared libraries" >&2; exit 127; }\\necho "Ghostty 1.3.0 stub"\\n' > "$3/usr/bin/ghostty"; chmod +x "$3/usr/bin/ghostty" ;;
  libgtk4-layer-shell0_*) mkdir -p "$3/usr/lib/x86_64-linux-gnu"; : > "$3/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so.0" ;;
esac
"""


def write_exe(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class GhosttyUser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stubs = os.path.join(self.tmp, "stubs")
        self.system = os.path.join(self.tmp, "system-bin")
        for d in (self.home, self.stubs, self.system):
            os.makedirs(d)
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()
        write_exe(os.path.join(self.stubs, "apt-get"), APT_GET)
        write_exe(os.path.join(self.stubs, "dpkg-deb"), DPKG_DEB)
        write_exe(os.path.join(self.stubs, "dpkg-architecture"), "#!/bin/sh\necho x86_64-linux-gnu\n")

    def run_script(self, *args, **env):
        full = {
            "HOME": self.home,
            "PATH": self.stubs + ":/usr/bin:/bin",
            "STUB_LOG": self.log,
            "GHOSTTY_SYSTEM_DIRS": self.system,
        }
        full.update(env)
        return subprocess.run(["sh", SCRIPT, *args], env=full, capture_output=True, text=True, check=False)

    def calls(self):
        with open(self.log) as fh:
            return fh.read().splitlines()

    def user_files(self):
        return {
            "wrapper": os.path.join(self.home, ".local/bin/ghostty"),
            "entry": os.path.join(self.home, ".local/share/applications/com.mitchellh.ghostty.desktop"),
            "binary": os.path.join(self.home, ".local/opt/ghostty/usr/bin/ghostty"),
        }

    def test_check_on_a_bare_machine_is_drift_and_changes_nothing(self):
        res = self.run_script("--check")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("MISSING", res.stdout)
        self.assertEqual(os.listdir(self.home), [])
        self.assertEqual(self.calls(), [])

    def test_plan_is_read_only_and_names_the_download(self):
        res = self.run_script("--plan")
        self.assertEqual(res.returncode, 0)
        self.assertIn("apt-get download ghostty libgtk4-layer-shell0", res.stdout)
        self.assertEqual(os.listdir(self.home), [])
        self.assertEqual(self.calls(), [])

    def test_install_extracts_writes_the_wrapper_and_check_then_passes(self):
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        files = self.user_files()
        for path in files.values():
            self.assertTrue(os.path.exists(path), path)
        self.assertTrue(os.access(files["wrapper"], os.X_OK))
        with open(files["wrapper"]) as fh:
            wrapper = fh.read()
        self.assertIn("LD_LIBRARY_PATH=", wrapper)
        self.assertIn(os.path.join(self.home, ".local/opt/ghostty/usr/lib/x86_64-linux-gnu"), wrapper)
        launched = subprocess.run([files["wrapper"], "--version"], capture_output=True, text=True, check=False)
        self.assertIn("Ghostty 1.3.0", launched.stdout)
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_second_run_downloads_nothing(self):
        self.run_script()
        before = self.calls()
        res = self.run_script()
        self.assertEqual(res.returncode, 0)
        self.assertIn("already installed", res.stdout)
        self.assertEqual(self.calls(), before)

    def test_a_system_ghostty_counts_as_installed(self):
        write_exe(os.path.join(self.system, "ghostty"), "#!/bin/sh\n")
        self.assertEqual(self.run_script("--check").returncode, 0)
        res = self.run_script()
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_a_stale_wrapper_is_drift_and_install_repairs_it(self):
        self.run_script()
        with open(self.user_files()["wrapper"], "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        self.assertEqual(self.run_script("--check").returncode, 3)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_a_network_failure_is_the_transient_exit_and_leaves_nothing_behind(self):
        res = self.run_script(APT_FAIL="W: Failed to fetch http://archive.ubuntu.com  Temporary failure resolving 'archive.ubuntu.com'")
        self.assertEqual(res.returncode, 75, res.stdout + res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local")))

    def test_a_missing_package_is_a_final_failure(self):
        res = self.run_script(APT_FAIL="E: Unable to locate package ghostty")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local")))

    def test_stale_package_lists_are_final_not_transient(self):
        res = self.run_script(APT_FAIL="E: Failed to fetch http://archive.ubuntu.com/ubuntu/pool/universe/g/ghostty/ghostty_1.deb  404  Not Found [IP: 1.2.3.4 80]")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("sudo apt update", res.stderr)

    def test_progress_lines_with_404_in_them_do_not_hide_a_network_failure(self):
        res = self.run_script(APT_FAIL="Get:1 http://archive.ubuntu.com ghostty amd64 1.3.0 [3,404 kB]\nE: Failed to fetch http://x  Temporary failure resolving 'archive.ubuntu.com'")
        self.assertEqual(res.returncode, 75, res.stdout + res.stderr)

    def test_an_unreachable_mirror_is_transient(self):
        res = self.run_script(APT_FAIL="E: Failed to fetch http://x  Could not connect to archive.ubuntu.com:80 - connect (101: Network is unreachable)")
        self.assertEqual(res.returncode, 75, res.stdout + res.stderr)

    def test_a_downloaded_ghostty_that_does_not_start_replaces_nothing(self):
        self.run_script()
        with open(self.user_files()["wrapper"], "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")  # drift, so install runs again
        res = self.run_script(STUB_BROKEN_BIN="1")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("does not start on this host", res.stderr)
        launched = subprocess.run([self.user_files()["binary"], "--version"], capture_output=True, text=True, check=False)
        self.assertIn("Ghostty 1.3.0", launched.stdout, "the working copy was replaced by one that does not start")

    def test_a_failed_reinstall_keeps_the_previous_copy(self):
        self.run_script()
        with open(self.user_files()["wrapper"], "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")  # makes check fail, so install tries again
        res = self.run_script(APT_FAIL="E: Unable to locate package ghostty")
        self.assertEqual(res.returncode, 1)
        self.assertTrue(os.path.exists(self.user_files()["binary"]), "the extracted copy was lost by a failed run")

    def test_usage_error(self):
        self.assertEqual(self.run_script("--frobnicate").returncode, 2)


if __name__ == "__main__":
    unittest.main()
