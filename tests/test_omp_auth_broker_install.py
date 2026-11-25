"""ubuntu-pc scripts/omp-auth-broker/install.sh against fake systemctl, ssh, ss and omp.

The fake ssh runs the remote command locally with bash under the consumer's own fake HOME
(`<base>/consumers/<alias>`), so the remote omp calls, the bearer write and its config file are the real script's.
The fake systemctl keeps one state file per side (broker PC, consumer) and logs every call. Starting a tunnel
instance writes the record the real unit's ExecStart writes on the consumer, naming this test process as the live
sshd-session, so the script's own-session check runs for real against /proc.
"""
import os
import pathlib
import stat
import subprocess
import sys

import pytest
import yaml

pytestmark = pytest.mark.skipif(not pathlib.Path("/proc/sys/kernel/random/boot_id").exists(),
                                reason="needs Linux /proc (the script and the fakes read boot_id and /proc/<pid>/stat)")

ROOT = pathlib.Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "ubuntu/machines/ubuntu-pc"
SCRIPT = OVERLAY / "scripts/omp-auth-broker/install.sh"
SRC_DIR = (OVERLAY / "files/.config/systemd/user").resolve()
FAKE_BEARER = "YOUR_KEY_HERE-fake-bearer"
URL = "http://127.0.0.1:18765"

# Remote commands run under sh (dash on Ubuntu), the plainest login shell a consumer may have.
FAKE_SSH = r"""#!/usr/bin/env bash
dump=0
while [ "${1#-}" != "$1" ]; do case "$1" in -o) shift 2 ;; -G) dump=1; shift ;; *) shift ;; esac; done
host=$1; shift
if [ "$dump" = 1 ]; then printf '%s\n' "remotecommand none" "clearallforwardings no" "exitonforwardfailure no"; cat "$FAKE_BASE/ssh-G-$host" 2>/dev/null; exit 0; fi
printf '%s\t%s\n' "$host" "$*" >> "$FAKE_BASE/ssh.log"
[ -d "$FAKE_BASE/consumers/$host" ] || { echo "ssh: Could not resolve hostname $host" >&2; exit 255; }
grep -qxF "$host" "$FAKE_BASE/ssh-fail-config" 2>/dev/null && [[ "$*" == *" config "* ]] && exit 255
grep -qxF "$host" "$FAKE_BASE/ssh-fail-probe" 2>/dev/null && [[ "$*" == "ss -Hltn"* ]] && exit 255
HOME="$FAKE_BASE/consumers/$host" FAKE_SIDE="$host" exec sh -c "$*"
"""

