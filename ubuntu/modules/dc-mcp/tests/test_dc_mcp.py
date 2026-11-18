"""modules/dc-mcp (a thin installer over `computer chatgpt`) against a throwaway HOME with a stub `computer` on PATH, plus the files
the profile still ships next to it: the ChatGPT window launcher with its icon and the tool-call log pruner.

Nothing touches the real network, the real systemd or the real $HOME.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UBUNTU = os.path.dirname(os.path.dirname(MODULE))

with open(os.path.join(MODULE, "install.sh")) as _fh:
    _pin = dict(line.split("=", 1) for line in _fh.read().splitlines() if line.startswith(("CLI_REPO=", "CLI_REV=")))
PINNED = f"{_pin['CLI_REPO']}?rev={_pin['CLI_REV']}"

# `doctor --local --json` reports the pieces of `init` as present once `init` ran (under other check names while $STUB/renamed
# exists: a newer CLI); `status` is healthy once `start` ran; the full `doctor` fails while $STUB/dns-elsewhere exists (the
# hostname kept pointing at another machine); $STUB/net-down makes the downloading commands fail with the CLI's network exit
# code 7, $STUB/init-127 makes init fail as a missing command.
COMPUTER = """#!/bin/sh
echo "computer $*" >> "$STUB/calls.log"
case "$*" in
  "mcp install") [ -e "$STUB/net-down" ] && exit 7 ;;
  "chatgpt init") [ -e "$STUB/net-down" ] && exit 7; [ -e "$STUB/init-127" ] && exit 127; : > "$STUB/initialized" ;;
  "chatgpt doctor --local --json")
    ok=false; [ -e "$STUB/initialized" ] && ok=true
    suffix=; [ -e "$STUB/renamed" ] && suffix=" (v2)"
    printf '[{"name": "mcp-auth-proxy installed%s", "ok": %s}, {"name": "cloudflared installed", "ok": %s},' "$suffix" "$ok" "$ok"
    printf ' {"name": "desktop-commander installed", "ok": %s}, {"name": "login password", "ok": %s},' "$ok" "$ok"
    printf ' {"name": "unit files current%s", "ok": %s}, {"name": "dc-mcp-tunnel.service active", "ok": false}]\\n' "$suffix" "$ok"
    ;;
  "chatgpt doctor") [ -e "$STUB/public-down" ] && exit 3 ;;
  "chatgpt status") [ -e "$STUB/started" ] || exit 3 ;;
  "chatgpt start") : > "$STUB/started" ;;
  "chatgpt login") [ -e "$STUB/login-fails" ] && exit 8 ;;
esac
exit 0
"""

# `uv tool install --force "hyper-computer @ git+<repo>@<rev>"` writes a receipt like uv's and puts the computer stub into the
# uv bin directory ($FAKEBIN, which is also on PATH).
UV = """#!/bin/sh
echo "uv $*" >> "$STUB/calls.log"
case "$1 $2 ${3:-}" in
  "tool dir --bin") echo "$FAKEBIN" ;;
  "tool dir ") echo "$STUB/uvtools" ;;
  "tool install "*)
    [ -e "$STUB/offline" ] && exit 2
    [ -e "$STUB/uv-fails" ] && exit 2
    spec=$(printf '%s' "$4" | sed 's/^hyper-computer @ git+//')
    mkdir -p "$STUB/uvtools/hyper-computer"
    printf '[tool]\\nrequirements = [{ name = "hyper-computer", git = "%s?rev=%s" }]\\n' "${spec%@*}" "${spec##*@}" > "$STUB/uvtools/hyper-computer/uv-receipt.toml"
    cp "$STUB/computer.src" "$FAKEBIN/computer"; chmod +x "$FAKEBIN/computer" ;;
