"""chatgpt-guardian module: option validation, plan/check on a machine without it, unit templates.

No network and no change to the real systemd: `systemctl` is a stub on PATH, HOME is a temp dir.
"""

import json
import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
INSTALL = MODULE / "install.sh"
UNITS = sorted((MODULE / "units").glob("*.service"))
RENDER_TOKENS = {"@CHROME@", "@HOME_PORT@", "@GLIDE_PORT@", "@XVFB@", "@HOME_MEMORY_HIGH@", "@HOME_MEMORY_MAX@", "@GLIDE_DOMAIN@", "@RELOGIN@",
                 "@HOME_DISPLAY_UNIT@", "@HOME_DISPLAY_SERVICE@", "@HOME_PRIVATE_FLAG@", "@HOME_PRIVATE_GUARD@"}  # the keys install.sh's render_unit passes
GATED = {  # unit -> workspace whose deployment file gates it
    "chatgpt-guardian-home": "home",
    "chatgpt-guardian-home-browser": "home",
    "chatgpt-guardian-home-observer": "home",
    "chatgpt-guardian-glide": "glide.vc",
    "chatgpt-guardian-glide-observer": "glide.vc",
}

SYSTEMCTL_STUB = """#!/bin/sh
# stub: a user manager that exists and has nothing enabled or running
case "$*" in
  *"is-active"*) echo inactive; exit 3 ;;
  *"is-enabled"*) echo disabled; exit 1 ;;
esac
exit 0
"""


@pytest.fixture()
def fake_bin(tmp_path):
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stub = bin_dir / "systemctl"
    stub.write_text(SYSTEMCTL_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def run(home, fake_bin, *args, config=None, **env_extra):
    env = {**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}", "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(config or {})}
    env.pop("XDG_CONFIG_HOME", None)
    env.update(env_extra)
    return subprocess.run([str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=60, check=False)


@pytest.fixture()
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


def test_unknown_option_is_refused(home, fake_bin):
    done = run(home, fake_bin, "--plan", config={"api_key": "x"})
    assert done.returncode == 1
    assert "unknown option(s): api_key" in done.stderr


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"home_port": 80}, "port out of range"),
        ({"glide_port": "abc"}, "ports must be numbers"),
        ({"home_port": 9300, "glide_port": 9300}, "must differ"),
        ({"dir": "relative/path"}, "dir must be absolute"),
        ({"chrome": "google-chrome"}, 'chrome must be an absolute path or "user"'),
        ({"repo": "--upload-pack=x"}, "repo must not start with"),
        ({"chats_home": "https://chatgpt.com/c/x"}, "chats_home must be a list"),
        ({"chats_glide": ["--help"]}, "chats_glide must be a list"),
        ({"chats_home": ["https://example.com/c/x"]}, "chats_home must be a list"),
        ({"glide_email_domain": 5}, "glide_email_domain must be a domain name"),
        ({"glide_email_domain": "-x"}, "glide_email_domain must be a domain name"),
        ({"home_memory_high": "infinity"}, "home_memory_high must look like"),
        ({"home_memory_max": "infinity"}, "home_memory_max must look like"),
        ({"home_memory_high": 3}, "home_memory_high must look like"),
        ({"home_memory_high": "0G"}, "home_memory_high must look like"),
        ({"home_memory_max": "4g"}, "home_memory_max must look like"),
        ({"home_memory_max": "512M"}, "home_memory_max must be at least 1G"),
        ({"home_memory_high": "5G", "home_memory_max": "4G"}, "home_memory_high must not exceed home_memory_max"),
        ({"home_memory_high": "5000M", "home_memory_max": "4G"}, "home_memory_high must not exceed home_memory_max"),
        ({"home_private_display": "true"}, "home_private_display must be true or false"),
        ({"home_private_display": 1}, "home_private_display must be true or false"),
        ({"home_private_display": None}, "home_private_display must be true or false"),
    ],
)
def test_bad_options_are_refused(home, fake_bin, config, message):
    done = run(home, fake_bin, "--plan", config=config)
    assert done.returncode == 1
    assert message in done.stderr


