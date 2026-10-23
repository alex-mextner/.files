"""Tests for rig/merge-config.py: idempotence, extras that stay, scalar lists, source wins."""
import importlib.util
import pathlib
import subprocess
import sys

import yaml

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "rig" / "merge-config.py"
spec = importlib.util.spec_from_file_location("merge_config", SCRIPT)
merge_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_config)


def run(tmp_path, source, target, *extra):
    src = tmp_path / "source.yaml"
    dst = tmp_path / "target.yaml"
    src.write_text(yaml.safe_dump(source))
    if target is not None:
        dst.write_text(yaml.safe_dump(target))
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(src), str(dst), *extra],
        capture_output=True, text=True, check=False,
    )
    return proc, dst


def test_empty_target_gets_everything_and_second_run_is_ok(tmp_path):
    source = {"tmux": {"enabled": True}, "bun": {"enabled": True}}
    proc, dst = run(tmp_path, source, None, "--apply")
    assert proc.stdout.strip() == "changed"
    assert yaml.safe_load(dst.read_text()) == source
    again = subprocess.run([sys.executable, "-I", str(SCRIPT), str(tmp_path / "source.yaml"), str(dst)],
                           capture_output=True, text=True, check=False)
    assert again.stdout.strip() == "ok"


def test_machine_extras_stay_and_source_wins_on_conflict(tmp_path):
    source = {"sysmon": {"heal": {"enabled": True}}, "tmux": {"enabled": True}}
    target = {"sysmon": {"heal": {"enabled": False}, "extra": 1}, "local": "kept"}
    proc, dst = run(tmp_path, source, target, "--apply", "--backup-suffix", ".bak")
    assert proc.stdout.strip() == "changed"
    merged = yaml.safe_load(dst.read_text())
    assert merged["sysmon"] == {"heal": {"enabled": True}, "extra": 1}
    assert merged["local"] == "kept"
    assert yaml.safe_load((tmp_path / "target.yaml.bak").read_text()) == target


def test_scalar_lists_are_a_union_with_the_source_first(tmp_path):
    source = {"roots": ["~/xp", "~/work"]}
    target = {"roots": ["~/src", "~/xp"]}
    proc, dst = run(tmp_path, source, target, "--apply")
    assert proc.returncode == 0
    assert yaml.safe_load(dst.read_text())["roots"] == ["~/xp", "~/work", "~/src"]


def test_apply_without_a_backup_suffix_does_not_crash(tmp_path):
    proc, dst = run(tmp_path, {"a": 1}, {"b": 2}, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert yaml.safe_load(dst.read_text()) == {"a": 1, "b": 2}


def test_report_only_does_not_write(tmp_path):
    proc, dst = run(tmp_path, {"a": 1}, {"b": 2})
    assert proc.stdout.strip() == "changed"
    assert yaml.safe_load(dst.read_text()) == {"b": 2}


def test_invalid_target_yaml_fails_loudly(tmp_path):
    src = tmp_path / "source.yaml"
    dst = tmp_path / "target.yaml"
    src.write_text("a: 1\n")
    dst.write_text("a: [unclosed\n")
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(src), str(dst)],
                          capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert "merge-config" in proc.stderr
