"""chatgpt-guardian `profile-sync`: the cookie copy, the safe stop / start order, the verification and the rollback, against fakes.

No real Chrome, systemd or ChatGPT: the two Cookies databases are sqlite files in a temp dir (Chrome's schema, made-up
"encrypted" blobs), systemctl and journalctl are shell fakes that append to one events log, the DevTools port is the
in-process server of test_relogin.py, HOME is a temp dir. Nothing of the real profiles is read or written.
"""

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_relogin import (
    CHATGPT_TAB,
    FAKE_SYSTEMCTL,
    NEWTAB,
    DevTools,
    TabbedDevTools,
    make_executable,
    session,
)

MODULE = Path(__file__).resolve().parents[1]
PROFILE_SYNC = MODULE / "profile-sync"

SCHEMA = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
CREATE TABLE cookies(creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL, name TEXT NOT NULL,
  value TEXT NOT NULL, encrypted_value BLOB NOT NULL, path TEXT NOT NULL, expires_utc INTEGER NOT NULL, is_secure INTEGER NOT NULL,
  is_httponly INTEGER NOT NULL, last_access_utc INTEGER NOT NULL, has_expires INTEGER NOT NULL, is_persistent INTEGER NOT NULL,
  priority INTEGER NOT NULL, samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL, source_port INTEGER NOT NULL,
  last_update_utc INTEGER NOT NULL, source_type INTEGER NOT NULL, has_cross_site_ancestor INTEGER NOT NULL);
