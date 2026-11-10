"""modules/aisis/install.sh against a throwaway HOME and a stand-in aisis repository.

The stand-in repository holds scripts/install-systemd-units.sh, which records its arguments in $HOME/calls.log and
exits with the code in $HOME/installer.rc (default 0). `repo` points at that local repository, so nothing touches the
network, the real $HOME, the real systemd or the real aisis checkout.
"""

import json
import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install.sh")

FAKE_INSTALLER = """#!/usr/bin/env bash
echo "installer $*" >> "$HOME/calls.log"
echo "installer says: $*"
exit "$(cat "$HOME/installer.rc" 2>/dev/null || echo 0)"
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class AisisModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.upstream = os.path.join(self.tmp.name, "upstream")
        os.makedirs(os.path.join(self.upstream, "scripts"))
        installer = os.path.join(self.upstream, "scripts", "install-systemd-units.sh")
        with open(installer, "w") as handle:
            handle.write(FAKE_INSTALLER)
        os.chmod(installer, 0o755)
        git("init", "-q", "-b", "main", cwd=self.upstream)
        git("add", "-A", cwd=self.upstream)
        git("commit", "-q", "-m", "stand-in", cwd=self.upstream)
        self.checkout = os.path.join(self.home, "xp", "aisis")  # fixed: the units name %h/xp/aisis

    def run_module(self, *args, options=None):
        env = {**os.environ, "HOME": self.home}
        env.pop("XDG_CONFIG_HOME", None)
        env["UBUNTU_TWEAK_MODULE_CONFIG"] = json.dumps({"repo": self.upstream, **(options or {})})
        return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env)

    def calls(self):
        path = os.path.join(self.home, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return handle.read().splitlines()

    def test_check_without_checkout_is_drift_and_writes_nothing(self):
        result = self.run_module("--check")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("no aisis checkout", result.stdout)
        self.assertFalse(os.path.exists(self.checkout))

    def test_plan_without_checkout_is_read_only_and_succeeds(self):
        result = self.run_module("--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(self.checkout))
        self.assertEqual(self.calls(), [])

    def test_install_clones_then_runs_the_installer(self):
        result = self.run_module()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.checkout, ".git")))
        self.assertEqual(self.calls(), ["installer "])

    def test_install_keeps_an_existing_checkout_untouched(self):
        self.assertEqual(self.run_module().returncode, 0)
        marker = os.path.join(self.upstream, "new-upstream-file")
        with open(marker, "w"):
            pass
        git("add", "-A", cwd=self.upstream)
        git("commit", "-q", "-m", "later", cwd=self.upstream)
        self.assertEqual(self.run_module().returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.checkout, "new-upstream-file")))
        self.assertEqual(self.calls(), ["installer ", "installer "])

    def test_check_delegates_and_passes_exit_codes_through(self):
        self.assertEqual(self.run_module().returncode, 0)
        with open(os.path.join(self.home, "calls.log"), "w"):
            pass
        self.assertEqual(self.run_module("--check").returncode, 0)
        with open(os.path.join(self.home, "installer.rc"), "w") as handle:
            handle.write("3")
        self.assertEqual(self.run_module("--check").returncode, 3)
        with open(os.path.join(self.home, "installer.rc"), "w") as handle:
            handle.write("7")
        broken = self.run_module("--check")
        self.assertEqual(broken.returncode, 1)
        self.assertIn("failed (exit 7)", broken.stderr)
        self.assertEqual(self.calls(), ["installer --check"] * 3)

    def test_plan_delegates_to_the_installer_plan(self):
        self.assertEqual(self.run_module().returncode, 0)
        result = self.run_module("--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], "installer --plan")

    def test_checkout_without_the_installer_needs_a_manual_pull(self):
        os.makedirs(os.path.join(self.checkout, ".git"))
        result = self.run_module()
        self.assertEqual(result.returncode, 10)
        self.assertIn("predates the unit installer", result.stderr)
        self.assertEqual(self.run_module("--check").returncode, 3)

    def test_non_git_directory_in_the_way_needs_a_manual_move(self):
        os.makedirs(self.checkout)
        with open(os.path.join(self.checkout, "keep"), "w"):
            pass
        result = self.run_module()
        self.assertEqual(result.returncode, 10)
        self.assertTrue(os.path.exists(os.path.join(self.checkout, "keep")))

    def test_failed_clone_needs_a_manual_step(self):
        result = self.run_module(options={"repo": os.path.join(self.tmp.name, "nope")})
        self.assertEqual(result.returncode, 10)
        self.assertIn("git clone", result.stderr)

    def test_repo_url_credentials_never_reach_the_output(self):
        url = "https://user:SECRETTOKEN@127.0.0.1:9/aisis.git"
        for args in (("--plan",), ("--check",), ()):
            result = self.run_module(*args, options={"repo": url})
            self.assertNotIn("SECRETTOKEN", result.stdout + result.stderr)

    def test_missing_mount_stops_before_any_directory_or_clone(self):
        result = self.run_module(options={"require_mount": "~/xp"})   # a plain directory, not a mount point
        self.assertEqual(result.returncode, 10)
        self.assertIn("is not mounted", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "xp")))
        self.assertEqual(self.calls(), [])

    def test_mount_requirement_is_ignored_once_the_checkout_exists(self):
        self.assertEqual(self.run_module().returncode, 0)
        self.assertEqual(self.run_module(options={"require_mount": "~/xp"}).returncode, 0)
        self.assertEqual(self.run_module("--check", options={"require_mount": "~/xp"}).returncode, 0)

    def test_bad_options_are_rejected(self):
        self.assertEqual(self.run_module(options={"bogus": "x"}).returncode, 1)
        dir_option = self.run_module(options={"dir": "/elsewhere/aisis"})   # the checkout path is not an option
        self.assertEqual(dir_option.returncode, 1)
        self.assertIn("unknown option dir", dir_option.stderr)
        self.assertEqual(self.run_module(options={"require_mount": "relative"}).returncode, 1)


if __name__ == "__main__":
    unittest.main()
