"""control-precheck.py: the fallback Go of the 30-minute control pass, against a fake Guardian.

No real Guardian, browser, systemd or ChatGPT: a throwaway loopback HTTP server plays the account's
control API (``GET /auto-continue``, ``POST /go-now``, ``GET /go-now/<id>``, ``POST /continue-standing``),
``pm_json`` / ``unit_state`` / ``run`` are replaced, and the real ``GuardianClient`` of pm talks to the
fake through a throwaway home directory (token file mode 0600, ports of the fake servers). Every test is a
full ``main()`` pass, so what is asserted is what the timer pass does: how many POSTs reached the fake.

Run with the Guardian venv's python (it provides ``pmlib``) and any pytest:
``python -m pytest ubuntu/modules/chatgpt-guardian/control-pass/tests -q``.
"""

import importlib.util
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

VENV_SITE = Path.home() / ".local/share/agent-tools/chatgpt-guardian-venv/lib"
for site in sorted(VENV_SITE.glob("python3.*/site-packages")):
    if str(site) not in sys.path:
        sys.path.append(str(site))
pytest.importorskip("pmlib.chatgpt_control.guardian_client")
from pmlib.chatgpt_control.guardian_client import (
    GuardianClient,
    GuardianConfig,
)

SCRIPT = Path(__file__).resolve().parents[1] / "control-precheck.py"
spec = importlib.util.spec_from_file_location("control_precheck", SCRIPT)
pre = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pre)

HOME_CHAT = "https://chatgpt.com/c/6aa00000-aaaa-83eb-9aaa-000000000001"
SECOND = "https://chatgpt.com/c/6aa00000-aaaa-83eb-9aaa-000000000002"
THIRD = "https://chatgpt.com/c/6aa00000-aaaa-83eb-9aaa-000000000003"
FOURTH = "https://chatgpt.com/c/6aa00000-aaaa-83eb-9aaa-000000000004"
FIFTH = "https://chatgpt.com/c/6aa00000-aaaa-83eb-9aaa-000000000005"
AISIS_CHAT = "https://chatgpt.com/c/6adb6df9-291c-83eb-b586-55cfb30a7d30"
TURN_A = "11111111-1111-4111-8111-111111111111"
TURN_B = "22222222-2222-4222-8222-222222222222"
MIN = 60.0


class FakeGuardian:
    """One account's control API. ``posts`` records every POST (path, body); replies are scriptable."""

    def __init__(self, token):
        self.token = token
        self.consents = [{"chat": "*", "active": True, "expires_at": None}]
        self.posts = []
        self.receipts = {}
        self.post_reply = None  # (status, json) overrides the default 202 queued
        self.reply_for_basis = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self):
                if self.headers.get("Authorization") != "Bearer " + fake.token:
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._auth():
                    return
                if self.path == "/auto-continue":
                    return self._send(200, {"ok": True, "consents": fake.consents})
                for prefix in ("/go-now/", "/continuations/"):
                    if self.path.startswith(prefix):
                        rid = self.path[len(prefix):]
                        receipt = fake.receipts.get(rid)
                        if receipt is None:
                            return self._send(404, {"error": "unknown"})
                        body = {"request_id": rid, **receipt}
                        if prefix == "/go-now/":
                            body.setdefault("stage", "go-now")
                            body["turn"] = body.pop("expected_turn", "")
                        return self._send(200, body)
                self._send(404, {"error": "Unknown route"})

            def do_POST(self):
                if not self._auth():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                fake.posts.append((self.path, body))
                forced = fake.reply_for_basis.get(body.get("basis"))
                if forced is not None:
                    return self._send(*forced)
                if fake.post_reply is not None:
                    return self._send(*fake.post_reply)
                self._send(202, {"ok": True, "request_id": body["request_id"], "status": "queued"})

        return Handler

    def paths(self):
        return [p for p, _ in self.posts]