def test_plan_on_a_fresh_home_names_every_step(home, fake_bin):
    done = run(home, fake_bin, "--plan", config={"chats_home": ["https://chatgpt.com/c/x"], "chrome": "user"})
    assert done.returncode == 0
    for step in ("git clone", "chatgpt-guardian-venv", "wrapper", "home: chatgpt-guardian init", "glide.vc: chatgpt-guardian init",
                 "home: watch https://chatgpt.com/c/x", "9226", "9223", "logins:"):
        assert step in done.stdout, step


def test_plan_writes_nothing(home, fake_bin):
    done = run(home, fake_bin, "--plan")
    assert done.returncode == 0
    assert list(home.iterdir()) == []


def test_check_on_a_fresh_home_reports_drift_without_writing(home, fake_bin):
    done = run(home, fake_bin, "--check")
    assert done.returncode == 3
    lines = done.stdout.splitlines()
    assert lines and all(line.startswith("drift: ") for line in lines)
    assert any("package missing" in line for line in lines)
    assert list(home.iterdir()) == []


def test_check_ignores_xdg_config_home(home, fake_bin):
    """The units hardcode %h/.config; the script must look in the same place."""
    done = run(home, fake_bin, "--check", XDG_CONFIG_HOME=str(home / "cfg"))
    assert str(home / "cfg") not in done.stdout
    assert f"{home}/.config/agent-tools/chatgpt-guardian/home.json is missing" in done.stdout


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.stem)
def test_unit_templates_use_only_tokens_the_renderer_fills(unit):
    text = unit.read_text(encoding="utf-8")
    assert set(re.findall(r"@[A-Z_]+@", text)) <= RENDER_TOKENS


def test_every_unit_template_is_installed_and_vice_versa():
    script = INSTALL.read_text(encoding="utf-8")
    listed = re.search(r"unit_names=\(([^)]*)\)", script).group(1).split()
    conditional = re.search(r"^home_display_unit=(\S+)$", script, re.M).group(1)  # prepended to unit_names only when home_private_display is true
    assert sorted([*listed, conditional]) == sorted(p.stem for p in UNITS)
    assert conditional not in listed


@pytest.mark.parametrize(("name", "workspace"), sorted(GATED.items()))
def test_gated_units_check_their_own_deployment_file(name, workspace):
    text = (MODULE / "units" / f"{name}.service").read_text(encoding="utf-8")
    assert f"ConditionPathExists=%h/.config/agent-tools/chatgpt-guardian/{workspace}.json" in text


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.stem)
def test_no_unit_asks_for_an_api_key(unit):
    text = unit.read_text(encoding="utf-8")
    assert "API_KEY" not in text and "LoadCredential" not in text


def test_glide_stack_does_not_depend_on_the_desktop_session():
    for name in ("chatgpt-guardian-glide-browser", "chatgpt-guardian-glide", "chatgpt-guardian-glide-display"):
        text = (MODULE / "units" / f"{name}.service").read_text(encoding="utf-8")
        assert "graphical-session.target" not in text, name
        assert "WantedBy=default.target" in text, name


def test_glide_email_domain_option_is_accepted(home, fake_bin):
    done = run(home, fake_bin, "--plan", config={"glide_email_domain": "example.org"})
    assert done.returncode == 0, done.stderr


def test_the_module_declares_the_glide_login_finish_step():
    manifest = tomllib.loads((MODULE / "module.toml").read_text(encoding="utf-8"))
    (step,) = [s for s in manifest["finish"] if s["id"] == "glide-chatgpt-login"]
    for key in ("title", "title_ru", "why", "why_ru", "action", "action_ru"):
        assert step[key].strip(), key
    assert step["check"] == ["./relogin", "check"]
    assert step["run"] == ["./relogin", "run"]
    assert "requires" not in step
    assert os.access(MODULE / "relogin", os.X_OK)


