"""chatgpt-guardian ./revive: what it starts after a browser/display (re)start and what the timer sweep restarts.

No real systemd: REVIVE_SYSTEMCTL is a stub that logs every call and answers `is-failed` from $STUB_FAILED.
"""

import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

REVIVE = Path(__file__).resolve().parents[1] / "revive"

STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
if [ "$1" = is-failed ]; then
  case " $STUB_FAILED " in *" $2 "*) echo failed; exit 0 ;; esac
  echo inactive; exit 1
fi
exit 0
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeDevTools:
    """The two DevTools HTTP endpoints revive uses: GET /json/list and PUT /json/new?<url>."""

    def __init__(self, tabs):
        self.tabs = tabs
        self.created = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, body):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(json.dumps(outer.tabs).encode())

            def do_PUT(self):
                outer.created.append(self.path)
                self._send(b"{}")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def devtools():
    made = []

    def make(tabs):
        made.append(FakeDevTools(tabs))
        return made[-1]

    yield make
    for fake in made:
        fake.close()


@pytest.fixture()
def env(tmp_path):
    stub = tmp_path / "systemctl"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    locks = tmp_path / "locks"
    locks.mkdir()
    # the tab step never reaches the real glide browser (9223) and never waits: a dead port and zero waits
    return {
        **os.environ,
        "REVIVE_SYSTEMCTL": str(stub),
        "STUB_LOG": str(tmp_path / "log"),
        "STUB_FAILED": "",
        "CHATGPT_RELOGIN_LOCK_DIR": str(locks),
        "REVIVE_PYTHON": sys.executable,
        "CHATGPT_REVIVE_DEVTOOLS_PORT": str(free_port()),
        "REVIVE_TAB_WAIT": "0",
        "REVIVE_TAB_SETTLE": "0",
    }


