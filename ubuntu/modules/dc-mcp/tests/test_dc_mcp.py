"""modules/dc-mcp against a throwaway HOME with stub `curl`, `systemctl` and `cloudflared` on PATH.

Nothing touches the real network, the real systemd or the real $HOME. The "release" the stub `curl` serves is a small file
whose digest is written into a test pins file, so the real verification code runs: a wrong digest is refused for real.
"""

import hashlib
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UBUNTU = os.path.dirname(os.path.dirname(MODULE))
MACHINE = subprocess.run(["uname", "-m"], capture_output=True, text=True, check=True).stdout.strip()

CURL = """#!/bin/sh
# usage: curl -fsSL --retry N --retry-delay N -o FILE URL ; serves $STUB/release-<basename of URL>
echo "curl $*" >> "$STUB/calls.log"
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift ;; -*) ;; *) url=$1 ;; esac
  shift
done
[ -e "$STUB/offline" ] && exit 6
cp "$STUB/release-$(basename "$url")" "$out"
"""

SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$STUB/calls.log"
shift
case "$1" in
  is-active) [ -e "$STUB/active-$2" ] && { echo active; exit 0; }; echo inactive; exit 3 ;;
  enable) shift; [ "$1" = "--now" ] && shift; for u in "$@"; do : > "$STUB/active-$u"; done ;;
esac
exit 0
"""

CLOUDFLARED = """#!/bin/sh
echo "cloudflared $*" >> "$STUB/calls.log"
case "$1 $2" in
  "tunnel login") mkdir -p "$HOME/.cloudflared"; echo CERT > "$HOME/.cloudflared/cert.pem" ;;
  "tunnel list") [ -e "$STUB/list-fails" ] && exit 1; [ -e "$STUB/tunnel-exists" ] && echo '[{"id":"11111111-2222-3333-4444-555555555555","name":"x"}]' || echo '[]' ;;
  "tunnel create") : > "$STUB/tunnel-exists"; mkdir -p "$HOME/.cloudflared"; echo CRED > "$HOME/.cloudflared/11111111-2222-3333-4444-555555555555.json" ;;
