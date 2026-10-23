"""ubuntu-pc finish step `build-server-cleanup`: two scratch directories on the Hetzner build server, removed on request."""
import os
import pathlib
import re
import stat
import subprocess
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROFILE = ROOT / "ubuntu"
SCRIPT = PROFILE / "machines/ubuntu-pc/scripts/build-server-cleanup"
MACHINE = PROFILE / "machines/ubuntu-pc/machine.toml"
HOST = "ultra@65.21.40.51"
SSH_OPTIONS = "-n -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=3"
DIRS = ["/home/ultra/projects/rig-ctmux-20261023", "/home/ultra/projects/ubuntu-sync-ctmux-20261022"]

# Fake ssh. It parses exactly the call shapes of the script: `ssh -n -o K=V -o K=V HOST test ! -e PATH`, `... HOST true`
# and `... HOST rm -rf -- PATH...`. `$FAKE_REMOTE/present` lists the remote paths that exist, one per line; every call is
# logged. Without -n it reads (and loses) its standard input, as real ssh does.
FAKE_SSH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_REMOTE/calls"
if [ -n "$FAKE_SSH_FAIL" ]; then echo "ssh: connect to host 65.21.40.51 port 22: $FAKE_SSH_FAIL" >&2; exit 255; fi
no_stdin=0
while [ "${1#-}" != "$1" ]; do
  case "$1" in -n) no_stdin=1; shift ;; -o) shift 2 ;; *) shift ;; esac
done
[ "$no_stdin" = 1 ] || cat > /dev/null
host=$1; shift
[ "$host" = "ultra@65.21.40.51" ] || { echo "unexpected host $host" >&2; exit 254; }
case "$1" in
  true) exit 0 ;;
  test)  # test ! -e PATH
    grep -qxF -- "$4" "$FAKE_REMOTE/present" && exit 1
    exit 0 ;;
  rm)    # rm -rf -- PATH...
    shift 3
    for p in "$@"; do grep -vxF -- "$p" "$FAKE_REMOTE/present" > "$FAKE_REMOTE/present.new"; mv "$FAKE_REMOTE/present.new" "$FAKE_REMOTE/present"; done
    exit 0 ;;
