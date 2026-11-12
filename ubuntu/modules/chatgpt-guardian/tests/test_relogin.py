"""chatgpt-guardian `relogin` helper: the `check` rule and the `run` mechanics, against fakes.

No real systemd, Chrome or ChatGPT: systemctl, journalctl and Chrome are shell fakes on environment-variable paths,
the DevTools port is a tiny in-process HTTP + WebSocket server, HOME is a temp dir. Every fake appends to one events
log, so the order of the stop / open / wait / start steps is asserted on that log.
"""

import base64
import fcntl
import hashlib
import json
import os
import re
import signal
import socketserver
import stat
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
RELOGIN = MODULE / "relogin"
GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT = (
    "chatgpt-guardian-glide",
    "chatgpt-guardian-glide-observer",
    "chatgpt-guardian-glide-browser",
    "chatgpt-guardian-glide-display",
)

FAKE_SYSTEMCTL = """#!/bin/sh
# fake `systemctl --user`: a unit is active while $FAKE_STATE/active/<name> exists
verb=$1; unit=${2%.service}
case "$verb" in
  is-active)
    if [ -n "$FAKE_NO_BUS" ]; then echo "Failed to connect to user scope bus" >&2; exit 1; fi
    if [ -e "$FAKE_STATE/active/$unit" ]; then echo active; exit 0; fi; echo inactive; exit 3 ;;
  stop)
    if [ -n "$FAKE_NO_BUS" ]; then echo "Failed to connect to user scope bus" >&2; exit 1; fi
    echo "systemctl stop $unit" >> "$FAKE_EVENTS"; rm -f "$FAKE_STATE/active/$unit"; exit 0 ;;
  start)
    echo "systemctl start $unit" >> "$FAKE_EVENTS"
    case " $FAKE_FAIL_START " in *" $unit "*) echo "Job for $unit failed" >&2; exit 1 ;; esac
    touch "$FAKE_STATE/active/$unit"; exit 0 ;;
esac
echo "systemctl $*" >> "$FAKE_EVENTS"
"""

FAKE_JOURNALCTL = """#!/bin/sh
# fake `journalctl --user`: the glide guardian prints its health line only once it has been started
echo "journalctl $*" >> "$FAKE_EVENTS"
if [ -e "$FAKE_STATE/active/chatgpt-guardian-glide" ]; then cat "$FAKE_STATE/journal" 2>/dev/null; fi
exit 0
"""

FAKE_CHROME = """#!/bin/sh
# fake Chrome: records argv and environment, "shows a window" until $FAKE_STATE/close exists.
# FAKE_CHROME_MODE=forward: Chrome's single-instance behaviour, the launched process hands over to a background
# instance and exits at once; FAKE_CHROME_MODE=die: it exits with an error and no window.
if [ "$1" = --daemon ]; then
  shift
else
  printf '%s\\n' "$@" > "$FAKE_STATE/chrome-argv"
  printf 'WAYLAND_DISPLAY=%s\\nDISPLAY=%s\\n' "${WAYLAND_DISPLAY-unset}" "${DISPLAY-unset}" > "$FAKE_STATE/chrome-env"
  echo "chrome open; active units: $(ls "$FAKE_STATE/active" | tr '\\n' ' ')" >> "$FAKE_EVENTS"
  case "$FAKE_CHROME_MODE" in
    die) echo "chrome died" >&2; exit 3 ;;
    forward) nohup sh "$0" --daemon "$@" >/dev/null 2>&1 & exit 0 ;;
  esac
fi
trap 'echo "chrome exit (signal)" >> "$FAKE_EVENTS"; exit 0' TERM
while [ ! -e "$FAKE_STATE/close" ]; do sleep 0.05; done
echo "chrome exit" >> "$FAKE_EVENTS"
"""


def make_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# -- a DevTools port that answers like Chrome's: /json/list and Runtime.evaluate over a WebSocket ---------------------