esac
exit 0
"""


def write_exec(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def slurp(path):
    with open(path) as fh:
        return fh.read()


def tree_state(root):
    state = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                state[path] = hashlib.sha256(fh.read()).hexdigest()
    return state


class Base(unittest.TestCase):
    PROXY = b"fake mcp-auth-proxy binary\n"
    CF = b"fake cloudflared binary\n"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stub = os.path.join(self.tmp, "stub")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        for d in (self.home, self.stub, self.fakebin):
            os.makedirs(d)
        write_exec(os.path.join(self.fakebin, "curl"), CURL)
        write_exec(os.path.join(self.fakebin, "systemctl"), SYSTEMCTL)
        self.serve("mcp-auth-proxy-linux", self.PROXY)
        self.serve("cloudflared-linux", self.CF)
        self.pins = os.path.join(self.tmp, "pins.tsv")
        self.write_pins(self.PROXY, self.CF)
        self.bin_dir = os.path.join(self.home, ".local", "bin")
        self.auth = os.path.join(self.home, ".config", "dc-mcp-auth.env")
        self.settings = os.path.join(self.home, ".config", "dc-mcp.env")
        self.config = {"hostname": "dc.example.com", "tunnel": "dc-test"}

    def serve(self, name, data):
        with open(os.path.join(self.stub, f"release-{name}-{MACHINE}"), "wb") as fh:
            fh.write(data)

    def write_pins(self, proxy_bytes, cf_bytes):
        rows = []
        for tool, data in (("mcp-auth-proxy", proxy_bytes), ("cloudflared", cf_bytes)):
            rows.append("\t".join([tool, "v1", MACHINE, hashlib.sha256(data).hexdigest(), f"https://example.invalid/{tool}-linux-{MACHINE}"]))
        with open(self.pins, "w") as fh:
            fh.write("\n".join(rows) + "\n")

    def env(self, **extra):
        return {
            **os.environ,
            "HOME": self.home,
            "STUB": self.stub,
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            # the stubs come first; /usr/bin and /bin stay for coreutils, python3, awk, sha256sum
            "PATH": f"{self.fakebin}:/usr/bin:/bin",
            "DC_MCP_PINS_FILE": self.pins,
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(self.config),
            **extra,
        }

    def run_script(self, script, *args, stdin="", **extra):
        return subprocess.run(
            ["bash", os.path.join(MODULE, script), *args],
            env=self.env(**extra),
            input=stdin,
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

    def fake_npx(self):
        write_exec(os.path.join(self.fakebin, "npx"), "#!/bin/sh\nexit 0\n")

    def install_ok(self):
        self.fake_npx()
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res

    def password(self):
        with open(self.auth) as fh:
            return re.search(r"^PASSWORD=(.+)$", fh.read(), re.M).group(1)


class Installer(Base):
    def test_check_before_install_is_drift_and_writes_nothing(self):
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("mcp-auth-proxy is missing", res.stdout)
        self.assertIn("no PASSWORD", res.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_plan_lists_the_steps_and_writes_nothing(self):
        res = self.run_script("install.sh", "--plan")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("download https://example.invalid/cloudflared", res.stdout)
        self.assertIn("generate a random password", res.stdout)
        self.assertEqual(os.listdir(self.home), [])
        self.assertEqual(self.calls(), [])

    def test_install_puts_binaries_settings_and_a_private_password_in_place(self):
        res = self.install_ok()
        for tool, data in (("mcp-auth-proxy", self.PROXY), ("cloudflared", self.CF)):
            path = os.path.join(self.bin_dir, tool)
            self.assertTrue(os.access(path, os.X_OK))
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), data)
        with open(self.settings) as fh:
            text = fh.read()
        self.assertIn("DC_MCP_HOSTNAME=dc.example.com\n", text)
        self.assertIn("DC_MCP_TUNNEL=dc-test\n", text)
        self.assertIn("DC_MCP_PORT=8766\n", text)
        self.assertIn("DC_MCP_SHIM_PORT=8767\n", text)
        self.assertEqual(sorted(line.split("=")[0] for line in text.splitlines() if not line.startswith("#")), sorted(["DC_MCP_HOSTNAME", "DC_MCP_TUNNEL", "DC_MCP_PORT", "DC_MCP_SHIM_PORT", "DC_MCP_LOG_MAX_KIB", "DC_MCP_LOG_KEEP", "DC_MCP_TOOL_LOG_KEEP", "DC_MCP_TOOL_LOG_MAX_AGE_DAYS", "DC_MCP_PACKAGE"]))
        self.assertIn("DC_MCP_LOG_MAX_KIB=2048\nDC_MCP_LOG_KEEP=3\nDC_MCP_TOOL_LOG_KEEP=5\nDC_MCP_TOOL_LOG_MAX_AGE_DAYS=30\n", text)
        self.assertIn("DC_MCP_PACKAGE=@wonderwhy-er/desktop-commander@latest\n", text)
        self.assertEqual(stat.S_IMODE(os.stat(self.auth).st_mode), 0o600)
        self.assertRegex(self.password(), r"^[A-Za-z0-9]{32}$")
        self.assertNotIn(self.password(), res.stdout + res.stderr)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_install_touches_no_unit_and_enables_nothing(self):
        self.install_ok()
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd")))
        self.assertEqual([c for c in self.calls() if c.startswith("systemctl")], [])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cloudflared")))

    def test_a_second_install_changes_nothing_and_keeps_the_password(self):
        self.install_ok()
        first = self.password()
        downloads = len([c for c in self.calls() if c.startswith("curl")])
        before = tree_state(self.home)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(tree_state(self.home), before)
        self.assertEqual(len([c for c in self.calls() if c.startswith("curl")]), downloads)
        self.assertEqual(self.password(), first)

    def test_a_checksum_mismatch_is_refused_and_leaves_no_binary(self):
        self.serve("cloudflared-linux", b"tampered download\n")
        self.fake_npx()
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("refusing cloudflared: sha256", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.bin_dir, "cloudflared")))
        leftovers = [n for n in os.listdir(self.bin_dir) if n.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_a_checksum_mismatch_keeps_the_installed_binary(self):
        self.install_ok()
        installed = os.path.join(self.bin_dir, "mcp-auth-proxy")
        newer = b"a newer pinned build\n"
        self.write_pins(newer, self.CF)  # the pin moved on ...
        self.serve("mcp-auth-proxy-linux", b"but the download is not that build\n")  # ... and the server serves something else
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 1)
        with open(installed, "rb") as fh:
            self.assertEqual(fh.read(), self.PROXY)

    def test_a_modified_binary_is_drift_and_install_restores_the_pinned_one(self):
        self.install_ok()
        with open(os.path.join(self.bin_dir, "cloudflared"), "ab") as fh:
            fh.write(b"local edit")
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("cloudflared is missing or is not the pinned", res.stdout)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 0)

    def test_a_network_failure_is_the_transient_exit_code(self):
        self.fake_npx()
        open(os.path.join(self.stub, "offline"), "w").close()
        self.assertEqual(self.run_script("install.sh").returncode, 75)

    def test_an_existing_password_and_its_other_lines_are_kept_and_the_mode_repaired(self):
        os.makedirs(os.path.dirname(self.auth))
        with open(self.auth, "w") as fh:
            fh.write("PASSWORD=existing-secret\nEXTERNAL_URL=https://old.example.com\n")
        os.chmod(self.auth, 0o644)
        res = self.run_script("install.sh", "--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("is not mode 600", res.stdout)
        self.install_ok()
        with open(self.auth) as fh:
            self.assertEqual(fh.read(), "PASSWORD=existing-secret\nEXTERNAL_URL=https://old.example.com\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.auth).st_mode), 0o600)

    def test_a_password_is_appended_on_its_own_line_after_an_unterminated_file(self):
        os.makedirs(os.path.dirname(self.auth))
        with open(self.auth, "w") as fh:
            fh.write("# my comment without a newline")
        os.chmod(self.auth, 0o600)
        self.install_ok()
        lines = slurp(self.auth).splitlines()
        self.assertEqual(lines[0], "# my comment without a newline")
        self.assertRegex(lines[1], r"^PASSWORD=[A-Za-z0-9]{32}$")

    def test_no_hostname_installs_the_binaries_and_asks_for_the_option(self):
        self.config = {}
        self.fake_npx()
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 10, res.stdout + res.stderr)
        self.assertIn("needs manual: set [modules.dc-mcp] hostname", res.stderr)
        self.assertTrue(os.access(os.path.join(self.bin_dir, "cloudflared"), os.X_OK))
        self.assertFalse(os.path.exists(self.settings))
        # the settings file is never written without a hostname: a half-configured machine cannot serve a wrong name
        self.assertEqual(self.run_script("install.sh", "--check").returncode, 10)

    def test_missing_npx_is_a_manual_step_after_everything_else_is_in_place(self):
        res = self.run_script("install.sh")
        self.assertEqual(res.returncode, 10, res.stdout + res.stderr)
        self.assertIn("npx (node) is not installed", res.stderr)
        self.assertTrue(os.path.exists(self.auth))
        self.assertTrue(os.path.exists(self.settings))

    def test_invalid_options_are_refused(self):
        for bad in ({"hostname": "Not A Host"}, {"port": 80}, {"port": "8766"}, {"shim_port": 8766}, {"shim_port": 70000}, {"log_keep": -1}, {"log_keep": 21}, {"log_max_kib": 1}, {"tool_log_keep": 0}, {"tool_log_max_age_days": 0}, {"tunnel": "bad name"}, {"package": "x y"}, {"nope": 1}):
            self.config = {"hostname": "dc.example.com", **bad}
            res = self.run_script("install.sh", "--check")
            self.assertEqual(res.returncode, 1, f"{bad}: {res.stdout}{res.stderr}")
            self.assertIn("[modules.dc-mcp]", res.stderr)

    def test_the_options_reach_the_settings_file(self):
        self.config = {"hostname": "a.b.example.org", "tunnel": "t1", "port": 9001, "shim_port": 9002, "log_max_kib": 512, "log_keep": 0, "tool_log_keep": 2, "tool_log_max_age_days": 7, "package": "@scope/pkg@1.2.3"}
        self.install_ok()
        with open(self.settings) as fh:
            text = fh.read()
        for line in ("DC_MCP_HOSTNAME=a.b.example.org", "DC_MCP_TUNNEL=t1", "DC_MCP_PORT=9001", "DC_MCP_SHIM_PORT=9002", "DC_MCP_LOG_MAX_KIB=512", "DC_MCP_LOG_KEEP=0", "DC_MCP_TOOL_LOG_KEEP=2", "DC_MCP_TOOL_LOG_MAX_AGE_DAYS=7", "DC_MCP_PACKAGE=@scope/pkg@1.2.3"):
            self.assertIn(line, text)

    def test_the_repository_tree_is_never_written_by_install(self):
        before = tree_state(UBUNTU)
        self.install_ok()
        after = tree_state(UBUNTU)
        self.assertEqual(after, before)
        password = self.password()
        for path in after:
            with open(path, "rb") as fh:
                self.assertNotIn(password.encode(), fh.read(), path)

    def test_the_shipped_pins_cover_both_machine_types_with_full_digests(self):
        with open(os.path.join(MODULE, "pins.tsv")) as fh:
            rows = [line.split("\t") for line in fh.read().splitlines() if line and not line.startswith("#")]
        self.assertEqual(sorted((r[0], r[2]) for r in rows), sorted((t, m) for t in ("mcp-auth-proxy", "cloudflared") for m in ("x86_64", "aarch64")))
        for row in rows:
            self.assertRegex(row[3], r"^[0-9a-f]{64}$")
            self.assertTrue(row[4].startswith("https://github.com/"))


class Units(unittest.TestCase):
    """The unit files read nothing the module does not provide, and name no machine or user."""

    UNIT_DIR = os.path.join(UBUNTU, "files", ".config", "systemd", "user")

    def read(self, name):
        with open(os.path.join(self.UNIT_DIR, name)) as fh:
            return fh.read()

    def test_every_variable_a_unit_expands_is_written_by_the_settings_file(self):
        provided = {"DC_MCP_HOSTNAME", "DC_MCP_TUNNEL", "DC_MCP_PORT", "DC_MCP_SHIM_PORT", "DC_MCP_LOG_MAX_KIB", "DC_MCP_LOG_KEEP", "DC_MCP_PACKAGE"}
        for name in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            exec_start = re.search(r"^ExecStart=(.*)$", self.read(name), re.M).group(1)
            self.assertLessEqual(set(re.findall(r"\$\{(\w+)\}", exec_start)), provided, name)
            self.assertNotRegex(exec_start, r"\$[A-Za-z]", "an unbraced variable would be split on spaces")
        self.assertIn("${DC_MCP_HOSTNAME}", self.read("dc-mcp-bridge.service"))
        self.assertIn("${DC_MCP_TUNNEL}", self.read("dc-mcp-tunnel.service"))
        self.assertIn("http://127.0.0.1:${DC_MCP_SHIM_PORT}", self.read("dc-mcp-tunnel.service"))
        self.assertIn("UPSTREAM_PORT=${DC_MCP_PORT}", self.read("dc-mcp-shim.service"))

    def test_every_unit_restarts_for_ever_and_the_chain_is_ordered(self):
        for name in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            text = self.read(name)
            self.assertRegex(text, r"(?m)^Restart=always$", name)
            self.assertRegex(text, r"(?m)^StartLimitIntervalSec=0$", name)
            self.assertRegex(text, r"(?m)^WantedBy=default.target$", name)
        self.assertIn("After=dc-mcp-bridge.service", self.read("dc-mcp-shim.service"))
        self.assertIn("After=dc-mcp-shim.service", self.read("dc-mcp-tunnel.service"))

    def test_units_and_launcher_name_no_user_or_home(self):
        paths = [
            os.path.join(self.UNIT_DIR, "dc-mcp-bridge.service"),
            os.path.join(self.UNIT_DIR, "dc-mcp-tunnel.service"),
            os.path.join(self.UNIT_DIR, "dc-mcp-shim.service"),
            os.path.join(UBUNTU, "files", ".local", "share", "dc-mcp-shim", "shim.mjs"),
            os.path.join(UBUNTU, "files", ".config", "systemd", "user", "dc-mcp-tool-log-prune.service"),
            os.path.join(UBUNTU, "files", ".config", "systemd", "user", "dc-mcp-tool-log-prune.timer"),
            os.path.join(UBUNTU, "files", ".local", "bin", "dc-mcp-quiet"),
            os.path.join(UBUNTU, "files", ".local", "bin", "dc-mcp-log"),
            os.path.join(UBUNTU, "files", ".local", "bin", "dc-mcp-tool-log-prune"),
            os.path.join(UBUNTU, "files", ".local", "share", "applications", "chatgpt.desktop"),
            os.path.join(UBUNTU, "files", ".local", "bin", "chatgpt-window"),
        ]
        for path in paths:
            with open(path) as fh:
                text = fh.read()
            self.assertNotIn("/home/", text, path)
            self.assertNotIn("ultra", text, path)
            self.assertNotIn("mextner", text, path)

    def test_the_bridge_keeps_the_password_out_of_its_command_line(self):
        text = self.read("dc-mcp-bridge.service")
        self.assertIn("EnvironmentFile=%h/.config/dc-mcp-auth.env", text)
        self.assertNotIn("--password", text)


class Finish(Base):
    PID = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        super().setUp()
        write_exec(os.path.join(self.fakebin, "cloudflared"), CLOUDFLARED)
        self.install_ok()
        # the module installed the real stubbed tools; the finish step runs the one in ~/.local/bin, so put the stub there
        write_exec(os.path.join(self.bin_dir, "cloudflared"), CLOUDFLARED)
        unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(unit_dir)
        for unit in ("dc-mcp-bridge.service", "dc-mcp-shim.service", "dc-mcp-tunnel.service"):
            shutil.copy(os.path.join(UBUNTU, "files", ".config", "systemd", "user", unit), unit_dir)

    def cloudflared_calls(self):
        return [c for c in self.calls() if c.startswith("cloudflared")]

    def test_check_is_done_only_while_both_units_are_active(self):
        self.assertEqual(self.run_script("finish.sh", "check").returncode, 1)
        open(os.path.join(self.stub, "active-dc-mcp-bridge.service"), "w").close()
        self.assertEqual(self.run_script("finish.sh", "check").returncode, 1)
        open(os.path.join(self.stub, "active-dc-mcp-tunnel.service"), "w").close()
        self.assertEqual(self.run_script("finish.sh", "check").returncode, 1, "the shim is not active")
        open(os.path.join(self.stub, "active-dc-mcp-shim.service"), "w").close()
        self.assertEqual(self.run_script("finish.sh", "check").returncode, 0)

    def test_run_logs_in_creates_the_tunnel_routes_dns_and_starts_both_units(self):
        res = self.run_script("finish.sh", "run", stdin="y\n")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        cf = self.cloudflared_calls()
        self.assertIn("cloudflared tunnel login", cf)
        self.assertIn("cloudflared tunnel create dc-test", cf)
        self.assertIn("cloudflared tunnel route dns --overwrite-dns dc-test dc.example.com", cf)
        self.assertIn("systemctl --user enable --now dc-mcp-bridge.service dc-mcp-shim.service dc-mcp-tunnel.service", self.calls())
        self.assertEqual(self.run_script("finish.sh", "check").returncode, 0)
        self.assertIn("https://dc.example.com/mcp", res.stdout)
        self.assertIn("dc-mcp-auth.env", res.stdout)
        self.assertNotIn(self.password(), res.stdout + res.stderr)

    def test_declining_the_dns_question_changes_no_record_and_starts_nothing(self):
        res = self.run_script("finish.sh", "run", stdin="n\n")
        self.assertEqual(res.returncode, 1)
        self.assertFalse([c for c in self.cloudflared_calls() if "route" in c])
        self.assertFalse([c for c in self.calls() if "enable" in c])

    def test_a_second_run_reuses_login_and_tunnel(self):
        self.assertEqual(self.run_script("finish.sh", "run", stdin="y\n").returncode, 0)
        before = len(self.cloudflared_calls())
        self.assertEqual(self.run_script("finish.sh", "run", stdin="y\n").returncode, 0)
        again = self.cloudflared_calls()[before:]
        self.assertNotIn("cloudflared tunnel login", again)
        self.assertFalse([c for c in again if "tunnel create" in c])

    def test_a_tunnel_whose_credentials_are_not_here_is_not_recreated_and_dns_is_not_touched(self):
        os.makedirs(os.path.join(self.home, ".cloudflared"))
        with open(os.path.join(self.home, ".cloudflared", "cert.pem"), "w") as fh:
            fh.write("CERT\n")
        open(os.path.join(self.stub, "tunnel-exists"), "w").close()
        res = self.run_script("finish.sh", "run", stdin="y\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("credentials", res.stderr)
        self.assertFalse([c for c in self.cloudflared_calls() if "create" in c or "route" in c])

    def test_a_failing_tunnel_list_is_not_taken_for_a_missing_tunnel(self):
        os.makedirs(os.path.join(self.home, ".cloudflared"))
        with open(os.path.join(self.home, ".cloudflared", "cert.pem"), "w") as fh:
            fh.write("CERT\n")
        open(os.path.join(self.stub, "list-fails"), "w").close()
        res = self.run_script("finish.sh", "run", stdin="y\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("cannot ask Cloudflare", res.stderr)
        self.assertFalse([c for c in self.cloudflared_calls() if "create" in c or "route" in c])

    def test_run_before_apply_says_what_is_missing(self):
        os.remove(os.path.join(self.home, ".config", "systemd", "user", "dc-mcp-tunnel.service"))
        res = self.run_script("finish.sh", "run", stdin="y\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("is not linked yet", res.stderr)
        self.assertEqual(self.cloudflared_calls(), [])


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

    def test_the_icon_the_entry_names_is_shipped_as_a_png(self):
        icon = os.path.join(UBUNTU, "files", ".local", "share", "icons", "hicolor", "48x48", "apps", "chatgpt-window.png")
        with open(icon, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")
        with open(self.DESKTOP) as fh:
            self.assertIn("\nIcon=chatgpt-window\n", fh.read())


NODE = shutil.which("node") or (os.path.join(os.path.expanduser("~"), ".local", "opt", "node", "bin", "node") if os.access(os.path.join(os.path.expanduser("~"), ".local", "opt", "node", "bin", "node"), os.X_OK) else None)
SHIM = os.path.join(UBUNTU, "files", ".local", "share", "dc-mcp-shim", "shim.mjs")
EXTERNAL = "https://dc.example.com"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Upstream(http.server.ThreadingHTTPServer):
    """A stand-in for mcp-auth-proxy: records every request and answers from `self.routes[path]`."""

    daemon_threads = True

    def __init__(self):
        self.seen = []
        self.routes = {}
        self.release_second_chunk = threading.Event()
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.answer(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.answer(self.rfile.read(length))

    def answer(self, body):
        path = self.path.split("?")[0]
        self.server.seen.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        status, headers, payload = self.server.routes.get(path, (404, {}, b"not found"))
        if payload == "idle":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            self.server.release_second_chunk.wait(5)
            return
        if payload == "stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"6\r\nfirst\n\r\n")
            self.wfile.flush()
            self.server.release_second_chunk.wait(5)
            self.wfile.write(b"7\r\nsecond\n\r\n0\r\n\r\n")
            return
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ShimHarness:
    """shim.mjs run for real in front of a stub upstream: setup shared by the shim test classes."""

    def setUp(self):
        self.shim_tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.shim_tmp, ignore_errors=True)
        self.stderr_path = os.path.join(self.shim_tmp, "stderr.txt")
        self.upstream = Upstream()
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.start_shim(**self.shim_env())

    def shim_env(self):
        return {}

    def start_shim(self, **env):
        self.port = free_port()
        with open(self.stderr_path, "w") as stderr:
            self.proc = subprocess.Popen(
                [NODE, SHIM],
                env={**os.environ, "EXTERNAL_URL": EXTERNAL, "LISTEN_PORT": str(self.port), "UPSTREAM_PORT": str(self.upstream.server_address[1]), **env},
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.terminate)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail("the shim did not start listening")

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data



@unittest.skipUnless(NODE, "node is not installed")
class Shim(ShimHarness, unittest.TestCase):
    """files/.local/share/dc-mcp-shim/shim.mjs: the proxying behaviour."""

    def test_a_401_without_a_challenge_gets_the_resource_metadata_challenge(self):
        self.upstream.routes["/mcp"] = (401, {"Content-Type": "text/plain"}, b"unauthorized body")
        res, body = self.request("POST", "/mcp", b"{}")
        self.assertEqual(res.status, 401)
        self.assertEqual(res.getheader("WWW-Authenticate"), f'Bearer resource_metadata="{EXTERNAL}/.well-known/oauth-protected-resource"')
        self.assertEqual(body, b"unauthorized body")

    def test_the_login_pages_of_the_proxy_get_no_challenge(self):
        for path in ("/.auth/password", "/.idp/token"):
            self.upstream.routes[path] = (401, {}, b"wrong password")
            res, body = self.request("POST", path, b"x=1")
            self.assertEqual(res.status, 401, path)
            self.assertIsNone(res.getheader("WWW-Authenticate"), path)
            self.assertEqual(body, b"wrong password")

    def test_a_challenge_the_proxy_already_sent_is_left_alone(self):
        self.upstream.routes["/mcp"] = (401, {"WWW-Authenticate": 'Bearer realm="proxy"'}, b"")
        res, _ = self.request("GET", "/mcp")
        self.assertEqual(res.getheader("WWW-Authenticate"), 'Bearer realm="proxy"')

    def test_other_statuses_get_no_challenge(self):
        self.upstream.routes["/ok"] = (200, {"X-Kept": "yes"}, b"fine")
        self.upstream.routes["/forbidden"] = (403, {}, b"no")
        res, body = self.request("GET", "/ok")
        self.assertEqual((res.status, body, res.getheader("X-Kept"), res.getheader("WWW-Authenticate")), (200, b"fine", "yes", None))
        res, _ = self.request("GET", "/forbidden")
        self.assertEqual((res.status, res.getheader("WWW-Authenticate")), (403, None))

    def test_the_path_suffix_form_of_the_resource_metadata_is_rewritten_to_the_root_form(self):
        self.upstream.routes["/.well-known/oauth-protected-resource"] = (200, {"Content-Type": "application/json"}, b'{"resource":"x"}')
        res, body = self.request("GET", "/.well-known/oauth-protected-resource/mcp?a=1")
        self.assertEqual((res.status, body), (200, b'{"resource":"x"}'))
        self.assertEqual(self.upstream.seen[-1]["path"], "/.well-known/oauth-protected-resource?a=1")
        self.request("GET", "/.well-known/oauth-authorization-server/mcp")
        self.assertEqual(self.upstream.seen[-1]["path"], "/.well-known/oauth-authorization-server/mcp")

    def test_method_query_headers_and_body_reach_the_upstream_unchanged(self):
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        self.request("POST", "/mcp?session=abc", payload, {"Authorization": "Bearer t0k", "Content-Type": "application/json", "X-Custom": "1"})
        seen = self.upstream.seen[-1]
        self.assertEqual((seen["method"], seen["path"], seen["body"]), ("POST", "/mcp?session=abc", payload))
        self.assertEqual((seen["headers"]["authorization"], seen["headers"]["x-custom"]), ("Bearer t0k", "1"))

    def test_hop_by_hop_headers_do_not_cross_the_shim(self):
        self.upstream.routes["/mcp"] = (200, {"Keep-Alive": "timeout=5", "Proxy-Authenticate": "Basic", "X-End-To-End": "kept"}, b"ok")
        res, _ = self.request("GET", "/mcp", headers={"Connection": "X-Private", "X-Private": "secret", "TE": "trailers", "Keep-Alive": "300", "X-Public": "kept"})
        seen = self.upstream.seen[-1]["headers"]
        for name in ("x-private", "te", "keep-alive"):
            self.assertNotIn(name, seen, name)
        self.assertEqual(seen["x-public"], "kept")
        self.assertNotEqual(res.getheader("Keep-Alive"), "timeout=5", "the upstream hop's keep-alive hint is not the client's; Node adds its own")
        self.assertIsNone(res.getheader("Proxy-Authenticate"))
        self.assertEqual(res.getheader("X-End-To-End"), "kept")

    def test_a_streamed_body_is_forwarded_as_it_arrives_not_buffered(self):
        self.upstream.routes["/sse"] = (200, {}, "stream")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/sse")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(res.read1(6), b"first\n", "the first chunk must arrive while the upstream still holds the second")
        self.upstream.release_second_chunk.set()
        self.assertEqual(res.read(), b"second\n")
        conn.close()

    def test_headers_of_an_idle_event_stream_reach_the_client_before_any_body_byte(self):
        """GET /mcp with Accept: text/event-stream: the upstream sends headers and then stays silent; the client must not wait."""
        self.upstream.routes["/mcp"] = (200, {}, "idle")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", "/mcp", headers={"Accept": "text/event-stream", "Authorization": "Bearer t"})
        started = time.time()
        res = conn.getresponse()  # raises a timeout when the shim holds the headers back
        self.assertLess(time.time() - started, 2)
        self.assertEqual((res.status, res.getheader("Content-Type")), (200, "text/event-stream"))
        self.upstream.release_second_chunk.set()
        conn.close()

    def test_an_upstream_that_is_down_is_a_502_not_a_hang(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        res, body = self.request("GET", "/mcp")
        self.assertEqual((res.status, body), (502, b"bad gateway"))

    def test_without_the_public_url_it_refuses_to_start(self):
        proc = subprocess.run([NODE, SHIM], env={k: v for k, v in os.environ.items() if k != "EXTERNAL_URL"}, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("EXTERNAL_URL", proc.stderr)


def rpc_body(method, **extra):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, **extra}).encode()


@unittest.skipUnless(NODE, "node is not installed")
class ShimLog(ShimHarness, unittest.TestCase):
    """The access log of shim.mjs: what it writes, what it must never write, rotation, failure."""

    LOG_ENV = {"LOG_FLUSH_MS": "30"}

    def shim_env(self):
        self.log_dir = os.path.join(self.shim_tmp, "state", "dc-mcp")
        self.log = os.path.join(self.log_dir, "access.log")
        return {"LOG_FILE": self.log, **self.LOG_ENV}

    def lines(self, expected, timeout=5):
        """The log's lines once at least `expected` of them are there (the shim writes in batches)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(self.log) as fh:
                    found = fh.read().splitlines()
            except FileNotFoundError:
                found = []
            if len(found) >= expected:
                return found
            time.sleep(0.03)
        self.fail(f"expected {expected} log lines, found {found}")

    def settle(self):
        time.sleep(0.4)  # several flush intervals: nothing more is coming

    def test_a_chatgpt_shaped_sequence_is_logged_compactly_and_leaks_nothing(self):
        for path in ("/.well-known/oauth-protected-resource", "/.idp/token", "/.idp/auth", "/mcp"):
            self.upstream.routes[path] = (200, {}, b"ok")
        ua_oauth = {"User-Agent": "openai-connectors-oauth/9.8.7-ua (+https://openai.com)"}
        ua_mcp = {"User-Agent": "openai-mcp/9.8.7-ua", "Authorization": "Bearer SECRETTOKEN", "Content-Type": "application/json"}
        self.request("GET", "/.well-known/oauth-protected-resource/mcp?state=SECRETSTATE", headers=ua_oauth)
        self.request("POST", "/.idp/token?code=SECRETCODE", b"code=SECRETCODE&password=hunter2hunter2", ua_oauth)
        self.request("POST", "/mcp", rpc_body("initialize", params={"clientInfo": {"name": "CLIENTSECRET"}}), ua_mcp)
        self.request("POST", "/mcp", rpc_body("tools/list"), ua_mcp)
        self.request("POST", "/mcp", rpc_body("tools/call", params={"name": "get_config", "arguments": {"path": "ARGSECRET"}}), ua_mcp)
        lines = self.lines(5)
        pattern = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z (\d{3}) ([A-Z]+) (\S+) (\d+)ms client=(\S+)( rpc=\S+)?( tool=\S+)?$"
        parsed = [re.match(pattern, line) for line in lines]
        self.assertTrue(all(parsed), lines)
        summary = [(m.group(1), m.group(2), m.group(3), m.group(5), m.group(6), m.group(7)) for m in parsed]
        self.assertEqual(
            summary,
            [
                ("200", "GET", "/.well-known/oauth-protected-resource/mcp", "openai-connectors-oauth", None, None),
                ("200", "POST", "/.idp/token", "openai-connectors-oauth", None, None),
                ("200", "POST", "/mcp", "openai-mcp", " rpc=initialize", None),
                ("200", "POST", "/mcp", "openai-mcp", " rpc=tools/list", None),
                ("200", "POST", "/mcp", "openai-mcp", " rpc=tools/call", " tool=get_config"),
            ],
        )
        text = slurp(self.log)
        for secret in ("SECRETSTATE", "SECRETCODE", "hunter2", "SECRETTOKEN", "ARGSECRET", "CLIENTSECRET", "9.8.7-ua", "Bearer", "arguments", "?"):
            self.assertNotIn(secret, text, secret)
        self.assertEqual(stat.S_IMODE(os.stat(self.log).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.log_dir).st_mode), 0o700)

    def test_every_authenticated_mcp_call_and_oauth_step_is_logged_but_noise_collapses(self):
        for path in ("/.well-known/oauth-protected-resource", "/mcp", "/.idp/token", "/favicon.ico"):
            self.upstream.routes[path] = (401, {}, b"no")
        for _ in range(5):
            self.request("GET", "/.well-known/oauth-protected-resource")
            self.request("POST", "/mcp", rpc_body("tools/list"))  # no Authorization: the discovery handshake, or a scanner
            self.request("GET", "/favicon.ico")
        for _ in range(3):
            self.request("POST", "/mcp", rpc_body("tools/list"), {"Authorization": "Bearer t"})
            self.request("POST", "/.idp/token", b"x=1")
        self.settle()
        lines = self.lines(1)
        by_kind = {}
        for line in lines:
            fields = line.split()
            by_kind.setdefault((fields[2], fields[3], fields[1]), []).append(line)
        self.assertEqual(len(by_kind[("GET", "/.well-known/oauth-protected-resource", "401")]), 1)
        self.assertEqual(len(by_kind[("POST", "/mcp", "401")]), 1 + 3, "one collapsed unauthenticated line plus three authenticated calls")
        self.assertEqual(len(by_kind[("POST", "/.idp/token", "401")]), 3)
        self.assertFalse([line for line in lines if "/favicon.ico" in line])

    def test_collapsed_lines_carry_the_suppressed_count_when_the_minute_is_over(self):
        self.upstream.routes["/probe"] = (404, {}, b"")
        self.request("GET", "/probe")
        self.request("GET", "/probe")
        self.request("GET", "/probe")
        self.assertEqual(len(self.lines(1)), 1)
        self.settle()
        self.assertEqual(len(self.lines(1)), 1)  # still collapsed: the minute has not passed

    def test_a_big_or_broken_body_is_forwarded_untouched_and_logged_without_a_method(self):
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        auth = {"Authorization": "Bearer t"}
        big = rpc_body("tools/call", params={"name": "BIGTOOL", "pad": "x" * 70000})
        broken = b'{"method": "tools/call", "params": {"name": "BROKENTOOL"'
        self.request("POST", "/mcp", big, auth)
        self.request("POST", "/mcp", broken, auth)
        self.request("POST", "/mcp", b"[1, 2]", auth)
        lines = self.lines(3)
        self.assertTrue(all("rpc=?" in line and "tool=" not in line for line in lines), lines)
        self.assertNotIn("TOOL", slurp(self.log))
        self.assertEqual([seen["body"] for seen in self.upstream.seen], [big, broken, b"[1, 2]"], "forwarding is not affected")

    def test_an_unframed_chunked_body_is_not_parsed(self):
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/mcp", body=iter([rpc_body("tools/call", params={"name": "CHUNKTOOL"})]), headers={"Authorization": "Bearer t"}, encode_chunked=True)
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertTrue(self.lines(1)[0].endswith("rpc=?"))
        self.assertNotIn("CHUNKTOOL", slurp(self.log))

    def test_a_batch_logs_each_method_and_tool_name_only(self):
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        batch = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "/etc/shadow"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "bad method with spaces"},
        ]).encode()
        self.request("POST", "/mcp", batch, {"Authorization": "Bearer t"})
        line = self.lines(1)[0]
        self.assertTrue(line.endswith("rpc=tools/call,tools/list,? tool=read_file"), line)
        self.assertNotIn("shadow", slurp(self.log))

    def test_a_scanner_inventing_paths_cannot_push_the_real_records_out(self):
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        self.request("POST", "/mcp", rpc_body("tools/list"), {"Authorization": "Bearer t"})
        for i in range(400):
            self.request("GET", f"/wp-admin/{i}.php")
        self.request("POST", "/mcp", rpc_body("tools/list"), {"Authorization": "Bearer t"})
        self.settle()
        lines = self.lines(3)
        self.assertEqual(len([line for line in lines if " /mcp " in line]), 2)
        self.assertLessEqual(len([line for line in lines if "/wp-admin/" in line]), 1)

    def test_stopping_the_shim_writes_the_buffered_lines_exactly_once(self):
        self.proc.terminate()
        self.proc.wait()
        self.start_shim(LOG_FILE=self.log, LOG_FLUSH_MS="60000")  # nothing is flushed until the shim is told to stop
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        for i in range(3):
            self.request("POST", "/mcp", rpc_body("tools/call", params={"name": f"stop{i}"}), {"Authorization": "Bearer t"})
        self.assertFalse(os.path.exists(self.log))
        self.proc.terminate()
        self.assertEqual(self.proc.wait(timeout=5), 0)
        lines = slurp(self.log).splitlines()
        self.assertEqual([line.split()[-1] for line in lines], ["tool=stop0", "tool=stop1", "tool=stop2"])

    def test_a_server_error_is_always_logged(self):
        self.upstream.routes["/boom"] = (500, {}, b"")
        for _ in range(3):
            self.request("GET", "/boom")
        self.assertEqual(len(self.lines(3)), 3)

    def test_an_upstream_that_is_down_is_logged_as_502_and_reported_once_in_the_journal(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        for _ in range(4):
            res, _ = self.request("GET", "/mcp")
            self.assertEqual(res.status, 502)
        self.assertEqual(len(self.lines(4)), 4)
        self.assertTrue(all(" 502 GET /mcp " in line for line in self.lines(4)))
        with open(self.stderr_path) as fh:
            self.assertEqual(len([line for line in fh if "upstream" in line]), 1)

    def test_requests_leave_nothing_in_the_journal_streams_while_they_succeed(self):
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        for _ in range(5):
            self.request("POST", "/mcp", rpc_body("tools/list"), {"Authorization": "Bearer t"})
        self.lines(5)
        with open(self.stderr_path) as fh:
            self.assertEqual(fh.read(), "")

    def test_rotation_keeps_the_newest_files_drops_the_oldest_and_never_stops_serving(self):
        self.proc.terminate()
        self.proc.wait()
        self.start_shim(LOG_FILE=self.log, LOG_FLUSH_MS="20", LOG_MAX_KIB="1", LOG_KEEP="2")
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        for wave in range(6):
            for i in range(8):
                res, _ = self.request("POST", "/mcp", rpc_body("tools/call", params={"name": f"t{wave}x{i}"}), {"Authorization": "Bearer t"})
                self.assertEqual(res.status, 200)
            time.sleep(0.15)
        self.settle()
        names = sorted(n for n in os.listdir(self.log_dir))
        self.assertEqual(names, ["access.log", "access.log.1", "access.log.2"])
        everything = "".join(slurp(os.path.join(self.log_dir, n)) for n in names)
        self.assertIn("tool=t5x7", everything, "the newest request is kept")
        self.assertNotIn("tool=t0x0", everything, "the oldest was rotated out of existence")
        newest, older = slurp(self.log), slurp(self.log + ".1")
        self.assertLess(older.splitlines()[-1].split()[0], newest.splitlines()[0].split()[0] + "Z", "a rotated file holds older lines than the live one")
        for name in names:
            self.assertLess(os.path.getsize(os.path.join(self.log_dir, name)), 4096)

    def test_an_unwritable_log_directory_never_affects_requests_and_warns_once(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.proc.terminate()
        self.proc.wait()
        locked = os.path.join(self.shim_tmp, "locked")
        os.mkdir(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        log = os.path.join(locked, "sub", "access.log")
        self.start_shim(LOG_FILE=log, LOG_FLUSH_MS="20")
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        for _ in range(6):
            res, body = self.request("POST", "/mcp", rpc_body("tools/list"), {"Authorization": "Bearer t"})
            self.assertEqual((res.status, body), (200, b"ok"))
            time.sleep(0.06)
        with open(self.stderr_path) as fh:
            warnings = [line for line in fh if "cannot write the access log" in line]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("EACCES", warnings[0])
        os.chmod(locked, 0o700)  # fixed: logging resumes by itself and says so once
        self.request("POST", "/mcp", rpc_body("tools/list"), {"Authorization": "Bearer t"})
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.exists(log):
            self.request("POST", "/mcp", rpc_body("tools/list"), {"Authorization": "Bearer t"})
            time.sleep(0.05)
        self.assertTrue(os.path.exists(log))
        time.sleep(0.2)
        with open(self.stderr_path) as fh:
            self.assertEqual(len([line for line in fh if "writable again" in line]), 1)

    def test_without_a_log_file_nothing_is_written_anywhere(self):
        self.proc.terminate()
        self.proc.wait()
        self.start_shim(LOG_FLUSH_MS="20")
        self.upstream.routes["/mcp"] = (200, {}, b"ok")
        self.request("POST", "/mcp", rpc_body("tools/list"), {"Authorization": "Bearer t"})
        time.sleep(0.2)
        self.assertFalse(os.path.exists(self.log_dir))


class JournalFilter(unittest.TestCase):
    """files/.local/bin/dc-mcp-quiet: only warnings and errors survive; the command's own status and stderr do."""

    QUIET = os.path.join(UBUNTU, "files", ".local", "bin", "dc-mcp-quiet")

    def run_quiet(self, script, *args):
        return subprocess.run([self.QUIET, "sh", "-c", script, "sh", *args], capture_output=True, text=True, timeout=20, check=False)

    def test_request_lines_and_message_copies_are_dropped_and_problems_are_kept(self):
        script = r"""
printf '%s\n' '{"severity":"INFO","message":"/mcp","status":200,"user-agent":"openai-mcp"}'
printf '%s\n' '[supergateway] StreamableHttp → Child: {"method":"tools/call","params":{"arguments":{"path":"/private/file"}}}'
printf '%s\n' '[supergateway] Child → StreamableHttp: {"result":"TOOL OUTPUT with an error word in it"}'
printf '%s\n' '[supergateway] Initialize response received'
printf '%s\n' '[supergateway] Child exited: code=null, signal=SIGTERM'
printf '%s\n' '[supergateway] Error: spawn npx ENOENT'
printf '%s\n' '{"severity":"ERROR","message":"backend unreachable"}'
printf '%s\n' 'plain startup problem'
"""
        done = self.run_quiet(script)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ['[supergateway] Error: spawn npx ENOENT', '{"severity":"ERROR","message":"backend unreachable"}', "plain startup problem"])

    def test_the_exit_status_stderr_and_arguments_of_the_command_are_its_own(self):
        done = self.run_quiet('echo "$1 $2" >&2; exit 7', "a b", "c*d")
        self.assertEqual(done.returncode, 7)
        self.assertEqual(done.stderr, "a b c*d\n")

    def test_without_a_command_it_says_how_to_use_it(self):
        done = subprocess.run([self.QUIET], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 2)
        self.assertIn("usage: dc-mcp-quiet", done.stderr)


class LogReader(unittest.TestCase):
    """files/.local/bin/dc-mcp-log over a fixture log and its rotated files."""

    READER = os.path.join(UBUNTU, "files", ".local", "bin", "dc-mcp-log")
    OLD = "2026-11-08T10:00:00.000Z 200 POST /mcp 5ms client=openai-mcp rpc=tools/call tool=old_tool"
    LIVE = [
        "2026-11-08T11:00:00.000Z 401 POST /mcp 1ms client=openai-mcp rpc=initialize",
        "2026-11-08T11:00:01.000Z 200 POST /.idp/token 2ms client=openai-connectors-oauth",
        "2026-11-08T11:00:02.000Z 200 POST /mcp 3ms client=openai-mcp rpc=tools/call tool=get_config",
        "2026-11-08T11:00:03.000Z 200 POST /mcp 4ms client=curl rpc=tools/call tool=read_file",
        "2026-11-08T11:00:04.000Z 500 POST /mcp 9ms client=openai-mcp rpc=tools/call tool=get_config",
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = os.path.join(self.tmp, "access.log")
        with open(self.log, "w") as fh:
            fh.write("\n".join(self.LIVE) + "\n")
        with open(self.log + ".1", "w") as fh:
            fh.write(self.OLD + "\n")

    def read(self, *args):
        return subprocess.run([self.READER, *args], env={**os.environ, "DC_MCP_LOG_FILE": self.log}, capture_output=True, text=True, check=False)

    def test_the_last_lines_and_the_filters(self):
        self.assertEqual(self.read("-n", "2").stdout.splitlines(), self.LIVE[-2:])
        self.assertEqual(self.read("--tool", "get_config").stdout.splitlines(), [self.LIVE[2], self.LIVE[4]])
        self.assertEqual(self.read("--status", "500").stdout.splitlines(), [self.LIVE[4]])
        self.assertEqual(self.read("--rpc", "initialize").stdout.splitlines(), [self.LIVE[0]])
        self.assertEqual(self.read("--client", "curl").stdout.splitlines(), [self.LIVE[3]])
        self.assertEqual(self.read("--oauth").stdout.splitlines(), [self.LIVE[1]])
        self.assertEqual(self.read("--tool", "get_config", "--status", "200").stdout.splitlines(), [self.LIVE[2]])

    def test_a_batch_line_matches_each_of_its_methods_and_tools(self):
        with open(self.log, "a") as fh:
            fh.write("2026-11-08T11:00:05.000Z 200 POST /mcp 7ms client=openai-mcp rpc=tools/call,tools/list tool=read_file,write_file\n")
        self.assertEqual(len(self.read("--tool", "read_file").stdout.splitlines()), 2, "one single call and the batch")
        self.assertEqual(len(self.read("--tool", "write_file").stdout.splitlines()), 1)
        self.assertEqual(len(self.read("--rpc", "tools/list").stdout.splitlines()), 1)
        self.assertEqual(len(self.read("--rpc", "tools/call").stdout.splitlines()), 4, "three single calls plus the batch")
        self.assertEqual(self.read("--tool", "read").stdout, "", "a part of a name is not the name")

    def test_rotated_files_only_with_all_and_oldest_first(self):
        self.assertNotIn("old_tool", self.read().stdout)
        self.assertEqual(self.read("--all", "--tool", "old_tool").stdout.splitlines(), [self.OLD])
        self.assertEqual(self.read("--all", "-n", "6").stdout.splitlines(), [self.OLD, *self.LIVE])

    def test_a_missing_log_and_bad_arguments_are_reported(self):
        os.remove(self.log)
        done = self.read()
        self.assertEqual(done.returncode, 1)
        self.assertIn("no log yet", done.stderr)
        self.assertEqual(self.read("-n", "x").returncode, 2)
        self.assertEqual(self.read("--bogus").returncode, 2)


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
