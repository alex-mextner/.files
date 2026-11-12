"""chatgpt-guardian module: apply's hint for a started unit that stays down.

A home guardian that its own start check (`relogin check --refuse-other`) refuses, because the home browser holds another account,
is named as that, not blamed on the display. Same offline harness as test_control_audit_install.py: the two functions are sliced
out of install.sh and run against a temp HOME; the real ./relogin asks a DevTools stub (domains only, no account is ever read).
"""

import os
import subprocess
import time

from test_control_audit_install import between, harness
from test_install import fake_bin, home  # noqa: F401  (fixtures)
from test_relogin import CHATGPT_TAB, devtools, session  # noqa: F401  (devtools is a fixture)

HINT_FUNCTIONS = between("# The home guardian refuses to start while", "# the started units must come up")
DISPLAY_HINT = "needs the desktop session's DISPLAY"
HOME_UNIT = "chatgpt-guardian-home"


def down_hint(home, *, port, domain, units=(HOME_UNIT,), fast=True):
    """What apply prints for `units` that stayed down; `fast` gives relogin the tests' own short waits instead of apply's bounds."""
    body = f"home_domain={domain!r}\nhome_port={port}\n{HINT_FUNCTIONS}\ndown_hint {' '.join(units)}"
    env = {**os.environ, "UBUNTU_TWEAK_LANG": "en"}
    env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
    for name in ("CHATGPT_RELOGIN_ACCOUNT_TIMEOUT", "CHATGPT_RELOGIN_PROBE_TIMEOUT"):
        env.pop(name, None)
    if fast:
        env.update(CHATGPT_RELOGIN_ACCOUNT_TIMEOUT="0.5", CHATGPT_RELOGIN_PROBE_TIMEOUT="1")
    done = subprocess.run(["bash", "-c", harness(home, body)], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_a_home_browser_with_another_account_is_named_not_blamed_on_the_display(home, devtools):
    server = devtools([CHATGPT_TAB], session("app.glide.vc"))
    text = down_hint(home, port=server.port, domain="gmail.com")
    assert "the home guardian refuses to start" in text
    assert "app.glide.vc" in text and "gmail.com" in text
    assert DISPLAY_HINT not in text and "@" not in text


def test_a_home_browser_with_the_required_account_keeps_the_display_hint(home, devtools):
    server = devtools([CHATGPT_TAB], session("gmail.com"))
    text = down_hint(home, port=server.port, domain="gmail.com")
    assert DISPLAY_HINT in text and "refuses" not in text


def test_an_unreachable_home_browser_keeps_the_display_hint(home):
    assert DISPLAY_HINT in down_hint(home, port=1, domain="gmail.com")


def test_without_a_declared_home_domain_the_start_check_is_not_asked(home, devtools):
    server = devtools([CHATGPT_TAB], session("app.glide.vc"))
    assert DISPLAY_HINT in down_hint(home, port=server.port, domain="")


def test_units_other_than_the_home_guardian_keep_the_display_hint(home, devtools):
    server = devtools([CHATGPT_TAB], session("app.glide.vc"))
    text = down_hint(home, port=server.port, domain="gmail.com", units=("chatgpt-guardian-glide", "chatgpt-guardian-home-browser"))
    assert DISPLAY_HINT in text and "refuses" not in text


def test_the_hint_never_waits_for_a_silent_browser_as_long_as_the_guardians_own_start_check(home):
    started = time.monotonic()
    assert DISPLAY_HINT in down_hint(home, port=1, domain="gmail.com", fast=False)
    assert time.monotonic() - started < 20  # relogin's own default waits 30 s for an answer