class DevTools:
    """Serves one list of tabs and answers every Runtime.evaluate with `answer` (what the page's fetch returned)."""

    def __init__(self, tabs, answer, hang=False):
        self.tabs, self.answer, self.hang, self.raw, self.expressions = tabs, answer, hang, None, []
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner.handle(self.request)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def handle(self, sock):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                return
            head += chunk
        request = head.decode("latin-1")
        path = request.split(" ", 2)[1]
        if path == "/json/list":
            body = json.dumps(
                [{"type": t["type"], "url": t["url"], "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/{i}"} for i, t in enumerate(self.tabs)]
            ).encode()
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            return
        key = re.search(r"Sec-WebSocket-Key: (\S+)", request, re.IGNORECASE).group(1)
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        sock.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        message = json.loads(self.read_frame(sock))
        self.expressions.append(message["params"]["expression"])
        if self.hang:  # a tab that accepts the connection and never answers
            time.sleep(30)
            return
        reply = (self.raw if self.raw is not None else json.dumps({"id": message["id"], "result": {"result": {"type": "object", "value": self.answer}}})).encode()
        header = b"\x81" + (bytes([len(reply)]) if len(reply) < 126 else b"\x7e" + struct.pack(">H", len(reply)))
        sock.sendall(header + reply)

    @staticmethod
    def read_frame(sock):
        def exact(n):
            data = b""
            while len(data) < n:
                chunk = sock.recv(n - len(data))
                if not chunk:
                    raise ConnectionError
                data += chunk
            return data

        _, second = exact(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", exact(8))[0]
        mask = exact(4)
        return bytes(b ^ mask[i % 4] for i, b in enumerate(exact(length)))


class Replay:
    """A socket whose first recv returns an HTTP head that was already read."""

    def __init__(self, sock, head):
        self.sock, self.head = sock, head

    def recv(self, size):
        if self.head:
            data, self.head = self.head, b""
            return data
        return self.sock.recv(size)

    def sendall(self, data):
        self.sock.sendall(data)


class TabbedDevTools(DevTools):
    """DevTools that also serves PUT /json/new (a ChatGPT tab appears) and records /json/close/<id>."""

    def __init__(self, tabs, answer, hang=False):
        self.requests = []
        super().__init__(tabs, answer, hang)

    def handle(self, sock):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                return
            head += chunk
        method, path, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        if path.startswith("/json/new"):
            self.requests.append((method, path))
            self.tabs.append(dict(CHATGPT_TAB))
            body = json.dumps({"id": "tab-new", "type": "page", "url": "https://chatgpt.com/"}).encode()
        elif path.startswith("/json/close/"):
            self.requests.append((method, path))
            body = b"Target is closing"
        else:
            super().handle(Replay(sock, head))
            return
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)


CHATGPT_TAB = {"type": "page", "url": "https://chatgpt.com/"}
NEWTAB = {"type": "page", "url": "chrome://newtab/"}


def session(domain="glide.vc"):
    return {"state": "user", "domain": domain}


@pytest.fixture()
def devtools():
    servers = []

    def make(tabs, answer, hang=False, kind=DevTools):
        server = kind(tabs, answer, hang)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


# -- the sandbox -------------------------------------------------------------------------------------------------------


