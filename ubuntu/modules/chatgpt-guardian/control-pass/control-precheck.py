#!/usr/bin/env python3
"""Deterministic pre-check and Go sender of the 30-minute Guardian control pass (no model, no network).

Accessed via: control-pass.sh (systemd user timer guardian-proactive-control), or by hand with
``--dry-run``. Run it with the Guardian venv python: it imports pm's own gates (``pmlib``).

What one run does, in order (evidence/control-fallback/RESULT.md in the recovery state dir is the contract):
  a. per account (home, glide.vc): sender/browser/observer units, monitor connection + heartbeat,
     global stop, quota latch, 429 cooldown; an inactive sender is started (``systemctl --user
     start``; never a browser restart, never a hold/cooldown cleared).
  b. lists unfinished chats: the stored ``pm chatgpt audit`` verdict incomplete+continuable (never
     re-runs the audit: it reads the owned browser for minutes, and a verdict is void once the chat
     made progress after it) or ``observed_state.state == stopped`` (Guardian >= 0.3.30). A stopped
     (cancelled) turn is listed but never posted: it may be the owner's own Stop and Guardian's
     standing route refuses every turn state but ``complete``.
  c. sends ONE Go per unfinished chat through Guardian's ``POST /go-now`` (the chat only; the patrol
     that owns the browser serves the ticket out of turn, names the turn from its OWN read and
     re-reads every gate at the click, so a monitor that is not current never blocks it) when the
     monitor row, the consent (per chat or ``*``, unlimited is fine) and the account gates allow it
     and the Guardian itself has not sent within 30 min. One request per chat and turn marker, a
     pending request is never repeated, a refused one is retried after 2 h at the earliest.
  d. verifies earlier requests: receipt queued -> sent -> acknowledged, then a new answer observed.
Writes control-passes/latest.json (+ <ts>.md); exit 0 = nothing for a model to do, 10 = anomalies
that need the model, 2 = internal error. ``--dry-run`` never starts, posts, requests or stores state.
``--record-model ok|failed`` (control-pass.sh, after the model ran) stores that latest.json's anomaly set as analysed (failed: asked again sooner).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

P = Path(os.environ.get("GUARDIAN_CONTROL_DIR", Path(__file__).resolve().parent))
OUT = P / "control-passes"
VENV = Path.home() / ".local/share/agent-tools/chatgpt-guardian-venv"
PM = str(VENV / "bin/pm")
ACCOUNTS = {
    "home": {
        "sender": "chatgpt-guardian-home.service",
        "browser": "chatgpt-guardian-home-browser.service",
        "observer": "chatgpt-guardian-home-observer.service",
    },
    "glide.vc": {
        "sender": "chatgpt-guardian-glide.service",
        "browser": "chatgpt-guardian-glide-browser.service",
        "observer": "chatgpt-guardian-glide-observer.service",
    },
}
GRACE = 28 * 60  # the owner's 30 minutes, minus timer jitter (RandomizedDelaySec=60, AccuracySec=30s)
HEARTBEAT_STALE = 600.0
SOFT_ANOMALY_AFTER = 2 * 3600  # an unfinished chat the pass could not continue for this long, for a non-intended reason
UNVERIFIED_AFTER = 2 * 3600
PENDING_MAX = 6 * 3600  # a request that nothing confirmed by now is closed as unverified, so it cannot block its chat for good
MODEL_RECHECK = 6 * 3600  # same anomaly set: ask the model again only after this long
MODEL_RETRY = 2 * 3600  # ... or this long after a model run that failed
MAX_GO_PER_ACCOUNT = 3
AISIS = "6adb6df9"
FRESH_BY_GUARDIAN = {"pm:observation-not-current", "pm:session-stale"}  # go-now reads the page itself
HARD_QUIET = (  # intended refusals and requests in flight (go-unverified reports those after 2 h), never "stuck"
    "hold:", "draft", "aisis", "running", "challenge", "cancelled-turn", "last-observed:working", "go-pending", "go-recent",
)
REFUSED_RETRY = 2 * 3600  # a refused request for the same chat and turn marker waits this long
TRANSIENT_REFUSALS = ("go-now-expired", "restart-interrupted", "go-now-already-requested")
STATE_FILE = OUT / "state.json"


def run(cmd: list[str], timeout: float = 90.0) -> tuple[int, str, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def pm_json(account: str, *args: str, timeout: float = 90.0) -> dict | list | None:
    rc, out, _ = run([PM, "chatgpt", "--workspace", account, *args], timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def unit_state(name: str) -> dict:
    rc, out, _ = run(
        ["systemctl", "--user", "show", name, "-p", "ActiveState,SubState,NRestarts,ActiveEnterTimestamp"],
        15,
    )
    fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {
        "active": fields.get("ActiveState") == "active" and fields.get("SubState") == "running",
        "state": f"{fields.get('ActiveState', '?')}/{fields.get('SubState', '?')}",
        "restarts": fields.get("NRestarts", ""),
        "since": fields.get("ActiveEnterTimestamp", ""),
        "rc": rc,
    }


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


def num(value: object) -> bool:
    return type(value) in (int, float)


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text("utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


def save_state(state: dict) -> None:
    write_atomic(STATE_FILE, json.dumps(state, indent=1) + "\n")


class Gates:
    """pm's own continuation gates and Guardian client, or ``None`` when pmlib is unavailable."""

    def __init__(self) -> None:
        self.error = ""
        try:
            from pmlib.chatgpt_control import audit_store, policy
            from pmlib.chatgpt_control.guardian_client import (
                ACCOUNT_WIDE,
                GuardianClient,
                GuardianUnavailable,
                GuardianUncertain,
            )
            from pmlib.chatgpt_control.service import Services
        except ImportError as exc:
            self.error = f"pmlib import failed: {exc}"
            return
        self.policy, self.audit_store, self.wide = policy, audit_store, ACCOUNT_WIDE
        self.unavailable, self.uncertain = GuardianUnavailable, GuardianUncertain
        self.client, self.services = GuardianClient(), Services()

    @property
    def ok(self) -> bool:
        return not self.error

    def audit_incomplete(self, account: str, now: float) -> dict[str, dict]:
        """Chat id -> stored audit entry judged incomplete and continuable, newer than 24 h."""
        if not self.ok:
            return {}
        try:
            report = self.audit_store.AuditStore().last_report() or {}
        except Exception:
            return {}
        generated = report.get("generated_at")
        if not num(generated) or now - generated > 86400:
            return {}
        conversations = (report.get("accounts", {}).get(account, {}) or {}).get("conversations", [])
        return {str(item.get("conversation_id")): item for item in conversations if self._continuable(item)}

    @staticmethod
    def _continuable(item: dict) -> bool:
        return (
            item.get("task_state") == "incomplete"
            and item.get("recovery_state") == "continuable"
            and not item.get("archived")
        )

    def consents(self, account: str):
        try:
            return self.client.consents(account), ""
        except (self.unavailable, self.uncertain) as exc:
            return None, str(exc)

    def telemetry(self, account: str) -> dict | None:
        try:
            return self.services.fetch((account,))
        except Exception:
            return None

    def refusal(self, data: dict | None, account: str, chat: str, now: float) -> tuple[str, str]:
        """``(turn, "")`` when pm's gates allow a continuation of the latest turn, else why not."""
        if data is None:
            return "", "pm:telemetry-unavailable"
        _, row, refusal = self.policy.locate(data, account, chat, now)
        if refusal is not None:
            return "", "pm:" + refusal.reason
        turn = row.get("turn") if isinstance(row.get("turn"), str) else ""
        verdict = self.policy.evaluate_continue(data, account, chat, turn, now, require_quiet=True)
        if not verdict.ok:
            return "", "pm:" + verdict.reason
        return turn, ""

    def post(self, account: str, chat: str) -> dict:
        """One go-now ticket; a Guardian that does not know ``owner-directive`` gets the older basis."""
        result = {"status": "unavailable", "reason": "", "request_id": "", "basis": ""}
        for basis in ("owner-directive", "audit-incomplete"):
            body = {"request_id": str(uuid.uuid4()), "chat": chat, "basis": basis}
            try:
                reply = self.client.request_go_now(account, body)
            except self.unavailable as exc:
                return {**result, "reason": str(exc)}
            except self.uncertain as exc:
                return {"status": "uncertain", "reason": str(exc), "request_id": body["request_id"], "basis": basis}
            if reply.state == "queued":
                return {"status": "queued", "reason": "", "request_id": body["request_id"], "basis": basis}
            result = {"status": "refused", "reason": reply.detail, "request_id": "", "basis": basis}
            if "basis" not in reply.detail.lower():
                break
        return result

    def receipt(self, account: str, request_id: str) -> dict | None:
        try:
            return self.client.go_now_receipt(account, request_id)
        except (self.unavailable, self.uncertain, ValueError):
            return None