def test_plan_mentions_memory_limits_and_glide_domain(home, fake_bin):
    done = run(home, fake_bin, "--plan", config={"home_memory_high": "6G", "home_memory_max": "8G", "glide_email_domain": "@Example.ORG"})
    assert done.returncode == 0, done.stderr
    units_line = next(line for line in done.stdout.splitlines() if line.startswith("units:"))
    assert "MemoryHigh 6G" in units_line and "MemoryMax 8G" in units_line
    assert "glide domain example.org" in units_line


def test_default_memory_limits_are_the_old_ones(home, fake_bin):
    done = run(home, fake_bin, "--plan")
    assert "MemoryHigh 3G" in done.stdout and "MemoryMax 4G" in done.stdout


def test_equal_and_mixed_unit_memory_limits_are_accepted(home, fake_bin):
    assert run(home, fake_bin, "--plan", config={"home_memory_high": "4096M", "home_memory_max": "4G"}).returncode == 0
    assert run(home, fake_bin, "--plan", config={"home_memory_high": "1G", "home_memory_max": "1G"}).returncode == 0


def render_via_install(home, fake_bin, unit, config):
    """The text install.sh would write: its own render_unit, taken from --check's drift-free path is not reachable
    without a deployment, so source the function the way the script defines it."""
    script = INSTALL.read_text(encoding="utf-8")
    start = script.index("render() {")
    end = script.index("site_pkg() {")
    assert "home_display_tokens" in script[start:end]
    display_unit = re.search(r"^home_display_unit=(\S+)$", script, re.M).group(1)
    private = "true" if config.get("private") else "false"
    harness = f"""set -euo pipefail
here={MODULE}
header=x
chrome=/c; home_port=9226; glide_port=9223; xvfb=/x
home_memory_high={config.get("high", "3G")}; home_memory_max={config.get("max", "4G")}
home_private_display={private}; home_display_unit={display_unit}
glide_domain=glide.vc
{script[start:end]}
render_unit {unit}
"""
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30, check=False)


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.stem)
def test_every_unit_renders_without_a_leftover_token(home, fake_bin, unit):
    done = render_via_install(home, fake_bin, unit.stem, {})
    assert done.returncode == 0, done.stderr
    assert not re.findall(r"@[A-Z_]+@", done.stdout)


def test_home_browser_unit_gets_the_configured_memory_limits(home, fake_bin):
    done = render_via_install(home, fake_bin, "chatgpt-guardian-home-browser", {"high": "6G", "max": "8G"})
    assert "MemoryHigh=6G\n" in done.stdout and "MemoryMax=8G\n" in done.stdout


def test_glide_unit_checks_the_live_account_before_starting():
    text = (MODULE / "units" / "chatgpt-guardian-glide.service").read_text(encoding="utf-8")
    pre = text.index("ExecStartPre=@RELOGIN@ check --live")
    assert pre < text.index("ExecStart=%h")
    assert "Environment=CHATGPT_RELOGIN_DEVTOOLS_PORT=@GLIDE_PORT@" in text
    assert "Environment=CHATGPT_RELOGIN_DOMAIN=@GLIDE_DOMAIN@" in text


def unit_setting(text, key):
    found = re.search(rf"^{key}=(.+)$", text, re.M)
    return found.group(1).strip() if found else None


@pytest.mark.parametrize("name", ["chatgpt-guardian-home", "chatgpt-guardian-glide", "chatgpt-guardian-home-observer", "chatgpt-guardian-glide-observer"])
def test_recovery_restarts_never_exhaust_the_start_limit(name):
    text = (MODULE / "units" / f"{name}.service").read_text(encoding="utf-8")
    assert unit_setting(text, "Restart") == "on-failure"
    burst, interval, spacing = (int(unit_setting(text, k)) for k in ("StartLimitBurst", "StartLimitIntervalSec", "RestartSec"))
    assert spacing * burst >= interval


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.stem)
def test_no_unit_restarts_always(unit):
    assert unit_setting(unit.read_text(encoding="utf-8"), "Restart") != "always"


