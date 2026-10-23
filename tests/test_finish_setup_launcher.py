"""finish-setup launcher and its desktop entry (ubuntu/files/.local/...): what they run, where they look, how they end."""
import configparser
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "ubuntu/files/.local/bin/finish-setup"
DESKTOP = ROOT / "ubuntu/files/.local/share/applications/finish-setup.desktop"
TIMEOUT = 30

FAKE_ENGINE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_OUT/@NAME@"
printf 'FINISH_COMMAND=%s\\nPATH=%s\\n' "$UBUNTU_TWEAK_FINISH_COMMAND" "$PATH" > "$FAKE_OUT/@NAME@-env"
if [ -n "$FAKE_TRAP" ]; then trap 'echo caught > "$FAKE_OUT/caught"; exit 5' INT; fi
if [ -n "$FAKE_INTERRUPT" ]; then kill -INT 0; sleep 5; fi
exit "${FAKE_EXIT:-0}"
"""


def make_engine(directory: pathlib.Path, name: str = "argv") -> pathlib.Path:
    """A fake ubuntu-tweak: its argv goes to <FAKE_OUT>/<name>, its environment to <FAKE_OUT>/<name>-env."""
    directory.mkdir(parents=True, exist_ok=True)
    engine = directory / "ubuntu-tweak"
    engine.write_text(FAKE_ENGINE.replace("@NAME@", name), encoding="utf-8")
    engine.chmod(engine.stat().st_mode | stat.S_IXUSR)
    return engine


def argv_of(out: pathlib.Path, name: str = "argv") -> list[str]:
    return (out / name).read_text(encoding="utf-8").splitlines()


def env_of(out: pathlib.Path, name: str = "argv") -> dict[str, str]:
    return dict(line.split("=", 1) for line in (out / f"{name}-env").read_text(encoding="utf-8").splitlines())


@pytest.fixture()
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture()
def out(tmp_path):
    path = tmp_path / "out"
    path.mkdir()
    return path


def run(argv, env, stdin="", cwd=None):
    return subprocess.run(argv, env=env, input=stdin, capture_output=True, text=True, timeout=TIMEOUT, check=False, cwd=cwd)


def launch(home, out, *args, path="/usr/bin:/bin", stdin="", **env):
    # cwd = the out directory, which holds no executable: an empty PATH entry must not find a stray engine
    return run(["/bin/sh", str(LAUNCHER), *args], {"HOME": str(home), "PATH": path, "FAKE_OUT": str(out), **env}, stdin, cwd=out)


def test_launcher_is_posix_sh_and_executable():
    assert LAUNCHER.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert os.access(LAUNCHER, os.X_OK)
    assert subprocess.run(["/bin/sh", "-n", str(LAUNCHER)], check=False).returncode == 0


def test_runs_finish_in_russian_and_names_itself_for_the_next_step(home, out):
    make_engine(home / ".local/bin")
    done = launch(home, out, "--dry-run", path=f"{home}/.local/bin:/usr/bin:/bin")
    assert done.returncode == 0, done.stderr
    assert argv_of(out) == ["finish", "--lang", "ru", "--dry-run"]
    assert env_of(out)["FINISH_COMMAND"] == "finish-setup"


def test_finds_the_engine_when_local_bin_is_not_on_the_desktop_path(home, out):
    make_engine(home / ".local/bin")
    done = launch(home, out, path="/usr/bin:/bin")
    assert done.returncode == 0, done.stderr
    # the engine's own steps call gh, passwords-app and friends from ~/.local/bin: it comes first on their PATH
    assert env_of(out)["PATH"].split(":")[0] == f"{home}/.local/bin"


def test_an_empty_path_gets_no_current_directory_entry(home, out):
    make_engine(home / ".local/bin")
    launch(home, out, path="")
    assert env_of(out)["PATH"] == f"{home}/.local/bin"


def test_an_engine_found_through_an_empty_path_entry_is_the_one_that_runs(home, out, tmp_path):
    """`command -v` prints a bare name for it: run THAT file, not whatever the prepended ~/.local/bin would resolve."""
    make_engine(home / ".local/bin")
    here = tmp_path / "here"
    make_engine(here, name="argv-here")
    done = subprocess.run(
        ["/bin/sh", str(LAUNCHER)],
        env={"HOME": str(home), "PATH": ":/usr/bin:/bin", "FAKE_OUT": str(out)},
        cwd=here,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert (out / "argv-here").exists() and not (out / "argv").exists()


def test_an_empty_entry_at_the_end_of_path_is_the_current_directory(home, out, tmp_path):
    here = tmp_path / "here"
    make_engine(here, name="argv-here")
    done = run(["/bin/sh", str(LAUNCHER)], {"HOME": str(home), "PATH": "/usr/bin:/bin:", "FAKE_OUT": str(out)}, cwd=here)
    assert done.returncode == 0, done.stderr
    assert (out / "argv-here").exists()


def test_a_glob_character_in_a_path_entry_is_a_literal_name(home, out, tmp_path):
    make_engine(tmp_path / "real", name="argv-real")
    done = launch(home, out, path=f"{tmp_path}/re*:/usr/bin:/bin")
    assert done.returncode == 127, "the entry `re*` is a directory name that does not exist, not a pattern"
    assert not (out / "argv-real").exists()


def test_finds_the_engine_in_the_xp_checkout(home, out):
    make_engine(home / "xp/ubuntu-tweaks/bin")
    assert launch(home, out).returncode == 0
    assert (out / "argv").exists()


def test_prefers_the_engine_on_path(home, out, tmp_path):
    make_engine(home / ".local/bin")
    other = make_engine(tmp_path / "other", name="argv-other")
    done = launch(home, out, path=f"{other.parent}:/usr/bin:/bin")
    assert done.returncode == 0, done.stderr
    assert (out / "argv-other").exists() and not (out / "argv").exists()


def test_finds_the_stable_checkout_when_nothing_else_is_there(home, out, tmp_path):
    checkout = tmp_path / "checkout"
    make_engine(checkout / "bin")
    done = launch(home, out, UBUNTU_TWEAKS_DIR=str(checkout))
    assert done.returncode == 0, done.stderr
    assert (out / "argv").exists()


def test_a_missing_engine_is_said_in_russian_with_the_searched_checkout_and_exits_127(home, out, tmp_path):
    done = launch(home, out, UBUNTU_TWEAKS_DIR=str(tmp_path / "nowhere"))
    assert done.returncode == 127
    assert done.stderr.startswith("finish-setup:")
    assert re.search("[а-яё]", done.stderr, re.IGNORECASE)
    assert f"{tmp_path}/nowhere/bin" in done.stderr
    assert "bootstrap.sh" in done.stderr


def test_engine_exit_code_is_passed_through(home, out):
    make_engine(home / ".local/bin")
    assert launch(home, out, FAKE_EXIT="3").returncode == 3


def test_later_options_reach_the_engine_after_the_russian_default(home, out):
    make_engine(home / ".local/bin")
    launch(home, out, "--lang", "en", "--step", "amo-key")
    assert argv_of(out) == ["finish", "--lang", "ru", "--lang", "en", "--step", "amo-key"]


def test_pause_is_the_launchers_own_word_and_keeps_the_exit_code(home, out):
    make_engine(home / ".local/bin")
    done = launch(home, out, "--pause", stdin="\n", FAKE_EXIT="3")
    assert done.returncode == 3
    assert argv_of(out) == ["finish", "--lang", "ru"]


def test_pause_prints_the_enter_prompt_and_returns_after_the_answer(home, out):
    make_engine(home / ".local/bin")
    done = launch(home, out, "--pause", stdin="\n")
    assert "Enter" in done.stdout and "код 0" in done.stdout


def test_pause_also_prompts_when_the_engine_is_missing(home, out, tmp_path):
    done = launch(home, out, "--pause", stdin="\n", UBUNTU_TWEAKS_DIR=str(tmp_path / "nowhere"))
    assert done.returncode == 127
    assert "Enter" in done.stdout


def test_without_pause_there_is_no_prompt(home, out):
    make_engine(home / ".local/bin")
    assert "Enter" not in launch(home, out).stdout


def test_pause_really_waits_for_the_answer(home, out):
    make_engine(home / ".local/bin")
    proc = subprocess.Popen(
        ["/bin/sh", str(LAUNCHER), "--pause"],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "FAKE_OUT": str(out)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=1.0)
    proc.stdin.write("\n")
    proc.stdin.close()
    assert proc.wait(timeout=TIMEOUT) == 0


def terminal_ctrl_c(home, out, *args, **env):
    """Like a terminal's Ctrl+C: SIGINT goes to the whole foreground group (the launcher and the engine it runs)."""
    return subprocess.run(
        ["/bin/sh", str(LAUNCHER), *args],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "FAKE_OUT": str(out), "FAKE_INTERRUPT": "1", **env},
        input="\n",
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
        start_new_session=True,  # its own group, so the test runner itself is not signalled
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),  # a runner started in the background ignores SIGINT
    )