def holds_for(account: str) -> dict:
    sched = pm_json(account, "schedule", "--json") or {}
    return sched.get("holds") or {}


def classify(session: dict, audit: dict[str, dict]) -> str | None:
    """Why this chat counts as stopped-unfinished, or ``None``."""
    observed = session.get("observed_state") or {}
    if observed.get("state") == "stopped":
        return "observed-stopped"
    entry = audit.get(session["chat"].rstrip("/").rsplit("/", 1)[-1])
    if entry is None:
        return None
    progress, verified = session.get("last_progress_at"), entry.get("verified_at")
    if num(progress) and num(verified) and progress > verified:
        return None  # the chat moved after the verdict: it no longer says anything
    return "audit-incomplete" + ("" if entry.get("response_state") == "finished" else ":no-finished-turn")


def observed_turn(session: dict) -> str:
    return str((session.get("observed_state") or {}).get("turn") or session.get("turn") or "")


def observed_refusal(session: dict) -> str:
    """What the last classified read of the chat says against a Go."""
    observed = session.get("observed_state")
    if observed is None:  # a Guardian older than 0.3.30 reports no classification
        return "" if session.get("status") == "complete" else "last-observed:unknown"
    state = observed.get("state")
    if state == "stopped":
        return "cancelled-turn"
    if state != "complete" or (observed.get("evidence") or {}).get("turn_state") not in ("complete", ""):
        return f"last-observed:{state}"
    return ""


