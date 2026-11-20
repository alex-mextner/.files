"""tg-ctl-clvm-reverse-ssh.service: the remote scripts that record the link's own box session and free it on restart.

The two scripts run on the clvm box through ssh. Here they run locally, after the same substitutions systemd does
(%H -> hostname, ${BOX_PORT} / ${RECORD} -> the unit's Environment= values, $$ -> $), against a temporary HOME, with
BOX_PORT pointed at a test port. A small Python process stands in for the box's sshd-session: like the real one it
holds a listening port (the forward) and runs the ExecStart script as its child, so the recorded $PPID is a process
this test owns and killing it is what frees the port.
"""
import contextlib
import os
import pathlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

BOOT_ID_FILE = pathlib.Path("/proc/sys/kernel/random/boot_id")
pytestmark = pytest.mark.skipif(not BOOT_ID_FILE.exists() or shutil.which("ss") is None or shutil.which("tail") is None,
                                reason="needs Linux /proc, iproute2 ss and tail")

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNIT = ROOT / "ubuntu/files/.config/systemd/user/tg-ctl-clvm-reverse-ssh.service"
HOSTNAME = "testhost"
TERMINATED = "terminated recorded stale tunnel process"

# Listens on an ephemeral port (the stand-in for the -R forward), prints it, then runs the ExecStart script as its
# child without letting it inherit the socket, as sshd does, and idles until killed.
SSHD_STAND_IN = """
import socket, subprocess, sys, time
s = socket.socket()
s.bind(("127.0.0.1", 0))
s.listen()
print(s.getsockname()[1], flush=True)
subprocess.Popen(["bash", "-c", sys.argv[1]], stdout=subprocess.DEVNULL)
while True:
    time.sleep(3600)
"""

# A listener this unit never recorded (another user's forward, or a session whose record was never written).
HOLDER = """
import socket, time
s = socket.socket()
s.bind(("127.0.0.1", 0))
s.listen()
print(s.getsockname()[1], flush=True)
while True:
    time.sleep(3600)
"""


def unit_lines(key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in UNIT.read_text().splitlines() if line.startswith(f"{key}=")]


def unit_env() -> dict[str, str]:
    return dict(value.replace("%H", HOSTNAME).split("=", 1) for value in unit_lines("Environment"))


def unit_argv(key: str, port: int | None = None) -> list[str]:
    env = unit_env()
    if port is not None:
        env["BOX_PORT"] = str(port)
    (line,) = unit_lines(key)
    line = re.sub(r"\$\{(\w+)\}", lambda m: env[m.group(1)], line.replace("%H", HOSTNAME)).replace("$$", "$")
    argv = shlex.split(line)
    assert argv[0] == "/usr/bin/ssh" and argv[-2] == "ultra@clvm"
    return argv


def remote_script(key: str, port: int | None = None) -> str:
    return unit_argv(key, port)[-1]


def stat_fields(pid: int) -> list[str]:
    """Fields of /proc/<pid>/stat from field 3 (state) on; the process name may contain spaces or parentheses."""
    return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def starttime(pid: int) -> str:
    return stat_fields(pid)[19]


def alive(pid: int) -> bool:
    try:
        return stat_fields(pid)[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def wait_until(predicate, seconds: float, message: str) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.05)


def listening(port: int) -> bool:
    return bool(subprocess.run(["ss", "-Hltn", f"sport = :{port}"], capture_output=True, text=True).stdout.strip())


def kill_if_same(pid: int, start: str) -> None:
    """SIGKILL pid only while it is still the process that had this starttime (never a recycled pid)."""
    with contextlib.suppress(FileNotFoundError, ProcessLookupError):
        if starttime(pid) == start:
            os.kill(pid, signal.SIGKILL)


def children(pid: int) -> list[int]:
    found = []
    for proc in pathlib.Path("/proc").glob("[0-9]*"):
        with contextlib.suppress(FileNotFoundError, ProcessLookupError, IndexError):
            if int(stat_fields(int(proc.name))[1]) == pid:
                found.append(int(proc.name))
    return found


@pytest.fixture(scope="module")
def free_port():
    # Bound but never listening, so `ss -l` never shows it.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        yield reserved.getsockname()[1]


@pytest.fixture
def pids_file(tmp_path):
    return tmp_path / f".local/state/tg-ctl-clvm-reverse-ssh.{HOSTNAME}.pids"


def run_pre(home: pathlib.Path, port: int) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", remote_script("ExecStartPre", port)], env={**os.environ, "HOME": str(home)},
                          capture_output=True, text=True, timeout=30)


