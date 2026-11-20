"""Offline deployment fixtures: no browser, live Guardian, or real user systemd."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
UNIT = "pm-chatgpt-native-stall"
PRINCIPAL = "owner+home@example.test"


class NativeScannerInstall(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="dot-237-native-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.policy = self.root / "policy.json"
        self.set_policy({"native-stall:principal": PRINCIPAL})
        self.calls = self.root / "calls.jsonl"
        self.manager = self.root / "manager.json"
        self.manager.write_text('{"active": false, "enabled": false}', encoding="utf-8")
        self.pm = self.root / "pm-release"
        self.templates = self.pm / "pmlib/chatgpt_control/units"
        self.templates.mkdir(parents=True)
        self.service = (
            "[Unit]\nDescription=Fixture native scanner\n[Service]\nType=oneshot\n"
            "EnvironmentFile=%h/.config/pm-native-stall/home.env\n"
            "ExecStart=%h/.local/bin/pm chatgpt native-stall once --workspace home --apply --json\n"
        )
        self.timer = "[Timer]\nOnUnitInactiveSec=1min\n[Install]\nWantedBy=timers.target\n"
        (self.templates / f"{UNIT}.service").write_text(self.service, encoding="utf-8")
        (self.templates / f"{UNIT}.timer").write_text(self.timer, encoding="utf-8")
        self.deployment = self.home / ".config/agent-tools/chatgpt-guardian/home.json"
        self.deployment.parent.mkdir(parents=True)
        self.deployment.write_text("{}", encoding="utf-8")
        guardian = self.home / ".local/bin/chatgpt-guardian"
        guardian.parent.mkdir(parents=True)
        self.executable(guardian, """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["DOT237_CALLS"], "a") as log:
    log.write(json.dumps(["guardian", *args]) + "\\n")
if args != ["--workspace", "home", "status"]:
    sys.exit(90)
sys.stdout.write(Path(os.environ["DOT237_POLICY"]).read_text())
""")
        self.executable(self.bin / "systemctl", """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["DOT237_CALLS"], "a") as log:
    log.write(json.dumps(["systemctl", *args]) + "\\n")
if not args or args.pop(0) != "--user":
    sys.exit(91)
if args == ["daemon-reload"]:
    sys.exit(0)
if args == ["show", "-p", "Id", "--value", "default.target"]:
    print("default.target")
    sys.exit(0)
if not args or args[-1] != "pm-chatgpt-native-stall.timer":
    sys.exit(92)
path = Path(os.environ["DOT237_MANAGER"])
state = json.loads(path.read_text())
command = args[0]
if command == "is-enabled":
    if not (Path(os.environ["HOME"]) / ".config/systemd/user/pm-chatgpt-native-stall.timer").exists():
        print("not-found")
        sys.exit(4)
    print("enabled" if state["enabled"] else "disabled")
    sys.exit(0 if state["enabled"] else 1)
if command == "is-active":
    print("active" if state["active"] else "inactive")
    sys.exit(0 if state["active"] else 3)
if args == ["enable", "--now", "pm-chatgpt-native-stall.timer"]:
    state.update(active=True, enabled=True)
elif args == ["disable", "--now", "pm-chatgpt-native-stall.timer"]:
    state.update(active=False, enabled=False)
elif args == ["restart", "pm-chatgpt-native-stall.timer"]:
    state["active"] = True
else:
    sys.exit(93)