def monitor_refusal(session: dict) -> str:
    """What the monitor row alone says against a Go.

    pm's own gates answer ``observation-not-current`` first for every chat the single-tab patrol is not
    standing on, which masks all the others; Guardian's go-now serves the ticket from its own read and
    re-checks at the click, but a chat whose last read was not a finished, plain ``complete`` turn is never
    ticketed, and a cancelled turn is never ticketed at all (it may be the owner's own Stop; Guardian's
    standing route refuses it).
    """
    recovery = session.get("recovery") or {}
    if recovery.get("enrolled") is not True:
        return "not-enrolled"
    if [b for b in recovery.get("blockers") or [] if b != "hold"]:
        return "recovery-blocked"
    if recovery.get("unresolved_send") or session.get("last_go_state") in ("uncertain", "reserved"):
        return "send-uncertain"
    return observed_refusal(session)


def refused_reason(entry: dict) -> str:
    """Why Guardian refused this request (at the POST or in its receipt), or ``""``."""
    if entry.get("receipt") == "refused":
        return str(entry.get("receipt_reason") or "refused")
    if entry.get("status") == "refused":
        return str(entry.get("reason") or "refused")
    return ""


# --- one account ------------------------------------------------------------------------------------------


def check_units(name: str, dry: bool) -> tuple[dict, list[str], list[dict]]:
    """Unit states, what was done about them, and the anomalies left; an inactive sender is started."""
    spec = ACCOUNTS[name]
    units = {role: unit_state(unit) for role, unit in spec.items()}
    actions: list[str] = []
    anomalies: list[dict] = []
    if not units["sender"]["active"] and units["browser"]["active"]:  # a bound browser is never restarted
        if dry:
            actions.append(f"dry-run: would start {spec['sender']}")
        else:
            rc, _, err = run(["systemctl", "--user", "start", spec["sender"]], 60)
            time.sleep(5)
            units["sender"] = unit_state(spec["sender"])
            actions.append(f"started {spec['sender']} rc={rc} now={units['sender']['state']} {err.strip()[:120]}")
    for role, kind in (("sender", "sender-inactive"), ("browser", "browser-inactive"), ("observer", "observer-inactive")):
        if not units[role]["active"]:
            anomalies.append({"account": name, "kind": kind, "subject": spec[role], "detail": units[role]["state"]})
    return units, actions, anomalies


