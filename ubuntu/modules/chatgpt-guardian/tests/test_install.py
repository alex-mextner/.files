"""chatgpt-guardian module: option validation, plan/check on a machine without it, unit templates.

No network and no change to the real systemd: `systemctl` is a stub on PATH, HOME is a temp dir.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
INSTALL = MODULE / "install.sh"
UNITS = sorted((MODULE / "units").glob("*.service"))
RENDER_TOKENS = {"@CHROME@", "@HOME_PORT@", "@GLIDE_PORT@", "@XVFB@"}  # the keys install.sh's render_unit passes
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