path.write_text(json.dumps(state))
""")
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "DOT237_POLICY": str(self.policy),
            "DOT237_CALLS": str(self.calls),
            "DOT237_MANAGER": str(self.manager),
        }

    @staticmethod
    def executable(path, body):
        path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def set_policy(self, controls):
        self.policy.write_text(json.dumps({"controls": controls}), encoding="utf-8")

    def setup(self, *args, enabled=True):
        result = subprocess.run(
            [sys.executable, "-B", str(MODULE / "native-stall-setup"),
             "--pm-package", str(self.pm), "--enabled" if enabled else "--disabled", *args],
            env=self.env, capture_output=True, text=True, timeout=20, check=False,
        )
        self.clean_logs(result)
        self.assertNotIn(PRINCIPAL, result.stdout + result.stderr)
        return result

    def clean_logs(self, result):
        for raw in set(re.findall(r"log: (/tmp/chatgpt-guardian-(?:native|install)-[^\s]+/run.log)", result.stderr)):
            log = Path(raw)
            self.addCleanup(shutil.rmtree, log.parent)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(PRINCIPAL, log.read_text(encoding="utf-8"))

    def calls_after(self, index=0):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()][index:]

    def mutations(self, index=0):
        return [
            row for row in self.calls_after(index)
            if row[0] == "systemctl" and row[2] not in ("is-active", "is-enabled", "show")
        ]

    def test_enabled_policy_installs_private_environment_and_timer(self):
        """Missing environment, wrong scope or missing timer activation breaks durable deployment."""
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.home / ".config/pm-native-stall/home.env"
        self.assertEqual(env.read_text(), f"PM_NATIVE_STALL_PRINCIPAL={PRINCIPAL}\n")
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(env.parent.stat().st_mode & 0o777, 0o700)
        units = self.home / ".config/systemd/user"
        self.assertTrue((units / f"{UNIT}.service").read_text().endswith(self.service))
        self.assertTrue((units / f"{UNIT}.timer").read_text().endswith(self.timer))
        self.assertEqual(json.loads(self.manager.read_text()), {"active": True, "enabled": True})
        self.assertEqual(self.mutations(), [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"],
        ])

    def test_repeat_apply_and_read_only_check_do_not_restart_anything(self):
        self.assertEqual(self.setup().returncode, 0)
        position = len(self.calls_after())
        self.assertEqual(self.setup().returncode, 0)
        self.assertEqual(self.setup("--check").returncode, 0)
        self.assertEqual(self.mutations(position), [])

    def test_disabled_guardian_policy_stops_timer_without_regrant_or_erasing_environment(self):
        self.assertEqual(self.setup().returncode, 0)
        env = self.home / ".config/pm-native-stall/home.env"
        previous = env.read_bytes()
        position = len(self.calls_after())
        self.set_policy({})
        self.assertEqual(self.setup().returncode, 0)
        self.assertEqual(env.read_bytes(), previous)
        self.assertEqual(self.mutations(position), [
            ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"],
        ])
        self.assertTrue(all(
            row == ["guardian", "--workspace", "home", "status"]
            for row in self.calls_after() if row[0] == "guardian"
        ))
        self.assertEqual(self.setup("--check").returncode, 0)

    def test_disabled_option_only_stops_its_managed_timer(self):
        self.assertEqual(self.setup().returncode, 0)
        position = len(self.calls_after())
        self.assertEqual(self.setup(enabled=False).returncode, 0)
        self.assertEqual(self.mutations(position), [
            ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"],
        ])
        self.assertFalse(any(row[0] == "guardian" for row in self.calls_after(position)))

    def test_missing_policy_keeps_new_timer_disabled(self):
        self.set_policy({})
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".config/pm-native-stall/home.env").exists())
        self.assertFalse(json.loads(self.manager.read_text())["active"])
        self.assertEqual(self.mutations(), [])

    def test_unknown_policy_changes_no_units_or_environment(self):
        self.assertEqual(self.setup().returncode, 0)
        position = len(self.calls_after())
        env = self.home / ".config/pm-native-stall/home.env"
        before = env.read_bytes()
        self.policy.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.setup().returncode, 1)
        self.assertEqual(env.read_bytes(), before)
        self.assertEqual(self.mutations(position), [])

    def test_private_environment_drift_is_detected_and_repaired(self):
        self.assertEqual(self.setup().returncode, 0)
        env = self.home / ".config/pm-native-stall/home.env"
        env.chmod(0o644)
        position = len(self.calls_after())
        self.assertEqual(self.setup("--check").returncode, 3)
        self.assertEqual(env.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.mutations(position), [])
        self.assertEqual(self.setup().returncode, 0)
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_symlinked_environment_is_refused_without_touching_target(self):
        directory = self.home / ".config/pm-native-stall"
        directory.mkdir(parents=True)
        target = self.root / "unrelated"
        target.write_text("keep", encoding="utf-8")
        (directory / "home.env").symlink_to(target)
        self.assertEqual(self.setup().returncode, 1)
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(self.mutations(), [])

    def test_changed_timer_restarts_only_timer(self):
        self.assertEqual(self.setup().returncode, 0)
        position = len(self.calls_after())
        (self.templates / f"{UNIT}.timer").write_text(
            self.timer.replace("1min", "2min"), encoding="utf-8",
        )
        self.assertEqual(self.setup().returncode, 0)
        self.assertEqual(self.mutations(position), [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "restart", f"{UNIT}.timer"],
        ])

    def install(self, *args, **config):
        env = {
            **self.env,
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps({
                "native_stall_home": True, "pm_package": str(self.pm), **config,
            }),
        }
        result = subprocess.run(
            [str(MODULE / "install.sh"), *args],
            env=env, capture_output=True, text=True, timeout=20, check=False,
        )
        self.clean_logs(result)
        self.assertNotIn(PRINCIPAL, result.stdout + result.stderr)
        return result

    def test_whole_apply_disables_timer_before_an_unrelated_manual_gate(self):
        """A failed package precondition must not block an explicit disable request."""
        broken = self.root / "incomplete-checkout"
        broken.mkdir()
        for flag in (False, True):
            with self.subTest(option=flag):
                self.set_policy({"native-stall:principal": PRINCIPAL})
                self.assertEqual(self.setup().returncode, 0)
                position = len(self.calls_after())
                if flag:
                    self.set_policy({})
                result = self.install(native_stall_home=flag, dir=str(broken), package=str(broken))
                self.assertEqual(result.returncode, 10, result.stderr)
                self.assertEqual(json.loads(self.manager.read_text()), {"active": False, "enabled": False})
                self.assertEqual(self.mutations(position), [
                    ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"],
                ])
                self.assertTrue(result.stderr.splitlines()[-1].startswith("needs manual: "))
                self.assertIn("has no lib/agenttools_chatgpt/startup.py", result.stderr.splitlines()[-1])
                if not flag:
                    self.assertFalse(any(row[0] == "guardian" for row in self.calls_after(position)))

    def test_unreadable_early_policy_does_not_block_package_repair(self):
        broken = self.root / "incomplete-checkout"
        broken.mkdir()
        guardian = self.home / ".local/bin/chatgpt-guardian"
        working = guardian.read_text(encoding="utf-8")
        for failure in ("missing-cli", "failed-cli", "missing-deployment", "invalid-policy"):
            with self.subTest(failure=failure):
                guardian.write_text(working, encoding="utf-8")
                guardian.chmod(0o755)
                self.deployment.write_text("{}", encoding="utf-8")
                self.set_policy({"native-stall:principal": PRINCIPAL})
                self.assertEqual(self.setup().returncode, 0)
                position = len(self.calls_after())
                env = self.home / ".config/pm-native-stall/home.env"
                previous = env.read_bytes()
                if failure == "missing-cli":
                    guardian.unlink()
                elif failure == "failed-cli":
                    self.executable(guardian, "import sys\nsys.exit(1)\n")
                elif failure == "missing-deployment":
                    self.deployment.unlink()
                else:
                    self.policy.write_text("{broken", encoding="utf-8")
                result = self.install(dir=str(broken), package=str(broken))
                self.assertEqual(result.returncode, 10, result.stderr)
                self.assertIn("deferred until Guardian is repaired", result.stdout)
                self.assertIn("has no lib/agenttools_chatgpt/startup.py", result.stderr.splitlines()[-1])
                self.assertEqual(env.read_bytes(), previous)
                self.assertEqual(self.mutations(position), [])
                self.assertEqual(json.loads(self.manager.read_text()), {"active": True, "enabled": True})

    def test_known_principal_change_stops_old_timer_before_later_gates(self):
        self.assertEqual(self.setup().returncode, 0)
        env = self.home / ".config/pm-native-stall/home.env"
        previous = env.read_bytes()
        position = len(self.calls_after())
        self.set_policy({"native-stall:principal": "replacement@example.test"})
        broken = self.root / "incomplete-checkout"
        broken.mkdir()
        result = self.install(dir=str(broken), package=str(broken))
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertEqual(env.read_bytes(), previous)
        self.assertEqual(self.mutations(position), [
            ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"],
        ])
        self.assertEqual(json.loads(self.manager.read_text()), {"active": False, "enabled": False})
        self.assertNotIn("replacement@example.test", result.stdout + result.stderr)

    def test_unknown_native_check_is_an_error_instead_of_repair_drift(self):
        self.assertEqual(self.setup().returncode, 0)
        position = len(self.calls_after())
        self.policy.write_text("{broken", encoding="utf-8")
        result = self.install("--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("home native scanner cannot be checked", result.stderr.splitlines()[-1])
        self.assertEqual(self.mutations(position), [])

    def test_missing_user_bus_is_reported_before_policy_or_package_work(self):
        self.executable(self.bin / "systemctl", "import sys\nsys.exit(1)\n")
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("systemctl --user is unavailable", result.stderr.splitlines()[-1])
        self.assertFalse(any(row[0] == "guardian" for row in self.calls_after()))

    def test_principal_normalization_matches_guardian_and_pm(self):
        who = "  Owner+Home@Example.Test  "
        self.set_policy({"native-stall:principal": who})
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.home / ".config/pm-native-stall/home.env"
        self.assertEqual(env.read_text(), f"PM_NATIVE_STALL_PRINCIPAL={PRINCIPAL}\n")
        self.assertNotIn(who, result.stdout + result.stderr)
        position = len(self.calls_after())
        self.assertEqual(self.setup("--disable-only").returncode, 0)
        self.assertEqual(self.mutations(position), [])

    def test_whole_check_does_not_open_an_uninitialized_home_store(self):
        self.deployment.unlink()
        result = self.install("--check")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("home native scanner", result.stdout)
        self.assertFalse(any(row[0] == "guardian" for row in self.calls_after()))
        self.assertFalse(self.deployment.exists())
        self.assertEqual(self.mutations(), [])

    def test_disabled_policy_needs_no_pinned_templates(self):
        self.assertEqual(self.setup().returncode, 0)
        position = len(self.calls_after())
        self.set_policy({})
        shutil.rmtree(self.pm)
        self.assertEqual(self.setup().returncode, 0)
        self.assertEqual(self.mutations(position), [
            ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"],
        ])

    def test_unmanaged_units_are_preserved_for_both_option_values(self):
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        timer = units / f"{UNIT}.timer"
        timer.write_text(self.timer, encoding="utf-8")
        self.manager.write_text('{"active": true, "enabled": true}')
        self.assertEqual(self.setup().returncode, 1)
        self.assertEqual(self.setup(enabled=False).returncode, 0)
        self.assertEqual(timer.read_text(), self.timer)
        self.assertFalse((self.home / ".config/pm-native-stall/home.env").exists())
        self.assertEqual(self.mutations(), [])

    def test_invalid_principal_never_becomes_environment_text(self):
        for who in ("a@b.co\nPM_X=y", "$(id)@x.io", "0", 0):
            with self.subTest(principal=who):
                self.set_policy({"native-stall:principal": who})
                self.assertEqual(self.setup().returncode, 1)
                self.assertFalse((self.home / ".config/pm-native-stall/home.env").exists())
                self.assertEqual(self.mutations(), [])

    def test_changed_service_reloads_without_restarting_any_process(self):
        self.assertEqual(self.setup().returncode, 0)
        position = len(self.calls_after())
        (self.templates / f"{UNIT}.service").write_text(self.service + "Nice=10\n", encoding="utf-8")
        self.assertEqual(self.setup().returncode, 0)
        self.assertEqual(self.mutations(position), [["systemctl", "--user", "daemon-reload"]])

    def test_opted_in_plan_is_read_only_and_names_home_scanner(self):
        """Rejecting the new option prevents the canonical installer from managing the scanner."""
        isolated = self.root / "plan-home"
        isolated.mkdir()
        env = {
            **self.env,
            "HOME": str(isolated),
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps({
                "native_stall_home": True, "pm_package": str(self.pm),
            }),
        }
        result = subprocess.run(
            [str(MODULE / "install.sh"), "--plan"],
            env=env, capture_output=True, text=True, timeout=20, check=False,
        )
        self.clean_logs(result)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install service/timer from the pinned PM source", result.stdout)
        self.assertEqual(list(isolated.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