def revive(env, *args, **extra):
    done = subprocess.run([str(REVIVE), *args], env={**env, **extra}, capture_output=True, text=True, timeout=30, check=False)
    log = Path(env["STUB_LOG"])
    return done, (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def starts(calls):
    return [c for c in calls if c.startswith("--no-block start")]


@pytest.mark.parametrize("ws", ["home", "glide"])
def test_a_browser_restart_starts_its_observer_and_sender_without_waiting(env, ws):
    done, calls = revive(env, "after-browser", ws)
    assert done.returncode == 0, done.stderr
    both = f"chatgpt-guardian-{ws}-observer.service chatgpt-guardian-{ws}.service"
    assert starts(calls) == [f"--no-block start {both}"]
    assert f"reset-failed {both}" in calls  # a sender in the start-limit-hit state could not start otherwise


def test_a_browser_restart_never_starts_the_browser_or_the_other_account(env):
    _, calls = revive(env, "after-browser", "glide")
    assert not any("browser" in c.replace("after-browser", "") or "home" in c for c in calls)


def test_the_glide_display_restart_starts_the_glide_browser(env):
    done, calls = revive(env, "after-display")
    assert done.returncode == 0, done.stderr
    assert starts(calls) == ["--no-block start chatgpt-guardian-glide-browser.service"]


def test_unknown_account_and_command_are_refused(env):
    assert revive(env, "after-browser", "laya")[0].returncode == 2
    assert revive(env, "after-browser")[0].returncode != 0
    assert revive(env, "restart-everything")[0].returncode == 2
    assert revive(env)[0].returncode == 2


@pytest.mark.parametrize(("args", "profile"), [(("after-browser", "glide"), "glide.vc"), (("after-display",), "glide.vc"), (("after-browser", "home"), "home")])
def test_a_profile_held_by_relogin_or_profile_sync_is_left_alone(env, args, profile):
    with open(Path(env["CHATGPT_RELOGIN_LOCK_DIR"]) / f"{profile}.lock", "a", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done, calls = revive(env, *args)
    assert done.returncode == 0 and calls == []


def test_a_released_lock_file_does_not_block_the_restart(env):
    (Path(env["CHATGPT_RELOGIN_LOCK_DIR"]) / "glide.vc.lock").write_text("", encoding="utf-8")
    assert starts(revive(env, "after-browser", "glide")[1])


def test_sweep_restarts_only_failed_units_in_dependency_order(env):
    failed = "chatgpt-guardian-glide.service chatgpt-guardian-glide-browser.service chatgpt-guardian-home-observer.service"
    done, calls = revive(env, "sweep", STUB_FAILED=failed)
    assert done.returncode == 0, done.stderr
    assert starts(calls) == [
        "--no-block start chatgpt-guardian-glide-browser.service",
        "--no-block start chatgpt-guardian-glide.service",
        "--no-block start chatgpt-guardian-home-observer.service",
    ]


def test_sweep_leaves_operator_stopped_units_alone(env):
    # `systemctl stop` leaves a unit inactive, never failed: nothing to start
    done, calls = revive(env, "sweep")
    assert done.returncode == 0
    assert starts(calls) == [] and not any(c.startswith("reset-failed") for c in calls)


def test_sweep_skips_a_locked_profile_but_still_serves_the_other_account(env):
    failed = "chatgpt-guardian-glide.service chatgpt-guardian-home.service"
    with open(Path(env["CHATGPT_RELOGIN_LOCK_DIR"]) / "glide.vc.lock", "a", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, calls = revive(env, "sweep", STUB_FAILED=failed)
    assert starts(calls) == ["--no-block start chatgpt-guardian-home.service"]


def test_sweep_reports_a_start_that_systemd_refused(env, tmp_path):
    stub = tmp_path / "systemctl"
    stub.write_text(STUB.replace("exit 0\n", 'if [ "$1" = --no-block ]; then exit 1; fi\nexit 0\n'), encoding="utf-8")
    done, _ = revive(env, "sweep", STUB_FAILED="chatgpt-guardian-glide.service")
    assert done.returncode == 1


PAGE_NEWTAB = {"type": "page", "url": "chrome://newtab/"}
PAGE_CHAT = {"type": "page", "url": "https://chatgpt.com/c/6ad9deb2-1344-83eb-88e0-40e6f8262a0e"}


def test_glide_browser_with_only_a_new_tab_gets_one_chatgpt_tab_before_its_sender_starts(env, devtools):
    fake = devtools([PAGE_NEWTAB, {"type": "service_worker", "url": "https://chatgpt.com/sw.js"}])
    done, calls = revive(env, "after-browser", "glide", CHATGPT_REVIVE_DEVTOOLS_PORT=str(fake.port))
    assert done.returncode == 0, done.stderr
    assert fake.created == ["/json/new?https%3A%2F%2Fchatgpt.com%2F"]  # a service worker of that origin is not a page
    assert starts(calls) == ["--no-block start chatgpt-guardian-glide-observer.service chatgpt-guardian-glide.service"]


def test_glide_browser_that_restored_a_chatgpt_page_gets_no_extra_tab(env, devtools):
    fake = devtools([PAGE_NEWTAB, PAGE_CHAT])
    done, calls = revive(env, "after-browser", "glide", CHATGPT_REVIVE_DEVTOOLS_PORT=str(fake.port))
    assert done.returncode == 0, done.stderr
    assert fake.created == []
    assert starts(calls)


def test_a_look_alike_host_does_not_count_as_a_chatgpt_page(env, devtools):
    fake = devtools([{"type": "page", "url": "https://chatgpt.com.example/"}])
    revive(env, "after-browser", "glide", CHATGPT_REVIVE_DEVTOOLS_PORT=str(fake.port))
    assert fake.created == ["/json/new?https%3A%2F%2Fchatgpt.com%2F"]


def test_an_unreachable_browser_never_keeps_the_sender_down(env):
    done, calls = revive(env, "after-browser", "glide")  # fixture: dead DevTools port, no waiting
    assert done.returncode == 0
    assert starts(calls)


def test_the_home_browser_restart_opens_no_tab(env, devtools):
    fake = devtools([PAGE_NEWTAB])
    revive(env, "after-browser", "home", CHATGPT_REVIVE_DEVTOOLS_PORT=str(fake.port))
    assert fake.created == []


def test_a_profile_held_by_relogin_gets_no_tab(env, devtools):
    fake = devtools([PAGE_NEWTAB])
    with open(Path(env["CHATGPT_RELOGIN_LOCK_DIR"]) / "glide.vc.lock", "a", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        revive(env, "after-browser", "glide", CHATGPT_REVIVE_DEVTOOLS_PORT=str(fake.port))
    assert fake.created == []