class World:
    """Everything one pass reads: per-account monitor sessions, the stored audit, and the fake Guardians."""

    def __init__(self, tmp_path, monkeypatch):
        self.now = time.time()
        self.tmp = tmp_path
        self.monitor_extra = {a: {} for a in pre.ACCOUNTS}
        self.sessions = {a: [] for a in pre.ACCOUNTS}
        self.audit = {a: {} for a in pre.ACCOUNTS}
        self.holds = {a: {} for a in pre.ACCOUNTS}
        self.rotation = {a: {"cooldown": None, "last_go": {}} for a in pre.ACCOUNTS}
        self.controls = {a: {"global_stop": "", "quota_latched": False} for a in pre.ACCOUNTS}
        self.guardians = {}
        ports = {}
        for account in pre.ACCOUNTS:
            token = ("tok-" + account) * 8
            state_dir = tmp_path / "gstate" / account
            state_dir.mkdir(parents=True)
            token_file = state_dir / "control.token"
            token_file.write_text(token)
            token_file.chmod(0o600)
            cfg = tmp_path / "home/.config/agent-tools/chatgpt-guardian"
            cfg.mkdir(parents=True, exist_ok=True)
            (cfg / f"{account}.json").write_text(json.dumps({"workspace": account, "state": str(state_dir / "g.db")}))
            self.guardians[account] = FakeGuardian(token)
            ports[account] = self.guardians[account].port
        self.client = GuardianClient(GuardianConfig(home=tmp_path / "home", ports=ports))
        self.out = tmp_path / "ctl/control-passes"
        monkeypatch.setattr(pre, "OUT", self.out)
        monkeypatch.setattr(pre, "STATE_FILE", self.out / "state.json")
        monkeypatch.setattr(pre, "pm_json", self._pm_json)
        monkeypatch.setattr(pre, "unit_state", lambda name: {"active": True, "state": "active/running", "restarts": "0", "since": "", "rc": 0})
        monkeypatch.setattr(pre, "run", self._forbidden_run)
        real = pre.Gates

        def make():
            gates = real()
            gates.client = self.client
            gates.audit_incomplete = lambda account, now: self.audit[account]
            gates.telemetry = self._telemetry
            return gates

        monkeypatch.setattr(pre, "Gates", make)

    def _telemetry(self, account):
        """The shape pm's Services.fetch returns: one entry per account, rows tagged with the account."""
        return {
            "accounts": [{"workspace": account, "ok": True, "generated_at": self.now, "stale": False}],
            "sessions": [{**row, "account": account} for row in self.sessions[account]],
        }

    @staticmethod
    def _forbidden_run(cmd, timeout=90.0):
        raise AssertionError(f"the pass must not run a command in these tests: {cmd}")

    def _pm_json(self, account, *args, timeout=90.0):
        if args[0] == "monitor":
            return {
                "connection": "connected",
                "heartbeat_age_seconds": 30.0,
                "summary": {"sent_24h": 0, "enrolled": len(self.sessions[account])},
                "effective_controls": self.controls[account],
                "rotation": self.rotation[account],
                "sessions": self.sessions[account],
                **self.monitor_extra[account],
            }
        if args[0] == "schedule":
            return {"holds": self.holds[account]}
        raise AssertionError(args)

    def add(self, chat, account="home", *, audit=True, progress_age=3 * 3600, **override):
        """One watched, finished (``complete``), unfinished-by-audit chat that no one holds; override any field."""
        row = {
            "chat": chat,
            "title": "t",
            "watched": True,
            "present": False,
            "page_count": 0,
            "observation": "queued",  # single-tab rotation: not the chat the patrol tab stands on
            "error": "",
            "status": "unknown",
            "running": None,
            "reason": "queued",
            "last_go_at": None,
            "last_go_state": None,
            "last_progress_at": self.now - progress_age,
            "turn": TURN_A,
            "recovery": {"enrolled": True, "enabled": True, "blockers": [], "hold": "", "draft": False,
                         "challenge": False, "quota_latched": False, "unresolved_send": False},
            "observed_state": {"state": "complete", "reason": "", "live": False, "turn": TURN_A, "at": self.now - 300,
                               "evidence": {"status": "complete", "turn_state": "complete", "failure_evidence": ""}},
        }
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(row.get(key), dict):
                row[key] = {**row[key], **value}
            else:
                row[key] = value
        self.sessions[account].append(row)
        if audit:
            self.audit[account][chat.rsplit("/", 1)[-1]] = {
                "conversation_id": chat.rsplit("/", 1)[-1], "task_state": "incomplete", "recovery_state": "continuable",
                "response_state": "finished", "archived": False, "verified_at": self.now - 2 * 3600,
            }
        return row

    def run_pass(self, *argv):
        monkey_argv = ["control-precheck.py", *argv]
        old = sys.argv
        sys.argv = monkey_argv
        try:
            return pre.main()
        finally:
            sys.argv = old

    def state(self):
        return json.loads((self.out / "state.json").read_text())

    def latest(self):
        return json.loads((self.out / "latest.json").read_text())

    def posts(self, account="home"):
        return self.guardians[account].posts


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# --- the Go itself --------------------------------------------------------------------------------------