CREATE UNIQUE INDEX cookies_unique_index ON cookies(host_key, top_frame_site_key, name, path, source_scheme, source_port);
"""

# value = the made-up secret behind each row; the tests assert that none of them is ever printed
SOURCE_ROWS = [
    (".chatgpt.com", "__Secure-next-auth.session-token", b"v11SOURCE-SESSION-SECRET"),
    ("chatgpt.com", "oai-did", b"v11SOURCE-DEVICE-SECRET"),
    ("auth.openai.com", "login_session", b"v10SOURCE-AUTH-SECRET"),
    (".openai.com", "oai-sc", b"v11SOURCE-SC-SECRET"),
    (".google.com", "SID", b"v11SOURCE-GOOGLE-SECRET"),  # another site: never copied
    (".evilchatgpt.com", "look-alike", b"v11SOURCE-EVIL-SECRET"),  # a look-alike host: never copied
    ("fakeopenai.com", "look-alike", b"v11SOURCE-FAKE-SECRET"),
]
TARGET_ROWS = [
    (".chatgpt.com", "__Secure-next-auth.session-token", b"v11TARGET-OLD-SESSION"),
    (".chatgpt.com", "stale-cookie", b"v11TARGET-OLD-STALE"),
    (".google.com", "SID", b"v11TARGET-GOOGLE-KEEP"),
]
SECRETS = [blob.decode() for _, _, blob in SOURCE_ROWS + TARGET_ROWS]

FAKE_JOURNALCTL = """#!/bin/sh
# fake `journalctl --user -u <unit>.service ...`: a guardian prints its health line only while it is active
echo "journalctl $*" >> "$FAKE_EVENTS"
unit=${2%.service}
if [ -e "$FAKE_STATE/active/$unit" ]; then echo '{"health": "connected", "watched": 0}'; fi
exit 0
"""


def make_cookies(path: Path, rows, version="24"):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO meta VALUES ('version', ?)", (version,))
    for host, name, blob in rows:
        connection.execute("INSERT INTO cookies VALUES (1, ?, '', ?, '', ?, '/', 99999999999999999, 1, 1, 1, 1, 1, 1, -1, 2, 443, 1, 0, 0)", (host, name, blob))
    connection.commit()
    connection.close()


def read_rows(path: Path):
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return sorted(connection.execute("SELECT host_key, name, encrypted_value FROM cookies"))
    finally:
        connection.close()


class Sandbox:
    def __init__(self, tmp_path, workspace="home"):
        self.workspace = workspace
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.state = tmp_path / "state"
        (self.state / "active").mkdir(parents=True)
        self.events = tmp_path / "events.log"
        self.events.write_text("", encoding="utf-8")
        self.source = tmp_path / "dock"
        self.target = tmp_path / "profiles" / ("home" if workspace == "home" else "glide.vc")
        self.backups = tmp_path / "backups"
        make_cookies(self.source / "Default" / "Cookies", SOURCE_ROWS)
        make_cookies(self.target / "Default" / "Cookies", TARGET_ROWS)
        self.unit = f"chatgpt-guardian-{'home' if workspace == 'home' else 'glide'}"
        self.units = [f"{self.unit}-browser", f"{self.unit}-observer", self.unit]
        domain = "gmail.com" if workspace == "home" else "app.glide.vc"
        self.browser = DevTools([CHATGPT_TAB], session(domain))
        self.marker = tmp_path / "marker"
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "UBUNTU_TWEAK_LANG": "en",
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps({"glide_email_domain": "app.glide.vc"}),
            "FAKE_STATE": str(self.state),
            "FAKE_EVENTS": str(self.events),
            "CHATGPT_PROFILE_SYNC_SOURCE": str(self.source),
            "CHATGPT_PROFILE_SYNC_TARGET": str(self.target),
            "CHATGPT_PROFILE_SYNC_PORT": str(self.browser.port),
            "CHATGPT_PROFILE_SYNC_BACKUPS": str(self.backups),
            "CHATGPT_RELOGIN_LOCK_DIR": str(tmp_path / "locks"),
            "CHATGPT_RELOGIN_SYSTEMCTL": str(make_executable(tmp_path / "systemctl", FAKE_SYSTEMCTL)),
            "CHATGPT_RELOGIN_JOURNALCTL": str(make_executable(tmp_path / "journalctl", FAKE_JOURNALCTL)),
            "CHATGPT_RELOGIN_MARKER": str(self.marker),
            "CHATGPT_RELOGIN_POLL": "0.05",
            "CHATGPT_RELOGIN_STOP_TIMEOUT": "10",
            "CHATGPT_RELOGIN_VERIFY_TIMEOUT": "3",
            "CHATGPT_RELOGIN_ACCOUNT_TIMEOUT": "0.2",
        }
        for unit in self.units:
            (self.state / "active" / unit).touch()

    def call(self, *args, **env):
        return subprocess.run([sys.executable, str(PROFILE_SYNC), *args], env={**self.env, **env}, capture_output=True, text=True, timeout=90, check=False)

    def raw_events(self):
        return [line for line in self.events.read_text(encoding="utf-8").splitlines() if line.startswith("systemctl ")]

    def systemctl_events(self):
        return [line for line in self.raw_events() if " reset-failed " not in line]

    def active(self):
        return sorted(p.name for p in (self.state / "active").iterdir())

    def backup_files(self):
        return sorted((self.backups / self.workspace).glob("Cookies-*")) if self.backups.exists() else []


@pytest.fixture()
def box(tmp_path):
    sandbox = Sandbox(tmp_path)
    yield sandbox
    sandbox.browser.close()


@pytest.fixture()
def glide_box(tmp_path):
    sandbox = Sandbox(tmp_path, "glide")
    yield sandbox
    sandbox.browser.close()


def assert_no_secret(done):
    for secret in SECRETS:
        assert secret not in done.stdout + done.stderr


def target_rows(box):
    return read_rows(box.target / "Default" / "Cookies")


COPIED = [
    (".chatgpt.com", "__Secure-next-auth.session-token", b"v11SOURCE-SESSION-SECRET"),
    (".google.com", "SID", b"v11TARGET-GOOGLE-KEEP"),
    (".openai.com", "oai-sc", b"v11SOURCE-SC-SECRET"),
    ("auth.openai.com", "login_session", b"v10SOURCE-AUTH-SECRET"),
    ("chatgpt.com", "oai-did", b"v11SOURCE-DEVICE-SECRET"),
]


# -- a successful pull ---------------------------------------------------------------------------------------------------


def test_pull_home_replaces_only_the_chatgpt_rows_and_restarts_in_order(box):
    source_before = (box.source / "Default" / "Cookies").read_bytes()
    done = box.call("pull", "home")
    assert done.returncode == 0, done.stdout + done.stderr
    assert target_rows(box) == sorted(COPIED)  # the stale chatgpt row is gone, the google row and look-alikes untouched/absent
    assert (box.source / "Default" / "Cookies").read_bytes() == source_before
    assert box.systemctl_events() == [
        f"systemctl stop {box.unit}",
        f"systemctl stop {box.unit}-observer",
        f"systemctl stop {box.unit}-browser",
        f"systemctl start {box.unit}-browser",
        f"systemctl start {box.unit}-observer",
        f"systemctl start {box.unit}",
    ]
    assert box.active() == sorted(box.units)
    assert "gmail.com" in done.stdout
    assert_no_secret(done)
    assert not box.marker.exists()  # the home account has no relogin marker


def test_pull_backs_up_the_previous_target_cookies_privately(box):
    done = box.call("pull", "home")
    assert done.returncode == 0
    [backup] = box.backup_files()
    assert read_rows(backup) == sorted(TARGET_ROWS)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700


def test_only_the_newest_three_backups_are_kept(box):
    directory = box.backups / "home"
    directory.mkdir(parents=True)
    for stamp in ("20200101T000000", "20200102T000000", "20200103T000000"):
        (directory / f"Cookies-{stamp}").write_bytes(b"")
    assert box.call("pull", "home").returncode == 0
    names = [p.name for p in box.backup_files()]
    assert len(names) == 3
    assert "Cookies-20200101T000000" not in names


def test_pull_glide_writes_the_relogin_marker_for_the_glide_domain(glide_box):
    done = glide_box.call("pull", "glide")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "app.glide.vc" in done.stdout
    assert glide_box.marker.read_text(encoding="utf-8").split()[1] == "app.glide.vc"
    assert glide_box.systemctl_events()[0] == "systemctl stop chatgpt-guardian-glide"


def test_a_newer_chrome_cookie_layout_is_found(box):
    network = box.target / "Default" / "Network"
    network.mkdir()
    (box.target / "Default" / "Cookies").rename(network / "Cookies")
    assert box.call("pull", "home").returncode == 0
    assert read_rows(network / "Cookies") == sorted(COPIED)


def test_the_source_may_be_named_with_from(box, tmp_path):
    other = tmp_path / "elsewhere"
    make_cookies(other / "Default" / "Cookies", [(".chatgpt.com", "__Secure-next-auth.session-token", b"v11OTHER-SESSION")])
    done = box.call("pull", "home", "--from", str(other))
    assert done.returncode == 0
    assert (".chatgpt.com", "__Secure-next-auth.session-token", b"v11OTHER-SESSION") in target_rows(box)


# -- --dry-run, status, usage ----------------------------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_changes_nothing(box):
    before = (box.target / "Default" / "Cookies").read_bytes()
    done = box.call("pull", "home", "--dry-run")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "4 cookie rows" in done.stdout
    assert ".chatgpt.com 1" in done.stdout and "auth.openai.com 1" in done.stdout
    assert "v10 1" in done.stdout and "v11 3" in done.stdout
    assert "dry run" in done.stdout
    assert (box.target / "Default" / "Cookies").read_bytes() == before
    assert box.systemctl_events() == []
    assert box.backup_files() == []
    assert_no_secret(done)


def test_dry_run_refuses_like_the_real_run_when_the_source_is_open(box):
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", box.source / "SingletonLock")
    done = box.call("pull", "home", "--dry-run")
    assert done.returncode == 3


def test_status_reports_the_account_domain_per_profile_and_the_source(box):
    done = box.call("status")
    assert done.returncode == 0, done.stderr
    assert "home: signed in, email domain gmail.com" in done.stdout
    assert "glide: signed in, email domain gmail.com" in done.stdout  # both ask the one fake port
    assert "Chrome is closed, 4 chatgpt.com / openai.com cookie rows" in done.stdout
    assert box.systemctl_events() == []
    assert_no_secret(done)


def test_status_says_when_a_browser_cannot_be_asked_and_when_the_source_is_open(box):
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", box.source / "SingletonLock")
    done = box.call("status", CHATGPT_PROFILE_SYNC_PORT="1")
    assert done.returncode == 0
    assert "home: cannot be asked (no DevTools answer on port 1)" in done.stdout
    assert "Chrome is open" in done.stdout


def test_messages_follow_the_language(box):
    done = box.call("pull", "home", "--dry-run", UBUNTU_TWEAK_LANG="ru")
    assert done.returncode == 0
    assert "пробный запуск" in done.stdout


@pytest.mark.parametrize("args", [[], ["pull"], ["pull", "work"], ["bogus"], ["pull", "home", "--nope"]])
def test_usage_errors_are_exit_2(box, args):
    done = box.call(*args)
    assert done.returncode == 2
    assert "usage: profile-sync" in done.stderr
    assert box.systemctl_events() == []


def test_a_bad_expected_domain_and_a_same_profile_are_exit_2(box):
    assert box.call("pull", "home", "--expect-domain", "not a domain").returncode == 2
    assert box.call("pull", "home", "--from", str(box.target)).returncode == 2
    assert box.systemctl_events() == []


def test_a_missing_cookie_database_is_exit_2(box, tmp_path):
    assert box.call("pull", "home", "--from", str(tmp_path / "nowhere")).returncode == 2
    (box.target / "Default" / "Cookies").unlink()
    assert box.call("pull", "home").returncode == 2
    assert box.systemctl_events() == []


# -- refusals: nothing is stopped or changed -----------------------------------------------------------------------------


def test_an_open_source_chrome_by_its_lock_refuses_with_exit_3(box):
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", box.source / "SingletonLock")
    before = target_rows(box)
    done = box.call("pull", "home")
    assert done.returncode == 3
    assert "still has" in done.stderr
    assert box.systemctl_events() == []
    assert target_rows(box) == before
    assert box.backup_files() == []


def test_an_open_source_chrome_by_its_command_line_refuses_with_exit_3(box):
    holder = subprocess.Popen(["sh", "-c", "sleep 30", f"--user-data-dir={box.source}"])
    try:
        done = box.call("pull", "home")
    finally:
        holder.kill()
        holder.wait()
    assert done.returncode == 3
    assert box.systemctl_events() == []


def test_a_stale_source_lock_does_not_refuse(box):
    os.symlink(f"{socket.gethostname()}-999999999", box.source / "SingletonLock")
    assert box.call("pull", "home").returncode == 0


def test_a_source_without_chatgpt_cookies_is_exit_1_and_stops_nothing(box):
    make_cookies(box.source / "Default" / "Cookies.new", [(".google.com", "SID", b"v11X")])
    (box.source / "Default" / "Cookies.new").replace(box.source / "Default" / "Cookies")
    done = box.call("pull", "home")
    assert done.returncode == 1
    assert "holds no chatgpt.com / openai.com cookies" in done.stderr
    assert box.systemctl_events() == []


def test_a_target_profile_still_in_use_after_the_stop_is_exit_3_and_the_units_come_back(box):
    holder = subprocess.Popen(["sh", "-c", "sleep 30", f"--user-data-dir={box.target}"])
    try:
        done = box.call("pull", "home", CHATGPT_RELOGIN_STOP_TIMEOUT="0.5")
    finally:
        holder.kill()
        holder.wait()
    assert done.returncode == 3
    assert box.active() == sorted(box.units)
    assert target_rows(box) == sorted(TARGET_ROWS)


# -- failures after the stop: the old cookies and the units come back ---------------------------------------------------


def test_different_cookie_schemas_are_exit_1_with_the_target_untouched_and_the_units_started(box):
    (box.target / "Default" / "Cookies").unlink()
    make_cookies(box.target / "Default" / "Cookies", TARGET_ROWS, version="99")
    done = box.call("pull", "home")
    assert done.returncode == 1
    assert "differ" in done.stderr
    assert target_rows(box) == sorted(TARGET_ROWS)
    assert box.active() == sorted(box.units)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("wrong", "need app.glide.vc"),
        ("signed_out", "signed out"),
        ("unreachable", "could not be asked"),
    ],
)
def test_an_unconfirmed_glide_account_rolls_the_backup_back_with_exit_4(glide_box, kind, expected):
    glide_box.browser.answer = {"state": "user", "domain": "gmail.com"} if kind == "wrong" else {"state": "none", "domain": ""}
    env = {"CHATGPT_PROFILE_SYNC_PORT": "1"} if kind == "unreachable" else {}
    done = glide_box.call("pull", "glide", **env)
    assert done.returncode == 4, done.stdout + done.stderr
    assert expected in done.stderr
    assert target_rows(glide_box) == sorted(TARGET_ROWS)
    assert glide_box.active() == sorted(glide_box.units)
    assert not glide_box.marker.exists()
    stops = [e for e in glide_box.systemctl_events() if " stop " in e]
    assert len(stops) == 6  # the copy and the rollback each stop the three units
    assert_no_secret(done)


def test_the_home_account_must_not_be_the_glide_one(box):
    box.browser.answer = session("app.glide.vc")
    done = box.call("pull", "home")
    assert done.returncode == 4
    assert "dock Chrome was signed in to the other account" in done.stderr
    assert target_rows(box) == sorted(TARGET_ROWS)


def test_expect_domain_overrides_the_default_rule(box):
    box.browser.answer = session("example.org")
    assert box.call("pull", "home", "--expect-domain", "@Example.org").returncode == 0


def test_expect_domain_other_than_the_browsers_rolls_back(box):
    assert box.call("pull", "home", "--expect-domain", "example.org").returncode == 4
    assert target_rows(box) == sorted(TARGET_ROWS)


def test_units_that_do_not_come_back_roll_the_backup_back(box):
    done = box.call("pull", "home", FAKE_FAIL_START=f"{box.unit}-observer", CHATGPT_RELOGIN_VERIFY_TIMEOUT="0.5")
    assert done.returncode == 4
    assert "did not come back healthy" in done.stderr
    assert target_rows(box) == sorted(TARGET_ROWS)


def test_a_failed_copy_that_cannot_be_restored_is_exit_5_with_the_backup_path(box):
    (box.target / "Default" / "Cookies").chmod(0o444)  # the copy fails and so does the restore
    done = box.call("pull", "home")
    assert done.returncode == 5, done.stdout + done.stderr
    assert "COULD NOT RESTORE" in done.stderr
    [backup] = box.backup_files()
    assert str(backup) in done.stderr
    assert read_rows(backup) == sorted(TARGET_ROWS)
    assert box.active() == []  # the unconfirmed cookies are still in place: no guardian may start on them
    assert_no_secret(done)


# -- review findings: the browser verifies alone, one pull at a time, signals, retention ---------------------------------------


def test_the_observer_and_guardian_start_only_after_the_browser_showed_the_right_account(glide_box):
    glide_box.browser.answer = {"state": "user", "domain": "gmail.com"}
    assert glide_box.call("pull", "glide").returncode == 4
    events = glide_box.systemctl_events()
    unit = glide_box.unit
    first_start = events.index(f"systemctl start {unit}-browser")
    assert events[:first_start] == [f"systemctl stop {unit}", f"systemctl stop {unit}-observer", f"systemctl stop {unit}-browser"]
    # the rejected cookies met the browser only: the next event is the rollback's stop, not a guardian start
    assert events[first_start + 1 : first_start + 4] == [f"systemctl stop {unit}", f"systemctl stop {unit}-observer", f"systemctl stop {unit}-browser"]


def test_a_session_without_a_visible_email_domain_is_not_accepted_for_home(box):
    box.browser.answer = {"state": "user", "domain": ""}
    done = box.call("pull", "home")
    assert done.returncode == 4
    assert "email domain is not visible" in done.stderr
    assert target_rows(box) == sorted(TARGET_ROWS)


def test_glide_accepts_only_its_configured_domain_as_an_expected_domain(glide_box):
    done = glide_box.call("pull", "glide", "--expect-domain", "gmail.com")
    assert done.returncode == 2
    assert "cannot change that for glide" in done.stderr
    assert glide_box.systemctl_events() == []
    assert glide_box.call("pull", "glide", "--expect-domain", "App.Glide.vc").returncode == 0


# the machine declares the home account's domain (module option home_email_domain): the home guardian unit's ExecStartPre refuses any other one
HOME_REQUIRED = {"UBUNTU_TWEAK_MODULE_CONFIG": json.dumps({"glide_email_domain": "app.glide.vc", "home_email_domain": "gmail.com"})}


def test_home_with_a_declared_domain_takes_only_that_domain(box):
    """Any other account would be accepted here, then refused by the guardian's start check after the whole verify timeout."""
    box.browser.answer = session("outlook.com")
    done = box.call("pull", "home", **HOME_REQUIRED)
    assert done.returncode == 4, done.stdout + done.stderr
    assert "outlook.com" in done.stderr and "need gmail.com" in done.stderr
    assert target_rows(box) == sorted(TARGET_ROWS)
    assert box.active() == sorted(box.units)


