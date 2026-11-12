"""chatgpt-guardian module: the login helper units (login_assist): installed verbatim from units/login-assist, one enabled timer per account.

Same offline harness as test_control_audit_install.py: the install and enable blocks of install.sh run against a temp HOME with a stub systemctl.
"""

import fcntl
import subprocess

import pytest

from test_control_audit_install import UNITS_DIR, bash, between, harness, installed  # noqa: F401
from test_install import MODULE, fake_bin, home, run  # noqa: F401  (fixtures)

SRC = MODULE / "units/login-assist"
NAMES = ["pm-chatgpt-login-assist@.service", "pm-chatgpt-login-assist@.timer"]
LOGIN_ENABLE = between("# the login helper timers go on last", "if [ ${#down[@]} -gt 0 ]")
INSTANCES = ["pm-chatgpt-login-assist@home", "pm-chatgpt-login-assist@glide.vc"]




def install_login(home):
    from test_control_audit_install import INSTALL_BLOCK
    done = bash(home, f"{INSTALL_BLOCK}\necho changed=$changed", login="true")
    assert done.returncode == 0, done.stderr
    return done


def test_install_writes_the_helper_templates_and_nothing_when_off(home):
    install_login(home)
    for name in NAMES:
        text = (UNITS_DIR(home) / name).read_text(encoding="utf-8")
        assert text.startswith("# Managed by ubuntu-tweaks (profile module chatgpt-guardian)")
        assert (lambda src: src[src.index("[Unit]"):])((SRC / name).read_text(encoding="utf-8")) in text


def test_second_install_changes_nothing_and_drift_is_repaired(home):
    install_login(home)
    assert "changed=0" in install_login(home).stdout
    (UNITS_DIR(home) / NAMES[1]).write_text("[Timer]\nOnUnitInactiveSec=1s\n", encoding="utf-8")
    assert bash(home, "login_assist_ok", login="true").returncode != 0
    install_login(home)
    assert bash(home, "login_assist_ok", login="true").returncode == 0


def test_the_service_never_loops_a_signed_out_account_itself():
    text = (SRC / NAMES[0]).read_text(encoding="utf-8")
    assert "ExecStart=%h/.local/bin/pm chatgpt login-assist --account %i\n" in text
    assert "ExecStopPost=-%h/.local/bin/pm chatgpt login-assist --account %i --restore\n" in text
    assert "Restart=no" in text and "\n[Install]" not in text


def enable_calls(home, active):
    log = home / "sc.log"
    log.unlink(missing_ok=True)
    stub = f'sc() {{ echo "$*" >> {log}; case "$1" in is-enabled) case " {active} " in *" $2 "*) echo enabled;; *) echo disabled; return 1;; esac;; is-active) case " {active} " in *" $2 "*) echo active;; *) echo inactive; return 3;; esac;; esac; }}'
    script = harness(home, LOGIN_ENABLE, login="true").replace("sc() { :; }", stub)
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return [line for line in (log.read_text(encoding="utf-8").splitlines() if log.exists() else []) if line.startswith("enable")]


def test_enable_block_enables_each_instance_timer_once_and_leaves_running_ones_alone(home):
    assert enable_calls(home, "") == [f"enable --now {i}.timer" for i in INSTANCES]
    running = " ".join(f"{i}.timer" for i in INSTANCES)
    assert enable_calls(home, running) == []


def test_plan_names_the_helper_and_a_bad_option_is_refused(home, fake_bin):
    plan = run(home, fake_bin, "--plan", config={"login_assist": True, "pm_package": "~/pm-release"})
    assert "login helper: " in plan.stdout and "pm-chatgpt-login-assist@home" in plan.stdout
    off = run(home, fake_bin, "--plan")
    assert "login helper: not managed (login_assist is false)" in off.stdout
    bad = run(home, fake_bin, "--plan", config={"login_assist": "yes"})
    assert bad.returncode == 1 and "login_assist must be true or false" in bad.stderr


def test_login_assist_is_refused_without_a_pm_or_with_custom_ports(home, fake_bin):
    no_pm = run(home, fake_bin, "--plan", config={"login_assist": True})
    assert no_pm.returncode == 1 and "login_assist needs pm_package" in no_pm.stderr
    ports = run(home, fake_bin, "--plan", config={"login_assist": True, "pm_package": "~/pm-release", "glide_port": 9333})
    assert ports.returncode == 1 and "login_assist needs the default DevTools ports" in ports.stderr


def test_install_block_writes_nothing_when_the_option_is_off(home):
    from test_control_audit_install import INSTALL_BLOCK
    done = bash(home, f"{INSTALL_BLOCK}\necho changed=$changed", login="false")
    assert done.returncode == 0, done.stderr
    assert not any(p.name.startswith("pm-chatgpt-login-assist") for p in UNITS_DIR(home).iterdir())


def test_apply_leaves_a_profile_alone_while_a_login_session_holds_its_lock(home):
    lock_dir = home / "locks"
    lock_dir.mkdir()
    (lock_dir / "glide.vc.lock").write_text("", encoding="utf-8")
    body = f"""lock_dir={lock_dir}
profile_locked glide.vc && echo locked-glide
profile_locked home && echo locked-home
unit_locked chatgpt-guardian-glide-browser && echo unit-glide
unit_locked chatgpt-guardian-home && echo unit-home"""
    with open(lock_dir / "glide.vc.lock", "a", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        done = bash(home, body, login="true")
    assert done.stdout.split() == ["locked-glide", "unit-glide"], done.stderr
    free = bash(home, body, login="true")
    assert free.stdout.strip() == ""


def test_hold_profile_takes_the_shared_lock_and_gives_up_while_a_session_holds_it(home):
    lock_dir = home / "locks"
    body = f"""lock_dir={lock_dir}; lock_wait=1
hold_profile home && echo held-free
flock -x -n "$lock_dir/home.lock" true && echo exclusive-free || echo exclusive-blocked
release_profile
flock -x -n "$lock_dir/home.lock" true && echo released"""
    done = bash(home, body, login="true")
    assert done.stdout.split() == ["held-free", "exclusive-blocked", "released"], done.stderr
    assert (lock_dir.stat().st_mode & 0o777) == 0o700 and ((lock_dir / "home.lock").stat().st_mode & 0o777) == 0o600
    lock_dir.mkdir(exist_ok=True)
    with open(lock_dir / "glide.vc.lock", "a", encoding="utf-8") as session:
        fcntl.flock(session, fcntl.LOCK_EX)
        busy = bash(home, f"lock_dir={lock_dir}; lock_wait=1\nhold_profile glide.vc || echo gave-up", login="true")
    assert busy.stdout.split() == ["gave-up"], busy.stderr
    cached = bash(home, f"lock_dir={lock_dir}; lock_wait=1\nhold_profile glide.vc || true\nprofile_locked glide.vc && echo still-held", login="true")
    assert "still-held" not in cached.stdout  # no session now: hold_profile succeeded and left no cached 'held'