def test_ubuntu_pc_machine_config_is_consistent(home, fake_bin):
    machine = tomllib.loads((MODULE.parents[1] / "machines/ubuntu-pc/machine.toml").read_text(encoding="utf-8"))
    config = machine["modules"]["chatgpt-guardian"]
    urls = [*config["chats_home"], *config["chats_glide"]]
    assert all(re.fullmatch(r"https://chatgpt\.com/c/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", u) for u in urls)
    assert len(set(config["chats_home"])) == len(config["chats_home"])
    assert len(set(config["chats_glide"])) == len(config["chats_glide"])
    for chat in ("6adceea6-2ad8-83ed-98c4-5fad5308956c", "6adcb4fa-dfd0-83eb-a016-dd7c908cfc37"):
        assert f"https://chatgpt.com/c/{chat}" in config["chats_home"]
    assert "https://chatgpt.com/c/6adb6385-b8f8-83ed-935a-7e3adab09c81" in config["chats_home"]  # the active CCCC chat
    assert "https://chatgpt.com/c/6abe65a4-77e0-83eb-9ecc-56ec6ba10d9a" in config["chats_glide"]
    assert config["home_private_display"] is True
    assert "home_port" not in config and "glide_port" not in config
    plan = run(home, fake_bin, "--plan", config={k: v for k, v in config.items() if k not in ("enabled",)})
    assert plan.returncode == 0, plan.stderr
    assert "MemoryHigh 6G" in plan.stdout and "MemoryMax 8G" in plan.stdout
    assert "home private display true" in plan.stdout


HOME_DISPLAY = "chatgpt-guardian-home-display"
GLIDE_UNITS = [p.stem for p in UNITS if "glide" in p.stem]
SIZE = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def rendered(home, fake_bin, unit, **config):
    done = render_via_install(home, fake_bin, unit, config)
    assert done.returncode == 0, done.stderr
    return done.stdout


def memory_bytes(text):
    return int(text[:-1]) * SIZE[text[-1]]


def unit_lines(text, key):
    return re.findall(rf"^{key}=(.+)$", text, re.M)


def test_plan_says_whether_the_home_display_is_private(home, fake_bin):
    off = run(home, fake_bin, "--plan")
    assert "home private display false" in off.stdout and "render 7 units" in off.stdout
    on = run(home, fake_bin, "--plan", config={"home_private_display": True})
    assert on.returncode == 0, on.stderr
    assert "home private display true" in on.stdout and "render 8 units" in on.stdout


def test_check_expects_the_home_display_unit_only_when_private(home, fake_bin):
    problem = f"{HOME_DISPLAY}.service is not enabled"  # the stub systemctl enables nothing
    assert problem not in run(home, fake_bin, "--check").stdout
    assert problem in run(home, fake_bin, "--check", config={"home_private_display": True}).stdout


BROWSER_STUB = """#!/bin/sh
# stub: only the home browser is running
case "$*" in
  *"is-active chatgpt-guardian-home-browser.service"*) echo active; exit 0 ;;
  *"is-active"*) echo inactive; exit 3 ;;
  *"is-enabled"*) echo disabled; exit 1 ;;
esac
exit 0
"""
SS_STUB = """#!/bin/sh
# stub: the private display listens; one client is connected to the display number STUB_CLIENT_OF (none when empty)
echo "Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port"
echo "u_str LISTEN 0      4096   @/tmp/.X11-unix/X112 148707630  * 0"
[ -z "$STUB_CLIENT_OF" ] || echo "u_str ESTAB  0      0      @/tmp/.X11-unix/X$STUB_CLIENT_OF 148702043  * 148710680"
"""
DESKTOP_BROWSER = "chatgpt-guardian-home-browser.service still runs on the desktop display"


@pytest.mark.parametrize(
    ("client_of", "private", "complaint"),
    [("0", True, True), ("", True, True), ("112", True, False), ("0", False, False)],
    ids=["browser-on-desktop", "no-client-on-the-private-display", "browser-on-private-display", "desktop-by-choice"],
)
def test_check_flags_a_home_browser_that_is_still_on_the_desktop(home, fake_bin, client_of, private, complaint):
    (fake_bin / "systemctl").write_text(BROWSER_STUB, encoding="utf-8")
    ss = fake_bin / "ss"
    ss.write_text(SS_STUB, encoding="utf-8")
    ss.chmod(ss.stat().st_mode | stat.S_IXUSR)
    deployment = home / ".config/agent-tools/chatgpt-guardian/home.json"
    deployment.parent.mkdir(parents=True)
    deployment.write_text("{}", encoding="utf-8")
    done = run(home, fake_bin, "--check", config={"home_private_display": private}, STUB_CLIENT_OF=client_of)
    assert (DESKTOP_BROWSER in done.stdout) is complaint


