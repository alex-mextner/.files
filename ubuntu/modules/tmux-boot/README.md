# tmux-boot: the tmux workspace survives reboots, crashes and a dead autosave

The module keeps the user's tmux sessions (and the omp/claude/codex agents running in them) recoverable. It exists
because of the 2026-11-08 incident: continuum's autosave lived in a `#(...)` token in the global `status-right`, the
Moshi iOS client empties `status-right` on every attach, nothing noticed, and a reboot restored a 16-day-old layout.

Tracking: dotfiles #142 · rig-cli #853 (Linux autosave) · rig-cli #854 (the omp save is all-or-nothing).

## Layers (each works without the ones above it)

| Layer | What | Where it runs |
|---|---|---|
| boot | `tmux-boot` starts the server at boot (linger) so continuum restores; it first points `last` at the newest *non-degenerate* snapshot and refuses to restore again when the server keeps dying (restore-loop guard) | `tmux-boot.service` |
| timer | `tmux-autosave` runs continuum's own save entry point every 5 min, independent of the status line and of attached clients | `tmux-autosave.timer` |
| events | tmux layout hooks (`session-created`, `window-linked`, `pane-exited`, ...) run `tmux-save-debounce`: one `tmux-save-now` per 60 s burst of changes | the server's hook table, re-asserted by the timer and the watchdog |
| guard | `tmux-save-now` is the single guarded save: single flight, degenerate snapshots never replace `last`, retention, `last-with-harness`. `@resurrect-save-script-path` points at `tmux-resurrect-save`, so continuum's own trigger goes through it too | every layer above |
| watchdog | `tmux-watchdog` asserts the invariants, self-heals, alerts, and (opt-in) restarts a dead server | `tmux-watchdog.timer`, every 5 min |
| shutdown | a save at shutdown/logout (`tmux-shutdown-*`, added by the shutdown-save work) | systemd |

Scripts are installed to `~/.local/bin`, units to `~/.config/systemd/user`; the `systemd-dropins/*.scope.d/*.conf`
files land at `~/.config/systemd/user/<same path>` followed by a `daemon-reload` (they order the pane scopes after
the shutdown save). `install.sh` finds every `tmux-*` asset next to it by glob — an extension-less file is a script
whatever its mode (`install -m 755` sets it), a `tmux-*.service|timer` with an `[Install]` section is enabled and,
except `tmux-boot.service` (which waits for the next boot so a running server is never touched), started, and any
other `tmux-*.*` file is reported instead of silently skipped (`bash install.sh --plan` / `--check` / no argument;
`--check` exits 3 on drift).

## Is it working? (status check)

```sh
systemctl --user list-timers 'tmux-*'                 # autosave + watchdog timers, next/last run
systemctl --user status tmux-autosave tmux-watchdog   # last result of the oneshots
cat ~/.local/state/tmux-boot/save-status              # epoch=... result=ok|unchanged|degenerate|failed reason=... detail=...
ls -l ~/.tmux/resurrect/last ~/.tmux/resurrect/last-with-harness
ls ~/.tmux/resurrect/degenerate/                      # snapshots the guard refused (kept for forensics)
tmux show-option -gv @resurrect-save-script-path      # .../tmux-resurrect-save when guarded
tmux show-hooks -g window-linked                      # an entry [940..959] running tmux-save-debounce
~/.local/bin/tmux-watchdog                            # run the checks now (exit 0 = all hold or were healed)
```

Journal, per layer:

```sh
journalctl --user -u tmux-autosave -n 20 --no-pager
journalctl --user -u tmux-watchdog -n 40 --no-pager
journalctl --user -u tmux-boot -b --no-pager
journalctl --user -t tmux-save-now -n 20 --no-pager   # guarded saves, when run from a unit
```

Force a save: `tmux-save-now --reason manual` (add `--force` to accept a smaller state the guard holds back).

## The guard (`tmux-save-now`)

* A snapshot is **degenerate** when it has no pane; has no omp/claude/codex pane where the previous one had some; is a
  bare single window after 3+ windows; or lost more than `TMUX_SAVE_NOW_TOLERANCE` percent (50) of the panes/windows of a
  previous snapshot that had at least `TMUX_SAVE_NOW_MIN_PANES` (4). It is moved to `degenerate/`, `last`, the pane
  archive and rig's session maps go back to the previous good state, and one alert is sent (per 6 h).