def test_ctrl_c_in_a_paused_window_ends_the_engine_but_not_the_window(home, out):
    make_engine(home / ".local/bin")
    done = terminal_ctrl_c(home, out, "--pause")
    assert "код 130" in done.stdout, "the engine must die of the SIGINT (an ignored signal would be inherited by it) and be reported"
    assert "Enter" in done.stdout, "the window would have closed on the interrupt"
    assert done.returncode == 130


def test_ctrl_c_without_pause_belongs_to_the_engine_alone(home, out):
    """No shell in between: the engine handles the interrupt itself and its own exit status is the launcher's."""
    make_engine(home / ".local/bin")
    done = terminal_ctrl_c(home, out, FAKE_TRAP="1")
    assert (out / "caught").exists(), "the engine's INT handler must have run before the launcher ended"
    assert done.returncode == 5


def test_a_non_executable_file_early_on_path_does_not_hide_a_working_engine_later_on_it(home, out, tmp_path):
    stale = make_engine(tmp_path / "stale", name="argv-stale")
    stale.chmod(0o644)
    good = make_engine(tmp_path / "opt", name="argv")
    done = launch(home, out, path=f"{stale.parent}:{good.parent}:/usr/bin:/bin")
    assert done.returncode == 0, done.stderr
    assert (out / "argv").exists() and not (out / "argv-stale").exists()