esac
exit 0
"""

# The public URL as finish.sh probes it: the request lands in this machine's shim access log unless the hostname's DNS record
# points at another machine ($STUB/dns-elsewhere) or the public URL is down ($STUB/public-down).
CURL = """#!/bin/sh
for url; do :; done
[ -e "$STUB/public-down" ] && exit 7
[ -e "$STUB/dns-elsewhere" ] && exit 0
mkdir -p "$HOME/.local/state/dc-mcp"
echo "2026-11-18T00:00:00.000Z 404 GET /${url#https://*/} 0ms client=unverified" >> "$HOME/.local/state/dc-mcp/access.log"
"""

# The installer's reachability probe after a failed `uv tool install`: unreachable while $STUB/offline exists.
REACH = """#!/bin/sh
[ -e "$STUB/offline" ] && exit 6
exit 0
"""

CLOUDFLARED = """#!/bin/sh
echo "cloudflared $*" >> "$STUB/calls.log"
[ -e "$STUB/cf-down" ] && exit 1
echo '[{"id": "11111111-2222-3333-4444-555555555555", "name": "dc-test"}]'
"""
TUNNEL_ID = "11111111-2222-3333-4444-555555555555"

# a unit is active while $STUB/active-<unit> exists; a restart starts it, or, while $STUB/restart-fails-<unit> exists, fails and
# leaves it stopped; while $STUB/bus-down exists every call fails as with an unreachable user manager
SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$STUB/calls.log"
[ -e "$STUB/bus-down" ] && { echo "Failed to connect to bus" >&2; exit 1; }
case "$2" in
  is-active) [ -e "$STUB/active-$3" ] && { echo active; exit 0; }; echo inactive; exit 3 ;;
  restart)
    if [ -e "$STUB/restart-fails-$3" ]; then rm -f "$STUB/active-$3"; exit 1; fi
    : > "$STUB/active-$3" ;;
esac
exit 0
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
        write_exec(os.path.join(self.stub, "computer.src"), COMPUTER)
        write_exec(os.path.join(self.fakebin, "uv"), UV)
        write_exec(os.path.join(self.fakebin, "curl"), REACH)
        # never the real systemctl: on a machine running dc-mcp it would restart the live units
        write_exec(os.path.join(self.fakebin, "systemctl"), SYSTEMCTL)
        self.config_file = os.path.join(self.home, ".config", "computer", "chatgpt", "config.toml")
        self.cf_dir = os.path.join(self.home, ".config", "computer", "chatgpt", "cloudflared")
        self.legacy = os.path.join(self.home, ".cloudflared")
        self.options = {"hostname": "dc.example.com", "tunnel": "dc-test"}

    def install_cli(self, rev=None):
        """The CLI as an earlier apply left it: on PATH, with a uv receipt of `rev` (default: the pinned one)."""
        spec = PINNED.replace("?rev=", "@") if rev is None else PINNED.split("?rev=")[0] + "@" + rev
        subprocess.run([os.path.join(self.fakebin, "uv"), "tool", "install", "--force", "hyper-computer @ git+" + spec], env=self.env(), check=True)
        os.remove(os.path.join(self.stub, "calls.log"))

    def env(self):
        return {
            **os.environ,
            "HOME": self.home,
            "STUB": self.stub,
            "FAKEBIN": self.fakebin,
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            "XDG_DATA_HOME": os.path.join(self.home, ".local", "share"),
            "PATH": f"{self.fakebin}:/usr/bin:/bin",
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(self.options),
        }

    def run_script(self, script, *args):
        return subprocess.run(["bash", os.path.join(MODULE, script), *args], env=self.env(), capture_output=True, text=True, check=False)

    def calls(self):
        path = os.path.join(self.stub, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def changing_calls(self):
        read_only = ("uv tool dir", "systemctl --user is-active", "systemctl --user show-environment")
        return [c for c in self.calls() if "doctor" not in c and not c.startswith(read_only)]

    def cli_calls(self):
        return [c for c in self.calls() if c.startswith("computer ")]

    def config(self):
        with open(self.config_file, "rb") as fh:
            return tomllib.load(fh)

    def legacy_credentials(self):
        os.makedirs(self.legacy)
        for name, text in (("cert.pem", "CERT\n"), (f"{TUNNEL_ID}.json", '{"TunnelID": "%s"}\n' % TUNNEL_ID)):
            with open(os.path.join(self.legacy, name), "w") as fh:
                fh.write(text)
        bin_dir = os.path.join(self.home, ".local", "share", "computer", "chatgpt", "bin")
        os.makedirs(bin_dir, exist_ok=True)
        write_exec(os.path.join(bin_dir, "cloudflared"), CLOUDFLARED)


class Installer(Base):
    def test_check_before_install_is_drift_and_writes_nothing(self):
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("hostname", res.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_without_the_cli_check_is_drift_not_a_crash(self):
        # the engine runs every --check before it executes anything and accepts only 0 or 3 from it
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        os.remove(os.path.join(self.fakebin, "computer"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("computer CLI", res.stdout)

    def test_plan_lists_the_steps_and_writes_nothing(self):
        res = self.run_script("install.sh", "--plan")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("uv tool install --force hyper-computer @ git+" + PINNED.replace("?rev=", "@"), res.stdout)
        self.assertIn("into " + self.config_file, res.stdout)
        self.assertIn("computer chatgpt init", res.stdout)
        self.assertEqual(os.listdir(self.home), [])
        self.assertEqual(self.changing_calls(), [])

    def test_install_puts_the_pinned_cli_the_live_layout_and_runs_init_which_starts_nothing(self):
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(
            self.config(),
            {
                "mode": "public",
                "scheme": "https",
                "hostname": "dc.example.com",
                "tunnel_name": "dc-test",
                "proxy_port": 8766,
                "shim_port": 8767,
                "metrics_port": 20241,
                "unit_prefix": "dc-mcp",
                "state_dir": "~/.local/state/dc-mcp",
                "data_dir": "~/.local/share/mcp-auth-proxy",
                "password_file": "~/.config/dc-mcp-auth.env",
            },
        )
        # a daemon-reload starts nothing; no unit runs yet, so nothing restarts
        self.assertEqual(
            self.changing_calls(),
            [
                "uv tool install --force hyper-computer @ git+" + PINNED.replace("?rev=", "@"),
                "computer mcp install",
                "computer chatgpt init",
                "systemctl --user daemon-reload",
            ],
        )
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_cli_from_another_commit_is_drift_and_gets_replaced(self):
        self.install_cli(rev="0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)
        self.install_cli(rev="0123456789abcdef0123456789abcdef01234567")
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("not installed from", res.stdout)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertIn("uv tool install --force hyper-computer @ git+" + PINNED.replace("?rev=", "@"), self.calls())
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_failed_cli_download_is_the_transient_exit_code(self):
        open(os.path.join(self.stub, "offline"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 75)

    def test_a_uv_failure_with_the_network_up_is_a_plain_failure_not_retried(self):
        open(os.path.join(self.stub, "uv-fails"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 1)

    def test_a_cli_command_that_cannot_run_is_a_manual_step(self):
        self.install_cli()
        open(os.path.join(self.stub, "init-127"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 10)

    def test_a_network_failure_of_the_cli_is_the_transient_exit_code(self):
        self.install_cli()
        open(os.path.join(self.stub, "net-down"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 75)
        os.remove(os.path.join(self.stub, "net-down"))
        self.assertEqual(self.run_script("install.sh").returncode, 0)

    def test_without_uv_check_is_drift_and_install_a_manual_step(self):
        # the engine runs every --check before its packages phase (which brings uv) and accepts only 0 or 3 from it
        os.remove(os.path.join(self.fakebin, "uv"))
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertEqual(self.run_script("install.sh", "--plan").returncode, 0)
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 10)
        self.assertIn("uv is not installed", res.stderr)

    def test_another_computer_earlier_on_path_is_never_the_one_used(self):
        shadow = os.path.join(self.tmp, "shadow")
        os.makedirs(shadow)
        write_exec(os.path.join(shadow, "computer"), '#!/bin/sh\necho "shadow $*" >> "$STUB/calls.log"\nexit 0\n')
        env = {**self.env(), "PATH": f"{shadow}:{self.fakebin}:/usr/bin:/bin"}
        res = subprocess.run(["bash", os.path.join(MODULE, "install.sh")], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("shadow")], [])
        self.assertIn("computer chatgpt init", self.calls())

    def test_a_second_install_changes_nothing(self):
        self.run_script("install.sh")
        with open(self.config_file) as fh:
            first = fh.read()
        before = len(self.changing_calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        with open(self.config_file) as fh:
            self.assertEqual(fh.read(), first)
        self.assertEqual(len(self.changing_calls()), before)

    def test_keys_init_added_stay_and_a_changed_option_is_drift_then_merged(self):
        self.run_script("install.sh")
        with open(self.config_file, "a", encoding="utf-8") as fh:
            fh.write('cloudflared_dir = "/media/\U0001f512/cloudflared \\u007f\\"q\\""\ntunnel_health_port = 8794\n')
        self.options["port"] = 9766
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("proxy_port", res.stdout)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.config()["proxy_port"], 9766)
        self.assertEqual(self.config()["tunnel_health_port"], 8794)
        self.assertEqual(self.config()["cloudflared_dir"], '/media/\U0001f512/cloudflared \x7f"q"')

    def test_a_missing_or_invalid_option_is_a_manual_step_and_writes_nothing(self):
        # --check may only answer 0 or 3 to the engine, so there it is drift; the install is the manual step
        for options in ({}, {"hostname": "Not A Name"}, {"hostname": "dc.example.com", "port": 8767}, {"hostname": "dc.example.com", "package": "x"}):
            self.options = options
            for args, code in (((), 10), (("--check",), 3)):
                res = self.run_script("install.sh", *args)
                self.assertEqual(res.returncode, code, (options, args, res.stdout, res.stderr))
                self.assertIn("[modules.dc-mcp]", res.stderr)
        self.assertFalse(os.path.exists(self.config_file))
        self.assertEqual(self.calls(), [])


class LegacyCredentials(Base):
    """A machine set up by the first version of this module: the login and tunnel credentials are only in ~/.cloudflared."""

    def test_they_are_copied_private_and_the_originals_stay(self):
        self.legacy_credentials()
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for name, original in (("cert.pem", "cert.pem"), ("dc-test.json", f"{TUNNEL_ID}.json")):
            path = os.path.join(self.cf_dir, name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, name)
            with open(path) as fh, open(os.path.join(self.legacy, original)) as orig:
                self.assertEqual(fh.read(), orig.read())
        self.assertEqual(sorted(os.listdir(self.legacy)), sorted(["cert.pem", f"{TUNNEL_ID}.json"]))
        lookup = f" --origincert {self.cf_dir}/cert.pem list --name dc-test -o json"
        self.assertTrue(any(c.startswith("cloudflared tunnel --config ") and c.endswith(lookup) for c in self.calls()), self.calls())
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_until_they_are_copied_check_is_drift(self):
        self.run_script("install.sh")
        self.legacy_credentials()
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("not copied", res.stdout)
        self.assertIn("copy ", self.run_script("install.sh", "--plan").stdout)

    def test_credentials_of_another_tunnel_are_a_manual_step(self):
        self.legacy_credentials()
        os.rename(os.path.join(self.legacy, f"{TUNNEL_ID}.json"), os.path.join(self.legacy, "other.json"))
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 10, res.stdout + res.stderr)
        self.assertIn("finish-setup", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.cf_dir, "dc-test.json")))

    def test_a_fresh_machine_without_them_is_left_to_the_finish_step(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.cf_dir, "cert.pem")))

    def test_units_the_old_module_left_running_restart_on_the_new_text_in_chain_order(self):
        self.legacy_credentials()
        for unit in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            open(os.path.join(self.stub, "active-" + unit), "w").close()
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        restarts = [c for c in self.calls() if c.startswith("systemctl --user restart")]
        self.assertEqual(restarts, [f"systemctl --user restart dc-mcp-{u}.service" for u in ("bridge", "shim", "tunnel")])
        self.assertLess(self.calls().index("computer chatgpt init"), self.calls().index(restarts[0]))
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)  # current units: nothing restarts again
        self.assertEqual([c for c in self.calls()[before:] if "restart" in c], [])

    def test_stopped_units_stay_stopped(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual([c for c in self.calls() if "restart" in c], [])

    def test_a_doctor_without_the_expected_check_names_fails_and_never_reruns_init_or_restarts(self):
        # a newer CLI that renamed a check must not read as "missing piece": that would re-run init and restart every apply
        self.install_cli()
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        for unit in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            open(os.path.join(self.stub, "active-" + unit), "w").close()
        open(os.path.join(self.stub, "renamed"), "w").close()
        before = len(self.calls())
        for args in ((), ("--check",)):
            res = self.run_script("install.sh", *args)
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("no check named", res.stderr)
        self.assertEqual([c for c in self.calls()[before:] if "init" in c or "restart" in c], [])

    def test_the_tunnel_lookup_ignores_any_cloudflared_config_file(self):
        self.legacy_credentials()
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        lookups = [c for c in self.calls() if c.startswith("cloudflared tunnel")]
        self.assertTrue(lookups)
        self.assertTrue(all(c.startswith("cloudflared tunnel --config ") for c in lookups), lookups)

    def test_a_restart_left_pending_by_a_failure_after_init_runs_on_the_next_apply(self):
        self.legacy_credentials()
        for unit in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            open(os.path.join(self.stub, "active-" + unit), "w").close()
        open(os.path.join(self.stub, "cf-down"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 75)
        self.assertEqual([c for c in self.calls() if "restart" in c], [])
        os.remove(os.path.join(self.stub, "cf-down"))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 3)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        restarts = [c for c in self.calls() if c.startswith("systemctl --user restart")]
        self.assertEqual(restarts, [f"systemctl --user restart dc-mcp-{u}.service" for u in ("bridge", "shim", "tunnel")])
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_new_cli_revision_reinstalls_and_restarts_running_units_after_a_reload_even_with_the_same_unit_text(self):
        self.install_cli(rev="0123456789abcdef0123456789abcdef01234567")
        open(os.path.join(self.stub, "initialized"), "w").close()  # pieces and unit text already current
        for unit in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            open(os.path.join(self.stub, "active-" + unit), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        calls = self.changing_calls()
        self.assertIn("computer mcp install", calls)
        self.assertIn("computer chatgpt init", calls)
        restarts = [c for c in calls if c.startswith("systemctl --user restart")]
        self.assertEqual(restarts, [f"systemctl --user restart dc-mcp-{u}.service" for u in ("bridge", "shim", "tunnel")])
        self.assertLess(calls.index("systemctl --user daemon-reload"), calls.index(restarts[0]))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_tunnel_key_without_the_login_is_a_manual_step_not_a_silent_success(self):
        os.makedirs(self.legacy)
        with open(os.path.join(self.legacy, f"{TUNNEL_ID}.json"), "w") as fh:
            fh.write('{"TunnelID": "%s"}\n' % TUNNEL_ID)
        self.assertEqual(self.run_script("install.sh").returncode, 10)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 3)

    def test_a_restart_that_fails_and_stops_a_unit_is_retried_on_the_next_apply(self):
        self.legacy_credentials()
        for unit in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            open(os.path.join(self.stub, "active-" + unit), "w").close()
        open(os.path.join(self.stub, "restart-fails-dc-mcp-tunnel.service"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.stub, "active-dc-mcp-tunnel.service")))
        os.remove(os.path.join(self.stub, "restart-fails-dc-mcp-tunnel.service"))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 3)
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertIn("systemctl --user restart dc-mcp-tunnel.service", self.calls()[before:])
        self.assertTrue(os.path.exists(os.path.join(self.stub, "active-dc-mcp-tunnel.service")))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_an_upgrade_whose_installers_failed_runs_them_again_on_the_next_apply(self):
        self.install_cli(rev="0123456789abcdef0123456789abcdef01234567")
        open(os.path.join(self.stub, "initialized"), "w").close()
        open(os.path.join(self.stub, "net-down"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 75)
        os.remove(os.path.join(self.stub, "net-down"))
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 3)
        before = len(self.calls())
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertIn("computer mcp install", self.calls()[before:])
        self.assertIn("computer chatgpt init", self.calls()[before:])
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_an_unreachable_user_manager_records_no_restart_state_and_the_retry_restarts_what_ran(self):
        self.install_cli(rev="0123456789abcdef0123456789abcdef01234567")
        open(os.path.join(self.stub, "initialized"), "w").close()
        for unit in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            open(os.path.join(self.stub, "active-" + unit), "w").close()
        open(os.path.join(self.stub, "bus-down"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 1)
        self.assertNotIn("computer chatgpt init", self.calls())
        os.remove(os.path.join(self.stub, "bus-down"))
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        restarts = [c for c in self.calls() if c.startswith("systemctl --user restart")]
        self.assertEqual(restarts, [f"systemctl --user restart dc-mcp-{u}.service" for u in ("bridge", "shim", "tunnel")])

    def test_the_effective_legacy_password_is_kept_when_the_file_has_two_assignments(self):
        password_file = os.path.join(self.home, ".config", "dc-mcp-auth.env")
        os.makedirs(os.path.dirname(password_file), exist_ok=True)
        with open(password_file, "w") as fh:
            fh.write("# dc-mcp\nPASSWORD=\nPASSWORD=effective-one\n")
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        with open(password_file) as fh:
            self.assertEqual(fh.read(), "# dc-mcp\nPASSWORD=effective-one\n")
        self.assertEqual(stat.S_IMODE(os.stat(password_file).st_mode), 0o600)


class Finish(Base):
    def setUp(self):
        super().setUp()
        write_exec(os.path.join(self.fakebin, "curl"), CURL)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        os.remove(os.path.join(self.stub, "calls.log"))

    def test_run_logs_in_starts_proves_the_public_url_then_prints_the_connector_fields(self):
        res = self.run_script("finish.sh", "run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            self.cli_calls(),
            ["computer chatgpt login", "computer chatgpt start", "computer chatgpt doctor", "computer chatgpt connect --no-wait --no-open --no-copy"],
        )
        self.assertEqual(self.run_script("finish.sh", "check").returncode, 0)

    def test_a_hostname_that_still_points_at_another_machine_fails_and_stays_pending(self):
        # `login` exits 0 when the owner keeps a DNS record of another machine, and that machine's 401 passes doctor
        open(os.path.join(self.stub, "dns-elsewhere"), "w").close()
        res = self.run_script("finish.sh", "run")
        self.assertEqual(res.returncode, 1)
        self.assertIn("not from this machine", res.stderr)
        self.assertNotIn("computer chatgpt connect --no-wait --no-open --no-copy", self.cli_calls())
        self.assertNotEqual(self.run_script("finish.sh", "check").returncode, 0)

    def test_an_unreachable_public_url_fails_and_stays_pending(self):
        open(os.path.join(self.stub, "public-down"), "w").close()
        self.assertEqual(self.run_script("finish.sh", "run").returncode, 1)
        self.assertIn("computer chatgpt start", self.cli_calls())  # the local stack runs ...
        self.assertNotEqual(self.run_script("finish.sh", "check").returncode, 0)  # ... but the step is not done

    def test_a_failed_login_starts_nothing(self):
        open(os.path.join(self.stub, "login-fails"), "w").close()
        self.assertEqual(self.run_script("finish.sh", "run").returncode, 1)
        self.assertEqual(self.cli_calls(), ["computer chatgpt login"])


class ProfileFiles(unittest.TestCase):
    """The files the profile still ships for this module name no user, machine or home."""

    def test_they_name_no_user_or_home(self):
        for path in (
            os.path.join(UBUNTU, "files", ".config", "systemd", "user", "dc-mcp-tool-log-prune.service"),
            os.path.join(UBUNTU, "files", ".config", "systemd", "user", "dc-mcp-tool-log-prune.timer"),
            os.path.join(UBUNTU, "files", ".local", "bin", "dc-mcp-tool-log-prune"),
            os.path.join(UBUNTU, "files", ".local", "share", "applications", "chatgpt.desktop"),
            os.path.join(UBUNTU, "files", ".local", "bin", "chatgpt-window"),
        ):
            with open(path) as fh:
                text = fh.read()
            for word in ("/home/", "ultra", "mextner"):
                self.assertNotIn(word, text, path)


class Launcher(unittest.TestCase):
    """files/.local/bin/chatgpt-window and the desktop entry that starts it."""

    SCRIPT = os.path.join(UBUNTU, "files", ".local", "bin", "chatgpt-window")
    DESKTOP = os.path.join(UBUNTU, "files", ".local", "share", "applications", "chatgpt.desktop")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        os.makedirs(self.bin)

    def chrome(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_exec(path, '#!/bin/sh\nprintf "%s\\n" "$0" "$@" > "$HOME/chrome-argv"\n')

    def run_script(self, argv):
        return subprocess.run(argv, env={"HOME": self.home, "PATH": f"{self.bin}:/usr/bin:/bin"}, capture_output=True, text=True, check=False)

    def argv(self):
        with open(os.path.join(self.home, "chrome-argv")) as fh:
            return fh.read().splitlines()

    def test_system_chrome_opens_the_app_window_with_its_own_profile(self):
        self.chrome(os.path.join(self.bin, "google-chrome-stable"))
        self.assertEqual(self.run_script(["/bin/sh", self.SCRIPT]).returncode, 0)
        argv = self.argv()
        self.assertIn("--app=https://chatgpt.com", argv)
        self.assertIn("--class=ChatGPT", argv)
        self.assertIn(f"--user-data-dir={self.home}/.config/chatgpt-app", argv)
        self.assertNotIn("--no-sandbox", argv)

    def test_without_a_system_chrome_the_rootless_copy_runs_unsandboxed(self):
        self.chrome(os.path.join(self.home, ".local/opt/google-chrome/opt/google/chrome/chrome"))
        self.assertEqual(self.run_script(["/bin/sh", self.SCRIPT]).returncode, 0)
        self.assertIn("--no-sandbox", self.argv())
        self.assertIn("--app=https://chatgpt.com", self.argv())

    def test_the_hand_made_google_chrome_shim_is_not_preferred_over_the_rootless_copy(self):
        self.chrome(os.path.join(self.home, ".local/bin/google-chrome"))
        self.chrome(os.path.join(self.home, ".local/opt/google-chrome/opt/google/chrome/chrome"))
        res = subprocess.run(["/bin/sh", self.SCRIPT], env={"HOME": self.home, "PATH": f"{self.home}/.local/bin:/usr/bin:/bin"}, capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.argv()[0].endswith("opt/google/chrome/chrome"))

    def test_no_chrome_is_an_error_that_says_so(self):
        res = self.run_script(["/bin/sh", self.SCRIPT])
        self.assertEqual(res.returncode, 1)
        self.assertIn("no Chrome found", res.stderr)

    def test_the_desktop_entry_runs_the_launcher_from_the_home_of_whoever_starts_it(self):
        with open(self.DESKTOP) as fh:
            exec_line = next(line for line in fh if line.startswith("Exec="))[len("Exec="):].rstrip("\n")
        exec_line = exec_line.replace("\\\\", "\\")  # key-file string escapes
        argv, word, quoted, i = [], "", False, 0
        while i < len(exec_line):  # desktop-entry Exec quoting: inside double quotes \" \$ \` \\ lose the backslash
            c = exec_line[i]
            if c == "\\" and quoted:
                i += 1
                word += exec_line[i]
            elif c == '"':
                quoted = not quoted
            elif c == " " and not quoted:
                if word:
                    argv.append(word)
                word = ""
            else:
                word += c
            i += 1
        argv.append(word)
        self.chrome(os.path.join(self.bin, "google-chrome-stable"))
        os.symlink(self.SCRIPT, os.path.join(self.home, ".local", "bin", "chatgpt-window"))
        self.assertEqual(self.run_script(argv).returncode, 0)
        self.assertIn("--app=https://chatgpt.com", self.argv())

    def test_the_icon_the_entry_names_is_shipped_as_png_in_the_sizes_the_theme_directories_claim(self):
        import struct
        base = os.path.join(UBUNTU, "files", ".local", "share", "icons", "hicolor")
        for size in (48, 256):
            with open(os.path.join(base, "%dx%d" % (size, size), "apps", "chatgpt-window.png"), "rb") as fh:
                head = fh.read(24)
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", head[16:24]), (size, size))
        with open(self.DESKTOP) as fh:
            self.assertIn("\nIcon=chatgpt-window\n", fh.read())


class ToolLogRetention(unittest.TestCase):
    """files/.local/bin/dc-mcp-tool-log-prune over a directory of fake rotated Desktop Commander logs."""

    PRUNE = os.path.join(UBUNTU, "files", ".local", "bin", "dc-mcp-tool-log-prune")
    DAY = 86400

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = os.path.join(self.tmp, "dir with spaces")
        os.mkdir(self.dir)
        self.now = time.time()
        self.rotated = []
        for age_days in (1, 2, 3, 4, 5, 6, 40, 90):  # newest first
            name = f"claude_tool_call_2026-10-{30 - age_days if age_days < 30 else 1:02d}_00-00-{age_days:02d}.log"
            self.make(name, age_days)
            self.rotated.append(name)
        self.make("claude_tool_call.log", 0)  # the active file, however old it looks
        os.utime(os.path.join(self.dir, "claude_tool_call.log"), (self.now - 400 * self.DAY,) * 2)
        self.bystanders = ["notes [x] *.log", "claude_tool_call_2026-10-01_00-00-00.log.bak", "claude_tool_call_latest.log", "other.log"]
        for name in self.bystanders:
            self.make(name, 500)
        target = os.path.join(self.tmp, "target.log")
        with open(target, "w") as fh:
            fh.write("keep me")
        os.symlink(target, os.path.join(self.dir, "claude_tool_call_2025-01-01_00-00-00.log"))

    def make(self, name, age_days):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write("x")
        os.utime(path, (self.now - age_days * self.DAY,) * 2)

    def prune(self, *args, **env):
        return subprocess.run([self.PRUNE, *args], env={**os.environ, "DC_MCP_TOOL_LOG_KEEP": "5", "DC_MCP_TOOL_LOG_MAX_AGE_DAYS": "30", **env}, capture_output=True, text=True, check=False)

    def left(self):
        return sorted(os.listdir(self.dir))

    def test_a_dry_run_lists_what_would_go_and_removes_nothing(self):
        before = self.left()
        done = self.prune("--dry-run", self.dir)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.left(), before)
        self.assertEqual(len([line for line in done.stdout.splitlines() if line.startswith("would remove")]), 3)

    def test_only_rotated_files_beyond_the_newest_few_or_older_than_the_limit_go(self):
        done = self.prune(self.dir)
        self.assertEqual(done.returncode, 0, done.stderr)
        for name in self.rotated[:5]:
            self.assertIn(name, self.left())
        for name in self.rotated[5:]:
            self.assertNotIn(name, self.left())
        for name in ("claude_tool_call.log", *self.bystanders, "claude_tool_call_2025-01-01_00-00-00.log"):
            self.assertIn(name, self.left(), f"{name} must never be touched")
        with open(os.path.join(self.tmp, "target.log")) as fh:
            self.assertEqual(fh.read(), "keep me")

    def test_a_file_that_cannot_be_removed_is_a_failure_not_a_success(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        done = self.prune(self.dir)
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot remove", done.stderr)
        self.assertIn("0 rotated file(s) removed", done.stdout)

    def test_a_second_run_changes_nothing(self):
        self.prune(self.dir)
        after_first = self.left()
        done = self.prune(self.dir)
        self.assertEqual(self.left(), after_first)
        self.assertIn("0 rotated file(s) removed", done.stdout)

    def test_the_age_limit_applies_even_when_there_are_few_files(self):
        done = self.prune(self.dir, DC_MCP_TOOL_LOG_KEEP="50", DC_MCP_TOOL_LOG_MAX_AGE_DAYS="10")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([n for n in self.left() if n in self.rotated], sorted(self.rotated[:6]))

    def test_the_directory_comes_from_the_environment_and_a_missing_one_is_fine(self):
        self.assertEqual(self.prune(DC_MCP_TOOL_LOG_DIR=self.dir).returncode, 0)
        self.assertNotIn(self.rotated[-1], self.left())
        done = self.prune(os.path.join(self.tmp, "absent"))
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_bad_limits_are_refused_before_anything_is_removed(self):
        before = self.left()
        for env in ({"DC_MCP_TOOL_LOG_KEEP": "-1"}, {"DC_MCP_TOOL_LOG_KEEP": "1.5"}, {"DC_MCP_TOOL_LOG_MAX_AGE_DAYS": "x"}):
            self.assertEqual(self.prune(self.dir, **env).returncode, 2, env)
        self.assertEqual(self.left(), before)


if __name__ == "__main__":
    unittest.main()