@contextlib.contextmanager
def sshd_stand_in(home: pathlib.Path):
    """Yield (Popen, forwarded port) for an sshd-session stand-in running the ExecStart script; killed on exit."""
    sshd = subprocess.Popen([sys.executable, "-c", SSHD_STAND_IN, remote_script("ExecStart")],
                            env={**os.environ, "HOME": str(home)}, stdout=subprocess.PIPE, text=True)
    try:
        yield sshd, int(sshd.stdout.readline())
    finally:
        sshd.kill()
        sshd.wait(timeout=10)
        sshd.stdout.close()


def idle_process(sshd_pid: int) -> int:
    """The ExecStart script's process, once it has exec'd its idle tail."""
    found = []

    def exec_d_tail() -> bool:
        found[:] = [pid for pid in children(sshd_pid)
                    if pathlib.Path(f"/proc/{pid}/comm").read_text().strip() == "tail"]
        return bool(found)

    wait_until(exec_d_tail, 10, "the ExecStart script never reached its idle step")
    return found[0]


@contextlib.contextmanager
def session(home: pathlib.Path, pids_file: pathlib.Path):
    """Yield (sshd stand-in Popen, forwarded port, idle process pid) once the record is written."""
    with sshd_stand_in(home) as (sshd, port):
        wait_until(lambda: pids_file.exists() and len(pids_file.read_text().splitlines()) == 2, 10,
                   "ExecStart script never wrote its pids file")
        idle = idle_process(sshd.pid)
        idle_start = starttime(idle)
        try:
            yield sshd, port, idle
        finally:
            kill_if_same(idle, idle_start)


@pytest.mark.parametrize("key", ["ExecStartPre", "ExecStart"])
def test_unit_line_has_no_systemd_expansions_besides_escaped_dollar_env_and_hostname(key):
    # systemd would silently replace any other $NAME / ${NAME} or %x specifier, and unescape a backslash even
    # inside quotes, before ssh ever sees the script.
    (raw,) = unit_lines(key)
    assert "$" not in re.sub(r"\$\{(BOX_PORT|RECORD)\}", "", raw).replace("$$", "")
    assert "%" not in raw.replace("%H", "")
    assert "\\" not in raw


def test_port_and_record_are_written_once():
    assert unit_env() == {"BOX_PORT": "18766", "RECORD": f".local/state/tg-ctl-clvm-reverse-ssh.{HOSTNAME}.pids"}
    for key in ("ExecStartPre", "ExecStart"):
        (raw,) = unit_lines(key)
        assert "18766" not in raw and "tg-ctl-clvm-reverse-ssh" not in raw
    start = unit_argv("ExecStart")
    assert start[start.index("-R") + 1] == "127.0.0.1:18766:127.0.0.1:22"
    assert "ExitOnForwardFailure=yes" in start and "StrictHostKeyChecking=yes" in start and "-N" not in start
    assert "StrictHostKeyChecking=yes" in unit_argv("ExecStartPre")
    # The pre-step frees and waits on the very port ExecStart forwards.
    assert remote_script("ExecStartPre").count("sport = :18766") == 2


def test_start_records_only_its_session_with_starttime(tmp_path, pids_file):
    with session(tmp_path, pids_file) as (sshd, _, idle):
        boot_id, sshd_line = pids_file.read_text().splitlines()
        assert boot_id == BOOT_ID_FILE.read_text().strip()
        sshd_pid, sshd_start = sshd_line.split()
        assert int(sshd_pid) == sshd.pid and sshd_start == starttime(sshd.pid)
        # The idle process is tail itself (exec), forks nothing, and is not recorded.
        assert int(stat_fields(idle)[1]) == sshd.pid
        assert children(idle) == []


def test_idle_process_exits_once_its_session_is_gone(tmp_path, pids_file):
    # A clean disconnect ends sshd-session but sends the terminal-less command no signal.
    with session(tmp_path, pids_file) as (sshd, _, idle):
        sshd.terminate()
        sshd.wait(timeout=10)
        wait_until(lambda: not alive(idle), 5, "idle process outlived its session")


