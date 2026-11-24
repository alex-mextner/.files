# Source this LAST in interactive shell startup (.profile, .zprofile, .bashrc, .zshrc and the blocks
# install-ubuntu.sh merges into them: each of them can prepend ~/.local/bin or the Homebrew/bun dirs
# after the others ran, so the last one to run must win).
#
# rig's harness wrappers (`harness_wrappers` in rig/config.yaml: omp, claude, codex, opencode) only take
# effect when their directory comes BEFORE the real binaries on PATH, and rig never edits PATH itself.
# Moving it to the front here is what makes an ordinary interactive `omp` host itself in abduco under
# the rig crash supervisor; print mode, subcommands, --help and non-terminal launches pass straight
# through to the real binary. A machine without the directory (macOS, rig not applied) is untouched.
#
# POSIX sh only (no ${var//pat/rep}, no arrays): ~/.profile is sourced by dash logins too, where a
# syntax error in a sourced file ends the shell. The loop compares entries literally, so a HOME with
# glob characters cannot match another PATH entry.
#
# The wrappers run the real omp binary directly, not the ~/.local/bin/omp cgroup launcher: rig applies
# the same per-session scope and signal domain itself. The launcher's one default that rig does not
# carry is kept here: `review` spawns up to REVIEW_MAX_CONCURRENCY fat agent CLIs per process (3
# concurrent reviews x 4 was the 2026-10-11 memory incident).
_harness_wrappers_dir="$HOME/.local/share/rig/bin"
if [ -d "$_harness_wrappers_dir" ]; then
  _harness_wrappers_rest="$PATH:"
  _harness_wrappers_path=""
  while [ -n "$_harness_wrappers_rest" ]; do
    _harness_wrappers_entry=${_harness_wrappers_rest%%:*}
    _harness_wrappers_rest=${_harness_wrappers_rest#*:}
    [ "$_harness_wrappers_entry" = "$_harness_wrappers_dir" ] || _harness_wrappers_path="$_harness_wrappers_path:$_harness_wrappers_entry"
  done
  PATH="$_harness_wrappers_dir$_harness_wrappers_path"
  export PATH
  export REVIEW_MAX_CONCURRENCY="${REVIEW_MAX_CONCURRENCY:-2}"
fi
unset _harness_wrappers_dir _harness_wrappers_rest _harness_wrappers_path _harness_wrappers_entry