def monitor_status(name: str, monitor: dict, now: float) -> dict:
    """The account-level facts the report shows and the Go gates read."""
    summary = monitor.get("summary") or {}
    controls = monitor.get("effective_controls") or {}
    cooldown = (monitor.get("rotation") or {}).get("cooldown") or {}
    last_go = (monitor.get("rotation") or {}).get("last_go") or {}
    heartbeat = monitor.get("heartbeat_age_seconds")
    remaining = (cooldown.get("until") - now) if num(cooldown.get("until")) else 0.0
    return {
        "connection": monitor.get("connection"),
        "observer_age_seconds": round(heartbeat, 1) if num(heartbeat) else None,
        "sent_24h": summary.get("sent_24h"),
        "enrolled": summary.get("enrolled"),
        "global_stop": controls.get("global_stop") or "",
        "quota_latched": bool(controls.get("quota_latched")),
        "cooldown": {"active": remaining > 0, "remaining_seconds": round(max(remaining, 0.0)), "reason": cooldown.get("reason") or ""},
        "last_go": {"at": iso(last_go.get("sent_at")), "chat": last_go.get("chat"), "state": last_go.get("state")},
    }


def monitor_anomalies(name: str, monitor: dict, status: dict) -> list[dict]:
    found = []
    heartbeat = monitor.get("heartbeat_age_seconds")
    checks = (
        (monitor.get("connection") != "connected", "browser-not-connected", str(monitor.get("connection"))),
        (not num(heartbeat) or heartbeat > HEARTBEAT_STALE, "observer-heartbeat-stale", f"{heartbeat}"),
        (bool(status["global_stop"]), "global-stop", str(status["global_stop"])),
        (status["quota_latched"], "quota-latched", ""),
    )
    for bad, kind, detail in checks:
        if bad:
            found.append({"account": name, "kind": kind, "subject": name, "detail": detail})
    return found


def consent_covers(consents, wide: str, chat: str, now: float) -> bool:
    return consents is not None and any(
        c.active and c.chat in (chat, wide) and (c.expires_at is None or c.expires_at > now) for c in consents
    )


class AccountView:
    """Everything the per-chat gates of one account read, gathered once per pass."""

    def __init__(self, name: str, gates: Gates, status: dict, monitor: dict, state: dict, now: float) -> None:
        self.name, self.gates, self.status, self.now = name, gates, status, now
        self.state = state
        holds = holds_for(name)
        self.hold_chats = holds.get("chats") or {}
        hold_cooldown = holds.get("cooldown") or {}
        if hold_cooldown.get("remaining", 0) and not status["cooldown"]["active"]:
            status["cooldown"]["active"] = True
            status["cooldown"]["reason"] = str(hold_cooldown.get("reason") or "")
        self.consents, consent_error = gates.consents(name) if gates.ok else (None, gates.error)
        status["consent"] = (
            "unreadable: " + consent_error if self.consents is None
            else [{"chat": c.chat, "active": c.active, "expires_at": iso(c.expires_at)} for c in self.consents]
        )
        self.audit = gates.audit_incomplete(name, now)
        self.data = gates.telemetry(name) if gates.ok else None


