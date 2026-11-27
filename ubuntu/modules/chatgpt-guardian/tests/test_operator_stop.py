"""chatgpt-guardian module: apply leaves a unit that the operator stopped on purpose alone.

An operator stop is a `99-operator-stop-*.conf` drop-in with `ConditionPathExists=!MARKER`; while MARKER exists the unit is skipped. The
blocks of install.sh that start units, probe logins and end the run are sliced out and run against a temp HOME with stub systemctl calls
(same offline harness as test_control_audit_install.py): nothing is started, probed or waited for, and the run ends needing a manual step.
"""

import subprocess

from test_control_audit_install import ENABLE_BLOCK, SCRIPT, between, harness
from test_install import fake_bin, home  # noqa: F401  (fixtures)

START_BLOCK = between("# Only stopped units are started", "# the control pass and the audits are driven")
LOGIN_LOOP = between("login_held=()\nfor ws in", "# The home guardian refuses to start while")
ENDING = SCRIPT[SCRIPT.index("if [ ${#down[@]} -gt 0 ]; then\n  echo"):]
MARKER_NAME = "operator-stop-20261127"

STUBS = """
calls=()
is_active() { return 1; }
timer_on() { return 1; }
control_active() { return 1; }
enable_unit() { calls+=("enable $1"); }
start_unit() { calls+=("start $1"); }
sc() { calls+=("sc $*"); }
hold_profile() { return 0; }
release_profile() { :; }
home_browser_on_desktop() { return 1; }
login_state() { echo "probe $1" >> "$HOME/probes"; echo yes; } # runs in a subshell: logged to a file
sleep() { calls+=("sleep $1"); }
manual() { echo "needs manual: $1" >&2; exit 10; }
die() { echo "die: $1" >&2; exit 1; }
down_hint() { echo hint; }
revive_unit=chatgpt-guardian-revive
login_attempts=3
login_wait=3
declare -A ws_port=([home]=9226 [glide.vc]=9227)
rewritten_timers=()
"""


def stop(home, unit, *, marker=True, kind="service", name=f"99-{MARKER_NAME}.conf"):
    """Give `unit` the operator-stop drop-in of the real stop; `marker` says whether the marker file exists."""
    marker_file = home / ".local/state/agent-tools/chatgpt-guardian" / MARKER_NAME
    dropin = home / ".config/systemd/user" / f"{unit}.{kind}.d" / name
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(f"# Temporary explicit operator stop\n[Unit]\nConditionPathExists=!{marker_file}\n", encoding="utf-8")
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    if marker:
        marker_file.write_text("manifest\n", encoding="utf-8")
    return marker_file


def run(home, body):
    script = harness(home, STUBS + body)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False)


def test_a_unit_with_a_drop_in_and_its_marker_is_operator_stopped(home):
    stop(home, "chatgpt-guardian-home-browser")
    done = run(home, 'operator_stopped chatgpt-guardian-home-browser && echo held; operator_stopped chatgpt-guardian-glide-browser || echo free')
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["held", "free"]


def test_a_timer_and_an_instance_of_a_template_are_operator_stopped(home):
    stop(home, "chatgpt-guardian-revive", kind="timer")
    stop(home, "pm-chatgpt-login-assist@", kind="timer")
    done = run(home, "operator_stopped chatgpt-guardian-revive && operator_stopped pm-chatgpt-login-assist@home && echo held")
    assert done.stdout.strip() == "held", done.stderr


def test_a_marker_written_with_a_systemd_path_specifier_is_found(home):
    marker = stop(home, "chatgpt-guardian-home-browser")
    dropin = next((home / ".config/systemd/user/chatgpt-guardian-home-browser.service.d").glob("*.conf"))
    dropin.write_text(f"[Unit]\nConditionPathExists=!%h/{marker.relative_to(home)}\n", encoding="utf-8")
    done = run(home, "operator_stopped chatgpt-guardian-home-browser && echo held")
    assert done.stdout.strip() == "held", done.stderr


