# Agent guide

Repository instructions for coding agents (Claude Code, Codex, etc.). This is the
canonical file; the other agent-guide filename is a symlink to it, so every agent
reads the same guide.

## What this repository is

The user's dotfiles, checked out at `~/.files` on every machine (macOS, Ubuntu). Shell, vim, tmux,
git and `bin/` helpers sit at the top level; `ubuntu/` is the settings profile of the `ubuntu-tweak`
engine; `rig/config.yaml` is the user's global rig config and `rig.yaml` is this repository's own.

- Ubuntu: `./install-ubuntu.sh --plan` shows what it would change, `./install-ubuntu.sh` applies it,
  `--check` exits 1 while something is pending. It links, merges (never overwrites) the machine's
  `~/.profile`, `~/.bashrc`, `~/.zshrc`, `~/.gitconfig`, and merges `rig/config.yaml` into the rig
  global config. Converge the machine to that config with `rig apply commit`.
- macOS: `installer.sh` (brew, casks). Shared git settings live in `.gitconfig.shared`.
- Files must work on both systems: guard anything macOS-only (`/opt/homebrew`, `pbcopy`, `gsed`) so
  a missing tool never prints an error at shell start.
- Never commit a secret (`gitleaks dir .` runs as the local ship gate and in CI). Machine-specific
  identity, signing keys, credential helpers and tokens stay out of this repository.
- Tickets are GitHub issues of `alex-mextner/.files`; the ship-gate ticket code prefix is
  `task.code_prefix` in `rig.yaml` (rig writes it into `.ship-config`).

Keep the guardrail below — it applies to every repo.

## Writing reports for people (Telegram, PR bodies, spec summaries)

These are read by a human, not another agent, so optimize for being understood:

- Never invent abbreviations or compress terms into fragments. Write the full term.
- Prefer fewer points explained in full sentences over more points compressed into
  jargon. A short list of clear sentences beats a long list of cryptic stubs.
- Expand every non-obvious term at first use (name it in full, then abbreviate if you
  must reuse it).
- When a message exceeds the channel's length limit, cut secondary content — drop whole
  points — rather than compressing the wording of what remains.

# >>> rig-managed orchestrator (do not edit) >>>
# Orchestrator, not worker. Do not implement inline. Do not run `gh` except an unchained
# `gh ship <N>`, or read-only `gh issue` list/view, `gh pr` list/view/checks/diff, `gh run`
# list/view/watch, `gh repo view`, GET-only `gh api` (no -f/-F/--input). Do not `git
# worktree add` — `rig worktree create`.
# Read-only (`git status`, `ls`, `cat`, `grep`, `git worktree list`) is fine. Dispatch a subagent for the rest. Skill: delegate-work-to-subagents. Hook: orchestrator-stays-thin.
# Key reports MUST go to Telegram (`tg --tag report`): quote the user's original words, include ticket/PR URLs, name the measure already started, draft via `review just-ask`. Never "not in prod" with no next action.
# After a successful `gh ship` of a load-bearing feature, merge is not done: (1) turn it on
# in settings if it has a config knob, (2) `rig apply commit` on this machine, (3) prove it
# WORKS on this machine (command output, Telegram, deny text — SHA/on-disk is not a proof), (4) re-check the original user quote is solved.
# A helper yield is not done — do not tick the todo until the original ask is live-proven; wake parked helpers via hub send.
# <<< rig-managed orchestrator (do not edit) <<<

# >>> rig-managed autonomous-mode (do not edit) >>>
# Mode autonomous: extra-approval gate OFF. Do not invent a hold-for-human.
# Ship when gates pass. Skill: autonomous-mode (skill://autonomous-mode).
# <<< rig-managed autonomous-mode (do not edit) <<<