def row_reasons(view: AccountView, session: dict, chat: str, why: str) -> list[str]:
    """Every reason against a Go that the row, its holds and the account state give."""
    reasons: list[str] = []
    recovery = session.get("recovery") or {}
    hold = recovery.get("hold") or view.hold_chats.get(session["chat"]) or view.hold_chats.get(chat)
    if AISIS in chat:
        reasons.append("aisis")
    if hold:
        reasons.append(f"hold:{hold}")
    if why.endswith(":no-finished-turn"):
        reasons.append("no-finished-turn")
    elif why == "audit-incomplete" and session.get("status") != "complete" and session.get("observation") == "current":
        reasons.append("status-not-complete")
    row_refusal = monitor_refusal(session)
    if row_refusal:
        reasons.append(row_refusal)
    if recovery.get("draft") or "draft" in str(hold).lower():
        reasons.append("draft")
    if session.get("running") is True or session.get("status") == "working":
        reasons.append("running")
    status = view.status
    if recovery.get("challenge") or recovery.get("quota_latched") or status["quota_latched"] or status["global_stop"]:
        reasons.append("challenge-quota-or-stop")
    if status["cooldown"]["active"]:
        reasons.append("cooldown")
    return reasons


def ledger_reasons(view: AccountView, session: dict, chat: str, marker: str) -> list[str]:
    """Why an earlier Go (Guardian's own or ours) still stands in the way of another one."""
    reasons: list[str] = []
    if num(session.get("last_go_at")) and view.now - session["last_go_at"] < GRACE:
        reasons.append("go-recent")
    ledger = [e for e in view.state.get("ledger", []) if e["account"] == view.name and e["chat"] == chat]
    if any(not e.get("final") and e.get("marker") == marker for e in ledger):
        reasons.append("go-pending")
    for entry in ledger:
        why_refused = refused_reason(entry)
        recent = view.now - entry["created_at"] < REFUSED_RETRY
        if why_refused and entry.get("marker") == marker and recent and not why_refused.startswith(TRANSIENT_REFUSALS):
            reasons.append(f"go-refused-recently:{why_refused[:60]}")
            break
    return reasons


def evaluate_chat(view: AccountView, session: dict) -> tuple[dict, dict | None, float] | None:
    """``(report item, go plan or None, seconds waited)`` for one unfinished chat, ``None`` for any other."""
    chat = session["chat"].rstrip("/")
    why = classify(session, view.audit)
    if why is None:
        return None
    reasons = row_reasons(view, session, chat, why)
    if not consent_covers(view.consents, view.gates.wide if view.gates.ok else "*", chat, view.now):
        reasons.append("no-consent")
    turn, refusal = view.gates.refusal(view.data, view.name, chat, view.now) if view.gates.ok else ("", "pm:unavailable")
    if refusal and refusal not in FRESH_BY_GUARDIAN:
        reasons.append(refusal)
    marker = str(observed_turn(session) or why)
    seen = view.state.setdefault("seen", {})
    key = f"{view.name}|{chat}"
    if seen.get(key, {}).get("marker") != marker:
        seen[key] = {"marker": marker, "first": view.now}
    progress = session.get("last_progress_at")
    since = min(seen[key]["first"], progress) if num(progress) else seen[key]["first"]
    reasons += ledger_reasons(view, session, chat, marker)
    waited = view.now - since
    if not reasons and waited < GRACE:
        reasons.append(f"grace:{int(GRACE - waited)}s")
    item = {
        "chat": chat,
        "title": session.get("title"),
        "why": why,
        "status": session.get("status"),
        "eligible_since": iso(since),
        "blocked_by": reasons,
    }
    plan = {"chat": chat, "turn": turn, "marker": marker, "item": item} if not reasons else None
    return item, plan, waited