HOME_UNITS = [p.stem for p in UNITS if p.stem.startswith("chatgpt-guardian-home") and p.stem != HOME_DISPLAY]


@pytest.mark.parametrize("name", HOME_UNITS)
def test_a_visible_home_desktop_is_never_marked_private(home, fake_bin, name):
    text = rendered(home, fake_bin, name)
    assert "--private-display" not in text and ":112" not in text


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.stem)
def test_a_dropped_token_leaves_no_blank_line(home, fake_bin, unit, private):
    assert not re.search(r"\n\n(?!\[)", rendered(home, fake_bin, unit.stem, private=private))


def test_visible_home_browser_has_no_display_lines(home, fake_bin):
    text = rendered(home, fake_bin, "chatgpt-guardian-home-browser")
    for absent in ("DISPLAY=", "XAUTHORITY=", "Requires=", "X112", "home-display"):
        assert absent not in text, absent
    assert unit_lines(text, "After") == ["graphical-session.target"]


def test_private_home_guardian_gets_the_flag_exactly_once(home, fake_bin):
    text = rendered(home, fake_bin, "chatgpt-guardian-home", private=True)
    assert text.count("--private-display") == 1
    assert "--control-port 8790 --private-display --apply\n" in text


def test_private_home_guardian_cannot_start_without_a_client_on_the_private_display(home, fake_bin):
    text = rendered(home, fake_bin, "chatgpt-guardian-home", private=True)
    guard = unit_lines(text, "ExecStartPre")
    assert len(guard) == 1 and text.index("ExecStartPre=") < text.index("ExecStart=")
    pattern = re.search(r'grep -Eq "([^"]+)"', guard[0]).group(1)
    client_of_private = "u_str ESTAB 0      0      @/tmp/.X11-unix/X112 148702043  * 148710680"
    client_of_desktop = "u_str ESTAB 0      0      @/tmp/.X11-unix/X0 42358781  * 42364893"
    display_listening = "u_str LISTEN 0      4096   @/tmp/.X11-unix/X112 148707630  * 0"
    assert re.search(pattern, client_of_private)
    assert not re.search(pattern, client_of_desktop) and not re.search(pattern, display_listening)
    assert "ExecStartPre" not in rendered(home, fake_bin, "chatgpt-guardian-home")



def test_private_home_browser_runs_on_its_own_display(home, fake_bin):
    text = rendered(home, fake_bin, "chatgpt-guardian-home-browser", private=True)
    assert "Environment=DISPLAY=:112\n" in text
    assert "Environment=XAUTHORITY=%t/chatgpt-guardian-home-display/Xauthority\n" in text
    assert f"Requires={HOME_DISPLAY}.service\n" in text
    assert "BindsTo" not in text  # an Xvfb crash must not leave the browser (and with it the guardian) stopped
    assert f"{HOME_DISPLAY}.service" in unit_lines(text, "After")
    assert "/tmp/.X11-unix/X112" in "".join(unit_lines(text, "ExecStartPre"))
    assert "UnsetEnvironment=WAYLAND_DISPLAY" in text
    assert not re.search(r"\n\n(?!\[)", text)


def test_home_display_unit_is_a_private_xvfb_112(home, fake_bin):
    text = rendered(home, fake_bin, HOME_DISPLAY, private=True)
    assert unit_setting(text, "ExecStart").startswith("/x :112 ")
    assert "-nolisten tcp" in text
    assert unit_setting(text, "RuntimeDirectoryMode") == "0700" and unit_setting(text, "UMask") == "0077"
    assert unit_setting(text, "MemoryMax") == "256M"
    assert ":111" not in text