def test_eligible_chat_gets_exactly_one_go_now_and_a_second_pass_does_not_resend(world):
    world.add(HOME_CHAT)
    assert world.run_pass() == 0
    assert [p for p, _ in world.posts()] == ["/go-now"]
    _, body = world.posts()[0]
    assert set(body) == {"request_id", "chat", "basis"}, "go-now names the chat only, never a turn"
    assert body["chat"] == HOME_CHAT and body["basis"] == "owner-directive"
    assert world.latest()["go_requests_this_pass"][0]["status"] == "queued"
    assert world.state()["ledger"][0]["request_id"] == body["request_id"]
    for _ in range(3):  # idempotence: the request is pending, later passes only verify it
        world.run_pass()
    assert len(world.posts()) == 1
    assert world.posts("glide.vc") == []


def test_receipt_sent_then_new_answer_verifies_and_the_chat_is_never_sent_again(world):
    world.add(HOME_CHAT)
    world.run_pass()
    rid = world.posts()[0][1]["request_id"]
    world.guardians["home"].receipts[rid] = {"status": "sent", "reason": "", "chat": HOME_CHAT, "expected_turn": TURN_A,
                                             "sent_at": world.now + 5, "source": "go-now", "basis": "owner-directive"}
    world.run_pass()
    entry = world.state()["ledger"][0]
    assert entry["receipt"] == "sent" and entry["acknowledged"] and not entry.get("final"), "sent is not yet answered"
    assert len(world.posts()) == 1
    # the Guardian's visit reads the answer: a new turn, live
    world.sessions["home"][0]["turn"] = TURN_B
    world.sessions["home"][0]["observed_state"].update(live=True, turn=TURN_B)
    world.sessions["home"][0]["last_progress_at"] = world.now + 20  # progress after the audit verdict: no longer unfinished
    world.run_pass()
    entry = world.state()["ledger"][0]
    assert entry["final"] and entry["verified_at"]
    world.run_pass()
    assert len(world.posts()) == 1


def test_go_after_guardian_silence_but_not_inside_its_own_30_minutes(world):
    world.add(HOME_CHAT, last_go_at=world.now - 10 * MIN, last_go_state="sent")
    world.run_pass()
    assert world.posts() == [], "the Guardian itself sent 10 minutes ago"
    assert "go-recent" in world.latest()["accounts"]["home"]["stopped_unfinished"][0]["blocked_by"]
    world.sessions["home"][0]["last_go_at"] = world.now - 40 * MIN
    world.run_pass()
    assert [p for p, _ in world.posts()] == ["/go-now"]


def test_grace_waits_28_minutes_from_when_the_chat_became_unfinished(world):
    world.add(HOME_CHAT, progress_age=10 * MIN)
    world.audit["home"][HOME_CHAT.rsplit("/", 1)[-1]]["verified_at"] = world.now - 5 * MIN
    world.run_pass()
    assert world.posts() == []
    assert world.latest()["accounts"]["home"]["stopped_unfinished"][0]["blocked_by"][0].startswith("grace:")


def test_a_chat_the_monitor_does_not_show_current_is_still_goable_because_go_now_reads_the_page_itself(world):
    world.add(HOME_CHAT, observation="queued", error_detail="queued-awaiting-visit", session_stale=True, check_stale=True)
    world.run_pass()
    assert [p for p, _ in world.posts()] == ["/go-now"]