def evaluate_account(name: str, gates: Gates, state: dict, now: float, dry: bool) -> dict:
    units, actions, anomalies = check_units(name, dry)
    monitor = pm_json(name, "monitor", "--json")
    status = {
        "account": name,
        "sender_active": units["sender"]["active"],
        "browser_active": units["browser"]["active"],
        "observer_active": units["observer"]["active"],
        "units": units,
        "monitor_ok": isinstance(monitor, dict),
        "actions": actions,
        "stopped_unfinished": [],
    }
    if not isinstance(monitor, dict):
        anomalies.append({"account": name, "kind": "monitor-unreadable", "subject": name, "detail": ""})
        return {"status": status, "anomalies": anomalies, "go": []}
    status.update(monitor_status(name, monitor, now))
    anomalies += monitor_anomalies(name, monitor, status)
    view = AccountView(name, gates, status, monitor, state, now)
    go_plan: list[dict] = []
    for session in monitor.get("sessions", []):
        chat = session.get("chat")
        if not session.get("watched") or not isinstance(chat, str) or not chat:
            continue
        evaluated = evaluate_chat(view, session)
        if evaluated is None:
            continue
        item, plan, waited = evaluated
        status["stopped_unfinished"].append(item)
        if plan is not None:
            go_plan.append(plan)
        elif waited > SOFT_ANOMALY_AFTER and not any(r.startswith(HARD_QUIET) for r in item["blocked_by"]):
            anomalies.append({"account": name, "kind": "unfinished-chat-stuck", "subject": item["chat"], "detail": ",".join(item["blocked_by"])})
    return {"status": status, "anomalies": anomalies, "go": go_plan, "monitor": monitor}


# --- sending and verifying --------------------------------------------------------------------------------


def post_one(account: str, plan: dict, gates: Gates, state: dict, now: float) -> dict:
    entry = {"account": account, "chat": plan["chat"], "turn": plan["turn"], "marker": plan["marker"], "kind": "go-now", "created_at": now}
    reply = gates.post(account, plan["chat"])
    entry.update(status=reply["status"], reason=reply["reason"], request_id=reply["request_id"], basis=reply["basis"])
    entry["final"] = reply["status"] in ("refused", "unavailable")
    state.setdefault("ledger", []).append(entry)
    save_state(state)  # a crash later in the pass must not forget this POST
    return entry


def send_go(account: str, result: dict, gates: Gates, state: dict, now: float, dry: bool) -> list[dict]:
    sent = []
    if result["go"] and (OUT / "NO_GO").exists():
        result["status"]["actions"].append(f"go disabled: {OUT / 'NO_GO'} exists; {len(result['go'])} goable chat(s) not sent")
        return sent
    for plan in result["go"][:MAX_GO_PER_ACCOUNT]:
        if dry:
            entry = {"account": account, "chat": plan["chat"], "turn": plan["turn"], "marker": plan["marker"], "kind": "go-now", "created_at": now, "status": "dry-run:would-send"}
        else:
            entry = post_one(account, plan, gates, state, now)
        sent.append(entry)
        result["status"]["actions"].append(f"go {entry['status']} {plan['chat']} {entry.get('reason', '')}".strip())
    return sent


def refresh_receipt(entry: dict, receipt: dict | None) -> None:
    if receipt is not None and receipt.get("status"):
        entry["receipt"] = receipt.get("status")
        entry["receipt_reason"] = receipt.get("reason", "")
    if entry.get("receipt") == "refused":
        entry["final"] = True
    if entry.get("receipt") == "sent":
        entry["acknowledged"] = True
    named = (receipt or {}).get("expected_turn")
    if named:
        entry["named_turn"] = named


def answer_seen(entry: dict, last_go: dict, session: dict) -> bool:
    """A new answer to this Go: the Guardian's own last-Go record, or a live new turn after a sent receipt."""
    by_record = (
        str(last_go.get("chat") or "").rstrip("/") == entry["chat"]
        and bool(last_go.get("response_turn"))
        and num(last_go.get("sent_at"))
        and last_go["sent_at"] >= entry["created_at"]  # a send before our request is the Guardian's own, not ours
    )
    by_turn = bool(
        entry.get("acknowledged")
        and entry.get("named_turn")
        and session.get("turn")
        and session.get("turn") != entry["named_turn"]
        and (session.get("observed_state") or {}).get("live")
    )
    return by_record or by_turn