class Sandbox:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.state = tmp_path / "state"
        (self.state / "active").mkdir(parents=True)
        self.events = tmp_path / "events.log"
        self.events.write_text("", encoding="utf-8")
        self.profile = tmp_path / "profiles" / "glide.vc"
        self.profile.mkdir(parents=True)
        self.marker = self.home / ".local/state/ubuntu-tweaks/chatgpt-guardian-glide-login"
        self.browser = DevTools([CHATGPT_TAB], session("glide.vc"))
        self.chrome = make_executable(tmp_path / "chrome", FAKE_CHROME)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "WAYLAND_DISPLAY": "wayland-test",
            "DISPLAY": ":99",
            "UBUNTU_TWEAK_LANG": "en",
            "UBUNTU_TWEAK_MODULE_CONFIG": "{}",
            "FAKE_STATE": str(self.state),
            "FAKE_EVENTS": str(self.events),
            "CHATGPT_RELOGIN_PROFILE": str(self.profile),
            "CHATGPT_RELOGIN_CHROME": str(self.chrome),
            "CHATGPT_RELOGIN_SYSTEMCTL": str(make_executable(tmp_path / "systemctl", FAKE_SYSTEMCTL)),
            "CHATGPT_RELOGIN_JOURNALCTL": str(make_executable(tmp_path / "journalctl", FAKE_JOURNALCTL)),
            "CHATGPT_RELOGIN_DEVTOOLS_PORT": str(self.browser.port),  # a browser signed in with glide.vc, unless a test says otherwise
            "CHATGPT_RELOGIN_POLL": "0.05",
            "CHATGPT_RELOGIN_STOP_TIMEOUT": "10",
            "CHATGPT_RELOGIN_VERIFY_TIMEOUT": "6",
            "CHATGPT_RELOGIN_ACCOUNT_TIMEOUT": "0.2",
        }
        for unit in (GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT):
            self.set_active(unit)
        (self.state / "journal").write_text('{"health": "connected", "workspace": "glide.vc", "watched": 0}\n', encoding="utf-8")

    def set_active(self, unit):
        (self.state / "active" / unit).touch()

    def active(self):
        return sorted(p.name for p in (self.state / "active").iterdir())

    def log(self):
        return self.events.read_text(encoding="utf-8").splitlines()

    def call(self, *args, **env):
        return subprocess.run([str(RELOGIN), *args], env={**self.env, **env}, capture_output=True, text=True, timeout=60, check=False)

    def start(self, *args, **env):
        return subprocess.Popen([str(RELOGIN), *args], env={**self.env, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def close_window(self):
        (self.state / "close").touch()


NO_BROWSER = {"CHATGPT_RELOGIN_DEVTOOLS_PORT": "1"}  # nothing listens there


@pytest.fixture()
def box(tmp_path):
    sandbox = Sandbox(tmp_path)
    yield sandbox
    sandbox.browser.close()


def wait_for(predicate, what, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def finish(proc, timeout=40):
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out, err


# -- check -------------------------------------------------------------------------------------------------------------


def test_check_is_done_when_the_open_chatgpt_tab_is_signed_in_with_the_glide_domain(box, devtools):
    server = devtools([NEWTAB, CHATGPT_TAB], session("glide.vc"))
    done = box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "glide.vc" in done.stdout
    assert not box.marker.exists(), "check is read-only"


def test_check_never_asks_the_page_for_more_than_the_domain(box, devtools):
    server = devtools([CHATGPT_TAB], session("glide.vc"))
    box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    (expression,) = server.expressions
    assert "/api/auth/session" in expression
    assert "token" not in expression.lower()


def test_check_names_the_wrong_account_by_its_domain_only(box, devtools):
    server = devtools([CHATGPT_TAB], session("gmail.com"))
    done = box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert done.returncode == 1
    assert "gmail.com" in done.stdout and "glide.vc" in done.stdout
    assert "@" not in done.stdout + done.stderr


def test_check_is_pending_when_signed_out(box, devtools):
    server = devtools([CHATGPT_TAB], {"state": "none", "domain": ""})
    done = box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert done.returncode == 1
    assert "signed out" in done.stdout


def test_check_wrong_account_wins_over_an_old_marker(box, devtools):
    box.marker.parent.mkdir(parents=True)
    box.marker.write_text("2026-10-01T00:00:00+00:00 glide.vc\n", encoding="utf-8")
    server = devtools([CHATGPT_TAB], session("gmail.com"))
    assert box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port)).returncode == 1


def test_check_honours_the_domain_option(box, devtools):
    server = devtools([CHATGPT_TAB], session("example.org"))
    done = box.call("check", UBUNTU_TWEAK_MODULE_CONFIG=json.dumps({"glide_email_domain": "Example.org"}), CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert done.returncode == 0, done.stdout + done.stderr


def test_check_reads_the_port_from_the_module_option(box, devtools):
    server = devtools([CHATGPT_TAB], session())
    env = {k: v for k, v in box.env.items() if k != "CHATGPT_RELOGIN_DEVTOOLS_PORT"}
    done = subprocess.run(
        [str(RELOGIN), "check"],
        env={**env, "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps({"glide_port": server.port})},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_check_without_a_browser_and_without_a_marker_is_pending_and_says_why(box):
    done = box.call("check", **NO_BROWSER)
    assert done.returncode == 1
    assert "could not ask the browser" in done.stdout
    assert "marker" in done.stdout


def test_check_without_a_browser_trusts_the_marker(box):
    box.marker.parent.mkdir(parents=True)
    box.marker.write_text("2026-10-23T07:00:00+00:00 glide.vc\n", encoding="utf-8")
    done = box.call("check", **NO_BROWSER)
    assert done.returncode == 0
    assert "could not ask the browser" in done.stdout and "2026-10-23" in done.stdout


def test_check_with_no_chatgpt_tab_falls_back_to_the_marker(box, devtools):
    server = devtools([NEWTAB], session())
    done = box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert done.returncode == 1
    assert "no chatgpt.com tab" in done.stdout


@pytest.mark.parametrize(
    "url", ["https://chatgpt.com.attacker.example/", "http://chatgpt.com/", "https://evilchatgpt.com/", "https://attacker.example/https://chatgpt.com/"]
)
def test_check_only_trusts_tabs_on_the_real_chatgpt_origin(box, devtools, url):
    server = devtools([{"type": "page", "url": url}], session("glide.vc"))
    done = box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert done.returncode == 1
    assert "no chatgpt.com tab" in done.stdout
    assert server.expressions == [], "nothing may run on a look-alike origin"


@pytest.mark.parametrize("answer", [[1, 2], "text", 5, None])
def test_check_survives_a_reply_that_is_not_an_object(box, devtools, answer):
    server = devtools([CHATGPT_TAB], None)
    server.raw = json.dumps(answer)  # the whole DevTools message, not the page's value
    done = box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert done.returncode == 1
    assert "Traceback" not in done.stderr


def test_check_with_an_unreadable_session_answer_falls_back_to_the_marker(box, devtools):
    server = devtools([CHATGPT_TAB], {"state": "error", "domain": ""})
    done = box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert done.returncode == 1
    assert "could not ask the browser" in done.stdout


def test_check_gives_up_on_tabs_that_never_answer_within_one_shared_budget(box, devtools):
    """Three silent chatgpt.com tabs must not add up to more than the engine's own limit for `check`."""
    server = devtools([CHATGPT_TAB, CHATGPT_TAB, CHATGPT_TAB], session(), hang=True)
    started = time.monotonic()
    done = box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), CHATGPT_RELOGIN_PROBE_TIMEOUT="2.5")
    assert time.monotonic() - started < 8
    assert done.returncode == 1
    assert "could not ask the browser" in done.stdout


def test_check_speaks_russian(box):
    done = box.call("check", UBUNTU_TWEAK_LANG="ru", **NO_BROWSER)
    assert done.returncode == 1
    assert re.search("[а-я]", done.stdout)


def test_check_does_not_touch_systemd_or_chrome(box, devtools):
    server = devtools([CHATGPT_TAB], session())
    box.call("check", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port))
    assert box.log() == []


# -- run ---------------------------------------------------------------------------------------------------------------


def test_run_stops_opens_waits_restarts_and_verifies(box):
    proc = box.start("run")
    wait_for(lambda: (box.state / "chrome-argv").exists(), "the window to open")
    time.sleep(0.5)  # the wait is real: nothing may be started while the window is open
    assert proc.poll() is None
    assert not any(line.startswith("systemctl start") for line in box.log())
    box.close_window()
    code, out, err = finish(proc)
    assert code == 0, out + err
    log = box.log()
    stops = [line for line in log if line.startswith("systemctl stop")]
    starts = [line for line in log if line.startswith("systemctl start")]
    assert stops == [f"systemctl stop {GUARDIAN}", f"systemctl stop {OBSERVER}", f"systemctl stop {BROWSER}"]
    assert starts == [f"systemctl start {BROWSER}", f"systemctl start {OBSERVER}", f"systemctl start {GUARDIAN}"]
    opened = next(i for i, line in enumerate(log) if line.startswith("chrome open"))
    assert log.index(stops[-1]) < opened < log.index("chrome exit") < log.index(starts[0])
    assert box.active() == sorted([GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT])
    assert "connected" in out
    assert box.marker.read_text(encoding="utf-8").split()[1] == "glide.vc"


def test_run_leaves_the_display_unit_and_everything_else_alone(box):
    box.close_window()
    code, out, err = finish(box.start("run"))
    assert code == 0, out + err
    assert not any(DISPLAY_UNIT in line or "home" in line for line in box.log() if line.startswith("systemctl"))


def test_the_window_has_the_profile_the_login_url_the_real_display_and_no_debug_port(box):
    box.close_window()
    code, out, err = finish(box.start("run"))
    assert code == 0, out + err
    argv = (box.state / "chrome-argv").read_text(encoding="utf-8").splitlines()
    assert f"--user-data-dir={box.profile}" in argv
    assert argv[-1] == "https://chatgpt.com/auth/login"
    assert not any("remote-debugging" in a for a in argv)
    assert (box.state / "chrome-env").read_text(encoding="utf-8").splitlines() == ["WAYLAND_DISPLAY=wayland-test", "DISPLAY=:99"]


def test_run_warns_when_the_window_is_the_sandbox_free_rootless_chrome(box):
    box.close_window()
    wrapper = box.root / "chatgpt-guardian-chrome"
    wrapper.write_bytes(box.chrome.read_bytes())
    wrapper.chmod(0o755)
    code, out, err = finish(box.start("run", CHATGPT_RELOGIN_CHROME=str(wrapper)))
    assert code == 0, out + err
    assert "sandbox" in out
    code, out, err = finish(box.start("run"))
    assert "sandbox" not in out


def test_run_waits_for_the_instance_chrome_handed_over_to(box):
    """A launch that forwards to a running instance exits at once; the wait must follow the process that holds the profile."""
    proc = box.start("run", FAKE_CHROME_MODE="forward", CHATGPT_RELOGIN_STOP_TIMEOUT="1")
    wait_for(lambda: (box.state / "chrome-argv").exists(), "the window to open")
    time.sleep(2.5)  # longer than the stop timeout the cleanup path would wait
    assert proc.poll() is None, "run ended while the window was still open"
    assert not any(line.startswith("systemctl start") for line in box.log())
    box.close_window()
    code, out, err = finish(proc)
    assert code == 0, out + err


def test_run_without_a_graphical_session_stops_nothing(box):
    env = {k: v for k, v in box.env.items() if k not in ("WAYLAND_DISPLAY", "DISPLAY")}
    done = subprocess.run([str(RELOGIN), "run"], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 2
    assert "graphical session" in done.stderr
    assert box.log() == [] and len(box.active()) == 4


def test_run_with_a_missing_profile_stops_nothing(box):
    done = box.call("run", CHATGPT_RELOGIN_PROFILE=str(box.root / "nope"))
    assert done.returncode == 2
    assert "profile" in done.stderr
    assert box.log() == []


def test_run_with_a_missing_chrome_stops_nothing(box):
    done = box.call("run", CHATGPT_RELOGIN_CHROME=str(box.root / "no-chrome"))
    assert done.returncode == 2
    assert "Chrome" in done.stderr
    assert box.log() == []


def test_run_that_cannot_reach_the_user_manager_changes_nothing_and_opens_no_window(box):
    """A failed `stop` plus an unreadable `is-active` must not be taken for "the unit is stopped"."""
    code, out, err = finish(box.start("run", FAKE_NO_BUS="1"))
    assert code == 1
    assert "could not stop" in out + err
    assert not (box.state / "chrome-argv").exists()
    assert len(box.active()) == 4


def test_run_that_cannot_open_chrome_still_restarts_the_units(box):
    code, out, err = finish(box.start("run", FAKE_CHROME_MODE="die"))
    assert code == 1
    assert "Chrome" in out + err
    assert box.active() == sorted([GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT])
    assert not box.marker.exists()


def test_run_interrupted_closes_the_window_and_restarts_the_units(box):
    proc = box.start("run")
    wait_for(lambda: (box.state / "chrome-argv").exists(), "the window to open")
    proc.send_signal(signal.SIGTERM)
    code, out, err = finish(proc)
    assert code == 130
    assert "chrome exit (signal)" in box.log()
    assert box.active() == sorted([GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT])
    assert not box.marker.exists()


def test_run_reports_a_unit_that_does_not_come_back(box):
    box.close_window()
    code, out, err = finish(box.start("run", FAKE_FAIL_START=OBSERVER, CHATGPT_RELOGIN_VERIFY_TIMEOUT="1"))
    assert code == 1
    assert OBSERVER in out + err
    assert not box.marker.exists()


def test_run_requires_the_health_line_from_the_restarted_guardian(box):
    (box.state / "journal").write_text('{"chat": "x", "state": "missing-or-duplicate-page"}\n', encoding="utf-8")
    box.close_window()
    code, out, err = finish(box.start("run", CHATGPT_RELOGIN_VERIFY_TIMEOUT="1"))
    assert code == 1
    assert '"health": "connected"' in out + err
    assert not box.marker.exists()


def test_run_asks_the_journal_of_the_guardian_since_the_restart(box):
    box.close_window()
    finish(box.start("run"))
    query = next(line for line in box.log() if line.startswith("journalctl"))
    assert f"-u {GUARDIAN}.service" in query
    assert re.search(r"--since \d{4}-\d\d-\d\d \d\d:\d\d:\d\d", query)


def test_run_confirms_the_account_after_the_restart_and_writes_the_marker(box):
    box.close_window()
    code, out, err = finish(box.start("run"))
    assert code == 0, out + err
    stamp, domain = box.marker.read_text(encoding="utf-8").split()
    assert domain == "glide.vc" and re.match(r"\d{4}-\d\d-\d\dT", stamp)
    assert stat.S_IMODE(box.marker.stat().st_mode) == 0o600


def test_run_does_not_call_it_done_when_the_account_cannot_be_read(box):
    """Healthy units say nothing about WHICH account is signed in: no proof, no marker, not done."""
    box.close_window()
    code, out, err = finish(box.start("run", **NO_BROWSER))
    assert code == 1
    assert "could not be read" in out + err
    assert not box.marker.exists()
    assert box.active() == sorted([GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT]), "the units are back all the same"


def test_a_marker_is_only_ever_written_with_a_verified_domain(box):
    box.close_window()
    finish(box.start("run"))
    assert box.marker.read_text(encoding="utf-8").split()[1] == "glide.vc"


def test_run_fails_when_the_browser_came_back_with_another_account(box, devtools):
    server = devtools([CHATGPT_TAB], session("gmail.com"))
    box.close_window()
    code, out, err = finish(box.start("run", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port)))
    assert code == 1
    assert "gmail.com" in out + err and "glide.vc" in out + err
    assert not box.marker.exists()
    assert box.active() == sorted([GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT]), "the units stay up: only the login is wrong"


def chrome_in_dry_run(box, config):
    env = {k: v for k, v in box.env.items() if k != "CHATGPT_RELOGIN_CHROME"}
    done = subprocess.run(
        [str(RELOGIN), "run"],
        env={**env, "UBUNTU_TWEAK_FINISH_DRY_RUN": "1", "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(config)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_chrome_follows_the_module_option_like_the_browser_unit(box):
    assert f"{box.home}/.local/bin/chatgpt-guardian-chrome" in chrome_in_dry_run(box, {"chrome": "user"})
    assert "/opt/chrome/chrome" in chrome_in_dry_run(box, {"chrome": "/opt/chrome/chrome"})
    assert f"{box.home}/apps/chrome" in chrome_in_dry_run(box, {"chrome": "~/apps/chrome"})
    assert "/usr/bin/google-chrome" in chrome_in_dry_run(box, {})


def test_a_bad_chrome_option_is_refused(box):
    done = box.call("run", UBUNTU_TWEAK_MODULE_CONFIG=json.dumps({"chrome": 5}))
    assert done.returncode == 2
    assert "chrome" in done.stderr
    assert box.log() == []


def test_run_survives_a_terminal_that_went_away(box):
    """Closing the terminal hangs up the pipes and sends SIGHUP: the units must still come back."""
    proc = box.start("run")
    wait_for(lambda: (box.state / "chrome-argv").exists(), "the window to open")
    proc.stdout.close()
    proc.stderr.close()
    proc.send_signal(signal.SIGHUP)
    wait_for(lambda: proc.poll() is not None, "run to end", timeout=30)
    assert proc.returncode == 130
    assert box.active() == sorted([GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT])
    assert [line for line in box.log() if line.startswith("systemctl start")][0] == f"systemctl start {BROWSER}"


def test_a_second_signal_does_not_cut_the_restart_short(box):
    proc = box.start("run")
    wait_for(lambda: (box.state / "chrome-argv").exists(), "the window to open")
    proc.send_signal(signal.SIGTERM)
    proc.send_signal(signal.SIGTERM)
    proc.send_signal(signal.SIGHUP)
    code, out, err = finish(proc)
    assert code == 130
    assert box.active() == sorted([GUARDIAN, OBSERVER, BROWSER, DISPLAY_UNIT])


def test_run_dry_run_changes_nothing(box):
    done = box.call("run", UBUNTU_TWEAK_FINISH_DRY_RUN="1")
    assert done.returncode == 0
    assert str(box.profile) in done.stdout
    assert box.log() == []


def test_run_prints_secrets_nowhere(box):
    box.close_window()
    code, out, err = finish(box.start("run"))
    assert code == 0 and "@" not in out + err


def test_unknown_subcommand_is_a_usage_error(box):
    done = box.call("frobnicate")
    assert done.returncode == 2
    assert "usage" in done.stderr.lower()


def test_a_bad_domain_option_is_refused(box):
    done = box.call("check", UBUNTU_TWEAK_MODULE_CONFIG=json.dumps({"glide_email_domain": 5}))
    assert done.returncode == 2
    assert "glide_email_domain" in done.stderr


def test_the_script_is_executable_and_stdlib_only():
    assert os.access(RELOGIN, os.X_OK)
    text = RELOGIN.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3")
    assert "playwright" not in text.replace("playwright-free", "")


def test_the_unit_names_default_to_the_glide_stack_the_module_ships():
    text = RELOGIN.read_text(encoding="utf-8")
    for unit in (GUARDIAN, OBSERVER, BROWSER):
        assert (MODULE / "units" / f"{unit}.service").exists()
        assert unit in text
    assert DISPLAY_UNIT not in text.split('"""', 2)[2], "the display unit is left alone, never named in code"



# -- check --live (the guardian unit's ExecStartPre: the browser's answer or nothing) ------------------------------------

LIVE_FAST = {"CHATGPT_RELOGIN_ACCOUNT_TIMEOUT": "0.5", "CHATGPT_RELOGIN_PROBE_TIMEOUT": "1"}


def write_marker_file(box):
    box.marker.parent.mkdir(parents=True)
    box.marker.write_text("2026-10-23T07:00:00+00:00 glide.vc\n", encoding="utf-8")


def test_live_is_done_when_the_browser_shows_the_glide_domain(box, devtools):
    server = devtools([CHATGPT_TAB], session("glide.vc"))
    done = box.call("check", "--live", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **LIVE_FAST)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "glide.vc" in done.stdout


def test_live_never_falls_back_to_the_marker_when_the_browser_is_unreachable(box):
    write_marker_file(box)
    started = time.monotonic()
    done = box.call("check", "--live", **NO_BROWSER, **LIVE_FAST)
    assert time.monotonic() - started < 8
    assert done.returncode == 1
    assert "could not ask the browser" in done.stdout
    assert "no marker fallback" in done.stdout
    assert "2026-10-23" not in done.stdout


def test_live_retries_an_unavailable_answer_until_the_browser_shows_up(box, devtools):
    server = devtools([NEWTAB], session("glide.vc"))
    proc = box.start("check", "--live", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), CHATGPT_RELOGIN_ACCOUNT_TIMEOUT="10", CHATGPT_RELOGIN_PROBE_TIMEOUT="1")
    time.sleep(0.5)
    assert proc.poll() is None, "still waiting for a chatgpt.com tab"
    server.tabs[:] = [CHATGPT_TAB]
    code, out, err = finish(proc, timeout=20)
    assert code == 0, out + err


def test_live_fails_on_the_wrong_domain_and_prints_the_domain_only(box, devtools):
    write_marker_file(box)
    server = devtools([CHATGPT_TAB], session("gmail.com"))
    done = box.call("check", "--live", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **LIVE_FAST)
    assert done.returncode == 1
    assert "gmail.com" in done.stdout and "glide.vc" in done.stdout
    assert "@" not in done.stdout + done.stderr


def test_live_fails_when_signed_out(box, devtools):
    write_marker_file(box)
    server = devtools([CHATGPT_TAB], {"state": "none", "domain": ""})
    done = box.call("check", "--live", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **LIVE_FAST)
    assert done.returncode == 1
    assert "signed out" in done.stdout


# -- check --refuse-other (the home guardian unit's ExecStartPre: only another account blocks the start) ------------------

REFUSE = {"CHATGPT_RELOGIN_DOMAIN": "gmail.com", **LIVE_FAST}


def test_refuse_other_passes_the_required_domain(box, devtools):
    server = devtools([CHATGPT_TAB], session("gmail.com"))
    done = box.call("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "gmail.com" in done.stdout and "glide.vc" not in done.stdout


def test_refuse_other_blocks_another_domain_and_prints_domains_only(box, devtools):
    server = devtools([CHATGPT_TAB], session("app.glide.vc"))
    done = box.call("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 1
    assert "app.glide.vc" in done.stdout and "gmail.com" in done.stdout
    assert "@" not in done.stdout + done.stderr


def test_refuse_other_never_blocks_for_want_of_a_tab_or_a_browser(box, devtools):
    """The home browser restarts without a chatgpt.com tab and nothing opens one: `check --live` would keep the guardian down."""
    server = devtools([NEWTAB], session("app.glide.vc"))
    done = box.call("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "not known to be another one" in done.stdout
    assert box.call("check", "--refuse-other", **NO_BROWSER, **REFUSE).returncode == 0


def test_refuse_other_opens_a_chatgpt_tab_in_a_browser_that_has_none_and_refuses_the_account_it_shows(box, devtools):
    """A restarted home browser holds chrome://newtab/ only and nothing opens a chatgpt.com tab for home (revive does it for glide):
    without opening one the check would be blind exactly after a restart. The tab stays: the guardian itself needs it."""
    server = devtools([NEWTAB], session("app.glide.vc"), kind=TabbedDevTools)
    done = box.call("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "app.glide.vc" in done.stdout and "@" not in done.stdout + done.stderr
    assert server.requests == [("PUT", "/json/new?https://chatgpt.com/")]
    assert any(tab["url"] == "https://chatgpt.com/" for tab in server.tabs)


def test_refuse_other_lets_the_required_domain_start_from_a_tab_it_opened(box, devtools):
    server = devtools([NEWTAB], session("gmail.com"), kind=TabbedDevTools)
    done = box.call("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert server.requests == [("PUT", "/json/new?https://chatgpt.com/")]


def test_refuse_other_opens_no_tab_when_a_chatgpt_tab_is_already_there(box, devtools):
    server = devtools([CHATGPT_TAB], session("gmail.com"), kind=TabbedDevTools)
    done = box.call("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert server.requests == [] and len(server.tabs) == 1


def test_refuse_other_keeps_asking_until_a_restored_tab_shows_a_wrong_account(box, devtools):
    """Session restore brings the tabs back seconds after the start check begins: the check keeps asking for CHATGPT_RELOGIN_ACCOUNT_TIMEOUT
    (a browser that cannot open a tab itself here: plain DevTools has no /json/new), so a wrong account that shows up late is still refused."""
    server = devtools([NEWTAB], session("app.glide.vc"))
    proc = box.start("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), CHATGPT_RELOGIN_DOMAIN="gmail.com",
                     CHATGPT_RELOGIN_ACCOUNT_TIMEOUT="10", CHATGPT_RELOGIN_PROBE_TIMEOUT="1")
    time.sleep(0.5)
    assert proc.poll() is None, "still waiting for a chatgpt.com tab"
    server.tabs[:] = [CHATGPT_TAB]
    code, out, err = finish(proc, timeout=20)
    assert code == 1, out + err
    assert "app.glide.vc" in out and "gmail.com" in out


def test_refuse_other_does_not_block_a_signed_out_browser(box, devtools):
    server = devtools([CHATGPT_TAB], {"state": "none", "domain": ""})
    done = box.call("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 0
    assert "signed out" in done.stdout


def test_refuse_other_does_not_block_an_account_without_a_visible_domain(box, devtools):
    server = devtools([CHATGPT_TAB], {"state": "user", "domain": ""})
    done = box.call("check", "--refuse-other", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 0
    assert "not visible" in done.stdout


def test_refuse_other_speaks_russian_when_asked(box, devtools):
    server = devtools([CHATGPT_TAB], session("app.glide.vc"))
    done = box.call("check", "--refuse-other", UBUNTU_TWEAK_LANG="ru", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), **REFUSE)
    assert done.returncode == 1
    assert "другой аккаунт" in done.stdout


def test_plain_check_still_falls_back_to_the_marker(box):
    write_marker_file(box)
    assert box.call("check", **NO_BROWSER, **LIVE_FAST).returncode == 0


def test_live_speaks_russian(box):
    done = box.call("check", "--live", UBUNTU_TWEAK_LANG="ru", **NO_BROWSER, **LIVE_FAST)
    assert done.returncode == 1
    assert re.search("[а-я]", done.stdout)


@pytest.mark.parametrize("args", [["check", "--bogus"], ["check", "--live", "x"], ["run", "--live"], ["--live"], []])
def test_other_argument_shapes_are_usage_errors(box, args):
    done = box.call(*args)
    assert done.returncode == 2
    assert "usage" in done.stderr.lower()


def test_the_domain_env_overrides_the_module_option(box, devtools):
    server = devtools([CHATGPT_TAB], session("example.org"))
    done = box.call(
        "check", "--live", CHATGPT_RELOGIN_DEVTOOLS_PORT=str(server.port), CHATGPT_RELOGIN_DOMAIN="@Example.ORG",
        UBUNTU_TWEAK_MODULE_CONFIG=json.dumps({"glide_email_domain": "glide.vc"}), **LIVE_FAST,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_invalid_domain_env_is_refused(box):
    done = box.call("check", "--live", CHATGPT_RELOGIN_DOMAIN="not a domain", **LIVE_FAST)
    assert done.returncode == 2
    assert "CHATGPT_RELOGIN_DOMAIN" in done.stderr


def test_run_refuses_while_another_process_holds_the_profile_lock(box):
    locks = box.home / ".local/state/ubuntu-tweaks/chatgpt-guardian-locks"
    locks.mkdir(parents=True)
    with open(locks / "glide.vc.lock", "a", encoding="utf-8") as holder:  # profile-sync's pull takes this lock too
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = box.call("run")
    assert done.returncode == 1
    assert "another relogin or profile-sync run" in done.stderr
    assert not any(line.startswith("systemctl ") for line in box.log())