def test_home_with_a_declared_domain_confirms_that_domain(box):
    done = box.call("pull", "home", **HOME_REQUIRED)  # the sandbox's home browser shows gmail.com
    assert done.returncode == 0, done.stdout + done.stderr
    assert "email domain gmail.com" in done.stdout


def test_home_with_a_declared_domain_accepts_only_it_as_an_expected_domain(box):
    done = box.call("pull", "home", "--expect-domain", "outlook.com", **HOME_REQUIRED)
    assert done.returncode == 2
    assert "cannot change that for home" in done.stderr
    assert box.systemctl_events() == []
    assert box.call("pull", "home", "--expect-domain", "@GMail.com", **HOME_REQUIRED).returncode == 0


def test_the_plan_of_a_home_pull_names_the_declared_domain(box):
    done = box.call("pull", "home", "--dry-run", **HOME_REQUIRED)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "the email domain must be gmail.com" in done.stdout


def test_a_malformed_home_domain_option_is_a_config_error_before_anything_stops(box):
    done = box.call("pull", "home", UBUNTU_TWEAK_MODULE_CONFIG=json.dumps({"home_email_domain": "not a domain"}))
    assert done.returncode == 2
    assert "home_email_domain" in done.stderr
    assert box.systemctl_events() == []


def test_a_second_pull_of_the_same_workspace_is_refused_while_one_runs(box):
    locks = box.root / "locks"
    locks.mkdir(parents=True)
    with open(locks / "home.lock", "a", encoding="utf-8") as holder:  # the lock `relogin run` takes as well
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = box.call("pull", "home")
    assert done.returncode == 3
    assert "another relogin or profile-sync run" in done.stderr
    assert box.systemctl_events() == []
    assert target_rows(box) == sorted(TARGET_ROWS)