def settle_entry(entry: dict, last_go: dict, session: dict, now: float, anomalies: list[dict]) -> None:
    """Close a request that was answered, report one nobody confirmed, close it as unverified when it is old."""
    if entry.get("receipt") == "refused":  # a refusal is final; another send to the chat is not this request's answer
        return
    age = now - entry["created_at"]
    if answer_seen(entry, last_go, session):
        entry["verified_at"] = entry.get("verified_at") or now
        entry["final"] = True
    elif age > UNVERIFIED_AFTER:
        anomalies.append({"account": entry["account"], "kind": "go-unverified", "subject": entry["chat"], "detail": entry.get("receipt") or entry.get("status", "")})
        if age > PENDING_MAX:
            entry["final"] = True
            entry["expired_unverified"] = True


def verify_entry(entry: dict, gates: Gates, monitor: dict, now: float, anomalies: list[dict]) -> dict:
    receipt = gates.receipt(entry["account"], entry["request_id"]) if gates.ok else None
    refresh_receipt(entry, receipt)
    last_go = (monitor.get("rotation") or {}).get("last_go") or {}
    session = next((s for s in monitor.get("sessions", []) if str(s.get("chat") or "").rstrip("/") == entry["chat"]), {})
    settle_entry(entry, last_go, session, now, anomalies)
    fields = ("account", "chat", "request_id", "status", "receipt", "acknowledged", "verified_at", "final")
    return {k: entry.get(k) for k in fields} | {"sent_at": (receipt or {}).get("sent_at")}


def verify_ledger(gates: Gates, state: dict, monitors: dict[str, dict], now: float, anomalies: list[dict]) -> list[dict]:
    """queued -> sent (receipt) -> acknowledged -> verified (a new answer observed after the Go)."""
    return [
        verify_entry(entry, gates, monitors.get(entry["account"]) or {}, now, anomalies)
        for entry in state.get("ledger", [])
        if not entry.get("final") and entry.get("request_id")
    ]


# --- the pass ---------------------------------------------------------------------------------------------


def model_needed(state: dict, anomalies: list[dict], fingerprint: list[str], now: float) -> bool:
    model = state.get("model") or {}
    wait = MODEL_RETRY if model.get("failed") else MODEL_RECHECK
    return bool(anomalies) and (model.get("fingerprint") != fingerprint or now - model.get("at", 0) > wait)


def build_latest(now: float, dry: bool, accounts: dict, go_requests: list[dict], anomalies: list[dict], need_model: bool) -> dict:
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%dT%H%M%S")
    return {
        "generated_at": iso(now),
        "generated_epoch": now,
        "mode": "dry-run" if dry else "live",
        "stamp": stamp,
        "summary_file": str(OUT / f"{stamp}{'.dry-run' if dry else ''}.md"),
        "accounts": accounts,
        "go_requests_this_pass": go_requests,
        "anomalies": anomalies,
        "anomaly_fingerprint": fingerprint_of(anomalies),
        "model_needed": need_model,
    }


def fingerprint_of(anomalies: list[dict]) -> list[str]:
    return sorted({f"{a['account']}:{a['kind']}:{a['subject']}" for a in anomalies})


def persist(state: dict, now: float, anomalies: list[dict]) -> None:
    state["ledger"] = [e for e in state.get("ledger", []) if not e.get("final") or now - e["created_at"] < 86400]
    state["seen"] = {k: v for k, v in state.get("seen", {}).items() if now - v["first"] < 7 * 86400}
    if not anomalies:
        state.pop("model", None)  # a clean pass: the next incident is new, whatever it looks like
    save_state(state)