def test_any_negated_condition_with_systemd_whitespace_counts(home):
    marker = stop(home, "chatgpt-guardian-home-browser")
    dropin = next((home / ".config/systemd/user/chatgpt-guardian-home-browser.service.d").glob("*.conf"))
    dropin.write_text(f"[Unit]\nConditionPathExists=!{home}/absent\nConditionPathExists = !{marker}  \n", encoding="utf-8")
    done = run(home, "operator_stopped chatgpt-guardian-home-browser && echo held")
    assert done.stdout.strip() == "held", done.stderr


def test_a_held_browser_holds_its_guardian_and_observer(home):
    stop(home, "chatgpt-guardian-home-browser")
    done = run(home, "for u in chatgpt-guardian-home chatgpt-guardian-home-observer; do operator_stopped $u && echo held; done; operator_stopped chatgpt-guardian-glide || echo free")
    assert done.stdout.split() == ["held", "held", "free"], done.stderr


def test_units_of_an_account_whose_display_is_held_are_held_too(home):
    stop(home, "chatgpt-guardian-glide-display")
    done = run(home, "for u in chatgpt-guardian-glide-browser chatgpt-guardian-glide chatgpt-guardian-glide-observer; do operator_stopped $u && echo held; done; operator_stopped chatgpt-guardian-home-browser || echo free")
    assert done.stdout.split() == ["held", "held", "held", "free"], done.stderr


def test_a_held_service_is_recorded_while_its_timer_stays_enabled_and_active(home):
    stop(home, "guardian-proactive-control")
    stop(home, "pm-chatgpt-audit-home")
    body = f"""timer_on() {{ return 0; }}
control_active() {{ return 0; }}
control_unit=guardian-proactive-control
rewritten_timers=(guardian-proactive-control)
{ENABLE_BLOCK}
printf '%s\\n' "${{calls[@]:-}}"
echo "held=${{operator_held[*]}}"
"""
    done = run(home, body)
    assert done.returncode == 0, done.stderr
    assert "sc restart" not in done.stdout
    assert done.stdout.splitlines()[-1] == "held=guardian-proactive-control pm-chatgpt-audit-home"


def test_a_held_audit_timer_is_not_enabled(home):
    stop(home, "pm-chatgpt-audit-home", kind="timer")
    body = f"""{ENABLE_BLOCK}
printf '%s\\n' "${{calls[@]:-}}"
"""
    done = run(home, body)
    assert "pm-chatgpt-audit-home" not in done.stdout and "pm-chatgpt-audit-glide.timer" in done.stdout, done.stderr


def test_without_the_marker_the_unit_is_not_operator_stopped(home):
    stop(home, "chatgpt-guardian-home-browser", marker=False)
    done = run(home, "operator_stopped chatgpt-guardian-home-browser || echo free")
    assert done.stdout.strip() == "free", done.stderr


def test_a_drop_in_that_is_no_operator_stop_does_not_hold_the_unit(home):
    stop(home, "chatgpt-guardian-home-browser", name="91-revive.conf")
    done = run(home, "operator_stopped chatgpt-guardian-home-browser || echo free")
    assert done.stdout.strip() == "free", done.stderr


def test_an_operator_stopped_unit_is_not_enabled_or_started_the_others_are(home):
    stop(home, "chatgpt-guardian-home-browser")
    stop(home, "chatgpt-guardian-revive", kind="timer")
    body = f"""started_units=(chatgpt-guardian-home-browser chatgpt-guardian-glide-browser)
{START_BLOCK}
printf '%s\\n' "${{calls[@]}}"
echo "held=${{operator_held[*]}} marker=$operator_marker"
"""
    done = run(home, body)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[:2] == ["enable chatgpt-guardian-glide-browser", "start chatgpt-guardian-glide-browser"]
    assert not any("home-browser" in c or "revive" in c for c in lines[:-1]), lines
    assert lines[-1] == f"held=chatgpt-guardian-home-browser chatgpt-guardian-revive marker={MARKER_NAME}"