def test_a_non_executable_file_on_path_does_not_hide_the_working_engine(home, out, tmp_path):
    make_engine(home / ".local/bin")
    stale = make_engine(tmp_path / "stale", name="argv-stale")
    stale.chmod(0o644)
    done = launch(home, out, path=f"{stale.parent}:/usr/bin:/bin")
    assert done.returncode == 0, done.stderr
    assert (out / "argv").exists() and not (out / "argv-stale").exists()


# -- the desktop entry ---------------------------------------------------------------------------------------------------


class RawParser(configparser.RawConfigParser):
    optionxform = staticmethod(str)  # keep the case of `Name[ru]`, `Exec`


def desktop_entry():
    parser = RawParser()
    with DESKTOP.open(encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser["Desktop Entry"]


def exec_argv():
    """The argv GLib itself makes of Exec (key-file unescape, then Exec quoting), as GNOME runs it after the terminal wrapper."""
    pytest.importorskip("gi")
    import gi

    try:
        gi.require_version("GioUnix", "2.0")
        from gi.repository import GioUnix as Gio  # GLib 2.86 moved DesktopAppInfo there
    except (ValueError, ImportError):
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    from gi.repository import GLib

    info = Gio.DesktopAppInfo.new_from_filename(str(DESKTOP))
    assert info is not None and info.get_string("Exec")
    return GLib.shell_parse_argv(info.get_commandline())[1]


def test_desktop_entry_names_and_comment():
    entry = desktop_entry()
    assert entry["Type"] == "Application"
    assert entry["Name"] == "Finish setup"
    assert entry["Name[ru]"] == "Завершить настройку"
    assert entry["Comment"].strip() and entry["Comment[ru]"].strip()


def test_desktop_entry_opens_a_terminal_and_keeps_it_open():
    entry = desktop_entry()
    assert entry["Terminal"] == "true"
    assert "--pause" in entry["Exec"]


def test_desktop_entry_exec_has_no_tilde():
    assert "~" not in desktop_entry()["Exec"]


def test_desktop_entry_exec_lets_the_shell_expand_home():
    assert exec_argv() == ["sh", "-c", 'exec "$HOME/.local/bin/finish-setup" --pause']


def test_desktop_exec_argv_reaches_the_engine_through_the_linked_launcher(home, out):
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/finish-setup").symlink_to(LAUNCHER)
    make_engine(home / "engine")
    done = run(exec_argv(), {"HOME": str(home), "PATH": f"{home}/engine:/usr/bin:/bin", "FAKE_OUT": str(out)}, stdin="\n")
    assert done.returncode == 0, done.stderr
    assert argv_of(out) == ["finish", "--lang", "ru"]


def test_desktop_entry_validates():
    if shutil.which("desktop-file-validate") is None:
        pytest.skip("desktop-file-validate is not installed")
    done = subprocess.run(["desktop-file-validate", str(DESKTOP)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
