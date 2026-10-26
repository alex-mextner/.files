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
RENDER_TOKENS = {"@CHROME@", "@HOME_PORT@", "@GLIDE_PORT@", "@XVFB@", "@HOME_MEMORY_HIGH@", "@HOME_MEMORY_MAX@", "@GLIDE_DOMAIN@", "@RELOGIN@"}  # the keys install.sh's render_unit passes
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
    assert sorted(listed) == sorted(p.stem for p in UNITS)


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
    harness = f"""set -euo pipefail
here={MODULE}
header=x
chrome=/c; home_port=9226; glide_port=9223; xvfb=/x
home_memory_high={config.get("high", "3G")}; home_memory_max={config.get("max", "4G")}
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
    assert "https://chatgpt.com/c/6abe65a4-77e0-83eb-9ecc-56ec6ba10d9a" in config["chats_glide"]
    assert "home_port" not in config and "glide_port" not in config
    plan = run(home, fake_bin, "--plan", config={k: v for k, v in config.items() if k not in ("enabled",)})
    assert plan.returncode == 0, plan.stderr
    assert "MemoryHigh 6G" in plan.stdout and "MemoryMax 8G" in plan.stdout
