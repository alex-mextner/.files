"""Which processes an e2e run may kill when it cleans up its fixtures.

The e2e scripts start real omp sessions on fixture session files; those omp processes (and the abduco/rig harness
processes that host them) outlive the throwaway tmux server by design, so cleanup has to find and stop them. It must
not stop anything else: an inspector a person happens to run on a fixture (`vim <session file>`, `tail -f <file>`,
`python3 -m json.tool <file>`, `bash -c "tail -f <durable>.log"`, `omp read <session>`) carries the very same path in
its command line. So every kind of process is accepted only when its command line has the shape of the launch it
must be:

  omp      an INTERACTIVE `omp ...` (no inspector subcommand or headless flag, the rule tmux-lib's tl_pane_agents
           applies) naming one of our session files or durable names
  abduco   `abduco ...` naming our durable name (rig's durable launcher)
  rig      `rig harness ...` or `python* .../rig harness ...` naming ours (the harness host)
  bun/node `bun|node .../packages/coding-agent/src/cli.ts ...` naming ours (a source checkout of omp)
  a shell  only as the wrapper of an abduco launch: the command after `-c` is an abduco invocation naming our durable
"""

import os

SOURCE_ENTRYPOINT = "/packages/coding-agent/src/cli.ts"
SHELLS = {"sh", "bash", "zsh", "dash"}

# the omp subcommands and flags tmux-lib's tl_pane_agents reads as "not an interactive agent": a person running
# them against a fixture is inspecting it, not launching it
OMP_INSPECTOR_SUBCOMMANDS = {
    "acp", "agents", "auth-broker", "collab", "compress", "config", "dry-balance", "gallery", "gc", "images",
    "install", "join", "models", "plugin", "ps", "read", "render", "say", "search", "setup", "share", "shell",
    "ssh", "stats", "tiny-models", "token", "ttsr", "help", "version",
}
OMP_INSPECTOR_FLAGS = {"-p", "--print", "--help", "-h", "--version", "-V"}


def _hosts_harness(program, words):
    """`rig harness ...`, or any python process whose command line is `... rig harness ...` / `-m riglib harness ...`."""
    if "harness" not in words:
        return False
    return program == "rig" or any(os.path.basename(w) == "rig" or w == "riglib" for w in words)


def _runs_source_entrypoint(words):
    # omp's launcher spells the entrypoint .../packages/coding-agent/scripts/../src/cli.ts
    return any(os.path.normpath(w).endswith(SOURCE_ENTRYPOINT) for w in words)


def _interactive_omp(words):
    """The same rule tl_pane_agents applies: a subcommand first, or one of the headless flags, means not-an-agent."""
    if words and words[0] in OMP_INSPECTOR_SUBCOMMANDS:
        return False
    return not any(w in OMP_INSPECTOR_FLAGS or w.startswith("__omp_worker_") for w in words)

def _shell_wraps_abduco(words):
    """Only as the wrapper of an abduco launch: the command after `-c` is an abduco invocation (by basename)."""
    cmd = words[words.index("-c") + 1:] if "-c" in words else words
    return bool(cmd) and os.path.basename(cmd[0]) == "abduco"


def is_owned(args, session_files, durable_names):
    """True when `args` (a `ps -o args` line) is an omp/abduco/rig-harness process of one of these fixtures."""
    parts = args.split()
    if not parts:
        return False
    program = os.path.basename(parts[0])
    words = parts[1:]
    mentions_durable = any(n in args for n in durable_names)
    mentions_any = mentions_durable or any(n in args for n in session_files)
    if program == "omp":
        return mentions_any and _interactive_omp(words)
    if program == "abduco":
        return mentions_durable
    if program == "rig" or program.startswith("python"):
        return _hosts_harness(program, words) and mentions_any
    if program in {"bun", "node"}:
        return mentions_any and _runs_source_entrypoint(words)
    if program in SHELLS:
        return _shell_wraps_abduco(words) and mentions_durable
    return False


def owned_pids(rows, session_files, durable_names, exclude_pid):
    """The pids of `rows` ((pid, ppid, args) tuples) that is_owned() accepts, never `exclude_pid` (the caller)."""
    return sorted(pid for pid, _, args in rows if pid != exclude_pid and is_owned(args, session_files, durable_names))
