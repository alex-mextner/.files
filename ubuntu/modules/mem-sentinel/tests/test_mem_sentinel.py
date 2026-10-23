"""mem-sentinel module against a stub agent-tools checkout (real git, local paths) and a staged or fake-systemd root."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load_script():
    path = str(MODULE / "mem-sentinel")
    loader = importlib.machinery.SourceFileLoader("mem_sentinel_module", path)
    spec = importlib.util.spec_from_loader("mem_sentinel_module", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through sys.modules
    loader.exec_module(module)
    return module


ms = load_script()

SCRIPT = b'import os\nX = os.environ.get("MEM_SENTINEL_LOW_AVAIL_PCT")\n_env("LOW_AVAIL_PCT", 25.0)\n_env("SESSION_MAX_GB", 16.0)\n_env("FALL_GB_PER_MIN", 6.0)\n'
UNIT = b"[Unit]\nDescription=stub\n\n[Service]\nExecStart=/usr/bin/python3 -u /usr/local/sbin/mem-sentinel\n\n[Install]\nWantedBy=multi-user.target\n"
ENV = {"LOW_AVAIL_PCT": 25, "SESSION_MAX_GB": 24, "FALL_GB_PER_MIN": 8}


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def make_checkout(path, script=SCRIPT, unit=UNIT):
    """A stub agent-tools checkout whose origin/main holds `script` and `unit`."""
    (path / "ops/mem-sentinel").mkdir(parents=True)
    (path / "ops/mem-sentinel/mem_sentinel.py").write_bytes(script)
    (path / "ops/mem-sentinel/mem-sentinel.service").write_bytes(unit)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "stub")
    git(path, "remote", "add", "origin", str(path))  # fetch has something to talk to: itself
    git(path, "fetch", "-q", "origin")
    return path


class FakeRunner(ms.Runner):
    """Real git (against local stub repositories), a fake systemd."""

    def __init__(self, *, enabled=False, active=False):
        self.enabled = enabled
        self.active = active
        self.crash_on_start = False
        self.loaded = None  # (unit text, drop-in text) systemd has loaded
        self.files = lambda: None
        self.calls = []
        self.queried = []
        self.homes = []

    def _unwrap(self, argv):
        """`runuser -u U -- env HOME=... cmd` runs as the test user: drop the wrapper, remember the HOME it set."""
        if argv[0] == "runuser":
            argv = argv[4:]
            if argv[0] == "env":
                self.homes.append(argv[1])
                argv = argv[2:]
        return argv

    def _ctl(self, argv):
        return argv[2:] if argv[1] == "--user" else argv[1:]

    def query(self, argv, *, timeout=60.0):
        argv = list(argv)
        self.queried.append(tuple(argv))
        argv = self._unwrap(argv)
        if argv[0] == "systemctl":
            verb, *rest = self._ctl(argv)
            if verb == "is-enabled":
                return ms.Result(0 if self.enabled else 1, "enabled\n" if self.enabled else "disabled\n")
            if verb == "is-active":
                return ms.Result(0 if self.active else 3, "active\n" if self.active else "inactive\n")
            if verb == "show":
                return ms.Result(0, "yes\n" if self.files() != self.loaded else "no\n")
            if verb == "reset-failed":
                return ms.Result(0)
        return super().query(argv, timeout=timeout)

    def run(self, argv, *, timeout=300.0):
        argv = list(argv)
        self.calls.append(tuple(argv))
        argv = self._unwrap(argv)
        if argv[0] == "systemctl":
            verb, *rest = self._ctl(argv)
            if verb == "enable":
                self.enabled = True
            elif verb in ("start", "restart"):
                if self.crash_on_start:
                    self.active = False
                else:
                    self.active = True
            elif verb == "daemon-reload":
                self.loaded = self.files()
            return
        super().run(argv, timeout=timeout)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.home = self.tmp / "home/alice"
        self.home.mkdir(parents=True)
        self.account = ms.Account("alice", self.home, 1000)
        self.checkout = make_checkout(self.home / "dev/agent-tools")

    def options(self, **raw):
        raw.setdefault("dir", str(self.checkout))
        raw.setdefault("repo", str(self.tmp / "no-such-remote.git"))  # a test never reaches the network
        return ms.parse_options(raw)

    def staged(self, **raw):
        runner = FakeRunner()
        return ms.MemSentinel(runner, self.root, self.account, self.options(**raw), staged=True, is_root=False)

    def live(self, runner=None, **raw):
        runner = runner or FakeRunner()
        module = ms.MemSentinel(
            runner, self.root, self.account, self.options(**raw), staged=False, is_root=True, settle=0
        )
        runner.files = lambda: tuple(
            (self.root / r).read_bytes() if (self.root / r).exists() else None for r in (ms.UNIT, ms.DROPIN)
        )
        return module, runner

    def user(self, runner=None, **raw):
        runner = runner or FakeRunner()
        module = ms.UserSentinel(
            runner, self.root, self.account, self.options(**raw), staged=False, is_root=False, settle=0
        )
        runner.files = lambda: tuple(
            (self.home / r).read_bytes() if (self.home / r).exists() else None for r in (ms.USER_UNIT,)
        )
        return module, runner

    def installed(self, relative):
        return (self.root / relative).read_bytes()

    def tree(self):
        return sorted(
            (str(p.relative_to(self.root)), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()
        )

    def ok(self, module):
        return {finding.subject: finding.ok for finding in module.findings(module.check_source())}


class Options(unittest.TestCase):
    def test_defaults(self):
        options = ms.parse_options({})
        self.assertEqual(
            (options.dir, options.ref, options.repo_fallback, options.env), ("~/dev/agent-tools", "origin/main", "", ())
        )
        self.assertTrue(options.repo.startswith("https://"))

    def test_refusals(self):
        for raw in (
            {"nope": 1},
            {"dir": "relative/path"},
            {"dir": 3},
            {"repo": "-oProxyCommand=x"},
            {"repo": "ftp://x"},
            {"repo": "has space"},
            {"repo_fallback": "--upload-pack=x"},
            {"ref": "--all"},
            {"ref": "a b"},
            {"source_dir": "rel"},
            {"env": "x"},
            {"env": {"lower": 1}},
            {"env": {"USER": "root"}},
            {"env": {"UID": 0}},
            {"env": {"LOG": "a b"}},
            {"env": {"LOG": "a\nb"}},
            {"env": {"LOG": "%h/x"}},
            {"env": {"LOG": "$X"}},
            {"env": {"LOG": 'a"b'}},
            {"env": {"X": [1]}},
        ):
            with self.subTest(raw=raw), self.assertRaises(ms.ModuleError):
                ms.parse_options(raw)

    def test_numbers_are_rendered_exactly_never_as_an_exponent(self):
        options = ms.parse_options({"env": {"A": 1234567, "B": 1000000, "C": 0.0000123, "D": 2.5, "E": 7.0}})
        self.assertEqual(dict(options.env), {"A": "1234567", "B": "1000000", "C": "1.23e-05", "D": "2.5", "E": "7.0"})

    def test_nan_and_infinity_are_refused(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ms.ModuleError):
                ms.parse_options({"env": {"X": value}})

    def test_env_is_rendered_sorted_and_typed(self):
        options = ms.parse_options(
            {"env": {"SESSION_MAX_GB": 24, "DRY_RUN": True, "FALL_GB_PER_MIN": 7.5, "LOG": "/var/x.log"}}
        )
        text = ms.render_dropin(ms.Account("alice", Path("/home/alice"), 1000), options)
        lines = [line for line in text.splitlines() if line.startswith("Environment=")]
        self.assertEqual(
            lines,
            [
                "Environment=MEM_SENTINEL_USER=alice",
                "Environment=MEM_SENTINEL_UID=1000",
                "Environment=MEM_SENTINEL_DRY_RUN=1",
                "Environment=MEM_SENTINEL_FALL_GB_PER_MIN=7.5",
                "Environment=MEM_SENTINEL_LOG=/var/x.log",
                "Environment=MEM_SENTINEL_SESSION_MAX_GB=24",
            ],
        )
        self.assertEqual(text, ms.render_dropin(ms.Account("alice", Path("/home/alice"), 1000), options))

    def test_the_user_is_never_a_literal_name(self):
        bob = ms.render_dropin(ms.Account("bob", Path("/home/bob"), 1234), ms.parse_options({}))
        self.assertIn("MEM_SENTINEL_USER=bob", bob)
        self.assertIn("MEM_SENTINEL_UID=1234", bob)
        self.assertNotIn("ultra", bob)


class StagedInstall(Base):
    def test_install_writes_code_unit_dropin_logrotate_and_stamp(self):
        module = self.staged(env=ENV)
        self.assertEqual(set(self.ok(module).values()), {False})
        module.apply()
        self.assertEqual(self.installed(ms.BIN), SCRIPT)
        self.assertEqual(self.installed(ms.UNIT), UNIT)
        self.assertEqual((self.root / ms.BIN).stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / ms.UNIT).stat().st_mode & 0o777, 0o644)
        dropin = self.installed(ms.DROPIN).decode()
        self.assertIn("Environment=MEM_SENTINEL_USER=alice", dropin)
        self.assertIn("Environment=MEM_SENTINEL_SESSION_MAX_GB=24", dropin)
        self.assertIn("rotate 7", self.installed(ms.LOGROTATE).decode())
        self.assertIn(f" {ms.BIN}", self.installed(ms.STAMP).decode())
        self.assertEqual(set(self.ok(module).values()), {True})
        self.assertTrue((self.root / "var/log/hang-probe").is_dir())

    def test_a_second_install_changes_nothing(self):
        module = self.staged(env=ENV)
        module.apply()
        before = self.tree()
        module.apply()
        self.assertEqual(self.tree(), before)
        self.assertEqual(module.plan(), ["nothing to change"])

    def test_plan_is_read_only_and_shows_the_dropin_diff(self):
        module = self.staged(env=ENV)
        plan = module.plan()
        self.assertEqual(self.tree(), [])
        self.assertTrue(any(line.startswith("create /usr/local/sbin/mem-sentinel (mode 755") for line in plan))
        self.assertIn("create /etc/systemd/system/mem-sentinel.service.d/10-ubuntu-tweaks.conf", plan)
        self.assertTrue(any(line.strip() == "+Environment=MEM_SENTINEL_SESSION_MAX_GB=24" for line in plan))

    def test_changed_tunable_is_drift_and_an_update(self):
        self.staged(env=ENV).apply()
        changed = self.staged(env={**ENV, "SESSION_MAX_GB": 30})
        self.assertFalse(self.ok(changed)["/etc/systemd/system/mem-sentinel.service.d/10-ubuntu-tweaks.conf"])
        plan = changed.plan()
        self.assertTrue(any(line.strip() == "-Environment=MEM_SENTINEL_SESSION_MAX_GB=24" for line in plan))
        changed.apply()
        self.assertIn("SESSION_MAX_GB=30", self.installed(ms.DROPIN).decode())

    def test_a_hand_edited_installed_script_is_drift_even_without_a_source(self):
        module = self.staged(env=ENV)
        module.apply()
        (self.root / ms.BIN).write_bytes(SCRIPT + b"# local edit\n")
        shutil.rmtree(self.checkout)
        blind = self.staged(env=ENV)
        self.assertFalse(self.ok(blind)["/usr/local/sbin/mem-sentinel"])

    def test_without_a_source_the_stamp_vouches_for_the_installed_copy(self):
        self.staged(env=ENV).apply()
        shutil.rmtree(self.checkout)
        blind = self.staged(env=ENV)
        self.assertEqual(set(self.ok(blind).values()), {True})
        self.assertEqual(blind.plan(), ["nothing to change"])

    def test_without_a_source_a_hand_edited_install_is_not_re_stamped(self):
        module = self.staged(env=ENV)
        module.apply()
        (self.root / ms.BIN).write_bytes(SCRIPT + b"# local edit\n")
        shutil.rmtree(self.checkout)
        blind = self.staged(env=ENV, repo=str(self.tmp / "nope.git"))  # never the network in a test
        self.assertIn("cannot repair", " ".join(blind.plan()))
        with self.assertRaisesRegex(ms.ModuleError, "differs from its install stamp"):
            blind.apply()
        self.assertFalse(self.ok(blind)["/usr/local/sbin/mem-sentinel"])  # and it stays drift

    def test_on_a_fresh_machine_plan_asks_for_the_source_and_does_not_claim_an_edit(self):
        shutil.rmtree(self.checkout)
        plan = self.staged(env=ENV).plan()
        self.assertTrue(plan[0].startswith("get agent-tools (git clone"))
        self.assertNotIn("cannot repair", " ".join(plan))

    def test_nothing_installed_and_no_source_is_an_error_naming_every_attempt(self):
        shutil.rmtree(self.checkout)
        module = self.staged(
            repo=str(self.tmp / "missing.git"),
            repo_fallback=str(self.tmp / "missing2.git"),
            source_dir=str(self.tmp / "nodir"),
        )
        with self.assertRaises(ms.ModuleError) as caught:
            module.apply()
        message = str(caught.exception)
        for needle in ("missing.git", "missing2.git", "source_dir"):
            self.assertIn(needle, message)
        self.assertEqual(self.tree(), [])


class Source(Base):
    def test_work_in_progress_in_the_checkout_never_reaches_the_installed_copy(self):
        (self.checkout / "ops/mem-sentinel/mem_sentinel.py").write_bytes(b"raise SystemExit('wip')\n")  # uncommitted
        git(self.checkout, "checkout", "-q", "-b", "feature")
        (self.checkout / "ops/mem-sentinel/mem-sentinel.service").write_bytes(b"[Service]\nExecStart=/wip\n")
        git(self.checkout, "commit", "-qam", "wip")
        self.staged().apply()
        self.assertEqual(self.installed(ms.BIN), SCRIPT)
        self.assertEqual(self.installed(ms.UNIT), UNIT)

    def test_an_empty_ref_installs_the_working_tree(self):
        (self.checkout / "ops/mem-sentinel/mem_sentinel.py").write_bytes(SCRIPT + b"# edited\n")
        self.staged(ref="").apply()
        self.assertEqual(self.installed(ms.BIN), SCRIPT + b"# edited\n")

    def test_a_missing_checkout_is_cloned_from_repo(self):
        remote = make_checkout(self.tmp / "remote")
        target = self.home / "dev/fresh"
        module = self.staged(dir=str(target), repo=str(remote))
        module.apply()
        self.assertTrue((target / ".git").exists())
        self.assertEqual(self.installed(ms.BIN), SCRIPT)  # read from the clone's origin/main

    def test_the_second_url_is_tried_when_the_first_fails(self):
        remote = self.tmp / "remote"
        make_checkout(remote)
        git(remote, "branch", "-m", "main")
        target = self.home / "dev/fresh"
        module = self.staged(dir=str(target), repo=str(self.tmp / "nope.git"), repo_fallback=str(remote), ref="")
        module.apply()
        self.assertEqual(self.installed(ms.BIN), SCRIPT)

    def test_source_dir_is_the_last_resort(self):
        shutil.rmtree(self.checkout)
        flat = self.tmp / "flat"
        flat.mkdir()
        (flat / "mem_sentinel.py").write_bytes(SCRIPT)
        (flat / "mem-sentinel.service").write_bytes(UNIT)
        module = self.staged(dir=str(self.home / "dev/none"), repo=str(self.tmp / "nope.git"), source_dir=str(flat))
        module.apply()
        self.assertEqual(self.installed(ms.BIN), SCRIPT)

    def test_a_failed_fetch_keeps_the_last_fetched_ref_with_a_warning(self):
        git(self.checkout, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        module, runner = self.live()
        module.apply()
        self.assertEqual(self.installed(ms.BIN), SCRIPT)
        fetches = [call for call in runner.queried if "fetch" in call]
        self.assertTrue(fetches and fetches[0][:4] == ("runuser", "-u", "alice", "--"))

    def test_git_runs_as_the_desktop_user_when_root(self):
        module, runner = self.live()
        module.apply()
        git_calls = [call for call in runner.queried if "git" in call]
        self.assertTrue(git_calls)
        self.assertTrue(all(call[:4] == ("runuser", "-u", "alice", "--") for call in git_calls))

    def test_git_gets_the_desktop_users_own_home_not_roots(self):
        module, runner = self.live()
        module.apply()
        self.assertTrue(runner.homes)
        self.assertEqual(set(runner.homes), {f"HOME={self.home}"})

    def test_code_is_installed_byte_for_byte_and_code_python_cannot_read_is_refused(self):
        odd = SCRIPT + "# café ✓\r\n".encode()
        self.staged(dir=str(make_checkout(self.tmp / "odd", script=odd))).apply()
        self.assertEqual(self.installed(ms.BIN), odd)
        broken = make_checkout(self.tmp / "broken", script=SCRIPT + b"# caf\xe9 \xff\n")
        with self.assertRaisesRegex(ms.ModuleError, "does not parse"):
            self.staged(dir=str(broken)).apply()

    def test_a_git_blob_over_the_size_limit_is_refused(self):
        big = make_checkout(self.tmp / "big", script=SCRIPT + b"#" * (ms.MAX_SOURCE_BYTES + 1))
        with self.assertRaisesRegex(ms.ModuleError, "larger than"):
            self.staged(dir=str(big)).apply()

    def test_a_source_that_does_not_parse_is_refused_before_anything_is_written(self):
        bad = make_checkout(self.tmp / "bad", script=b"def (:\n")
        with self.assertRaisesRegex(ms.ModuleError, "does not parse"):
            self.staged(dir=str(bad)).apply()
        self.assertEqual(self.tree(), [])

    def test_a_unit_that_starts_another_path_is_refused(self):
        bad = make_checkout(self.tmp / "bad", unit=b"[Service]\nExecStart=/opt/elsewhere\n")
        with self.assertRaisesRegex(ms.ModuleError, "does not start"):
            self.staged(dir=str(bad)).apply()

    def test_an_env_name_the_sentinel_does_not_read_is_refused(self):
        with self.assertRaisesRegex(ms.ModuleError, "reads no such MEM_SENTINEL_ knob"):
            self.staged(env={"LOW_AVAIL_PTC": 25}).apply()
        self.assertEqual(self.tree(), [])

    def test_known_knobs_are_read_from_the_sentinels_own_config(self):
        self.assertEqual(ms.known_env_names(SCRIPT), {"LOW_AVAIL_PCT", "SESSION_MAX_GB", "FALL_GB_PER_MIN"})


class Logrotate(Base):
    def test_another_file_that_covers_the_log_replaces_ours(self):
        self.staged().apply()
        self.assertTrue((self.root / ms.LOGROTATE).exists())
        (self.root / "etc/logrotate.d").mkdir(exist_ok=True)
        (self.root / "etc/logrotate.d/hang-probe").write_text("/var/log/hang-probe/*.log {\n  weekly\n}\n")
        module = self.staged()
        self.assertFalse(self.ok(module)["/etc/logrotate.d/mem-sentinel"])
        self.assertTrue(any("remove /etc/logrotate.d/mem-sentinel" in line for line in module.plan()))
        module.apply()
        self.assertFalse((self.root / ms.LOGROTATE).exists())
        self.assertEqual(set(self.ok(module).values()), {True})


class LogrotateCoverage(Base):
    def cover(self, name, text):
        (self.root / "etc/logrotate.d").mkdir(parents=True, exist_ok=True)
        (self.root / "etc/logrotate.d" / name).write_text(text)
        return self.staged()._other_logrotate_covers_log()

    def test_a_comment_or_a_file_logrotate_skips_does_not_count(self):
        self.assertFalse(self.cover("hang-probe", "# /var/log/hang-probe/*.log {\n#   weekly\n# }\n"))
        for name in ("hang-probe.dpkg-old", "hang-probe.disabled", "hang-probe~", ".hang-probe", "hang-probe.swp"):
            with self.subTest(name=name):
                self.assertFalse(self.cover(name, "/var/log/hang-probe/*.log {\n  weekly\n}\n"))

    def test_the_glob_or_the_explicit_log_path_counts(self):
        self.assertTrue(self.cover("hang-probe", "/var/log/hang-probe/*.log {\n  weekly\n}\n"))
        self.assertTrue(self.cover("sentinel", "/var/log/hang-probe/sentinel.log {\n  weekly\n}\n"))

    def test_our_own_file_never_counts_for_itself(self):
        self.assertFalse(self.cover("mem-sentinel", ms.render_logrotate()))


class WithSystemd(Base):
    def test_first_install_enables_and_starts_once(self):
        module, runner = self.live()
        module.apply()
        verbs = [call[1] for call in runner.calls if call[0] == "systemctl"]
        self.assertEqual(verbs, ["daemon-reload", "enable", "start"])
        self.assertEqual(set(self.ok(module).values()), {True})

    def test_a_second_install_calls_systemctl_for_nothing(self):
        module, runner = self.live()
        module.apply()
        runner.calls.clear()
        module.apply()
        self.assertEqual([call for call in runner.calls if call[0] == "systemctl"], [])
        self.assertEqual(module.plan(), ["nothing to change"])

    def test_a_changed_tunable_reloads_and_restarts(self):
        module, runner = self.live(env=ENV)
        module.apply()
        runner.calls.clear()
        changed, _ = self.live(runner, env={**ENV, "SESSION_MAX_GB": 30})
        changed.apply()
        self.assertEqual([call[1] for call in runner.calls if call[0] == "systemctl"], ["daemon-reload", "restart"])

    def test_plan_predicts_a_restart_only_when_the_code_unit_or_dropin_changes(self):
        module, runner = self.live()
        module.apply()
        (
            self.root / ms.LOGROTATE
        ).unlink()  # only the log rotation differs: no restart in the plan, none in the install
        plan = module.plan()
        self.assertEqual(plan[0], "create /etc/logrotate.d/mem-sentinel")
        self.assertFalse([line for line in plan if line.startswith("systemctl")])
        runner.calls.clear()
        module.apply()
        self.assertEqual([call[1] for call in runner.calls if call[0] == "systemctl"], [])

    def test_a_stopped_service_is_started_without_rewriting_files(self):
        module, runner = self.live()
        module.apply()
        runner.active = False
        before = self.tree()
        runner.calls.clear()
        module.apply()
        self.assertEqual([call[1] for call in runner.calls if call[0] == "systemctl"], ["start"])
        self.assertEqual(self.tree(), before)

    def test_a_service_that_does_not_stay_up_is_an_error(self):
        module, runner = self.live()
        runner.crash_on_start = True
        with self.assertRaisesRegex(ms.ModuleError, "did not stay active"):
            module.apply()

    def test_install_needs_root(self):
        runner = FakeRunner()
        module = ms.MemSentinel(runner, self.root, self.account, self.options(), staged=False, is_root=False)
        with self.assertRaisesRegex(ms.ModuleError, "must run as root"):
            module.apply()
        self.assertEqual(self.tree(), [])

    def test_plan_lists_the_service_steps(self):
        module, _ = self.live()
        plan = module.plan()
        self.assertIn("systemctl enable mem-sentinel.service", plan)
        self.assertIn("systemctl start mem-sentinel.service", plan)


class RootlessFailover(Base):
    def test_user_install_writes_the_script_and_a_user_unit_that_yields_to_the_system_one(self):
        module, runner = self.user()
        module.apply()
        script = self.home / ms.USER_SCRIPT
        self.assertEqual(script.read_bytes(), SCRIPT)
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)
        unit = (self.home / ms.USER_UNIT).read_text()
        self.assertIn("ConditionPathExists=!/usr/local/sbin/mem-sentinel", unit)
        self.assertIn("ExecStart=/usr/bin/python3 -u %h/.local/lib/mem-sentinel/mem_sentinel.py", unit)
        self.assertIn("Environment=MEM_SENTINEL_USER=alice", unit)
        self.assertIn("Environment=MEM_SENTINEL_LOG=%h/.local/state/mem-sentinel/sentinel.log", unit)
        self.assertEqual(
            [call[1:3] for call in runner.calls if call[0] == "systemctl"],
            [("--user", "daemon-reload"), ("--user", "enable"), ("--user", "start")],
        )
        self.assertEqual(set(self.ok(module).values()), {True})
        self.assertFalse(any(call[0] == "runuser" for call in runner.queried))

    def test_user_install_is_idempotent_and_carries_the_tunables(self):
        module, runner = self.user(env=ENV)
        module.apply()
        self.assertIn("Environment=MEM_SENTINEL_SESSION_MAX_GB=24", (self.home / ms.USER_UNIT).read_text())
        runner.calls.clear()
        module.apply()
        self.assertEqual([call for call in runner.calls if call[0] == "systemctl"], [])

    def test_the_rootless_plan_with_an_unreachable_source_is_nothing_to_change(self):
        module, runner = self.user()
        module.apply()
        shutil.rmtree(self.checkout)
        blind, _ = self.user(runner)
        self.assertEqual(blind.plan(), ["nothing to change"])

    def test_root_must_not_install_the_rootless_copy(self):
        runner = FakeRunner()
        module = ms.UserSentinel(runner, self.root, self.account, self.options(), staged=False, is_root=True)
        with self.assertRaisesRegex(ms.ModuleError, "not by root"):
            module.apply()

    def test_check_judges_the_user_copy_only_when_there_is_no_system_install(self):
        user, _ = self.user()
        user.apply()
        as_user = ms.MemSentinel(FakeRunner(), self.root, self.account, self.options(), staged=False, is_root=False)
        self.assertIs(ms.choose(as_user, user), user)
        as_root, _ = self.live()  # root would ask root's own user manager about the copy: never judge it
        self.assertIs(ms.choose(as_root, user), as_root)
        (self.root / "usr/local/sbin").mkdir(parents=True)
        (self.root / ms.BIN).write_bytes(SCRIPT)
        self.assertIs(ms.choose(as_user, user), as_user)
        self.assertIs(ms.choose(as_user, None), as_user)

    def test_the_rootless_copy_refuses_to_install_beside_a_system_install(self):
        (self.root / "usr/local/sbin").mkdir(parents=True)
        (self.root / ms.BIN).write_bytes(SCRIPT)
        user, runner = self.user()
        with self.assertRaisesRegex(ms.ModuleError, "root install is the one to repair"):
            user.apply()
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.home / ms.USER_UNIT).exists())


class Coexistence(Base):
    """Both the system service and the rootless copy would steer the same session: the root install retires the copy."""

    def system_cli(self, runner):
        system = ms.MemSentinel(runner, self.root, self.account, self.options(), staged=False, is_root=True, settle=0)
        user = ms.UserSentinel(runner, self.root, self.account, self.options(), staged=False, is_root=True, settle=0)
        runner.files = lambda: tuple(
            (self.root / r).read_bytes() if (self.root / r).exists() else None for r in (ms.UNIT, ms.DROPIN)
        )
        return system, user

    def test_install_retires_a_user_copy_through_the_users_manager(self):
        runner = FakeRunner()
        unit = self.home / ms.USER_UNIT
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\n")
        system, user = self.system_cli(runner)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(ms.main(system, user, ["install"], out, err), 0, err.getvalue())
        retire = [call for call in runner.calls if "--machine" in call]
        self.assertEqual(
            retire, [("systemctl", "--user", "--machine", "alice@.host", "disable", "--now", "mem-sentinel.service")]
        )

    def test_a_machine_without_the_copy_runs_nothing_for_it(self):
        runner = FakeRunner()
        system, user = self.system_cli(runner)
        ms.main(system, user, ["install"], io.StringIO(), io.StringIO())
        self.assertEqual([call for call in runner.calls if "--machine" in call], [])

    def test_check_reports_a_copy_that_still_runs_beside_the_service(self):
        runner = FakeRunner()
        system, user = self.system_cli(runner)
        ms.main(system, user, ["install"], io.StringIO(), io.StringIO())
        user_runner = FakeRunner(enabled=True, active=True)  # the user manager: the copy is up
        user_module = ms.UserSentinel(user_runner, self.root, self.account, self.options(), staged=False, is_root=False)
        system = ms.MemSentinel(
            runner, self.root, self.account, self.options(), staged=False, is_root=False
        )  # check runs as the user
        (self.home / ms.USER_UNIT).parent.mkdir(parents=True, exist_ok=True)
        (self.home / ms.USER_UNIT).write_text("[Service]\n")
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(ms.main(system, user_module, ["check"], out, err), 3)
        self.assertIn("no rootless copy of mem-sentinel runs beside the service", out.getvalue())


class RootWithoutADesktopUser(unittest.TestCase):
    def test_a_root_shell_is_refused_rather_than_naming_root_as_the_user(self):
        if os.geteuid() != 0:
            self.skipTest("only meaningful as root")
        with self.assertRaisesRegex(ms.ModuleError, "cannot tell which desktop user"):
            ms.build({})

    def test_build_refuses_uid_0_when_not_staged(self):
        original = ms.os.geteuid
        ms.os.geteuid = lambda: 0
        try:
            with self.assertRaisesRegex(ms.ModuleError, "cannot tell which desktop user"):
                ms.build({"SUDO_USER": "root"})
        finally:
            ms.os.geteuid = original


class Cli(Base):
    """The script as the engine runs it: staged root, JSON options in the environment, exit codes."""

    def run_script(self, *argv, **raw):
        raw.setdefault("dir", str(self.checkout))
        raw.setdefault("repo", str(self.tmp / "no-such-remote.git"))
        env = {**os.environ, ms.ROOT_ENV: str(self.root), ms.CONFIG_ENV: json.dumps(raw)}
        return subprocess.run([str(MODULE / "mem-sentinel"), *argv], env=env, capture_output=True, text=True)

    def test_check_plan_install_check(self):
        self.assertEqual(self.run_script("check", env=ENV).returncode, 3)
        plan = self.run_script("plan", env=ENV)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn("create /usr/local/sbin/mem-sentinel", plan.stdout)
        self.assertEqual(self.tree(), [])
        installed = self.run_script("install", env=ENV)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(self.run_script("check", env=ENV).returncode, 0)
        self.assertEqual(self.run_script("plan", env=ENV).stdout, "nothing to change\n")
        self.assertEqual(self.run_script("check", env={**ENV, "SESSION_MAX_GB": 31}).returncode, 3)

    def test_bad_options_exit_1_and_write_nothing(self):
        for raw in ({"env": {"X": "a b"}}, {"bogus": 1}, {"repo": "-x"}):
            with self.subTest(raw=raw):
                result = self.run_script("install", **raw)
                self.assertEqual(result.returncode, 1)
                self.assertIn("mem-sentinel:", result.stderr)
        self.assertEqual(self.tree(), [])

    def test_usage_error(self):
        self.assertEqual(self.run_script("frobnicate").returncode, 2)


class AgentToolsParity(unittest.TestCase):
    """This module repeats the few steps of agent-tools' ops/mem-sentinel/install.sh; when a checkout exists the
    paths must still be the same ones."""

    INSTALLER = Path.home() / "dev/agent-tools/ops/mem-sentinel/install.sh"

    @unittest.skipUnless(INSTALLER.exists(), "no agent-tools checkout here")
    def test_destination_paths_match_the_installer(self):
        text = self.INSTALLER.read_text()
        self.assertIn(f"BIN=/{ms.BIN}", text)
        self.assertIn(f"UNIT=/{ms.UNIT}", text)
        self.assertIn(f"LOG={ms.SENTINEL_LOG}", text)
        self.assertIn("mem_sentinel.py", text)
        self.assertIn("mem-sentinel.service", text)


class Manifest(unittest.TestCase):
    def test_module_toml_is_a_root_system_module_with_a_rootless_fallback(self):
        import tomllib

        data = tomllib.loads((MODULE / "module.toml").read_text())
        self.assertEqual(
            data["source"],
            {
                "kind": "system",
                "root": True,
                "install": "mem-sentinel install",
                "check": "mem-sentinel check",
                "preview": "mem-sentinel plan",
            },
        )
        self.assertEqual(
            data["fallback"],
            [
                {
                    "install": "mem-sentinel install-user",
                    "root": False,
                    "description": data["fallback"][0]["description"],
                }
            ],
        )
        self.assertTrue(all(rule["class"] == "tracked" for rule in data["coverage"]))
        self.assertTrue(os.access(MODULE / "mem-sentinel", os.X_OK))


if __name__ == "__main__":
    unittest.main()