def test_without_a_marker_every_unit_is_started_as_before(home):
    stop(home, "chatgpt-guardian-home-browser", marker=False)
    body = f"""started_units=(chatgpt-guardian-home-browser chatgpt-guardian-glide-browser)
{START_BLOCK}
printf '%s\\n' "${{calls[@]}}"
echo "held=${{#operator_held[@]}}"
"""
    done = run(home, body)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "enable chatgpt-guardian-home-browser",
        "start chatgpt-guardian-home-browser",
        "enable chatgpt-guardian-glide-browser",
        "start chatgpt-guardian-glide-browser",
        "sc enable --now chatgpt-guardian-revive.timer",
        "held=0",
    ]


def test_an_operator_stopped_account_is_neither_probed_nor_waited_for_nor_counted(home):
    stop(home, "chatgpt-guardian-home")
    body = f"""signed_out=(); unreachable=(); desktop_browser=0
{LOGIN_LOOP}
printf '%s\\n' "${{calls[@]}}"
cat "$HOME/probes" 2>/dev/null || true
echo "held=${{operator_held[*]}} signed_out=${{#signed_out[@]}} unreachable=${{#unreachable[@]}}"
"""
    done = run(home, body)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert "probe 9227" in lines and "probe 9226" not in lines
    assert not any(line.startswith("sleep") for line in lines)
    assert lines[-1] == "held=chatgpt-guardian-home signed_out=0 unreachable=0"


def test_an_operator_stopped_browser_holds_its_account_too(home):
    stop(home, "chatgpt-guardian-glide-browser")
    body = f"""signed_out=(); unreachable=(); desktop_browser=0
{LOGIN_LOOP}
printf '%s\\n' "${{calls[@]}}"
cat "$HOME/probes" 2>/dev/null || true
"""
    done = run(home, body)
    lines = done.stdout.splitlines()
    assert "probe 9226" in lines and "probe 9227" not in lines, done.stderr


def test_without_a_marker_both_accounts_are_probed(home):
    stop(home, "chatgpt-guardian-home", marker=False)
    body = f"""signed_out=(); unreachable=(); desktop_browser=0
{LOGIN_LOOP}
printf '%s\\n' "${{calls[@]}}"
cat "$HOME/probes" 2>/dev/null || true
"""
    lines = run(home, body).stdout.splitlines()
    assert "probe 9226" in lines and "probe 9227" in lines


ENDING_VARS = "login_held=(); unreachable=(); desktop_browser=0; signed_out=()\nhome_browser_unit=x\nnative_error=0\nnative_deferred=0\n"


def test_the_run_ends_needing_a_manual_step_naming_the_marker(home):
    done = run(home, f"{ENDING_VARS}down=()\noperator_held=(chatgpt-guardian-home chatgpt-guardian-home-browser)\noperator_marker={MARKER_NAME}\n{ENDING}")
    assert done.returncode == 10
    assert f"marker {MARKER_NAME}" in done.stderr
    assert "chatgpt-guardian-home chatgpt-guardian-home-browser left stopped" in done.stderr


def test_without_held_units_the_run_ends_clean(home):
    done = run(home, f"{ENDING_VARS}down=()\n{ENDING}")
    assert done.returncode == 0, done.stderr


def test_a_unit_that_stays_down_after_start_still_fails_even_with_a_held_unit(home):
    done = run(home, f"{ENDING_VARS}down=(chatgpt-guardian-glide-browser)\noperator_held=(chatgpt-guardian-home)\noperator_marker={MARKER_NAME}\n{ENDING}")
    assert done.returncode == 1
    assert "not running after start: chatgpt-guardian-glide-browser" in done.stderr
