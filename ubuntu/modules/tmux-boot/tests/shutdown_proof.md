# Shutdown save: design and proof

Goal: on poweroff/reboot the tmux sessions are saved (tmux-save-now: tmux-resurrect's own save script plus rig's
post-save hooks that write the omp/claude/codex resume-id maps) WHILE the tmux server and the harness panes are alive.
Neither path depends on continuum or on `status-right` (the Moshi client wipes that on every attach).

## Mechanism

| path | unit | fires on | why it is ordered before teardown |
|---|---|---|---|
| 1 (primary) | `tmux-shutdown-watch.service` | logind `PrepareForShutdown(true)`: `systemctl poweroff/reboot/halt/kexec`, GNOME menu, power button | logind emits the signal and waits for every *delay* inhibitor lock BEFORE any unit stops. The watcher holds a `shutdown` delay lock, saves with a hard timeout (5/6 of `InhibitDelayMaxUSec`, 25 s here), then releases the lock |
| 2 (fallback) | `tmux-shutdown-save.service` | user manager stop without that signal (`loginctl terminate-user`, user manager restart) | `ExecStop` + `After=tmux-boot.service omp.slice`, and the `systemd-dropins/*.scope.d` files add `Before=tmux-shutdown-save.service` to every `tmux-spawn-*.scope` and `omp-*.scope`, so the pane and omp scopes stop AFTER the save |

Both call `tmux-shutdown-save`, the shared step: refuses a dead server (never a degenerate save), skips if a shutdown save
already succeeded in this boot (stamp), bounds `tmux-save-now` with `timeout`, always exits 0.

Detach/logout: nothing listens to detach, and this host has `Linger=yes`, so a GNOME logout or ssh/Moshi disconnect
never stops the user manager or the tmux server; the units fire on neither. Only a machine shutdown (or an explicit
user-manager stop) does.

`InhibitDelayMaxSec`: this host has 30 s (Ubuntu's `/usr/lib/systemd/logind.conf.d/unattended-upgrades-logind-maxdelay.conf`),
so no root step is needed. On a host with logind's own 5 s default (save budget 4 s), the manual root step would be
`/etc/systemd/logind.conf.d/50-tmux-shutdown-save.conf` with `[Login]` / `InhibitDelayMaxSec=30` and `systemctl reload systemd-logind`.
This module never writes it.

Why the scope drop-ins: `After=` cannot name the pane scopes (they do not exist when the unit is written) and units without
an ordering stop in parallel. The e2e below shows panes already gone at ExecStop without them, alive with them.

## Proof commands (run in this worktree, `cd ubuntu/modules/tmux-boot`)

```
shellcheck tmux-shutdown-save tmux-shutdown-watch tmux-boot-selfcheck          -> clean
HOME=<tmp> systemd-analyze --user verify tmux-shutdown-save.service tmux-shutdown-watch.service \
    tmux-boot-selfcheck.service tmux-boot-selfcheck.timer                      -> rc 0; only unrelated
    "/usr/lib/systemd/user/spice-vdagent.service:23: Unknown key 'StandardError' in section [Install]"
python3 -W error::ResourceWarning -m unittest tests.test_shutdown              -> Ran 33 tests ... OK
python3 tests/e2e_shutdown_ordering.py                                         -> 26 PASS lines, ALL PASS (about 30 s)
PATH=/usr/bin:/bin ./tmux-boot-selfcheck   (live host, read only)              -> ERROR newest snapshot is 22721 min older ... (the real 16-day incident)
```

Mutation check (copies of the module, not committed): running the save after the server is gone, releasing the lock
before the save, and dropping `timeout` each make a test in `tests.test_shutdown` fail.

e2e excerpt (`tests/e2e_shutdown_ordering.py`; real user systemd, throwaway tmux server in its own scope, transient units
named zzsd<pid>-*, nothing installed, no real shutdown):

```
[alone] ExecStop ran tmux-save-now / server reachable inside ExecStop / snapshot written       PASS
  [nodrop] teardown transaction WITHOUT drop-ins: journal "no tmux server reachable ... nothing saved"
  [drop]   save log: tmux-save-now --reason user-manager-stop server_reachable=yes pane_scope=active omp_scope=active
[watch] inhibit : tmux-shutdown-watch 1000 ultra <pid> systemd-inhibit shutdown Saving the tmux sessions ... delay
[watch] injected PrepareForShutdown(true) -> save log: tmux-save-now --reason shutdown server_reachable=yes pane_scope=active omp_scope=active
[watch] lock gone from `systemd-inhibit --list` after the save; unit still running; no lock after stop
ALL PASS
```

## Only a real reboot can confirm

* that logind on this host really emits `PrepareForShutdown(true)` to the user-bus-less `gdbus monitor --system` of a
  user service and that the 30 s delay is honoured with the live pane count (the signal is injected here, no shutdown);
* that the user manager's real `exit.target`/`shutdown.target` transaction orders like the throwaway target did
  (the throwaway `Conflicts=` target uses the same job engine);
* the wall time of the real tmux-save-now with every live pane (budget 25 s);
* that tmux-boot-selfcheck.timer fires 10 min after boot and finds the shutdown snapshot.

After the next reboot run:

```
journalctl --user -b -1 -u tmux-shutdown-save -u tmux-shutdown-watch --no-pager
journalctl --user -b 0 -u tmux-boot-selfcheck --no-pager     # "shutdown save landed (Ns before poweroff)"
```