def test_no_standing_route_and_no_pm_subprocess_is_used(world):
    world.add(HOME_CHAT)
    world.run_pass()
    assert "/continue-standing" not in world.guardians["home"].paths()


def test_at_most_three_requests_per_account_per_pass_and_never_two_for_one_chat(world):
    for chat in (HOME_CHAT, SECOND, THIRD, FOURTH, FIFTH):
        world.add(chat)
    world.run_pass()
    assert len(world.posts()) == 3
    world.run_pass()
    chats = [body["chat"] for _, body in world.posts()]
    assert len(chats) == 5 and len(set(chats)) == 5, "the next pass takes the other two; no chat is asked twice"
    world.run_pass()
    assert len(world.posts()) == 5
    assert world.latest()["anomalies"] == [], "requests in flight are not 'stuck' chats"


# --- refusals: every forbidden fixture posts nothing ------------------------------------------------------

FORBIDDEN = {
    "manual-stop": {"recovery": {"hold": "manual stop", "enabled": False, "blockers": ["hold"]}},
    "draft": {"recovery": {"hold": "human draft", "draft": True, "enabled": False, "blockers": ["hold"]}},
    "draft-flag-only": {"recovery": {"draft": True}},
    "challenge": {"recovery": {"challenge": True}},
    "quota-latched-chat": {"recovery": {"quota_latched": True}},
    "unresolved-send": {"recovery": {"unresolved_send": True}},
    "last-go-uncertain": {"last_go_state": "uncertain"},
    "not-enrolled": {"recovery": {"enrolled": False}},
    "running-status": {"status": "working", "running": True},
    "running-flag": {"running": True},
    "observed-working": {"observed_state": {"state": "working"}},
    "observed-unknown": {"observed_state": {"state": "unknown", "reason": "complete-without-proven-answer"}},
    "observed-failed": {"observed_state": {"state": "failed"}},
    "cancelled-turn-owner-stop-possible": {"observed_state": {"state": "stopped", "evidence": {"turn_state": "cancelled"}}},
    "complete-state-but-cancelled-evidence": {"observed_state": {"evidence": {"turn_state": "cancelled"}}},
    "recovery-blocker": {"recovery": {"blockers": ["something"]}},
}


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_forbidden_fixture_per_chat_posts_nothing(world, name):
    world.add(HOME_CHAT, **FORBIDDEN[name])
    world.run_pass()
    assert world.posts() == [], world.latest()["accounts"]["home"]["stopped_unfinished"]


def test_aisis_chat_is_never_sent_to_even_with_consent_and_every_other_gate_open(world):
    world.add(AISIS_CHAT)
    world.run_pass()
    assert world.posts() == []
    assert "aisis" in world.latest()["accounts"]["home"]["stopped_unfinished"][0]["blocked_by"]


def test_hold_in_the_schedule_holds_blocks(world):
    world.add(HOME_CHAT)
    world.holds["home"] = {"chats": {HOME_CHAT: "manual stop"}}
    world.run_pass()
    assert world.posts() == []


def test_no_finished_turn_in_the_audit_blocks(world):
    world.add(HOME_CHAT)
    world.audit["home"][HOME_CHAT.rsplit("/", 1)[-1]]["response_state"] = "none"
    world.run_pass()
    assert world.posts() == []
    assert "no-finished-turn" in world.latest()["accounts"]["home"]["stopped_unfinished"][0]["blocked_by"]


def test_progress_after_the_audit_voids_the_verdict(world):
    world.add(HOME_CHAT, progress_age=60)
    world.run_pass()
    assert world.posts() == []
    assert world.latest()["accounts"]["home"]["stopped_unfinished"] == []


def test_not_a_stopped_chat_at_all_posts_nothing(world):
    world.add(HOME_CHAT, audit=False)
    world.run_pass()
    assert world.posts() == []