def test_private_display_does_not_change_the_glide_stack(home, fake_bin):
    for name in GLIDE_UNITS:
        assert rendered(home, fake_bin, name) == rendered(home, fake_bin, name, private=True), name
    assert "--private-display" in rendered(home, fake_bin, "chatgpt-guardian-glide")
    assert ":111" in rendered(home, fake_bin, "chatgpt-guardian-glide-display", private=True)


def test_display_comes_up_before_the_browser_and_shares_its_authority_file(home, fake_bin):
    display = rendered(home, fake_bin, HOME_DISPLAY, private=True)
    browser = rendered(home, fake_bin, "chatgpt-guardian-home-browser", private=True)
    assert f"{HOME_DISPLAY}.service" in unit_lines(browser, "After")
    authority = re.search(r"-auth (\S+)", display).group(1)
    assert f"XAUTHORITY={authority}" in browser
    assert authority == f"%t/{unit_setting(display, 'RuntimeDirectory')}/Xauthority"


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.stem)
def test_memory_throttle_never_exceeds_the_hard_cap(home, fake_bin, unit, private):
    text = rendered(home, fake_bin, unit.stem, private=private, high="6G", max="8G")
    high, cap = unit_setting(text, "MemoryHigh"), unit_setting(text, "MemoryMax")
    assert (high is None) == (cap is None)
    if high:
        assert memory_bytes(high) <= memory_bytes(cap)


def test_display_cap_does_not_limit_the_home_browser(home, fake_bin):
    browser = rendered(home, fake_bin, "chatgpt-guardian-home-browser", private=True, high="6G", max="8G")
    display = rendered(home, fake_bin, HOME_DISPLAY, private=True, high="6G", max="8G")
    assert unit_setting(browser, "MemoryHigh") == "6G" and unit_setting(browser, "MemoryMax") == "8G"
    assert memory_bytes(unit_setting(display, "MemoryMax")) == 256 * 1024**2


# -- the opt-in profile-sync wrapper ---------------------------------------------------------------------------------------


def test_plan_and_check_name_the_profile_sync_wrapper(home, fake_bin):
    wrapper = f"{home}/.local/bin/chatgpt-guardian-profile-sync"
    plan = run(home, fake_bin, "--plan")
    assert f"write {wrapper}" in plan.stdout
    assert "apply never runs it" in plan.stdout
    check = run(home, fake_bin, "--check")
    assert f"drift: {wrapper} is missing or out of date" in check.stdout
    assert list(home.iterdir()) == []


def test_the_wrapper_hands_profile_sync_the_options_of_the_machine(home, fake_bin, tmp_path):
    done = run(home, fake_bin, "--print-sync-wrapper", config={"home_port": "09300", "glide_port": 9301, "glide_email_domain": "@App.Glide.vc"})
    assert done.returncode == 0, done.stderr
    assert list(home.iterdir()) == []
    wrapper = tmp_path / "chatgpt-guardian-profile-sync"
    wrapper.write_text(done.stdout, encoding="utf-8")
    wrapper.chmod(0o755)
    config = re.search(r"UBUNTU_TWEAK_MODULE_CONFIG='(.*)'", done.stdout).group(1)
    assert json.loads(config) == {"home_port": 9300, "glide_port": 9301, "glide_email_domain": "app.glide.vc"}
    # run it: `status` asks the two DevTools ports of this machine and reads nothing else (a temp HOME has no profiles)
    env = {**os.environ, "HOME": str(home), "UBUNTU_TWEAK_LANG": "en", "CHATGPT_RELOGIN_PROBE_TIMEOUT": "1"}
    env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
    status = subprocess.run([str(wrapper), "status"], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert status.returncode == 0, status.stderr
    assert "home: cannot be asked (no DevTools answer on port 9300)" in status.stdout
    assert "glide: cannot be asked (no DevTools answer on port 9301)" in status.stdout


def test_the_module_ships_profile_sync_next_to_relogin():
    sync = MODULE / "profile-sync"
    assert sync.is_file() and os.access(sync, os.X_OK)
    assert (MODULE / "relogin").is_file()