# State lines: "is-active U", "is-enabled U", "restarts U N", "cgroup-old U" (its main process runs outside the unit's
# cgroup until a restart), "fail-start U" (enable --now fails). Every unit's main process is this test process.
FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
side=${FAKE_SIDE:-pc}
state="$FAKE_BASE/systemd-$side"
touch "$state"
printf '%s\t%s\n' "$side" "$*" >> "$FAKE_BASE/systemctl.log"
[ "$1" = --user ] && shift
verb=$1; shift
case "$verb" in
  is-active|is-enabled)
    [ "$1" = --quiet ] && shift
    grep -qxF "$verb $1" "$state" ;;
  enable|reenable)
    now=0; [ "$1" = --now ] && { now=1; shift; }
    grep -qxF "fail-start $1" "$state" && { echo "Job for $1 failed" >&2; exit 1; }
    echo "is-enabled $1" >> "$state"
    if [ "$now" = 1 ]; then
      echo "is-active $1" >> "$state"
      grep -qxF "no-record $1" "$state" || case "$1" in omp-auth-broker-tunnel@*.service)
        alias=${1#omp-auth-broker-tunnel@}; alias=${alias%.service}
        rec="$FAKE_BASE/consumers/$alias/.local/state/omp-auth-broker-tunnel.$(uname -n).$alias.pids"
        mkdir -p "${rec%/*}"
        { cat /proc/sys/kernel/random/boot_id; echo "$FAKE_LIVE_PID $(cut -d' ' -f22 /proc/$FAKE_LIVE_PID/stat)"; } > "$rec" ;;
      esac
    fi ;;
  restart) grep -vxF "cgroup-old $1" "$state" > "$state.new"; mv "$state.new" "$state"; echo "is-active $1" >> "$state" ;;
  stop)
    grep -qxF "is-active $1" "$state" || { echo "Unit $1 not loaded." >&2; exit 5; }
    grep -vxF "is-active $1" "$state" > "$state.new"; mv "$state.new" "$state" ;;
  show)
    prop=$2; unit=$4
    case "$prop" in
      NRestarts) n=$(grep "^restarts $unit " "$state" | tail -n 1 | cut -d' ' -f3); echo "${n:-0}" ;;
      MainPID) echo "$FAKE_LIVE_PID" ;;
      ControlGroup)
        if grep -qxF "cgroup-old $unit" "$state"; then echo "/user.slice/app.slice/$unit"
        else sed -n 's/^0:://p' "/proc/$FAKE_LIVE_PID/cgroup"; fi ;;
      ActiveState) if grep -qxF "is-active $unit" "$state"; then echo active; else echo inactive; fi ;;
    esac ;;
  daemon-reload|reset-failed) ;;
  disable) [ "$1" = --now ] && shift; grep -vxF -e "is-enabled $1" -e "is-active $1" "$state" > "$state.new"; mv "$state.new" "$state" ;;
  *) echo "fake systemctl: unexpected $verb" >&2; exit 1 ;;
esac
"""

# The PC's broker always listens; a consumer's port is held only when the test says so.
FAKE_SS = r"""#!/usr/bin/env bash
if [ "${FAKE_SIDE:-pc}" = pc ] || [ -e "$FAKE_BASE/held-$FAKE_SIDE" ]; then echo 'LISTEN 0 512 127.0.0.1:18765 0.0.0.0:*'; fi
"""

# One omp for both sides: the broker PC answers `auth-broker token`, a consumer keeps its config in config.yml.
FAKE_OMP = r"""#!/usr/bin/env bash
cfg="$HOME/.omp/agent/config.yml"
printf '%s\n' "$*" >> "$HOME/omp.log"
case "$1 $2" in
  "auth-broker token") echo "$FAKE_BEARER" ;;
  "auth-broker status") echo "OK http://127.0.0.1:18765 (version=test)" ;;
  "config path") echo "$HOME/.omp/agent" ;;
  "config get") python3 -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1])) or {}
for k in sys.argv[2].split("."): c = (c or {}).get(k)
print("(not set)" if c is None else c)' "$cfg" "$3" ;;
  *) echo "fake omp: unexpected $*" >&2; exit 1 ;;
