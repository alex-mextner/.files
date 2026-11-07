"""chatgpt-guardian module: the control pass install (control_pass_dir), the per-account pm audit timers (audit_timers) and ./audit-gate.

No network, no real systemd and no real pm: the install block of install.sh is sliced out of the script and run against a temp HOME
(the same way test_install.py renders units), and audit-gate runs against stub `pm` and `systemctl` programs.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from test_install import INSTALL, MODULE, fake_bin, home, run  # noqa: F401  (fixtures)

GATE = MODULE / "audit-gate"
CONTROL_SRC = MODULE / "control-pass"
SCRIPT = INSTALL.read_text(encoding="utf-8")


def between(start, end):
    return SCRIPT[SCRIPT.index(start):SCRIPT.index(end)]


RENDER = between("render() {", "home_display_tokens() {")
FACTS = between("# The control pass (option control_pass_dir)", "stale_list() {")
INSTALL_BLOCK = between("if control_active; then\n  install_file()", "while IFS= read -r f; do rm -f --")
ENABLE_BLOCK = between("# the control pass and the audits are driven by their timers", "# logins live in the browser profiles")


def harness(home, body, *, control="1", audit="true"):
    control_dir = f"{home}/state/control" if control == "1" else ""
    return f"""set -euo pipefail
here={MODULE}
header="# Managed by ubuntu-tweaks (profile module chatgpt-guardian); local edits are overwritten on apply."
HOME={home}
units_dir=$HOME/.config/systemd/user
control_dir={control_dir}
audit_timers={audit}
workspaces=(home glide.vc)
declare -A ws_unit=([home]=home [glide.vc]=glide)
sc() {{ :; }}
die() {{ echo "$1" >&2; exit 1; }}
changed=0
rewritten_timers=()
install_text() {{ mkdir -p "$(dirname "$1")"; printf '%s\\n' "$2" > "$1.tmp"; mv -f "$1.tmp" "$1"; changed=1; }}
{RENDER}
{FACTS}
{body}
"""


def bash(home, body, **kw):
    return subprocess.run(["bash", "-c", harness(home, body, **kw)], capture_output=True, text=True, timeout=60, check=False)


def enabling(home, active=""):
    """Run the block of install.sh that enables the timers, with a `systemctl` that logs its calls; `active` lists timers that already run."""
    log = home / "sc.log"
    stub_sc = f"""sc() {{
  echo "$*" >> {log}
  case "$1" in
    is-enabled) case " {active} " in *" $2 "*) echo enabled ;; *) echo disabled; return 1 ;; esac ;;
    is-active) case " {active} " in *" $2 "*) echo active ;; *) echo inactive; return 3 ;; esac ;;
  esac
  return 0
}}"""
    script = harness(home, ENABLE_BLOCK).replace("sc() { :; }", stub_sc)
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def installed(home):
    done = bash(home, f"{INSTALL_BLOCK}\necho changed=$changed")
    assert done.returncode == 0, done.stderr
    return done


def verdict(home, fn):
    return bash(home, fn).returncode == 0


UNITS_DIR = lambda home: home / ".config/systemd/user"  # noqa: E731
CONTROL_DIR = lambda home: home / "state/control"  # noqa: E731


def test_install_writes_the_control_pass_audit_units_and_stubs(home):
    installed(home)
    for name in ("control-pass.sh", "control-precheck.py", "CONTROL_PASS_BRIEF.md"):
        copy = CONTROL_DIR(home) / name
        assert copy.read_bytes() == (CONTROL_SRC / name).read_bytes()
        assert os.access(copy, os.X_OK) == os.access(CONTROL_SRC / name, os.X_OK)
    stubs = sorted(p.name for p in (CONTROL_DIR(home) / "bin-quiet").iterdir())
    assert stubs == ["mail", "msmtp", "mutt", "sendmail", "slack", "telegram-send", "tg"]
    tg = CONTROL_DIR(home) / "bin-quiet/tg"
    assert os.access(tg, os.X_OK)
    refused = subprocess.run([str(tg)], capture_output=True, text=True, check=False)
    assert refused.returncode == 1 and "tg is disabled" in refused.stderr
    names = sorted(p.name for p in UNITS_DIR(home).iterdir())
    assert names == ["guardian-proactive-control.service", "guardian-proactive-control.timer",
                     "pm-chatgpt-audit-glide.service", "pm-chatgpt-audit-glide.timer",
                     "pm-chatgpt-audit-home.service", "pm-chatgpt-audit-home.timer"]
    control = (UNITS_DIR(home) / "guardian-proactive-control.service").read_text(encoding="utf-8")
    assert f"ExecStart={CONTROL_DIR(home)}/control-pass.sh" in control and f"WorkingDirectory={CONTROL_DIR(home)}" in control
    assert verdict(home, "control_ok") and verdict(home, "audit_ok")


def test_a_second_install_changes_nothing(home):
    installed(home)
    again = installed(home)
    assert "changed=0" in again.stdout


@pytest.mark.parametrize("damage", [
    lambda h: (CONTROL_DIR(h) / "control-precheck.py").write_text("# edited\n", encoding="utf-8"),
    lambda h: (CONTROL_DIR(h) / "control-pass.sh").chmod(0o644),
    lambda h: (CONTROL_DIR(h) / "bin-quiet/tg").unlink(),
    lambda h: (CONTROL_DIR(h) / "bin-quiet/mail").write_text("#!/bin/sh\nexec real-mail \"$@\"\n", encoding="utf-8"),
    lambda h: (UNITS_DIR(h) / "guardian-proactive-control.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n", encoding="utf-8"),
    lambda h: (UNITS_DIR(h) / "guardian-proactive-control.service").unlink(),
], ids=["edited-script", "lost-exec-bit", "missing-stub", "stub-that-sends", "edited-timer", "missing-service"])
def test_check_reports_every_kind_of_control_pass_drift_and_install_repairs_it(home, damage):
    installed(home)
    damage(home)
    assert not verdict(home, "control_ok")
    installed(home)
    assert verdict(home, "control_ok")


@pytest.mark.parametrize("damage", [
    lambda h: (UNITS_DIR(h) / "pm-chatgpt-audit-glide.timer").unlink(),
    lambda h: (UNITS_DIR(h) / "pm-chatgpt-audit-home.service").write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8"),
], ids=["missing-timer", "edited-service"])
def test_check_reports_audit_unit_drift_and_install_repairs_it(home, damage):
    installed(home)
    damage(home)
    assert not verdict(home, "audit_ok")
    installed(home)
    assert verdict(home, "audit_ok")


def audit_units(home):
    installed(home)
    return {(ws, kind): (UNITS_DIR(home) / f"pm-chatgpt-audit-{ws}.{kind}").read_text(encoding="utf-8")
            for ws in ("home", "glide") for kind in ("service", "timer")}


def test_the_audits_are_staggered_every_four_hours_and_never_notify(home):
    units = audit_units(home)
    assert "OnCalendar=*-*-* 00/4:10:00" in units["home", "timer"]
    assert "OnCalendar=*-*-* 00/4:40:00" in units["glide", "timer"]
    assert "Persistent=false" in units["home", "timer"]
    for ws, workspace in (("home", "home"), ("glide", "glide.vc")):
        service, timer = units[ws, "service"], units[ws, "timer"]
        assert f"Unit=pm-chatgpt-audit-{ws}.service" in timer
        assert f"ExecStart=%h/.local/bin/pm chatgpt audit --workspace {workspace} --days 7 --json\n" in service
        assert "--notify" not in re.sub(r"^#.*$", "", service, flags=re.M)
        assert f"ExecCondition={MODULE}/audit-gate {workspace}\n" in service
        assert "Type=oneshot" in service and "[Install]" not in service
        assert re.search(r"^Nice=1[0-9]$", service, re.M) and re.search(r"^CPUQuota=\d+%$", service, re.M)
        assert "TimeoutStartSec=1680" in service and "SuccessExitStatus=3 4 75" in service


def test_audit_units_leave_no_template_token(home):
    for text in audit_units(home).values():
        assert not re.findall(r"@[A-Z_]+@", text)


@pytest.mark.parametrize("template", sorted((MODULE / "units/audit").glob("*")) + sorted((MODULE / "units/control").glob("*")), ids=lambda p: p.name)
def test_new_unit_templates_use_only_tokens_their_renderer_fills(template):
    assert set(re.findall(r"@([A-Z_]+)@", template.read_text(encoding="utf-8"))) <= {"WORKSPACE", "UNIT", "MINUTE", "AUDIT_GATE", "CONTROL_DIR"}


def test_the_control_pass_script_runs_from_wherever_it_is_installed():
    text = (CONTROL_SRC / "control-pass.sh").read_text(encoding="utf-8")
    assert "/home/ultra" not in text
    assert 'P=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)' in text


def test_nothing_is_managed_without_the_options(home, fake_bin):
    plan = run(home, fake_bin, "--plan")
    assert "control pass: not managed" in plan.stdout and "pm audit timers: not managed" in plan.stdout
    check = run(home, fake_bin, "--check")
    assert not any("control pass" in line or "pm-chatgpt-audit" in line or "guardian-proactive-control" in line for line in check.stdout.splitlines())


def test_plan_and_check_name_the_control_pass_and_the_audit_timers(home, fake_bin):
    config = {"control_pass_dir": "~/state/control", "audit_timers": True, "pm_package": "~/pm-release"}
    plan = run(home, fake_bin, "--plan", config=config)
    assert plan.returncode == 0, plan.stderr
    assert f"control pass: copy control-pass.sh control-precheck.py CONTROL_PASS_BRIEF.md and the 7 quiet stubs into {home}/state/control" in plan.stdout
    assert "pm-chatgpt-audit-home / pm-chatgpt-audit-glide" in plan.stdout
    check = run(home, fake_bin, "--check", config=config)
    lines = check.stdout.splitlines()
    assert any(f"the control pass in {home}/state/control" in line for line in lines)
    assert any("a pm-chatgpt-audit-<account> service/timer is missing or differs" in line for line in lines)
    assert any("guardian-proactive-control.timer is not enabled and active" in line for line in lines)
    assert any("pm-chatgpt-audit-glide.timer is not enabled and active" in line for line in lines)
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("config,message", [
    ({"control_pass_dir": "state/control"}, "control_pass_dir must be absolute or start with ~/"),
    ({"control_pass_dir": 3}, "control_pass_dir must be a string"),
    ({"audit_timers": "yes"}, "audit_timers must be true or false"),
])
def test_bad_control_and_audit_options_are_refused(home, fake_bin, config, message):
    done = run(home, fake_bin, "--plan", config=config)
    assert done.returncode == 1
    assert message in done.stderr


# --- ./audit-gate ------------------------------------------------------------------------------------------------------------

def stub(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def gate(tmp_path, workspace="home", *, running=("chatgpt-guardian-home", "chatgpt-guardian-home-browser"), monitor=None, schedule=None,
         monitor_rc=0, schedule_rc=0):
    base_monitor = {"connection": "connected", "effective_controls": {}, "rotation": {"cooldown": None}}
    base_schedule = {"holds": {"chats": {}, "cooldown": None, "global_stop": None}}
    (tmp_path / "monitor.json").write_text(json.dumps(monitor if monitor is not None else base_monitor), encoding="utf-8")
    (tmp_path / "schedule.json").write_text(json.dumps(schedule if schedule is not None else base_schedule), encoding="utf-8")
    pm = stub(tmp_path / "pm", f"""#!/bin/sh