def test_no_consent_or_other_chat_consent_or_expired_or_withdrawn_blocks(world):
    world.add(HOME_CHAT)
    for consents in ([], [{"chat": SECOND, "active": True, "expires_at": None}],
                     [{"chat": "*", "active": False, "expires_at": None}],
                     [{"chat": "*", "active": True, "expires_at": world.now - 10}]):
        world.guardians["home"].consents = consents
        world.run_pass()
        assert world.posts() == [], consents
        assert "no-consent" in world.latest()["accounts"]["home"]["stopped_unfinished"][0]["blocked_by"]


def test_per_chat_consent_alone_is_enough(world):
    world.add(HOME_CHAT)
    world.guardians["home"].consents = [{"chat": HOME_CHAT, "active": True, "expires_at": None}]
    world.run_pass()
    assert len(world.posts()) == 1


def test_unreachable_guardian_means_no_consent_and_no_post(world):
    world.add(HOME_CHAT)
    world.guardians["home"].server.shutdown()
    world.guardians["home"].server.server_close()
    world.run_pass()
    assert world.posts() == []
    assert str(world.latest()["accounts"]["home"]["consent"]).startswith("unreadable")


@pytest.mark.parametrize(
    "account_state",
    [
        ("cooldown", lambda w: w.rotation["home"].update(cooldown={"until": w.now + 600, "reason": "429"})),
        ("global-stop", lambda w: w.controls["home"].update(global_stop="owner stop")),
        ("quota", lambda w: w.controls["home"].update(quota_latched=True)),
        ("schedule-cooldown", lambda w: w.holds["home"].update(cooldown={"remaining": 300, "reason": "429"})),
    ],
    ids=lambda p: p[0] if isinstance(p, tuple) else p,
)
def test_account_holds_block_every_chat(world, account_state):
    world.add(HOME_CHAT)
    account_state[1](world)
    world.run_pass()
    assert world.posts() == []


def test_no_go_file_lists_but_never_posts(world):
    world.add(HOME_CHAT)
    world.out.mkdir(parents=True)
    (world.out / "NO_GO").write_text("")
    world.run_pass()
    assert world.posts() == []
    assert any("go disabled" in a for a in world.latest()["accounts"]["home"]["actions"])


def test_dry_run_posts_nothing_and_stores_nothing(world):
    world.add(HOME_CHAT)
    world.run_pass("--dry-run")
    assert world.posts() == []
    assert not (world.out / "state.json").exists()
    assert (world.out / "latest.dry-run.json").exists()


def test_the_other_account_is_untouched(world):
    world.add(HOME_CHAT, account="glide.vc")
    world.run_pass()
    assert world.posts("home") == [] and len(world.posts("glide.vc")) == 1


# --- Guardian answers ---------------------------------------------------------------------------------------


def test_refusal_at_post_is_final_and_not_retried_for_two_hours(world):
    world.add(HOME_CHAT)
    world.guardians["home"].post_reply = (409, {"reason": "standing-daily-limit"})
    world.run_pass()
    assert len(world.posts()) == 1
    entry = world.state()["ledger"][0]
    assert entry["final"] and entry["status"] == "refused"
    for _ in range(3):
        world.run_pass()
    assert len(world.posts()) == 1
    assert any(b.startswith("go-refused-recently") for b in world.latest()["accounts"]["home"]["stopped_unfinished"][0]["blocked_by"])
    state = world.state()
    state["ledger"][0]["created_at"] -= 3 * 3600
    (world.out / "state.json").write_text(json.dumps(state))
    world.run_pass()
    assert len(world.posts()) == 2, "after the retry window the same chat is tried once more"


def test_receipt_refusal_is_final_and_recorded(world):
    world.add(HOME_CHAT)
    world.run_pass()
    rid = world.posts()[0][1]["request_id"]
    world.guardians["home"].receipts[rid] = {"status": "refused", "reason": "draft-present", "chat": HOME_CHAT, "expected_turn": ""}
    world.run_pass()
    entry = world.state()["ledger"][0]
    assert entry["final"] and entry["receipt"] == "refused" and entry["receipt_reason"] == "draft-present"
    world.run_pass()
    assert len(world.posts()) == 1, "a refusal for a real gate reason is not retried inside the window"