* A smaller state that persists `TMUX_SAVE_NOW_ACCEPT_AFTER` seconds (6 h) becomes the new baseline.
* Retention (replacing resurrect's own, which is pushed out of the way with `@resurrect-delete-backup-after 3650`): the
  newest 48 snapshots plus the newest of each day for 14 days; `last` and `last-with-harness` targets are never removed.
  Each accepted snapshot keeps its pane archive and session maps (`pane_contents_<stamp>.tar.gz`, `maps/<stamp>/`).
* Resurrect's own prefix+Ctrl-s binding calls its save script directly and bypasses the guard; `tmux-boot` still picks
  the newest non-degenerate snapshot at restore time.

## The watchdog invariants

server up · `@resurrect-save-script-path` is the wrapper · layout hooks present · `@resurrect-hook-post-save-all` /
`-post-restore-all` set in the server · `~/.config/rig/tmux/rig.tmux.conf` still sets them and the scripts exist (a
`rig apply` that forgets them is reported loudly) · `@continuum-save-interval` > 0 · `tmux-autosave.timer`,
`tmux-boot.service`, `tmux-shutdown-save.service`, `tmux-shutdown-watch.service` enabled and loaded (the last three only
when their unit files exist) · a save ran within 2x the continuum interval while panes exist · the omp/cc/codex session
map exists, is not empty and is fresh while an interactive agent pane runs (a one-shot `claude -p`, `codex exec` or
`codex mcp-server` that an agent starts for a review or a tool leaves no session and demands no map).

`status-right` without continuum's token is only a note: Moshi empties it on every attach and nothing here depends on it.
Self-heal never restarts or kills the server. Every heal and every unresolved problem alerts through `tmux-alert`
(mem-sentinel's channel: a `[tmux-boot]` steer into a live omp session via `tg-ctl omp-live send`, `omp-steer all`
when no session is live; once per 6 h per kind). Exit 1 shows the unit as failed.

### Automatic server recovery (off by default)

```sh
tmux-watchdog recovery status|on|off     # state file ~/.local/state/tmux-boot/recover-enabled; TMUX_WATCHDOG_RECOVER=1 forces on
```

When on, and the server is gone although `tmux-boot` started one earlier in the same boot, the watchdog runs
`rig omp watchdog --socket <default socket> --boot-script tmux-recover` (rig refuses to start a second server while any
process owns the socket, and keeps a 60 s cooldown). `tmux-recover` starts `tmux-boot`, which restores the newest
non-degenerate snapshot; rig's restore hook relaunches the agents. At most `TMUX_BOOT_MAX_RESTORES` (3) restores per hour:
after that the server starts WITHOUT a restore and an alert is sent.

## Disable / roll back

```sh
systemctl --user disable --now tmux-watchdog.timer tmux-autosave.timer       # stop the periodic layers
systemctl --user disable --now tmux-boot-selfcheck.timer tmux-shutdown-save.service tmux-shutdown-watch.service  # and the shutdown/selfcheck layers
rm -f ~/.config/systemd/user/tmux-spawn-.scope.d/10-tmux-shutdown-save.conf \
      ~/.config/systemd/user/omp-.scope.d/10-tmux-shutdown-save.conf && systemctl --user daemon-reload  # only the module's own drop-ins
tmux set-hook -gu 'window-linked[940]'                                       # (and the other hooks, see tmux-lib TL_*_HOOKS)
tmux set -g @resurrect-save-script-path "$(tmux show-option -gv @tmux-boot-resurrect-save-script)"   # unguard continuum's trigger
```

Removing a script from the module and re-running `install.sh` does not delete the installed copy — but `module.toml`
names the module's assets exactly, so the leftover shows up as untracked coverage instead of passing as ours.

## Tests

```sh
cd ubuntu/modules/tmux-boot
python3 -m unittest tests.test_install tests.test_save_now tests.test_hooks tests.test_watchdog tests.test_boot_guard tests.test_e2e_procs tests.test_shutdown
python3 tests/integration_installed.py    # the real installer into a temp HOME, then the deployed pieces, ~15 s
python3 tests/e2e_shutdown_ordering.py    # real user systemd, throwaway scopes/targets, ~30 s
python3 tests/e2e_isolated_restore.py    # real omp + rig, throwaway socket, ~6 min
python3 tests/e2e_hardening.py           # real plugins, fake agent, throwaway socket, ~5 min
shellcheck -x tmux-boot tmux-autosave tmux-lib tmux-save-now tmux-save-debounce tmux-resurrect-save \
           tmux-watchdog tmux-recover tmux-alert tmux-boot-selfcheck tmux-shutdown-save tmux-shutdown-watch install.sh
```

Only a real reboot proves: the units starting at boot with linger, the restore of the live workspace, and the shutdown
save under the real logind.
