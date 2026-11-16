"""clvm-auth-broker-tunnel.service: the remote scripts that record the tunnel's own box session and free it on restart.

The two scripts run on the clvm box through ssh. Here they run locally, after the same unescaping systemd does
($$ -> $, %H -> hostname), against a temporary HOME. A small Python process stands in for the box's sshd-session:
like the real one it holds a listening port (the forward) and runs the ExecStart script as its child, so the
recorded $PPID is a process this test owns and killing it is what frees the port.
"""
import contextlib
import os
import pathlib
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
UNIT = ROOT / "ubuntu/files/.config/systemd/user/clvm-auth-broker-tunnel.service"
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


def unit_line(key: str) -> str:
    return next(line for line in UNIT.read_text().splitlines() if line.startswith(f"{key}=")).split("=", 1)[1]


def unit_argv(key: str) -> list[str]:
    argv = shlex.split(unit_line(key).replace("%H", HOSTNAME).replace("$$", "$"))
    assert argv[0] == "/usr/bin/ssh" and argv[-2] == "ultra@65.21.40.51"
    return argv


def box_port() -> str:
    start = unit_argv("ExecStart")
    return start[start.index("-R") + 1].split(":")[1]


def remote_script(key: str, port: int | None = None) -> str:
    script = unit_argv(key)[-1]
    if key == "ExecStartPre":
        # The pre-step must wait on the very port ExecStart forwards.
        port_filter = f"sport = :{box_port()}"
        assert script.count(port_filter) == 2
        script = script.replace(port_filter, f"sport = :{port}")
    return script


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


@pytest.fixture(scope="module")
def free_port():
    # Bound but never listening, so `ss -l` never shows it.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        yield reserved.getsockname()[1]


@pytest.fixture
def pids_file(tmp_path):
    return tmp_path / f".local/state/clvm-auth-broker-tunnel.{HOSTNAME}.pids"


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


@contextlib.contextmanager
def session(home: pathlib.Path, pids_file: pathlib.Path):
    """Yield (sshd stand-in Popen, forwarded port, idle shell pid) once the record is written."""
    with sshd_stand_in(home) as (sshd, port):
        shell = None
        try:
            wait_until(lambda: pids_file.exists() and len(pids_file.read_text().splitlines()) == 3, 10,
                       "ExecStart script never wrote its pids file")
            shell = pids_file.read_text().splitlines()[2].split()
            yield sshd, port, int(shell[0])
        finally:
            if shell is not None:
                kill_if_same(int(shell[0]), shell[1])


def children(pid: int) -> list[int]:
    found = []
    for proc in pathlib.Path("/proc").glob("[0-9]*"):
        with contextlib.suppress(FileNotFoundError, ProcessLookupError):
            if int(stat_fields(int(proc.name))[1]) == pid:
                found.append(int(proc.name))
    return found


@pytest.mark.parametrize("key", ["ExecStartPre", "ExecStart"])
def test_unit_line_has_no_systemd_expansions_besides_escaped_dollar_and_hostname(key):
    # systemd would silently replace a single $NAME / ${NAME} or a %x specifier, and unescape a backslash even
    # inside quotes, before ssh ever sees the script.
    raw = unit_line(key)
    assert "$" not in raw.replace("$$", "")
    assert "%" not in raw.replace("%H", "")
    assert "\\" not in raw


def test_start_records_session_and_idle_process_with_starttimes(tmp_path, pids_file):
    with session(tmp_path, pids_file) as (sshd, _, shell_pid):
        boot_id, sshd_line, shell_line = pids_file.read_text().splitlines()
        assert boot_id == BOOT_ID_FILE.read_text().strip()
        (sshd_pid, sshd_start), (recorded_shell, shell_start) = sshd_line.split(), shell_line.split()
        assert int(sshd_pid) == sshd.pid and sshd_start == starttime(sshd.pid)
        assert int(recorded_shell) == shell_pid and shell_start == starttime(shell_pid)
        assert int(stat_fields(shell_pid)[1]) == sshd.pid
        # exec keeps the recorded pid: the idle process is tail itself, which forks nothing.
        wait_until(lambda: pathlib.Path(f"/proc/{shell_pid}/comm").read_text().strip() == "tail", 5,
                   "the recorded shell never exec'd tail")
        assert children(shell_pid) == []


def test_idle_process_exits_once_its_session_is_gone(tmp_path, pids_file):
    # A clean disconnect ends sshd-session but sends the terminal-less command no signal.
    with session(tmp_path, pids_file) as (sshd, _, shell_pid):
        sshd.terminate()
        sshd.wait(timeout=10)
        wait_until(lambda: not alive(shell_pid), 5, "idle process outlived its session")


def test_pre_frees_the_port_held_by_the_recorded_session(tmp_path, pids_file):
    with session(tmp_path, pids_file) as (sshd, port, shell_pid):
        assert listening(port)
        started = time.monotonic()
        result = run_pre(tmp_path, port)
        elapsed = time.monotonic() - started
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [f"{TERMINATED} {sshd.pid}", f"{TERMINATED} {shell_pid}"]
        assert result.stderr == ""
        # The wait loop stops as soon as the listener is gone instead of always sleeping its full 5 s.
        assert elapsed < 4
        assert not listening(port)
        sshd.wait(timeout=10)
        wait_until(lambda: not alive(shell_pid), 10, "recorded idle process survived the pre-step")
        assert not pids_file.exists()


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
        pids_file.write_text(f"{BOOT_ID_FILE.read_text()}{bystander.pid} {int(starttime(bystander.pid)) + 1}\n* 1\n-1 1\n0\n\n")
        other = pids_file.with_name("clvm-auth-broker-tunnel.otherhost.pids")
        other.write_text(f"{BOOT_ID_FILE.read_text()}{bystander.pid} {starttime(bystander.pid)}\n")
        result = run_pre(tmp_path, free_port)
        assert result.returncode == 0 and result.stdout == "", result.stderr
        assert alive(bystander.pid)
        assert not pids_file.exists()
        assert other.exists()
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
    # No record (full disk, or a drop before the record was written): nothing is killed, but the journal says why the
    # following forward will fail instead of showing only ssh's generic "remote port forwarding failed".
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        started = time.monotonic()
        result = run_pre(tmp_path, held.getsockname()[1])
        elapsed = time.monotonic() - started
    assert result.returncode == 0 and result.stdout == ""
    assert "held by a session this unit never recorded" in result.stderr
    assert elapsed < 4


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


def test_start_still_idles_when_record_cannot_be_written(tmp_path):
    # A box with ~/.local/state unwritable (full disk, quota) must keep the tunnel up, just unrecorded.
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local/state").write_text("not a directory")
    with sshd_stand_in(tmp_path) as (sshd, _):
        deadline = time.monotonic() + 10
        while not (shells := children(sshd.pid)):
            assert time.monotonic() < deadline, "idle process never started"
            time.sleep(0.05)
        wait_until(lambda: pathlib.Path(f"/proc/{shells[0]}/comm").read_text().strip() == "tail", 5,
                   "the shell never reached its idle step")
        time.sleep(1)
        assert alive(shells[0])
    # The orphaned idle process notices its session is gone and exits on its own.
    wait_until(lambda: not alive(shells[0]), 5, "idle process outlived its session")