def test_a_ticket_that_merely_expired_is_asked_again_on_the_next_pass(world):
    world.add(HOME_CHAT)
    world.run_pass()
    rid = world.posts()[0][1]["request_id"]
    world.guardians["home"].receipts[rid] = {"status": "refused", "reason": "go-now-expired", "chat": HOME_CHAT, "expected_turn": ""}
    world.run_pass()  # verifies: refused/expired, final
    world.run_pass()  # next timer pass: one fresh ticket
    assert len(world.posts()) == 2


def test_uncertain_reply_is_never_retried_automatically(world):
    world.add(HOME_CHAT)
    world.guardians["home"].post_reply = (500, {"error": "boom"})
    world.run_pass()
    assert world.state()["ledger"][0]["status"] == "uncertain"
    for _ in range(3):
        world.run_pass()
    assert len(world.posts()) == 1


def test_a_guardian_that_does_not_know_owner_directive_gets_the_older_basis_once(world):
    world.add(HOME_CHAT)
    world.guardians["home"].reply_for_basis["owner-directive"] = (400, {"reason": "basis must be one of audit-incomplete"})
    world.run_pass()
    bases = [b["basis"] for _, b in world.posts()]
    assert bases == ["owner-directive", "audit-incomplete"]
    assert world.state()["ledger"][0]["status"] == "queued"
    world.run_pass()
    assert len(world.posts()) == 2


def test_rejected_credentials_make_a_final_unavailable_entry_not_a_loop(world):
    world.add(HOME_CHAT)
    world.guardians["home"].token = "rotated-" * 8
    world.run_pass()
    assert world.posts() == [], "consent could not even be read with a rejected token: no Go"
    assert str(world.latest()["accounts"]["home"]["consent"]).startswith("unreadable")


# --- candidate listing -------------------------------------------------------------------------------------


def test_stopped_listing_names_why_each_chat_is_blocked(world):
    world.add(HOME_CHAT)
    world.add(SECOND, audit=False, observed_state={"state": "stopped", "evidence": {"turn_state": "cancelled"}})
    world.add(THIRD, recovery={"hold": "manual stop", "blockers": ["hold"]})
    world.run_pass()
    listed = {i["chat"]: i["blocked_by"] for i in world.latest()["accounts"]["home"]["stopped_unfinished"]}
    assert listed[HOME_CHAT] == []
    assert "cancelled-turn" in listed[SECOND]
    assert any(b.startswith("hold:") for b in listed[THIRD])


# --- crash safety (review-remote finding on this change) -------------------------------------------------


def test_a_crash_after_the_post_does_not_make_the_next_pass_send_again(world, monkeypatch):
    world.add(HOME_CHAT)
    original = pre.verify_ledger

    def boom(*args, **kwargs):
        raise RuntimeError("crash between the POST and the end-of-pass state write")

    monkeypatch.setattr(pre, "verify_ledger", boom)
    with pytest.raises(RuntimeError):
        world.run_pass()
    monkeypatch.setattr(pre, "verify_ledger", original)
    assert len(world.posts()) == 1
    assert world.state()["ledger"][0]["status"] == "queued"
    world.run_pass()
    assert len(world.posts()) == 1


def test_a_monitor_last_go_with_a_null_chat_does_not_crash_the_verification(world):
    world.add(HOME_CHAT)
    world.run_pass()
    world.rotation["home"]["last_go"] = {"chat": None, "state": None}
    assert world.run_pass() == 0
    assert len(world.posts()) == 1


# --- a request that nothing confirms must not block its chat for good (second review finding) -------------


def _age_ledger(world, seconds):
    state = world.state()
    for entry in state["ledger"]:
        entry["created_at"] -= seconds
    (world.out / "state.json").write_text(json.dumps(state))


