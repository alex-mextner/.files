#!/usr/bin/env python3
"""Integration check of the INSTALLED module (about 15 s), against a throwaway HOME and a throwaway tmux socket.

Branch-local stub tests cannot prove that the pieces link up once the real installer has deployed them. This script runs
the real install.sh into a temp HOME and then uses only what it deployed:

  1. the installer deploys the scripts (tmux-save-now with its helpers, tmux-shutdown-save, tmux-shutdown-watch,
     tmux-boot-selfcheck), the four shutdown/selfcheck unit files and both scope drop-ins at the right paths, runs
     daemon-reload after the drop-ins landed, starts every shipped unit with an [Install] section except
     tmux-boot.service (the two shutdown services plus the selfcheck, autosave and watchdog timers), and arms the
     selfcheck first-fire guard (boot-id keyed) before the timer is ever started;
  2. `install.sh --check` reports no drift, and a second install changes nothing;
  3. `systemd-analyze --user verify` is clean on the installed unit files;
  4. the installer never calls tmux (so it cannot touch a running server) and the live default tmux server's session
     list is identical before and after (read-only `list-sessions` only);
  5. the REAL installed tmux-save-now, reached through the installed tmux-shutdown-save, writes a snapshot of a
     throwaway tmux server (own socket, own resurrect dir); a second call in the same server lifetime is skipped.


systemctl and loginctl are stubs that record their calls (nothing is enabled or started on this host); tmux is a shim
that pins every call to the throwaway socket and records it.

    python3 tests/integration_installed.py        # exit 0 = all checks passed, 2 = a prerequisite is missing
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

# the systemctl/loginctl stubs live with the installer tests: one copy of the recorded-calls stub serves both
try:
    from tests.test_install import SYSTEMCTL, LOGINCTL
except ImportError:  # run directly as "python3 tests/integration_installed.py"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_install import SYSTEMCTL, LOGINCTL

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_HOME = os.path.expanduser("~")
TMUX = shutil.which("tmux", path="/usr/bin:/bin") or shutil.which("tmux")
PLUGIN = os.path.join(REAL_HOME, ".tmux/plugins/tmux-resurrect/resurrect.tmux")
failures = []



def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def write_exe(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def lines(path):
    try:
        with open(path) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def default_server_sessions():
    # session ids and names only: a window count would make any window opened or closed on the live server during
    # the run (the user, an agent) a false FAIL
    r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_id} #{session_name}"],
                       capture_output=True, text=True, env={**os.environ, "TMUX": ""})
    return r.stdout if r.returncode == 0 else "(no default server)"


def setup():
    """Builds the throwaway HOME, stubs and env; returns a context dict for the phases."""
    tmp = tempfile.mkdtemp(prefix="tmux-boot-integration-")
    home = os.path.join(tmp, "home")
    stub = os.path.join(tmp, "stub")
    fakebin = os.path.join(tmp, "fakebin")
    for d in (home, stub, fakebin):
        os.makedirs(d)
    sock = "tmuxint-%d" % os.getpid()
    resurrect = os.path.join(tmp, "resurrect")
    conf = os.path.join(tmp, "tmux.conf")
    os.makedirs(resurrect)
    with open(conf, "w") as f:
        f.write("set -g @resurrect-dir %s\nrun-shell %s\n" % (resurrect, PLUGIN))
    write_exe(os.path.join(fakebin, "systemctl"), SYSTEMCTL)
    write_exe(os.path.join(fakebin, "loginctl"), LOGINCTL)
    # records every tmux call, pins it to the throwaway socket; the live default server is unreachable through it
    write_exe(os.path.join(fakebin, "tmux"),
              '#!/bin/sh\necho "tmux $*" >> "$STUB/tmux-calls.log"\nexec "%s" -L "%s" -f "%s" "$@"\n' % (TMUX, sock, conf))
    bin_dir = os.path.join(home, ".local", "bin")
    unit_dir = os.path.join(home, ".config", "systemd", "user")
    env = {
        **os.environ,
        "HOME": home,
        "STUB": stub,
        "PATH": "%s:/usr/bin:/bin" % fakebin,
        "TMUX_BOOT_BIN_DIR": bin_dir,
        "TMUX_BOOT_UNIT_DIR": unit_dir,
        "TMUX_BOOT_AUTOSTART_DIR": os.path.join(home, ".config", "autostart"),
        "TMUX_BOOT_STATE_DIR": os.path.join(tmp, "state"),
        "TMUX_SAVE_NOW_MAP_DIR": os.path.join(tmp, "maps"),
        "TMUX_AUTOSAVE_TMUX": os.path.join(fakebin, "tmux"),
    }
    env.pop("TMUX", None)
    return {"tmp": tmp, "sock": sock, "conf": conf, "resurrect": resurrect, "bin_dir": bin_dir,
            "unit_dir": unit_dir, "env": env, "stub": stub}


def installer(ctx, *args):
    return subprocess.run(["bash", os.path.join(MODULE, "install.sh"), *args],
                          env=ctx["env"], capture_output=True, text=True)


def check_deployed(ctx):
    """1a. every expected script, unit and drop-in landed."""
    bin_dir, unit_dir = ctx["bin_dir"], ctx["unit_dir"]
    for s in ("tmux-save-now", "tmux-shutdown-save", "tmux-shutdown-watch", "tmux-boot-selfcheck", "tmux-alert"):
        check(os.access(os.path.join(bin_dir, s), os.X_OK), "deployed ~/.local/bin/%s (executable)" % s)
    for s in [n for n in ("tmux-lib", "tmux-resurrect-save") if os.path.exists(os.path.join(MODULE, n))]:
        check(os.path.exists(os.path.join(bin_dir, s)), "deployed helper ~/.local/bin/%s" % s)
    for u in ("tmux-shutdown-save.service", "tmux-shutdown-watch.service",
              "tmux-boot-selfcheck.service", "tmux-boot-selfcheck.timer"):
        check(os.path.exists(os.path.join(unit_dir, u)), "deployed unit %s" % u)
    for d in ("tmux-spawn-.scope.d/10-tmux-shutdown-save.conf", "omp-.scope.d/10-tmux-shutdown-save.conf"):
        check(os.path.exists(os.path.join(unit_dir, d)), "deployed drop-in ~/.config/systemd/user/%s" % d)


def check_reload_and_starts(calls):
    """1b-i. daemon-reload ran, before the first enable, and enable --now covers the new units."""
    reloads = [i for i, c in enumerate(calls) if c.endswith("daemon-reload")]
    check(bool(reloads), "daemon-reload ran (so the drop-ins reach already running scopes)")
    started = sorted(c.split()[-1] for c in calls if " --now " in c and "enable" in c)
    needed = {"tmux-boot-selfcheck.timer", "tmux-shutdown-save.service", "tmux-shutdown-watch.service"}
    check(needed <= set(started), "enable --now covers the two shutdown services and the selfcheck timer: %s" % started)
    first_enable = min((i for i, c in enumerate(calls) if " enable " in c), default=len(calls))
    check(bool(reloads) and reloads[0] < first_enable, "daemon-reload ran before the first enable")


def check_install_calls(ctx):
    """1b. the recorded systemctl calls: reload order, starts, no stops, no tmux calls, guard armed."""
    env, stub = ctx["env"], ctx["stub"]
    calls = lines(os.path.join(stub, "calls.log"))
    check_reload_and_starts(calls)
    check(not any(c.endswith(" start tmux-boot.service") or "--now tmux-boot.service" in c for c in calls),
          "tmux-boot.service is not started (the running server stays as it is)")
    check(not [c for c in calls if (" stop " in c or " restart " in c or " kill " in c)],
          "the installer never stops, restarts or kills a unit")
    check(not lines(os.path.join(stub, "tmux-calls.log")), "the installer made no tmux call at all")
    guard = os.path.join(env["TMUX_BOOT_STATE_DIR"], "selfcheck-first-fire")
    check(os.path.exists(guard) and bool(lines(guard)), "the selfcheck first-fire guard is armed (boot id) before the timer starts")


def phase_install(ctx):
    """1. the deploy itself: files, drop-ins, reload order, starts, guard, no tmux calls."""
    r = installer(ctx)
    check(r.returncode == 0, "install.sh exits 0 (%s)" % (r.stdout + r.stderr).strip()[:200])
    check_deployed(ctx)
    check_install_calls(ctx)


def phase_idempotent(ctx):
    """2. no drift, and a second install changes nothing."""
    stub = ctx["stub"]
    r = installer(ctx, "--check")
    check(r.returncode == 0, "install.sh --check reports no drift (rc %d: %s)" % (r.returncode, r.stdout.strip()[:200]))
    n = len(lines(os.path.join(stub, "calls.log")))
    r = installer(ctx)
    check(r.returncode == 0, "the second install.sh exits 0 (%s)" % (r.stdout + r.stderr).strip()[:200])
    changed = [c for c in lines(os.path.join(stub, "calls.log"))[n:] if " enable " in c or "daemon-reload" in c or "try-restart" in c]
    check(not changed, "a second install changes nothing")


def phase_verify_units(ctx):
    """3. systemd-analyze verify on what was installed (HOME is the temp HOME, so %h resolves to the deploy)."""
    unit_dir, home = ctx["unit_dir"], ctx["env"]["HOME"]
    units = [os.path.join(unit_dir, u) for u in sorted(os.listdir(unit_dir)) if u.endswith((".service", ".timer"))]
    v = subprocess.run(["systemd-analyze", "--user", "verify", *units], env={**os.environ, "HOME": home},
                       capture_output=True, text=True)
    mine = [l for l in (v.stdout + v.stderr).splitlines() if unit_dir in l or "tmux-" in l]
    check(v.returncode == 0 and not mine, "systemd-analyze --user verify is clean on %d installed units %s" % (len(units), mine[:3]))


def phase_real_save(ctx):
    """4./5. the real installed save, through the installed shutdown step, on a throwaway server."""
    sock, conf, env, resurrect, bin_dir = ctx["sock"], ctx["conf"], ctx["env"], ctx["resurrect"], ctx["bin_dir"]
    r = subprocess.run([TMUX, "-L", sock, "-f", conf, "new-session", "-d", "-s", "main", "sleep 300"], env=env,
                       capture_output=True, text=True)
    check(r.returncode == 0, "throwaway tmux server started on its own socket (%s)" % r.stderr.strip())
    save = os.path.join(bin_dir, "tmux-shutdown-save")
    r = subprocess.run([save, "--reason", "shutdown", "--timeout", "40"], env=env, capture_output=True, text=True, timeout=90)
    print("  tmux-shutdown-save: %s%s" % (r.stdout.strip(), (" | " + r.stderr.strip()) if r.stderr.strip() else ""))
    snaps = sorted(n for n in os.listdir(resurrect) if n.startswith("tmux_resurrect_") and n.endswith(".txt"))
    check(r.returncode == 0 and "saved (shutdown)" in r.stdout, "the installed tmux-shutdown-save ran the REAL installed tmux-save-now")
    check(len(snaps) == 1 and os.path.islink(os.path.join(resurrect, "last")),
          "a snapshot was written and `last` points at it: %s" % snaps)
    if snaps:
        with open(os.path.join(resurrect, snaps[0])) as f:
            body = f.read()
        check(any(l.startswith("pane\tmain\t") for l in body.splitlines()),
              "the snapshot holds the throwaway server's pane (session main)")
    r = subprocess.run([save, "--reason", "user-manager-stop"], env=env, capture_output=True, text=True, timeout=60)
    check("skipped (user-manager-stop)" in r.stdout, "the second path skips: the shutdown save of this server already landed")


def main():
    if not TMUX or not os.path.exists(PLUGIN):
        print("prerequisite missing: tmux and ~/.tmux/plugins/tmux-resurrect")
        return 2
    ctx = setup()
    before_default = default_server_sessions()
    try:
        phase_install(ctx)
        phase_idempotent(ctx)
        phase_verify_units(ctx)
        phase_real_save(ctx)
        check(before_default == default_server_sessions(), "the live default tmux server's session list is unchanged")
    finally:
        subprocess.run([TMUX, "-L", ctx["sock"], "kill-server"], capture_output=True)
        shutil.rmtree(ctx["tmp"], ignore_errors=True)
    print("\n%s" % ("ALL PASS" if not failures else "FAILURES: %d" % len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    print("(%.0f s)" % (time.time() - t0))
    sys.exit(rc)