case "$*" in
  *monitor*) cat {tmp_path}/monitor.json; exit {monitor_rc} ;;
  *schedule*) cat {tmp_path}/schedule.json; exit {schedule_rc} ;;
esac
exit 9
""")
    units = " ".join(f"{name}.service" for name in running)
    systemctl = stub(tmp_path / "systemctl", f"""#!/bin/sh
for u in {units}; do
  if [ "$2" = "$u" ]; then echo ActiveState=active; echo SubState=running; exit 0; fi
done
echo ActiveState=inactive; echo SubState=dead
""")
    env = {**os.environ, "AUDIT_GATE_PM": str(pm), "AUDIT_GATE_SYSTEMCTL": str(systemctl)}
    return subprocess.run([str(GATE), workspace], env=env, capture_output=True, text=True, timeout=30, check=False)


def test_the_gate_lets_a_healthy_account_through(tmp_path):
    done = gate(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_gate_checks_the_glide_units_for_glide(tmp_path):
    assert gate(tmp_path, "glide.vc", running=("chatgpt-guardian-glide", "chatgpt-guardian-glide-browser")).returncode == 0
    refused = gate(tmp_path, "glide.vc")  # only the home units run
    assert refused.returncode == 1 and "chatgpt-guardian-glide unit is not running" in refused.stdout


@pytest.mark.parametrize("kwargs,reason", [
    ({"running": ("chatgpt-guardian-home-browser",)}, "chatgpt-guardian-home unit is not running"),
    ({"running": ("chatgpt-guardian-home",)}, "chatgpt-guardian-home-browser unit is not running"),
    ({"monitor": {"connection": "disconnected", "effective_controls": {}, "rotation": {}}}, "browser disconnected"),
    ({"monitor": {"connection": "connected", "effective_controls": {"global_stop": "owner"}, "rotation": {}}}, "global stop owner"),
    ({"monitor": {"connection": "connected", "effective_controls": {"quota_latched": True}, "rotation": {}}}, "quota latched"),
    ({"monitor": {"connection": "connected", "effective_controls": {}, "rotation": {"cooldown": {"until": 4e12, "reason": "429"}}}}, "429 cooldown live"),
    ({"schedule": {"holds": {"cooldown": {"remaining": 600, "reason": "429-backoff"}}}}, "schedule hold: cooldown 600 s (429-backoff)"),
    ({"schedule": {"holds": {"global_stop": "owner"}}}, "stop owner"),
    ({"monitor_rc": 1}, "monitor unreadable"),
    ({"schedule_rc": 1}, "schedule unreadable"),
], ids=["sender-down", "browser-down", "disconnected", "global-stop", "quota", "rotation-cooldown", "schedule-cooldown", "schedule-stop",
        "monitor-unreadable", "schedule-unreadable"])
def test_the_gate_skips_whenever_the_account_cannot_take_an_audit(tmp_path, kwargs, reason):
    done = gate(tmp_path, **kwargs)
    assert done.returncode == 1, done.stdout
    assert reason in done.stdout


def test_an_expired_cooldown_does_not_block(tmp_path):
    monitor = {"connection": "connected", "effective_controls": {}, "rotation": {"cooldown": {"until": 1.0, "reason": "429"}}}
    assert gate(tmp_path, monitor=monitor).returncode == 0


def test_the_gate_refuses_an_unknown_workspace_and_missing_arguments(tmp_path):
    assert gate(tmp_path, "other").returncode == 2
    assert subprocess.run([str(GATE)], capture_output=True, text=True, timeout=30, check=False).returncode == 2


def test_the_gate_is_executable_and_the_audit_service_is_skipped_not_failed_by_it():
    assert os.access(GATE, os.X_OK)
    service = (MODULE / "units/audit/pm-chatgpt-audit.service").read_text(encoding="utf-8")
    assert re.search(r"^ExecCondition=@AUDIT_GATE@ @WORKSPACE@$", service, re.M)
    assert service.index("ExecCondition=") < service.index("ExecStartPre=")  # a skipped run must not even warm the classifier


def test_apply_enables_the_control_and_both_audit_timers(home):
    calls = [c for c in enabling(home) if c.startswith("enable")]
    assert calls == ["enable --now guardian-proactive-control.timer", "enable --now pm-chatgpt-audit-home.timer", "enable --now pm-chatgpt-audit-glide.timer"]


def test_a_rewritten_schedule_restarts_its_running_timer_only(home):
    script = harness(home, "rewritten_timers=(pm-chatgpt-audit-home guardian-proactive-control pm-chatgpt-audit-glide)\n" + ENABLE_BLOCK)
    log = home / "sc.log"
    stub_sc = f"""sc() {{
  echo "$*" >> {log}
  case "$1" in
    is-enabled) echo enabled ;;
    is-active) case "$2" in pm-chatgpt-audit-glide.timer) echo inactive; return 3 ;; *) echo active ;; esac ;;
  esac
  return 0
}}"""
    done = subprocess.run(["bash", "-c", script.replace("sc() { :; }", stub_sc)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    restarts = [c for c in log.read_text(encoding="utf-8").splitlines() if c.startswith("restart")]
    assert restarts == ["restart pm-chatgpt-audit-home.timer", "restart guardian-proactive-control.timer"]


def test_installing_a_changed_timer_file_marks_it_for_restart(home):
    installed(home)
    (UNITS_DIR(home) / "pm-chatgpt-audit-home.timer").write_text("[Timer]\nOnCalendar=hourly\n", encoding="utf-8")
    (UNITS_DIR(home) / "pm-chatgpt-audit-glide.service").write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    done = bash(home, f"{INSTALL_BLOCK}\necho marked=${{rewritten_timers[*]:-}}")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("marked=pm-chatgpt-audit-home")  # a rewritten service alone needs no timer restart


def test_apply_leaves_a_running_timer_alone(home):
    running = "guardian-proactive-control.timer pm-chatgpt-audit-home.timer"
    calls = [c for c in enabling(home, running) if c.startswith("enable")]
    assert calls == ["enable --now pm-chatgpt-audit-glide.timer"]


def test_apply_enables_nothing_the_options_do_not_ask_for(home):
    log = home / "sc.log"
    script = harness(home, ENABLE_BLOCK, control="", audit="false").replace("sc() { :; }", f'sc() {{ echo "$*" >> {log}; return 1; }}')
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert not log.exists()


@pytest.mark.parametrize("path", ["~/state/my dir", "~/state/a%h", "~/state/$x", "~/state/tab\tdir", "~/state/o'brien", "~/state/a\"b", "~/state/a\\b"])
def test_a_control_pass_dir_systemd_would_split_or_expand_is_refused(home, fake_bin, path):
    done = run(home, fake_bin, "--plan", config={"control_pass_dir": path})
    assert done.returncode == 1
    assert "control_pass_dir must not contain whitespace" in done.stderr


def test_plan_does_not_call_a_stopped_timer_ok(home, fake_bin):
    plan = run(home, fake_bin, "--plan", config={"control_pass_dir": "~/state/control", "audit_timers": True, "pm_package": "~/pm"})
    assert "control pass: copy" in plan.stdout and "ok" not in [w for w in plan.stdout.split("control pass:")[1].split("\n")[0].split()]


def test_a_failed_timer_restart_only_warns(home):
    script = harness(home, "rewritten_timers=(pm-chatgpt-audit-home)\n" + ENABLE_BLOCK).replace(
        "sc() { :; }", 'sc() { case "$1" in is-enabled) echo enabled ;; is-active) echo active ;; restart) return 1 ;; esac; return 0; }')
    done = subprocess.run(["bash", "-c", script + "\necho reached-the-end"], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0 and "reached-the-end" in done.stdout
    assert "warning: pm-chatgpt-audit-home.timer failed to restart" in done.stderr


def test_the_audit_timers_need_a_pinned_pm(home, fake_bin):
    refused = run(home, fake_bin, "--plan", config={"audit_timers": True})
    assert refused.returncode == 1 and "audit_timers needs pm_package" in refused.stderr
    assert run(home, fake_bin, "--plan", config={"audit_timers": True, "pm_package": str(home / "pm")}).returncode == 0


def test_the_pc_pins_a_pm_that_merges_single_account_audits():
    import tomllib
    machine = tomllib.loads((MODULE.parents[1] / "machines/ubuntu-pc/machine.toml").read_text(encoding="utf-8"))["modules"]["chatgpt-guardian"]
    assert machine["audit_timers"] is True and machine["pm_package"].rsplit("/", 1)[1] != "snapshot-06311d82b263a7bf45e317e643e0611bdf0267c4"


def test_the_repo_copy_of_the_pass_refuses_to_run_and_leaves_no_state(tmp_path):
    before = sorted(p.name for p in CONTROL_SRC.iterdir())
    done = subprocess.run([str(CONTROL_SRC / "control-pass.sh"), "--dry-run"], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 2 and "repo copy" in done.stderr
    assert sorted(p.name for p in CONTROL_SRC.iterdir()) == before


def test_the_control_service_outlives_the_pass_it_runs():
    service = (MODULE / "units/control/guardian-proactive-control.service").read_text(encoding="utf-8")
    script = (CONTROL_SRC / "control-pass.sh").read_text(encoding="utf-8")
    budget = sum(int(n) for n in re.findall(r"timeout (\d+) ", script))  # pre-check + model, seconds
    assert int(re.search(r"^TimeoutStartSec=(\d+)min$", service, re.M).group(1)) * 60 > budget


def test_the_control_service_sets_its_own_path_for_omp():
    service = (MODULE / "units/control/guardian-proactive-control.service").read_text(encoding="utf-8")
    assert re.search(r"^Environment=PATH=%h/\.local/bin:", service, re.M)