def acquire_lock():
    OUT.mkdir(parents=True, exist_ok=True)
    lock = open(OUT / ".precheck.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return lock


def record_model(outcome: str) -> int:
    """After the model ran: its anomaly set counts as analysed; a failed run is asked again after MODEL_RETRY."""
    latest = json.loads((OUT / "latest.json").read_text("utf-8"))
    state = load_state()
    state["model"] = {"fingerprint": latest["anomaly_fingerprint"], "at": time.time(), "failed": outcome == "failed"}
    save_state(state)
    return 0


def run_pass(dry: bool) -> int:
    now = time.time()
    state = load_state()
    gates = Gates()
    anomalies: list[dict] = []
    accounts: dict[str, dict] = {}
    monitors: dict[str, dict] = {}
    go_requests: list[dict] = []
    if not gates.ok:
        anomalies.append({"account": "*", "kind": "pmlib-unavailable", "subject": "", "detail": gates.error})
    for name in ACCOUNTS:
        result = evaluate_account(name, gates, state, now, dry)
        anomalies += result["anomalies"]
        accounts[name] = result["status"]
        if "monitor" in result:
            monitors[name] = result["monitor"]
            go_requests += send_go(name, result, gates, state, now, dry)
    ledger = verify_ledger(gates, state, monitors, now, anomalies)
    for name, account in accounts.items():
        account["go_requests"] = [e for e in ledger if e["account"] == name]
    need_model = model_needed(state, anomalies, fingerprint_of(anomalies), now)
    latest = build_latest(now, dry, accounts, go_requests, anomalies, need_model)
    suffix = ".dry-run" if dry else ""
    write_atomic(OUT / f"latest{suffix}.json", json.dumps(latest, indent=1, default=str) + "\n")
    write_atomic(OUT / f"{latest['stamp']}{suffix}.md", render(latest))
    if not dry:
        persist(state, now, anomalies)
    print(json.dumps({"stamp": latest["stamp"], "anomalies": len(anomalies), "model_needed": need_model, "go": len(go_requests)}))
    return 10 if need_model else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="never start, post, request or store state")
    parser.add_argument("--record-model", choices=("ok", "failed"), help="store the last pass's anomaly set as handled by the model run")
    args = parser.parse_args()
    lock = acquire_lock()
    if lock is None:
        print("another pre-check holds the lock; skipped")
        return 0
    return record_model(args.record_model) if args.record_model else run_pass(args.dry_run)


def word(active: bool) -> str:
    return "active" if active else "INACTIVE"


def render_account(name: str, acc: dict) -> list[str]:
    cooldown = acc.get("cooldown") or {}
    cooling = f"{cooldown.get('remaining_seconds')}s ({cooldown.get('reason')})" if cooldown.get("active") else "none"
    lines = [
        f"## {name}",
        f"- sender {word(acc['sender_active'])}, browser {word(acc['browser_active'])}, "
        f"observer {word(acc['observer_active'])}, connection {acc.get('connection')}, heartbeat {acc.get('observer_age_seconds')} s",
        f"- sent_24h {acc.get('sent_24h')}, last Go {(acc.get('last_go') or {}).get('at')}, global stop {acc.get('global_stop') or 'none'}, cooldown {cooling}",
        f"- consent: {acc.get('consent')}",
        f"- stopped-unfinished chats: {len(acc['stopped_unfinished'])}",
    ]
    lines += [f"  - {i['chat']} ({i['why']}, since {i['eligible_since']}): {', '.join(i['blocked_by']) or 'GOABLE'}" for i in acc["stopped_unfinished"]]
    lines += [f"- action: {a}" for a in acc["actions"]]
    lines += [f"- go request: {g}" for g in acc.get("go_requests", [])]
    return [*lines, ""]


def render(latest: dict) -> str:
    lines = [f"# Control pass {latest['generated_at']} ({latest['mode']})", ""]
    for name, acc in latest["accounts"].items():
        lines += render_account(name, acc)
    found = [f"- {a['account']} {a['kind']} {a['subject']} {a['detail']}" for a in latest["anomalies"]]
    return "\n".join([*lines, "## Anomalies", *(found or ["- none"]), ""])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"precheck internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