esac
"""


def fake_omp() -> str:
    return FAKE_OMP


def write_exec(path: pathlib.Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Env:
    def __init__(self, base: pathlib.Path):
        self.base = base
        self.home = base / "pc"
        self.unit_dir = self.home / ".config/systemd/user"
        (self.unit_dir / "default.target.wants").mkdir(parents=True)
        bin_dir = base / "bin"
        write_exec(bin_dir / "ssh", FAKE_SSH)
        write_exec(bin_dir / "systemctl", FAKE_SYSTEMCTL)
        write_exec(bin_dir / "ss", FAKE_SS)
        # systemd-run --user --wait --pipe ... CMD: run CMD here, unless the test says the user manager cannot.
        write_exec(bin_dir / "systemd-run", '#!/usr/bin/env bash\n[ -e "$FAKE_BASE/manager-cannot-ssh" ] && exit 1\n'
                   'while [ "${1#-}" != "$1" ]; do shift; done\nshift\nexec ssh "$@"\n')
        # Both sides' python3 is the interpreter running pytest, the one known to have PyYAML.
        write_exec(bin_dir / "python3", f'#!/bin/sh\nexec {sys.executable} "$@"\n')
        # The script's waits are external `sleep` calls; skipping them keeps the retry paths fast.
        write_exec(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
        write_exec(self.home / ".local/bin/omp", fake_omp())
        self.path = f"{bin_dir}:{os.environ['PATH']}"
        # The broker is already up on the PC, started from the current unit file.
        self.pc_state("is-enabled omp-auth-broker.service", "is-active omp-auth-broker.service")

    def pc_state(self, *lines: str) -> None:
        with (self.base / "systemd-pc").open("a") as f:
            f.writelines(line + "\n" for line in lines)

    def consumer(self, alias: str, text: str | None = None) -> pathlib.Path:
        home = self.base / "consumers" / alias
        write_exec(home / ".local/bin/omp", fake_omp())
        (home / ".omp/agent").mkdir(parents=True)
        (home / ".omp/agent/config.yml").write_text(text if text is not None else "update:\n  auto: true\n")
        return home

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(self.home), "PATH": self.path, "FAKE_BASE": str(self.base),
               "FAKE_BEARER": FAKE_BEARER, "FAKE_LIVE_PID": str(os.getpid())}
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=180)

    def systemctl_calls(self) -> list[str]:
        log = self.base / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []

    def ssh_log(self) -> str:
        log = self.base / "ssh.log"
        return log.read_text() if log.exists() else ""


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def config(home: pathlib.Path) -> dict:
    return yaml.safe_load((home / ".omp/agent/config.yml").read_text())


def changes(calls: list[str]) -> list[str]:
    return [c for c in calls if any(v in c for v in (" enable", " reenable", " disable", " stop", " restart"))]


def test_configures_consumer_and_never_puts_bearer_in_a_command_line(env):
    clvm = env.consumer("clvm")
    result = env.run("clvm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok clvm" in result.stdout
    assert config(clvm)["auth"]["broker"] == {"url": URL, "token": FAKE_BEARER}
    assert config(clvm)["update"] == {"auto": True}
    assert stat.S_IMODE((clvm / ".omp/agent/config.yml").stat().st_mode) == 0o600
    assert "pc\t--user enable --now omp-auth-broker-tunnel@clvm.service" in env.systemctl_calls()
    for log in (env.ssh_log(), (clvm / "omp.log").read_text(), result.stdout, result.stderr):
        assert FAKE_BEARER not in log
    assert (env.unit_dir / "omp-auth-broker-tunnel@.service").resolve() == SRC_DIR / "omp-auth-broker-tunnel@.service"


def test_token_write_changes_only_the_token_line(env):
    original = ("# hand-written note\nauth:\n  # broker on the PC\n  broker:\n    url: http://127.0.0.1:18765\n"
                "    token: old\nthinking:\n  level: off\n  quiet: 22:00\n")
    clvm = env.consumer("clvm", original)
    result = env.run("clvm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clvm / ".omp/agent/config.yml").read_text() == original.replace("token: old", f'token: "{FAKE_BEARER}"')


def test_token_line_is_inserted_after_url_when_missing(env):
    original = "auth:\n  broker:\n    url: http://127.0.0.1:18765\nupdate:\n  auto: true\n"
    clvm = env.consumer("clvm", original)
    assert env.run("clvm").returncode == 0
    assert (clvm / ".omp/agent/config.yml").read_text() == original.replace(
        "18765\n", f'18765\n    token: "{FAKE_BEARER}"\n')


def test_second_run_changes_nothing_even_after_earlier_restarts(env):
    clvm = env.consumer("clvm")
    assert env.run("clvm").returncode == 0
    # The tunnel restarted three times since (network drops); that alone is no failure.
    env.pc_state("restarts omp-auth-broker-tunnel@clvm.service 3")
    before_ssh, before_calls = env.ssh_log(), len(env.systemctl_calls())
    result = env.run("clvm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok clvm" in result.stdout
    assert changes(env.systemctl_calls()[before_calls:]) == []
    new_ssh = env.ssh_log()[len(before_ssh):]
    assert "tempfile" not in new_ssh
    assert "relinking" not in result.stdout


def test_broker_from_an_older_unit_file_is_restarted(env):
    env.consumer("clvm")
    env.pc_state("cgroup-old omp-auth-broker.service")
    assert env.run("clvm").returncode == 0
    assert "pc\t--user restart omp-auth-broker.service" in env.systemctl_calls()
    # Once restarted into its own cgroup, a rerun leaves it alone.
    before = len(env.systemctl_calls())
    assert env.run("clvm").returncode == 0
    assert changes(env.systemctl_calls()[before:]) == []


@pytest.mark.parametrize("args", [["--dry-run", "clvm"], ["clvm", "--dry-run"]])
def test_dry_run_changes_nothing_in_any_argument_order(env, args):
    clvm = env.consumer("clvm")
    result = env.run(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would send the broker token to clvm" in result.stdout
    assert changes(env.systemctl_calls()) == []
    assert not [c for c in env.systemctl_calls() if "daemon-reload" in c]
    assert not list(env.unit_dir.glob("omp-auth-broker*"))
    assert "auth" not in config(clvm)


@pytest.mark.parametrize("args, code", [(["--help"], 0), (["-x"], 2), (["clvm", "--force"], 2)])
def test_options_other_than_dry_run_change_nothing(env, args, code):
    env.consumer("clvm")
    result = env.run(*args)
    assert result.returncode == code
    assert "usage:" in (result.stdout + result.stderr)
    assert env.systemctl_calls() == [] and env.ssh_log() == ""


@pytest.mark.parametrize("running", [False, True])
def test_retires_old_unit_whose_link_points_at_nothing(env, running):
    clvm = env.consumer("clvm")
    old_record = clvm / f".local/state/clvm-auth-broker-tunnel.{os.uname().nodename}.pids"
    old_record.parent.mkdir(parents=True)
    old_record.write_text("00000000-0000-0000-0000-000000000000\n1 1\n")
    gone = env.base / "old-checkout/clvm-auth-broker-tunnel.service"
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(gone)
    (env.unit_dir / "default.target.wants/clvm-auth-broker-tunnel.service").symlink_to(gone)
    if running:
        env.pc_state("is-active clvm-auth-broker-tunnel.service")
    result = env.run("clvm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("pc\t--user stop clvm-auth-broker-tunnel.service" in env.systemctl_calls()) == running
    assert not os.path.lexists(env.unit_dir / "clvm-auth-broker-tunnel.service")
    assert not os.path.lexists(env.unit_dir / "default.target.wants/clvm-auth-broker-tunnel.service")
    assert not old_record.exists()


def test_old_unit_stays_until_clvm_itself_is_set_up(env):
    env.consumer("home")
    gone = env.base / "old-checkout/clvm-auth-broker-tunnel.service"
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(gone)
    env.pc_state("is-active clvm-auth-broker-tunnel.service")
    assert env.run("home").returncode == 0
    assert not [c for c in env.systemctl_calls() if "clvm-auth-broker-tunnel" in c and " stop" in c]
    assert os.path.lexists(env.unit_dir / "clvm-auth-broker-tunnel.service")


def test_failures_skip_only_their_host(env):
    env.consumer("clvm")
    env.consumer("held")
    env.consumer("nostart")
    home = env.consumer("home")
    (env.base / "ssh-fail-config").write_text("clvm\n")
    (env.base / "held-held").touch()
    env.pc_state("fail-start omp-auth-broker-tunnel@nostart.service")
    result = env.run("clvm", "nosuchalias", "held", "nostart", "home")
    assert result.returncode == 1
    assert "skip clvm: omp config path printed no absolute directory (its config was left unchanged)" in result.stdout
    assert "skip nosuchalias: ssh nosuchalias fails" in result.stdout
    assert "skip held: 127.0.0.1:18765 there is held by something other than this PC's tunnel" in result.stdout
    assert "skip nostart: starting omp-auth-broker-tunnel@nostart.service failed" in result.stdout
    # It was not enabled before this run, so it is not left enabled to retry forever.
    assert "pc\t--user disable --now omp-auth-broker-tunnel@nostart.service" in env.systemctl_calls()
    assert "ok home" in result.stdout
    assert config(home)["auth"]["broker"]["token"] == FAKE_BEARER
    assert "auth" not in config(env.base / "consumers/held")


def test_ok_needs_the_instances_own_session(env):
    clvm = env.consumer("clvm")
    assert env.run("clvm").returncode == 0
    # Another session holds the port now: the record names a process that no longer exists.
    rec = next((clvm / ".local/state").glob("omp-auth-broker-tunnel.*.clvm.pids"))
    boot_id = rec.read_text().splitlines()[0]
    rec.write_text(f"{boot_id}\n999999999 1\n")
    result = env.run("clvm")
    assert result.returncode == 1
    assert "FAIL clvm: omp-auth-broker-tunnel@clvm.service holds no session there" in result.stdout


def test_ok_rejects_a_truncated_record(env):
    clvm = env.consumer("clvm")
    assert env.run("clvm").returncode == 0
    rec = next((clvm / ".local/state").glob("omp-auth-broker-tunnel.*.clvm.pids"))
    rec.write_text(rec.read_text().splitlines()[0] + "\n")
    result = env.run("clvm")
    assert result.returncode == 1
    assert "FAIL clvm: omp-auth-broker-tunnel@clvm.service holds no session there" in result.stdout


@pytest.mark.parametrize("unit_state", ["is-active", "is-enabled"])
def test_consumer_with_its_own_broker_is_skipped(env, unit_state):
    env.consumer("home")
    (env.base / "systemd-home").write_text(f"{unit_state} omp-auth-broker.service\n")
    result = env.run("home")
    assert result.returncode == 1
    assert "skip home: omp-auth-broker.service or clvm-auth-broker-tunnel.service is active or enabled there" in result.stdout
    assert not [c for c in env.systemctl_calls() if "tunnel@home" in c and " enable" in c]


def test_old_link_does_not_hide_a_foreign_holder_on_clvm(env):
    clvm = env.consumer("clvm")
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(env.base / "old-checkout/clvm-auth-broker-tunnel.service")
    (env.base / "held-clvm").touch()
    result = env.run("clvm")
    assert result.returncode == 1
    assert "skip clvm: 127.0.0.1:18765 there is held by something other than this PC's tunnel" in result.stdout
    assert "auth" not in config(clvm)
    # Nothing replaced the old unit, so it is left as it was.
    assert not changes([c for c in env.systemctl_calls() if "clvm-auth-broker-tunnel" in c])
    assert os.path.lexists(env.unit_dir / "clvm-auth-broker-tunnel.service")


def test_old_session_proven_by_its_record_is_replaced(env):
    clvm = env.consumer("clvm")
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(env.base / "old-checkout/clvm-auth-broker-tunnel.service")
    env.pc_state("is-active clvm-auth-broker-tunnel.service")
    (env.base / "held-clvm").touch()
    # The old unit's record names a live session (a sleep standing in for its sshd-session).
    holder = subprocess.Popen(["/bin/sleep", "60"])
    try:
        start = pathlib.Path(f"/proc/{holder.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        rec = clvm / f".local/state/clvm-auth-broker-tunnel.{os.uname().nodename}.pids"
        rec.parent.mkdir(parents=True)
        rec.write_text(f"{pathlib.Path('/proc/sys/kernel/random/boot_id').read_text()}{holder.pid} {start}\n")
        # Retiring ends that session, which frees the port.
        result = env.run("clvm")
        holder.wait(timeout=10)
    finally:
        holder.kill()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok clvm" in result.stdout
    assert holder.returncode == -15
    assert not rec.exists()
    assert config(clvm)["auth"]["broker"]["token"] == FAKE_BEARER


@pytest.mark.parametrize("text, message", [
    ("auth:\n  broker:\n    token: >-\n      folded\n    url: http://127.0.0.1:18765\n", "auth.broker.token in"),
    ("auth:\n  broker: {url: 'http://127.0.0.1:18765'}\n", "auth.broker in"),
    ("auth:\n  broker:\n    url:\n      http://127.0.0.1:18765\n", "auth.broker.url in"),
    ("{}\n", "the top level in"),
    ("{update: {auto: true}}\n", "the top level in"),
    ("  update:\n    auto: true\n", "the top level in"),
])
def test_token_write_refuses_what_it_cannot_edit_line_by_line(env, text, message):
    clvm = env.consumer("clvm", text)
    result = env.run("clvm")
    assert result.returncode == 1
    assert "skip clvm: writing auth.broker there failed (its config was left unchanged)" in result.stdout
    assert message in result.stderr and "cannot be edited line by line" in result.stderr
    assert (clvm / ".omp/agent/config.yml").read_text() == text


def test_dry_run_reports_a_held_port_like_the_real_run(env):
    env.consumer("clvm")
    (env.base / "held-clvm").touch()
    result = env.run("--dry-run", "clvm")
    assert result.returncode == 1
    assert "skip clvm: 127.0.0.1:18765 there is held by something other than this PC's tunnel" in result.stdout
    assert "would send" not in result.stdout


def test_config_is_untouched_when_the_tunnel_never_holds_the_port(env):
    clvm = env.consumer("clvm")
    # The instance starts but its forward is refused, so no record is ever written.
    env.pc_state("is-enabled omp-auth-broker-tunnel@clvm.service", "is-active omp-auth-broker-tunnel@clvm.service")
    result = env.run("clvm")
    assert result.returncode == 1
    assert "holds no session there" in result.stdout and "its config was left unchanged" in result.stdout
    assert "auth" not in config(clvm)


def test_broker_running_outside_its_cgroup_is_restarted_even_when_not_enabled(env):
    env.consumer("clvm")
    (env.base / "systemd-pc").write_text("is-active omp-auth-broker.service\ncgroup-old omp-auth-broker.service\n")
    assert env.run("clvm").returncode == 0
    calls = env.systemctl_calls()
    assert "pc\t--user restart omp-auth-broker.service" in calls
    assert "pc\t--user enable --now omp-auth-broker.service" in calls


def test_retiring_resets_the_old_units_failed_state(env):
    env.consumer("clvm")
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(env.base / "old-checkout/clvm-auth-broker-tunnel.service")
    assert env.run("clvm").returncode == 0
    assert "pc\t--user reset-failed clvm-auth-broker-tunnel.service" in env.systemctl_calls()


def test_blocks_are_added_with_the_files_own_indent(env):
    original = "auth:\n    other: 1\nupdate:\n    auto: true\n"
    clvm = env.consumer("clvm", original)
    assert env.run("clvm").returncode == 0
    assert (clvm / ".omp/agent/config.yml").read_text() == (
        f'auth:\n    other: 1\n    broker:\n      url: "{URL}"\n      token: "{FAKE_BEARER}"\nupdate:\n    auto: true\n')


def test_config_is_made_private_on_every_run(env):
    clvm = env.consumer("clvm")
    assert env.run("clvm").returncode == 0
    cfg = clvm / ".omp/agent/config.yml"
    cfg.chmod(0o644)
    assert env.run("clvm").returncode == 0
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_unreachable_probe_counts_as_held(env):
    clvm = env.consumer("clvm")
    (env.base / "ssh-fail-probe").write_text("clvm\n")
    result = env.run("clvm")
    assert result.returncode == 1
    assert "skip clvm: 127.0.0.1:18765 there is held by something other than this PC's tunnel" in result.stdout
    assert "auth" not in config(clvm)


def test_links_into_another_checkout_are_moved_here(env):
    env.consumer("clvm")
    elsewhere = env.base / "other-checkout"
    elsewhere.mkdir()
    for name in ("omp-auth-broker.service", "omp-auth-broker-tunnel@.service"):
        (elsewhere / name).write_text("[Unit]\n")
        (env.unit_dir / name).symlink_to(elsewhere / name)
    (env.unit_dir / "default.target.wants/omp-auth-broker-tunnel@clvm.service").symlink_to(elsewhere / "omp-auth-broker-tunnel@.service")
    env.pc_state("is-enabled omp-auth-broker-tunnel@clvm.service")
    result = env.run("clvm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"relinking omp-auth-broker.service (was {elsewhere / 'omp-auth-broker.service'})" in result.stdout
    assert (env.unit_dir / "omp-auth-broker.service").resolve() == SRC_DIR / "omp-auth-broker.service"
    assert "pc\t--user enable omp-auth-broker-tunnel@clvm.service" in env.systemctl_calls()
    assert (env.unit_dir / "omp-auth-broker-tunnel@.service").resolve() == SRC_DIR / "omp-auth-broker-tunnel@.service"


def test_regular_unit_file_is_refused(env):
    env.consumer("clvm")
    (env.unit_dir / "omp-auth-broker.service").write_text("[Unit]\n")
    result = env.run("clvm")
    assert result.returncode == 2
    assert "is a regular file, not the profile's link" in result.stderr
    assert env.ssh_log() == ""


@pytest.mark.parametrize("alias_line", ["remoteforward 18765 [127.0.0.1]:18765", "remotecommand tmux attach"])
def test_alias_with_its_own_forward_is_skipped_before_anything_changes(env, alias_line):
    clvm = env.consumer("clvm")
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(env.base / "old-checkout/clvm-auth-broker-tunnel.service")
    env.pc_state("is-active clvm-auth-broker-tunnel.service")
    (env.base / "ssh-G-clvm").write_text(f"hostname 192.0.2.1\n{alias_line}\n")
    result = env.run("clvm")
    assert result.returncode == 1
    assert "skip clvm: the ssh alias clvm carries a forward or RemoteCommand" in result.stdout
    assert not changes([c for c in env.systemctl_calls() if "clvm" in c])
    assert "auth" not in config(clvm)


def test_old_unit_without_a_record_gets_a_precise_skip(env):
    env.consumer("clvm")
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(env.base / "old-checkout/clvm-auth-broker-tunnel.service")
    env.pc_state("is-active clvm-auth-broker-tunnel.service")
    (env.base / "held-clvm").touch()
    result = env.run("clvm")
    assert result.returncode == 1
    assert "this PC's clvm-auth-broker-tunnel.service runs without a record proving it is the holder" in result.stdout


def test_old_unit_stays_when_the_user_manager_cannot_reach_clvm(env):
    env.consumer("clvm")
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(env.base / "old-checkout/clvm-auth-broker-tunnel.service")
    env.pc_state("is-active clvm-auth-broker-tunnel.service")
    (env.base / "manager-cannot-ssh").touch()
    result = env.run("clvm")
    assert result.returncode == 1
    assert "skip clvm: the systemd user manager cannot ssh to clvm" in result.stdout
    assert not changes([c for c in env.systemctl_calls() if "clvm" in c])
    assert os.path.lexists(env.unit_dir / "clvm-auth-broker-tunnel.service")


def test_edit_that_would_not_read_back_is_refused(env):
    # Two auth: keys: the line editor would change the first, a YAML loader keeps the last.
    text = "auth:\n  broker:\n    url: x\nauth:\n  other: 1\n"
    clvm = env.consumer("clvm", text)
    result = env.run("clvm")
    assert result.returncode == 1
    assert "the result in" in result.stderr
    assert (clvm / ".omp/agent/config.yml").read_text() == text


def test_alias_that_clears_forwardings_still_gets_its_tunnel(env):
    env.consumer("clvm")
    (env.base / "ssh-G-clvm").write_text("hostname 192.0.2.1\nclearallforwardings yes\n")
    result = env.run("clvm")
    assert result.returncode == 0, result.stdout + result.stderr
    unit = (SRC_DIR / "omp-auth-broker-tunnel@.service").read_text()
    assert "-o ExitOnForwardFailure=yes -o ClearAllForwardings=no" in unit


def test_config_linked_out_of_its_directory_is_refused(env):
    clvm = env.consumer("clvm")
    cfg = clvm / ".omp/agent/config.yml"
    tracked = clvm / "dotfiles/omp-config.yml"
    tracked.parent.mkdir()
    tracked.write_text(cfg.read_text())
    cfg.unlink()
    cfg.symlink_to(tracked)
    result = env.run("clvm")
    assert result.returncode == 1
    assert "outside its directory" in result.stderr
    assert "auth" not in tracked.read_text()


def test_insert_keeps_a_comment_with_the_key_it_leads(env):
    original = "auth:\n  other: 1\n\n# update channel\nupdate:\n  auto: true\n"
    clvm = env.consumer("clvm", original)
    assert env.run("clvm").returncode == 0
    assert (clvm / ".omp/agent/config.yml").read_text() == (
        f'auth:\n  other: 1\n  broker:\n    url: "{URL}"\n    token: "{FAKE_BEARER}"\n\n# update channel\nupdate:\n  auto: true\n')


def test_dry_run_leaves_the_old_clvm_tunnel_alone(env):
    clvm = env.consumer("clvm")
    links = [env.unit_dir / "clvm-auth-broker-tunnel.service", env.unit_dir / "default.target.wants/clvm-auth-broker-tunnel.service"]
    for link in links:
        link.symlink_to(env.base / "old-checkout/clvm-auth-broker-tunnel.service")
    env.pc_state("is-active clvm-auth-broker-tunnel.service")
    holder = subprocess.Popen(["/bin/sleep", "60"])
    try:
        start = pathlib.Path(f"/proc/{holder.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        rec = clvm / f".local/state/clvm-auth-broker-tunnel.{os.uname().nodename}.pids"
        rec.parent.mkdir(parents=True)
        rec.write_text(f"{pathlib.Path('/proc/sys/kernel/random/boot_id').read_text()}{holder.pid} {start}\n")
        result = env.run("--dry-run", "clvm")
        assert result.returncode == 0, result.stdout + result.stderr
        assert holder.poll() is None
    finally:
        holder.kill()
    assert rec.exists() and all(os.path.lexists(link) for link in links)
    assert changes(env.systemctl_calls()) == []


def test_stale_broker_wants_link_is_replaced_and_its_unit_link_kept(env):
    env.consumer("clvm")
    gone = env.base / "old-shared/omp-auth-broker.service"
    (env.unit_dir / "default.target.wants/omp-auth-broker.service").symlink_to(gone)
    result = env.run("clvm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pc\t--user enable omp-auth-broker.service" in env.systemctl_calls()
    assert not any(" reenable " in c for c in env.systemctl_calls())
    assert (env.unit_dir / "omp-auth-broker.service").resolve() == SRC_DIR / "omp-auth-broker.service"


def test_new_instance_without_a_session_is_disabled_again(env):
    clvm = env.consumer("clvm")
    # The forward is refused, so no record ever appears: model it by deleting the record the fake writes.
    env.pc_state("no-record omp-auth-broker-tunnel@clvm.service")
    result = env.run("clvm")
    assert result.returncode == 1
    assert "pc\t--user disable --now omp-auth-broker-tunnel@clvm.service" in env.systemctl_calls()
    assert "auth" not in config(clvm)


def test_no_alias_serves_the_enabled_instances_and_retries_clvm_while_the_old_unit_serves_it(env):
    """A rerun after a token rotation reaches every current consumer: the enabled instances, plus clvm while the old
    unit still holds it, and never a consumer whose instance was disabled."""
    env.consumer("clvm")
    env.consumer("home")
    env.consumer("bread")
    gone = env.base / "gone.service"
    (env.unit_dir / "default.target.wants/omp-auth-broker-tunnel@home.service").symlink_to(gone)
    (env.unit_dir / "clvm-auth-broker-tunnel.service").symlink_to(gone)
    result = env.run("--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "home" in result.stdout and "clvm" in result.stdout
    assert "bread" not in result.stdout


def test_no_alias_and_nothing_enabled_is_an_error_that_names_the_fix(env):
    result = env.run()
    assert result.returncode == 2
    assert "no tunnel instance is enabled yet" in result.stderr