def test_a_signal_during_the_verification_restores_the_backup(box):
    box.browser.hang = True  # the browser never answers the account question: the pull waits in the verification
    env = {**box.env, "CHATGPT_RELOGIN_PROBE_TIMEOUT": "30", "CHATGPT_RELOGIN_ACCOUNT_TIMEOUT": "30"}
    proc = subprocess.Popen([sys.executable, str(PROFILE_SYNC), "pull", "home"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 20
    while f"systemctl start {box.unit}-browser" not in box.systemctl_events():
        assert time.monotonic() < deadline and proc.poll() is None, "the pull never reached the verification"
        time.sleep(0.05)
    assert target_rows(box) == sorted(COPIED)  # the copy is in place and unconfirmed
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 130, out + err
    assert target_rows(box) == sorted(TARGET_ROWS)
    assert box.active() == sorted(box.units)


def test_backups_are_pruned_after_rolled_back_pulls_too(box):
    box.browser.answer = {"state": "none", "domain": ""}
    for _ in range(5):
        assert box.call("pull", "home").returncode == 4
        time.sleep(1.1)  # the backup name carries seconds
    assert len(box.backup_files()) == 3


# -- second review: redaction, bounded copies, start limits, a browser without a ChatGPT tab -------------------------------------


def test_sqlite_errors_never_quote_a_cookie_value(box):
    connection = sqlite3.connect(box.source / "Default" / "Cookies")
    connection.execute("UPDATE cookies SET value = CAST(x'534543524554464f4f21ff' AS TEXT) WHERE name = 'oai-did'")  # SECRETFOO! + a byte that is not UTF-8
    connection.commit()
    connection.close()
    dry = box.call("pull", "home", "--dry-run")
    status = box.call("status")
    assert dry.returncode == 1 and status.returncode == 0
    for done in (dry, status):
        assert "OperationalError" in done.stdout + done.stderr
        assert "SECRETFOO" not in done.stdout + done.stderr


def test_a_locked_target_database_ends_the_pull_and_leaves_no_partial_backup(box):
    locker = sqlite3.connect(box.target / "Default" / "Cookies", isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        done = box.call("pull", "home", CHATGPT_PROFILE_SYNC_DB_TIMEOUT="1")
    finally:
        locker.close()
    assert done.returncode == 1, done.stdout + done.stderr
    assert "copying the cookies failed" in done.stderr
    assert box.backup_files() == []
    assert target_rows(box) == sorted(TARGET_ROWS)
    assert box.active() == sorted(box.units)


def test_the_start_limits_of_the_units_are_reset_before_the_first_start(box):
    assert box.call("pull", "home").returncode == 0
    events = box.raw_events()
    first_start = next(i for i, line in enumerate(events) if " start " in line)
    reset = [line for line in events[:first_start] if " reset-failed " in line]
    assert sorted(reset) == sorted(f"systemctl reset-failed {unit}.service" for unit in box.units)


def test_the_start_limits_are_reset_again_before_a_rollback_starts_the_units(glide_box):
    glide_box.browser.answer = {"state": "user", "domain": "gmail.com"}
    assert glide_box.call("pull", "glide").returncode == 4
    events = glide_box.raw_events()
    last_start = max(i for i, line in enumerate(events) if " start " in line)
    last_stop = max(i for i, line in enumerate(events) if " stop " in line)
    assert any(" reset-failed " in line for line in events[last_stop:last_start])


def test_a_browser_that_restarts_without_a_chatgpt_tab_gets_a_temporary_one(box):
    box.browser.close()
    box.browser = TabbedDevTools([NEWTAB], session("gmail.com"))
    done = box.call("pull", "home", CHATGPT_PROFILE_SYNC_PORT=str(box.browser.port))
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.browser.requests == [("PUT", "/json/new?https://chatgpt.com/")]  # and the tab stays: the guardian's start check needs it
    assert any(tab["url"] == "https://chatgpt.com/" for tab in box.browser.tabs)


FAKE_SYSTEMCTL_STOPFAIL = FAKE_SYSTEMCTL.replace(
    "  stop)\n",
    '  stop)\n    if [ -e "$FAKE_STATE/arm-stop-failure" ]; then case " $FAKE_FAIL_STOP " in *" $unit "*) echo "Job for $unit.service canceled" >&2; echo "systemctl stop $unit (failed)" >> "$FAKE_EVENTS"; exit 1 ;; esac; fi\n',
).replace("  start)\n", '  start)\n    case "$unit" in *-browser) touch "$FAKE_STATE/arm-stop-failure" ;; esac\n')


def test_a_unit_that_will_not_stop_for_the_rollback_does_not_hide_the_others_and_nothing_is_started(glide_box):
    glide_box.env["CHATGPT_RELOGIN_SYSTEMCTL"] = str(make_executable(glide_box.root / "systemctl-stopfail", FAKE_SYSTEMCTL_STOPFAIL))
    glide_box.browser.answer = {"state": "user", "domain": "gmail.com"}  # the rollback is needed...
    done = glide_box.call("pull", "glide", FAKE_FAIL_STOP=f"{glide_box.unit}-browser")  # ...and the running browser then refuses to stop
    assert done.returncode == 5, done.stdout + done.stderr
    assert "COULD NOT RESTORE" in done.stderr and "no glide unit was started again" in done.stderr
    events = glide_box.raw_events()
    rollback = events[events.index(f"systemctl start {glide_box.unit}-browser") + 1 :]
    assert rollback == [
        f"systemctl stop {glide_box.unit}",  # the guardian first, then the observer, then the one that fails: all are tried
        f"systemctl stop {glide_box.unit}-observer",
        f"systemctl stop {glide_box.unit}-browser (failed)",
    ]
    assert target_rows(glide_box) == sorted(COPIED)  # the restore never ran: a running browser holds the database
    [backup] = glide_box.backup_files()
    assert read_rows(backup) == sorted(TARGET_ROWS)


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(MODULE / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_a_status_query_that_times_out_is_a_failed_stop_not_a_crash(monkeypatch, tmp_path):
    relogin = load_script("relogin")
    settings = relogin.load_settings({"HOME": str(tmp_path), "CHATGPT_RELOGIN_SYSTEMCTL": "false"})  # `false stop x.service`: exit 1

    def hang(_settings, _unit):
        raise subprocess.TimeoutExpired("systemctl", 30)

    monkeypatch.setattr(relogin, "unit_state", hang)
    problem = relogin.stop_unit(settings, "x")
    assert problem and "could not stop x" in problem


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "soon"])
def test_a_database_timeout_that_is_not_a_positive_finite_number_is_refused_before_anything_stops(box, value):
    done = box.call("pull", "home", CHATGPT_PROFILE_SYNC_DB_TIMEOUT=value)
    assert done.returncode == 2
    assert "CHATGPT_PROFILE_SYNC_DB_TIMEOUT" in done.stderr
    assert box.systemctl_events() == []