def test_pre_frees_the_port_held_by_the_recorded_session(tmp_path, pids_file):
    with session(tmp_path, pids_file) as (sshd, port, idle):
        assert listening(port)
        started = time.monotonic()
        result = run_pre(tmp_path, port)
        elapsed = time.monotonic() - started
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [f"{TERMINATED} {sshd.pid}"]
        assert result.stderr == ""
        # The wait loop stops as soon as the listener is gone instead of always sleeping its full 5 s.
        assert elapsed < 4
        assert not listening(port)
        sshd.wait(timeout=10)
        # Never killed by the pre-step, the idle process follows its session on its own.
        wait_until(lambda: not alive(idle), 5, "idle process outlived its terminated session")
        assert not pids_file.exists()


def test_plain_restart_logs_no_kill(tmp_path, pids_file):
    # A clean stop ends the session; the next pre-step runs before the idle process has noticed (it polls once a
    # second). Only the session is recorded, so nothing is killed and nothing is logged.
    with session(tmp_path, pids_file) as (sshd, port, idle):
        sshd.terminate()
        sshd.wait(timeout=10)
        result = run_pre(tmp_path, port)
        assert result.returncode == 0
        assert result.stdout == "" and result.stderr == ""
        assert not pids_file.exists()
        wait_until(lambda: not alive(idle), 5, "idle process outlived its session")


def test_pre_warns_and_keeps_record_when_port_stays_held(tmp_path, pids_file):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    with held, session(tmp_path, pids_file):
        started = time.monotonic()
        result = run_pre(tmp_path, held.getsockname()[1])
        elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert "still in use 5 s after terminating the recorded session" in result.stderr
    assert 4 < elapsed < 15
    # The record stays so the next attempt retries instead of losing track of the holder.
    assert pids_file.exists()


def test_pre_spares_reused_pid_garbage_and_other_hosts_record(tmp_path, pids_file, free_port):
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        pids_file.parent.mkdir(parents=True)
        boot_id = BOOT_ID_FILE.read_text()
        other = pids_file.with_name("tg-ctl-clvm-reverse-ssh.otherhost.pids")
        other.write_text(f"{boot_id}{bystander.pid} {starttime(bystander.pid)}\n")
        for record in (f"{bystander.pid} {int(starttime(bystander.pid)) + 1}\n", f"{bystander.pid}\n",
                       "* 1\n", "-1 1\n", "\n"):
            pids_file.write_text(boot_id + record)
            result = run_pre(tmp_path, free_port)
            assert result.returncode == 0 and result.stdout == "", (record, result.stderr)
            assert alive(bystander.pid), record
            assert not pids_file.exists()
        assert other.exists()
    finally:
        bystander.kill()
        bystander.wait()


def test_pre_ignores_record_from_previous_boot(tmp_path, pids_file, free_port):
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        pids_file.parent.mkdir(parents=True)
        pids_file.write_text(f"00000000-0000-0000-0000-000000000000\n{bystander.pid} {starttime(bystander.pid)}\n")
        result = run_pre(tmp_path, free_port)
        assert result.returncode == 0 and result.stdout == "", result.stderr
        assert alive(bystander.pid)
        assert not pids_file.exists()
    finally:
        bystander.kill()
        bystander.wait()


def test_pre_succeeds_without_record(tmp_path, pids_file, free_port):
    result = run_pre(tmp_path, free_port)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    pids_file.parent.mkdir(parents=True)
    pids_file.write_text(f"{BOOT_ID_FILE.read_text()}999999999 1\n")
    assert run_pre(tmp_path, free_port).returncode == 0
    assert not pids_file.exists()


def test_pre_reports_holder_it_never_recorded(tmp_path):
    # No record (full disk, or a drop before the record was written): the holder is not killed, but the journal says
    # why the following forward will fail instead of showing only ssh's generic "remote port forwarding failed".
    holder = subprocess.Popen([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE, text=True)
    try:
        port = int(holder.stdout.readline())
        started = time.monotonic()
        result = run_pre(tmp_path, port)
        elapsed = time.monotonic() - started
        assert result.returncode == 0 and result.stdout == ""
        assert "held by a session this unit never recorded" in result.stderr
        assert elapsed < 4
        assert alive(holder.pid) and listening(port)
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()


def test_start_still_idles_when_record_cannot_be_written(tmp_path):
    # A box with ~/.local/state unwritable (full disk, quota) must keep the link up, just unrecorded.
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local/state").write_text("not a directory")
    with sshd_stand_in(tmp_path) as (sshd, _):
        idle = idle_process(sshd.pid)
        time.sleep(1)
        assert alive(idle)
    # The orphaned idle process notices its session is gone and exits on its own.
    wait_until(lambda: not alive(idle), 5, "idle process outlived its session")