esac
echo "unexpected remote command: $*" >&2
exit 253
"""


class Remote:
    """The fake server state plus how to run the script against it."""

    def __init__(self, base: pathlib.Path):
        self.state = base / "remote"
        self.state.mkdir()
        (self.state / "present").write_text("\n".join(DIRS) + "\n", encoding="utf-8")
        (self.state / "calls").write_text("", encoding="utf-8")
        self.bin = base / "bin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text(FAKE_SSH, encoding="utf-8")
        ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
        self.home = base

    def run(self, *args, stdin="", lang="en", **env):
        full = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_REMOTE": str(self.state),
            "UBUNTU_TWEAK_LANG": lang,
            **env,
        }
        return subprocess.run([str(SCRIPT), *args], env=full, input=stdin, capture_output=True, text=True, timeout=30, check=False)

    def calls(self):
        return (self.state / "calls").read_text(encoding="utf-8").splitlines()

    def removal_calls(self):
        return [c for c in self.calls() if " rm " in f" {c} "]

    def present(self):
        return (self.state / "present").read_text(encoding="utf-8").split()

    def set_present(self, *paths):
        (self.state / "present").write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")


@pytest.fixture()
def remote(tmp_path):
    return Remote(tmp_path)


def test_script_is_executable_posix_sh():
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["/bin/sh", "-n", str(SCRIPT)], check=False).returncode == 0


def test_check_is_pending_while_a_directory_is_there_and_names_it(remote):
    done = remote.run("check")
    assert done.returncode == 1
    assert DIRS[0] in done.stdout
    assert remote.calls()[0].endswith(f"{HOST} test ! -e {DIRS[0]}")


def test_check_names_only_what_is_left(remote):
    remote.set_present(DIRS[1])
    done = remote.run("check")
    assert done.returncode == 1
    assert DIRS[1] in done.stdout and DIRS[0] not in done.stdout


def test_check_is_done_when_both_are_gone(remote):
    remote.set_present()
    done = remote.run("check")
    assert done.returncode == 0
    assert len(remote.calls()) == 2


def test_every_ssh_call_is_batch_mode_with_a_timeout_and_the_full_user_at_ip_and_leaves_stdin_alone(remote):
    remote.run("check")
    remote.run("run", stdin="y\n")
    assert remote.calls()
    for line in remote.calls():
        assert line.startswith(f"{SSH_OPTIONS} {HOST} "), "-n: ssh must not swallow the answer typed for the prompt; ServerAlive: no hang on a silent server"


def test_an_unreachable_server_is_pending_with_the_reason_never_done(remote):
    done = remote.run("check", FAKE_SSH_FAIL="Connection timed out")
    assert done.returncode == 1
    assert "Connection timed out" in done.stdout + done.stderr
    assert "65.21.40.51" in done.stdout + done.stderr
    assert len(remote.calls()) == 1, "one timeout, not one per directory"


def test_check_speaks_russian(remote):
    done = remote.run("check", lang="ru")
    assert re.search("[а-яё]", done.stdout, re.IGNORECASE)


def test_run_shows_both_literal_paths_and_removes_them_on_yes(remote):
    done = remote.run("run", stdin="y\n")
    assert done.returncode == 0, done.stderr
    assert DIRS[0] in done.stdout and DIRS[1] in done.stdout
    assert remote.calls()[-1].endswith(f"{HOST} rm -rf -- {DIRS[0]} {DIRS[1]}")
    assert remote.present() == []


@pytest.mark.parametrize("answer", ["", "n\n", "N\n", "no\n", "yes please\n", "\n", " \n", "yy\n"], ids=["eof", "n", "N", "no", "yes-please", "empty-line", "blank", "yy"])
def test_run_removes_nothing_unless_the_answer_is_y(remote, answer):
    done = remote.run("run", stdin=answer)
    assert done.returncode == 1
    assert remote.removal_calls() == []
    assert remote.present() == DIRS


def test_a_capital_Y_is_a_yes_too(remote):
    assert remote.run("run", stdin="Y\n").returncode == 0
    assert remote.present() == []


def test_run_never_takes_a_path_from_its_arguments_or_the_environment(remote):
    done = remote.run("run", "/home/ultra", "--", "-rf", stdin="y\n", dirs="/", dir="/", HOME_DIRS="/")
    assert done.returncode == 0
    assert remote.removal_calls() == [f"{SSH_OPTIONS} {HOST} rm -rf -- {DIRS[0]} {DIRS[1]}"]


def test_run_stops_when_the_server_cannot_be_reached(remote):
    done = remote.run("run", stdin="y\n", FAKE_SSH_FAIL="No route to host")
    assert done.returncode == 1
    assert "No route to host" in done.stdout + done.stderr
    assert "Remove them?" not in done.stdout, "nothing is asked about a server that cannot be reached"


def test_a_failing_removal_is_reported(remote):
    (remote.bin / "ssh").write_text(FAKE_SSH.replace("    shift 3\n", "    echo \"rm: cannot remove\" >&2; exit 1\n    shift 3\n"), encoding="utf-8")
    done = remote.run("run", stdin="y\n")
    assert done.returncode == 1
    assert "failed" in done.stdout


def test_a_broken_remote_shell_is_cannot_ask_not_still_there(remote):
    (remote.bin / "ssh").write_text(FAKE_SSH.replace("    grep -qxF", "    echo 'bash: test: command not found' >&2; exit 127\n    grep -qxF"), encoding="utf-8")
    done = remote.run("check")
    assert done.returncode == 1
    assert "cannot ask" in done.stdout and "still on the build server" not in done.stdout


def test_dry_run_changes_nothing(remote):
    done = remote.run("run", stdin="y\n", UBUNTU_TWEAK_FINISH_DRY_RUN="1")
    assert done.returncode == 0
    assert DIRS[0] in done.stdout
    assert remote.removal_calls() == []
    assert remote.present() == DIRS


def test_unknown_subcommand_is_a_usage_error(remote):
    assert remote.run("frobnicate").returncode == 2
    assert remote.run().returncode == 2


# -- the machine overlay that declares the step -----------------------------------------------------------------------------


def test_machine_declares_the_optional_cleanup_step_with_both_languages_and_a_real_script():
    steps = tomllib.loads(MACHINE.read_text(encoding="utf-8"))["finish"]["steps"]
    (step,) = [s for s in steps if s["id"] == "build-server-cleanup"]
    assert step["optional"] is True
    for key in ("title", "title_ru", "why", "why_ru", "action", "action_ru"):
        assert step[key].strip(), key
    for argv in (step["check"], step["run"]):
        script = PROFILE / argv[0]  # `./x` argv[0] is relative to the profile directory
        assert argv[0].startswith("./") and script.resolve() == SCRIPT.resolve(), argv
        assert os.access(script, os.X_OK)
