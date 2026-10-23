"""modules/ctmux/install.sh against a throwaway HOME and a local git repo standing in for ctmux-cli.

The stand-in's install.sh records its arguments; nothing touches the real $HOME, the network or the real ctmux.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install.sh")

FAKE_INSTALL = """#!/bin/sh
echo "install.sh $*" >> "$HOME/calls.log"
[ "${1:-}" = --check ] && exit "${FAKE_CHECK_RC:-0}"
exit "${FAKE_INSTALL_RC:-0}"
"""

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


class CtmuxInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.upstream = os.path.join(self.tmp, "upstream")
        os.makedirs(self.upstream)
        with open(os.path.join(self.upstream, "install.sh"), "w") as fh:
            fh.write(FAKE_INSTALL)
        os.chmod(os.path.join(self.upstream, "install.sh"), 0o755)
        for entry in ("bin/ctmux", "linux/ctmux"):  # a ctmux checkout has both entry points
            os.makedirs(os.path.dirname(os.path.join(self.upstream, entry)), exist_ok=True)
            with open(os.path.join(self.upstream, entry), "w") as fh:
                fh.write("#!/bin/sh\n")
        env = {**os.environ, **GIT_ENV}
        subprocess.run(["git", "init", "-q", "-b", "main", self.upstream], check=True, env=env)
        subprocess.run(["git", "-C", self.upstream, "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", self.upstream, "commit", "-q", "-m", "x"], check=True, env=env)
        self.dir = os.path.join(self.home, "xp", "ctmux-cli")

    def run_script(self, *args, **extra):
        config = {"repo": self.upstream, "dir": self.dir}
        config.update(extra.pop("config", {}))
        env = {**os.environ, **GIT_ENV, "HOME": self.home, "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(config), **extra}
        return subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True, check=False)

    def calls(self):
        path = os.path.join(self.home, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def test_check_without_a_checkout_is_drift(self):
        res = self.run_script("--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("no ctmux checkout", res.stdout)
        self.assertFalse(os.path.exists(self.dir))

    def test_plan_names_the_clone_and_changes_nothing(self):
        res = self.run_script("--plan")
        self.assertEqual(res.returncode, 0)
        self.assertIn("git clone " + self.upstream, res.stdout)
        self.assertFalse(os.path.exists(self.dir))

    def test_install_clones_then_runs_the_tools_own_installer(self):
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "install.sh")))
        self.assertEqual(self.calls(), ["install.sh "])

    def test_check_asks_the_tool_and_maps_failure_to_drift(self):
        self.run_script()
        self.assertEqual(self.run_script("--check").returncode, 0)
        self.assertEqual(self.run_script("--check", FAKE_CHECK_RC="1").returncode, 3)
        self.assertEqual(self.calls()[-1], "install.sh --check")

    def test_an_existing_checkout_is_not_cloned_again(self):
        self.run_script()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.calls(), ["install.sh ", "install.sh "])

    def test_a_foreign_directory_is_a_manual_step_not_overwritten(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "mine.txt"), "w") as fh:
            fh.write("keep")
        res = self.run_script()
        self.assertEqual(res.returncode, 10)
        self.assertIn("needs manual", res.stderr)
        self.assertEqual(os.listdir(self.dir), ["mine.txt"])

    def test_installer_failure_is_passed_through(self):
        self.assertEqual(self.run_script(FAKE_INSTALL_RC="7").returncode, 7)

    def test_installer_exit_codes_the_engine_reads_as_protocol_are_reported_as_failure(self):
        for rc in ("3", "10", "75"):
            res = self.run_script(FAKE_INSTALL_RC=rc)
            self.assertEqual(res.returncode, 1, rc)

    def test_a_broken_checker_is_a_failure_not_drift(self):
        self.run_script()
        res = self.run_script("--check", FAKE_CHECK_RC="2")
        self.assertEqual(res.returncode, 1)
        self.assertIn("broken checker", res.stderr)

    def test_another_project_with_an_install_script_is_never_run(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "install.sh"), "w") as fh:
            fh.write("#!/bin/sh\necho pwned >> \"$HOME/calls.log\"\n")
        for args in ((), ("--check",), ("--plan",)):
            res = self.run_script(*args)
            self.assertEqual(res.returncode, 10, args)
        self.assertEqual(self.calls(), [])

    def test_a_missing_repository_is_final_and_never_prompts(self):
        res = self.run_script(config={"repo": os.path.join(self.tmp, "does-not-exist.git")})
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "install.sh")))

    def test_a_network_failure_while_cloning_is_the_transient_exit(self):
        res = self.run_script(config={"repo": "https://nonexistent.invalid/x/ctmux-cli.git"})
        self.assertEqual(res.returncode, 75, res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "install.sh")))

    def test_relative_dir_and_option_injection_are_refused(self):
        self.assertEqual(self.run_script(config={"dir": "relative/dir"}).returncode, 2)
        self.assertEqual(self.run_script(config={"repo": "--upload-pack=x"}).returncode, 2)


if __name__ == "__main__":
    unittest.main()