def test_an_unconfirmed_request_is_reported_after_2h_and_closed_after_6h_then_the_chat_is_asked_again(world):
    world.add(HOME_CHAT)
    world.guardians["home"].post_reply = (500, {"error": "boom"})  # uncertain: no receipt can ever confirm it
    world.run_pass()
    world.guardians["home"].post_reply = None
    _age_ledger(world, 3 * 3600)
    world.run_pass()
    assert [a["kind"] for a in world.latest()["anomalies"]] == ["go-unverified"]
    assert len(world.posts()) == 1 and not world.state()["ledger"][0].get("final")
    _age_ledger(world, 4 * 3600)
    world.run_pass()
    assert world.state()["ledger"][0]["expired_unverified"] and world.state()["ledger"][0]["final"]
    assert len(world.posts()) == 1
    world.run_pass()
    assert len(world.posts()) == 2, "closed as unverified: the next pass may ask the Guardian once more"


def test_a_pending_request_for_an_older_turn_does_not_block_a_new_stop_of_the_same_chat(world):
    world.add(HOME_CHAT)
    world.run_pass()
    assert len(world.posts()) == 1
    world.sessions["home"][0]["turn"] = TURN_B
    world.sessions["home"][0]["observed_state"]["turn"] = TURN_B
    world.sessions["home"][0]["last_progress_at"] = world.now - 3 * 3600
    world.audit["home"][HOME_CHAT.rsplit("/", 1)[-1]]["verified_at"] = world.now - 2 * 3600
    world.run_pass()
    assert len(world.posts()) == 2


def test_a_monitor_row_without_a_chat_is_skipped_not_fatal(world):
    world.add(HOME_CHAT)
    world.sessions["home"].insert(0, {"watched": True, "title": "malformed"})
    world.sessions["home"].insert(0, {"watched": True, "chat": None})
    assert world.run_pass() == 0
    assert len(world.posts()) == 1
    world.run_pass()  # verification with the malformed rows present
    assert len(world.posts()) == 1


# --- third review round: foreign sends, the model record -------------------------------------------------


def test_the_guardians_own_send_just_before_our_request_does_not_verify_it(world):
    world.add(HOME_CHAT)
    world.run_pass()
    created = world.state()["ledger"][0]["created_at"]
    world.rotation["home"]["last_go"] = {"chat": HOME_CHAT, "response_turn": TURN_B, "sent_at": created - 30, "state": "sent"}
    world.run_pass()
    assert not world.state()["ledger"][0].get("final"), "a send before our request is the Guardian's own"
    world.rotation["home"]["last_go"]["sent_at"] = created + 30
    world.run_pass()
    assert world.state()["ledger"][0]["verified_at"]


def test_the_model_is_asked_until_its_run_is_recorded_and_a_clean_pass_forgets_it(world):
    world.controls["home"]["global_stop"] = "owner stop"
    assert world.run_pass() == 10
    assert world.run_pass() == 10, "no record yet: the model has not run"
    assert world.run_pass("--record-model", "ok") == 0
    assert world.run_pass() == 0, "the same anomaly set was analysed"
    world.controls["home"]["global_stop"] = ""
    assert world.run_pass() == 0
    assert "model" not in world.state()
    world.controls["home"]["global_stop"] = "owner stop"
    assert world.run_pass() == 10, "the same anomaly again after a clean pass is a new incident"


def test_a_failed_model_run_is_asked_again_after_two_hours_not_every_pass(world):
    world.controls["home"]["global_stop"] = "owner stop"
    world.run_pass()
    world.run_pass("--record-model", "failed")
    assert world.run_pass() == 0
    state = world.state()
    state["model"]["at"] -= 3 * 3600
    (world.out / "state.json").write_text(json.dumps(state))
    assert world.run_pass() == 10


def test_a_refused_request_is_never_marked_verified_by_a_later_send_of_the_guardian(world):
    world.add(HOME_CHAT)
    world.run_pass()
    rid = world.posts()[0][1]["request_id"]
    world.guardians["home"].receipts[rid] = {"status": "refused", "reason": "draft-present", "chat": HOME_CHAT, "expected_turn": ""}
    world.rotation["home"]["last_go"] = {"chat": HOME_CHAT, "response_turn": TURN_B, "sent_at": world.now + 60, "state": "sent"}
    world.run_pass()
    entry = world.state()["ledger"][0]
    assert entry["final"] and entry["receipt"] == "refused" and not entry.get("verified_at")
