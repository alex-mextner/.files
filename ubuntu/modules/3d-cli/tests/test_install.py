"""modules/3d-cli/install.sh against a throwaway HOME and stand-in 3d-cli checkouts.

The stand-in `bin/3d` records its arguments in $HOME/calls.log and, for `init --env`, optionally writes the client
config the real command writes after a successful install. `lib/commands/init.py` of the stand-in names `--env` or not,
which is how the module tells a new checkout from an old one. Nothing touches the real $HOME, the network, a printer
or the real 3d-cli.
"""

import importlib
import sys
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install.sh")

FAKE_3D = """#!/bin/sh
echo "3d $*" >> "$HOME/calls.log"
pwd > "$HOME/cwd.log"
if [ "$1" = init ] && [ "$2" = --env ]; then
  echo "spoolman: not configured; run: 3d init --env --spoolman ssh:USER@HOST"
  {writes_config}
  exit {init_rc}
fi
exit 64
"""

WRITES_CONFIG = (
    'mkdir -p "$HOME/.config/3d-cli" && printf "api: http://127.0.0.1:1\\ntoken: po_test\\n" '
    '> "$HOME/.config/3d-cli/printops.yaml"'
)


BAD_CONFIGS = [
    'api: ""\ntoken: po_x\n',
    "api: http://192.0.2.1:8765\ntoken: po_x\n# \uffff\n",
    "api: http://192.0.2.1:8765\n\u00a0\ntoken: po_x\n",
    "api: http://192.0.2.1:8765\ntoken: 'po_x'\u00a0\n",
    "api: http://192.0.2.1:8765\n# c\u0085token: *po_secret\n",
    "api: http://192.0.2.1:8765\n# c\u2028token: *po_secret\n",
    "api: http://192.0.2.1:8765\n# c\u2029token: *po_secret\n",
    "api: null\ntoken: po_x\n",
    "api: false\ntoken: po_x\n",
    "api: 0\ntoken: po_x\n",
    "api: true\ntoken: po_x\n",
    "api: yes\ntoken: po_x\n",
    "api: [http://192.0.2.1:8765]\ntoken: po_x\n",
    'api: "  "\ntoken: po_x\n',
    "api: http://192.0.2.1:8765\ntoken: [unfinished\n",
    "api: http://192.0.2.1:8765 #c\r[\n",
    "token: po_x\n# c\r*po_secret: [\n",
    "api: ~\ntoken: po_x\n",
    "api: [http://192.0.2.1:8765\ntoken: po_x\n",
    "- api\n- token\n",
    "api:http://192.0.2.1:8765\ntoken:po_x\n",
    "api: http://192.0.2.1:8765\ntoken: po_x\nextra: [unfinished\n",
    "api: http://192.0.2.1:8765\ntoken: po_x\nextra: 'unterminated\n",
    "api: http://192.0.2.1:8765\ntoken: po_x\nextra: *missing\n",
    "api: http://192.0.2.1:8765\ntoken:\tpo_x\n",
    "api: http://192.0.2.1:8765\ntoken: po_x\n\x07\n",
    "api: http://192.0.2.1:8765\ntoken: po_x:\n",
    "api: http://192.0.2.1:8765\ntoken: *po_secret_token\n",
    "",
]
# valid YAML that the stand-in reader refuses on purpose (it fails closed: a status report, never a bad credential)
FALLBACK_ONLY_BAD_CONFIGS = [
    "api: http://192.0.2.1:8765\ntoken: &anchor po_x\n",
    "{api: http://192.0.2.1:8765, token: po_x}\n",
    "api: 12345\ntoken: po_x\n",
    "api: http://192.0.2.1:8765\ntoken: po_x\u00a0\n",
]
GOOD_CONFIGS = [
    "api: http://192.0.2.1:8765\ntoken: po_x\n",
    "# c\napi: 'http://192.0.2.1:8765'\ntoken: \"po_x\"  # note\n",
    "api: http://192.0.2.1:8765/\ntoken: po_AbC-123_x\nextra: 1\n",
]


class ThreeDCliInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.dir = os.path.join(self.home, "xp", "3d-cli")
        self.link = os.path.join(self.home, ".local", "bin", "3d")
        self.config = os.path.join(self.home, ".config", "3d-cli", "printops.yaml")

    def make_checkout(self, *, env_init=True, init_rc=0, writes_config=False, skips_bootstrap=True):
        os.makedirs(os.path.join(self.dir, "bin"))
        os.makedirs(os.path.join(self.dir, ".git"))
        body = FAKE_3D.format(writes_config=WRITES_CONFIG if writes_config else ":", init_rc=init_rc)
        os.makedirs(os.path.join(self.dir, "lib", "commands"))
        with open(os.path.join(self.dir, "lib", "commands", "init.py"), "w") as fh:
            fh.write('HELP = "3d init [path] --name N"\n' + ('FLAG = "--env"\n' if env_init else ""))
        os.makedirs(os.path.join(self.dir, "lib", "cli"))
        with open(os.path.join(self.dir, "lib", "cli", "dispatch.py"), "w") as fh:
            fh.write("def _wants_openscad_bootstrap(resolved, rest): ...\n" if skips_bootstrap else "def main(): ...\n")
        path = os.path.join(self.dir, "bin", "3d")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def write_config(self, text):
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        with open(self.config, "w") as fh:
            fh.write(text)

    def run_script(self, *args, config=None, **extra):
        table = {"dir": self.dir}
        table.update(config or {})
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PRINTOPS_API", "PRINTOPS_TOKEN", "XDG_CONFIG_HOME")
        }
        env.update({"HOME": self.home, "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(table), **extra})
        return subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True, check=False)

    def calls(self):
        path = os.path.join(self.home, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def init_calls(self):
        return [line for line in self.calls() if line.startswith("3d init --env")]

    # -- no checkout: the module is inert -------------------------------------------------------
    def test_without_a_checkout_every_mode_is_a_no_op(self):
        for args in (("--check",), ("--plan",), ()):
            res = self.run_script(*args)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("no 3d-cli checkout", res.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local")))

    # -- an old checkout without `3d init --env`: link, skip, never run the flag ------------------
    def test_old_checkout_is_linked_and_the_env_step_is_skipped(self):
        self.make_checkout(env_init=False)
        check = self.run_script("--check")
        self.assertEqual(check.returncode, 3)
        self.assertIn("3d is missing", check.stdout)

        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("no `3d init --env` yet", res.stdout)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(os.path.join(self.dir, "bin", "3d")))
        self.assertEqual(self.init_calls(), [])
        self.assertEqual(self.run_script("--check").returncode, 0)  # nothing is left that apply could do

    # -- a checkout whose `--env` would still run the OpenSCAD library bootstrap: never run it -------
    def test_a_checkout_whose_env_init_still_bootstraps_openscad_is_not_run(self):
        self.make_checkout(skips_bootstrap=False)
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("no `3d init --env` yet", res.stdout)
        self.assertEqual(self.init_calls(), [])

    # -- a new checkout ---------------------------------------------------------------------------
    def test_unconfigured_check_is_drift_and_plan_changes_nothing(self):
        self.make_checkout()
        check = self.run_script("--check")
        self.assertEqual(check.returncode, 3)
        self.assertIn("not configured", check.stdout)
        plan = self.run_script("--plan")
        self.assertEqual(plan.returncode, 0)
        self.assertIn("init --env --no-input --spoolman auto", plan.stdout)
        self.assertFalse(os.path.exists(self.link))
        self.assertEqual(self.calls(), [])  # read-only modes never run 3d: it bootstraps itself before any output

    def test_check_and_plan_never_run_3d_in_any_state(self):
        self.make_checkout(env_init=False)
        for args in (("--check",), ("--plan",)):
            self.run_script(*args)
        self.assertEqual(self.calls(), [])

    def test_install_reports_with_the_one_non_interactive_command_then_asks_for_a_manual_step(self):
        self.make_checkout()
        res = self.run_script()
        self.assertEqual(res.returncode, 10, res.stdout + res.stderr)
        self.assertIn("needs manual", res.stderr)
        self.assertIn("3d init --env", res.stderr)
        self.assertEqual(self.init_calls(), ["3d init --env --no-input --spoolman auto"])
        self.assertTrue(os.path.islink(self.link))
        self.assertFalse(os.path.exists(self.config))  # the module itself never writes the client config

    def test_a_failing_init_is_not_fatal(self):
        self.make_checkout(init_rc=1)
        res = self.run_script()
        self.assertEqual(res.returncode, 10, res.stdout + res.stderr)
        self.assertIn("exited 1 (not fatal)", res.stderr)

    def test_install_succeeds_once_the_tool_has_configured_the_client(self):
        self.make_checkout(writes_config=True)
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_a_configured_client_is_not_touched_and_needs_no_network(self):
        self.make_checkout()
        self.write_config("api: http://192.0.2.1:8765\ntoken: po_x\n")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("already configured", res.stdout)
        self.assertEqual(self.init_calls(), [])
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_an_api_without_a_token_is_configured(self):
        # 3d init --env accepts a tokenless api (a plain Spoolman server), so the module must not loop on it
        self.make_checkout()
        self.link_3d()
        self.write_config("api: http://192.0.2.1:8765\n")
        self.assertEqual(self.run_script("--check").returncode, 0)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.init_calls(), [])

    def test_a_token_without_an_api_is_not_configured(self):
        self.make_checkout()
        self.link_3d()
        self.write_config("token: po_x\n")
        self.assertEqual(self.run_script("--check").returncode, 3)

    def link_3d(self):
        os.makedirs(os.path.dirname(self.link), exist_ok=True)
        os.symlink(os.path.join(self.dir, "bin", "3d"), self.link)

    def test_empty_null_and_malformed_values_are_not_configured(self):
        self.make_checkout()
        self.link_3d()  # with the link in place the only possible drift is the client config
        for text in BAD_CONFIGS:
            self.write_config(text)
            res = self.run_script("--check")
            self.assertEqual(res.returncode, 3, text)
            self.assertIn("not configured", res.stdout, text)

    def test_an_unparsable_file_is_never_handed_to_3d_and_never_echoed(self):
        self.make_checkout()
        self.link_3d()
        self.write_config("api: http://192.0.2.1:8765\ntoken: *po_secret_token\n")
        res = self.run_script()
        self.assertEqual(res.returncode, 10, res.stdout + res.stderr)
        self.assertIn("does not parse", res.stderr)
        self.assertNotIn("po_secret_token", res.stdout + res.stderr)
        self.assertEqual(self.init_calls(), [])

    def test_empty_quoted_value_reports_instead_of_claiming_a_parse_error(self):
        self.make_checkout(writes_config=True)
        self.link_3d()
        self.write_config('api: ""\ntoken: po_x\n')
        res = self.run_script()
        self.assertEqual(len(self.init_calls()), 1, res.stdout + res.stderr)
        self.assertNotIn("does not parse", res.stdout + res.stderr)

    def test_a_non_executable_launcher_is_drift_and_a_manual_step(self):
        self.make_checkout()
        self.link_3d()
        self.write_config(GOOD_CONFIGS[0])
        os.chmod(os.path.join(self.dir, "bin", "3d"), 0o644)
        check = self.run_script("--check")
        self.assertEqual(check.returncode, 3, check.stdout)
        self.assertIn("not executable", check.stdout)
        self.assertEqual(self.run_script().returncode, 10)

    def test_an_old_checkout_with_a_malformed_config_is_skipped_not_rejected(self):
        self.make_checkout(env_init=False)
        self.write_config("api: x\ntoken: *oops\n")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.init_calls(), [])

    def test_finish_check_and_run(self):
        self.make_checkout(writes_config=True)
        self.assertEqual(self.run_script("--finish-check").returncode, 1)
        self.assertEqual(self.run_script("--finish-check", config={"spoolman": "skip"}).returncode, 0)
        self.run_script("--finish-run")
        self.assertEqual(self.init_calls(), ["3d init --env"])
        with open(os.path.join(self.home, "cwd.log")) as fh:
            self.assertEqual(os.path.realpath(fh.read().strip()), os.path.realpath(self.home))
        self.assertEqual(self.run_script("--finish-check").returncode, 0)

    def test_finish_run_never_hands_an_unparsable_file_to_3d(self):
        self.make_checkout()
        self.write_config("api: http://192.0.2.1:8765\ntoken: *po_review_secret\n")
        res = self.run_script("--finish-run")
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("po_review_secret", res.stdout + res.stderr)
        self.assertEqual(self.init_calls(), [])

    def test_finish_run_under_dry_run_changes_nothing(self):
        self.make_checkout(writes_config=True)
        res = self.run_script("--finish-run", UBUNTU_TWEAK_FINISH_DRY_RUN="1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.init_calls(), [])

    def test_finish_run_on_an_old_checkout_does_nothing(self):
        self.make_checkout(env_init=False)
        self.assertEqual(self.run_script("--finish-run").returncode, 0)
        self.assertEqual(self.init_calls(), [])

    def test_finish_run_on_a_declined_machine_does_nothing(self):
        self.make_checkout()
        self.assertEqual(self.run_script("--finish-run", config={"spoolman": "skip"}).returncode, 0)
        self.assertEqual(self.init_calls(), [])

    def test_quoted_and_commented_values_are_configured(self):
        self.make_checkout()
        self.link_3d()
        self.write_config("# the PrintOps client\napi: 'http://192.0.2.1:8765'  # 3d-api\ntoken: \"po_x\"\n")
        res = self.run_script("--check")
        self.assertEqual(res.returncode, 0, res.stdout)

    def test_the_flat_reader_without_pyyaml_never_accepts_what_pyyaml_rejects(self):
        self.make_checkout()
        self.link_3d()
        stub = os.path.join(self.tmp, "no-yaml")
        os.makedirs(stub)
        with open(os.path.join(stub, "yaml.py"), "w") as fh:
            fh.write("raise ImportError('no PyYAML here')\n")
        good = "# c\napi: 'http://192.0.2.1:8765'\ntoken: po_x  # note\n"
        self.write_config(good)
        self.assertEqual(self.run_script("--check", PYTHONPATH=stub).returncode, 0)
        for text in BAD_CONFIGS + FALLBACK_ONLY_BAD_CONFIGS:
            self.write_config(text)
            res = self.run_script("--check", PYTHONPATH=stub)
            self.assertEqual(res.returncode, 3, text)
            self.assertIn("not configured", res.stdout, text)

    def test_the_flat_reader_is_never_more_permissive_than_pyyaml(self):
        try:
            yaml = importlib.import_module("yaml")
        except ImportError:
            self.skipTest("PyYAML is not installed: nothing to compare the stand-in reader with")
        path = os.path.join(os.path.dirname(SCRIPT), "printops_configured.py")
        spec = importlib.util.spec_from_file_location("printops_configured", path)
        module = importlib.util.module_from_spec(spec)
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)

        def configured(data):
            return isinstance(data, dict) and isinstance(data.get("api"), str) and bool(data["api"].strip())

        for text in BAD_CONFIGS + GOOD_CONFIGS:
            try:
                flat = configured(module.flat(text))
            except ValueError:
                flat = False
            try:
                real = configured(yaml.safe_load(text) or {})
            except yaml.YAMLError:
                real = False
            self.assertFalse(flat and not real, text)
        for text in GOOD_CONFIGS:
            self.assertTrue(configured(module.flat(text)), text)

    def test_a_broken_file_is_reported_even_when_the_api_comes_from_the_environment(self):
        self.make_checkout()
        self.link_3d()
        self.write_config("api: http://192.0.2.1:8765\ntoken: *oops\n")
        res = self.run_script("--check", PRINTOPS_API="http://192.0.2.1:8765")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.write_config("token: po_x\n")
        res = self.run_script(PRINTOPS_API="http://192.0.2.1:8765")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.init_calls(), [])

    def test_environment_variables_count_as_configured(self):
        self.make_checkout()
        res = self.run_script(PRINTOPS_API="http://192.0.2.1:8765", PRINTOPS_TOKEN="po_x")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.init_calls(), [])

    def test_xdg_config_home_is_honoured(self):
        self.make_checkout()
        xdg = os.path.join(self.tmp, "xdg")
        os.makedirs(os.path.join(xdg, "3d-cli"))
        with open(os.path.join(xdg, "3d-cli", "printops.yaml"), "w") as fh:
            fh.write("api: http://192.0.2.1:8765\ntoken: po_x\n")
        res = self.run_script(XDG_CONFIG_HOME=xdg)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.init_calls(), [])

    def test_spoolman_skip_declines_the_step_but_still_links(self):
        self.make_checkout()
        res = self.run_script(config={"spoolman": "skip"})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(self.init_calls(), [])
        self.assertEqual(self.run_script("--check", config={"spoolman": "skip"}).returncode, 0)

    # -- the link ---------------------------------------------------------------------------------
    def test_a_foreign_file_at_the_link_path_is_never_replaced(self):
        self.make_checkout()
        os.makedirs(os.path.dirname(self.link))
        with open(self.link, "w") as fh:
            fh.write("mine")
        self.assertEqual(self.run_script("--check").returncode, 3)
        res = self.run_script()
        self.assertEqual(res.returncode, 10)
        self.assertIn("not a link to", res.stderr)
        with open(self.link) as fh:
            self.assertEqual(fh.read(), "mine")

    def test_an_existing_correct_link_is_left_alone(self):
        self.make_checkout()
        os.makedirs(os.path.dirname(self.link))
        os.symlink(os.path.join(self.dir, "bin", "3d"), self.link)
        self.write_config("api: http://192.0.2.1:8765\ntoken: po_x\n")
        res = self.run_script()
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("linked", res.stdout)

    def test_a_link_made_by_a_concurrent_apply_is_accepted(self):
        self.make_checkout()
        shim = os.path.join(self.tmp, "shim")
        os.makedirs(shim)
        real_ln = shutil.which("ln")
        with open(os.path.join(shim, "ln"), "w") as fh:  # the other apply wins the race, then our ln finds it there
            fh.write(f'#!/bin/sh\n{real_ln} "$@"\nexec {real_ln} "$@"\n')
        os.chmod(os.path.join(shim, "ln"), 0o755)
        res = self.run_script(config={"spoolman": "skip"}, PATH=shim + os.pathsep + os.environ["PATH"])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(os.path.join(self.dir, "bin", "3d")))

    # -- options ----------------------------------------------------------------------------------
    def test_unknown_and_bad_options_are_failures(self):
        self.make_checkout()
        for bad in ({"nope": 1}, {"spoolman": "maybe"}, {"dir": "relative/dir"}):
            res = self.run_script("--check", config=bad)
            self.assertEqual(res.returncode, 1, (bad, res.stdout, res.stderr))
            self.assertIn("[modules.3d-cli]", res.stderr)


if __name__ == "__main__":
    unittest.main()
