# Backups-disk bind mounts: boot / unplug / late plug-in hardening (ticket 858)

`xp`, `work`, `.codex`, `Downloads`, `Pictures`, `.rustup`, `.npm`, `.bun/install/cache` and several `.cache`,
`.local/state`, `Applications` directories are bind mounts of one USB disk (ext4, mounted by an `/etc/fstab` line at
`/run/media/<user>/backups`, binds `nofail,x-systemd.requires-mounts-for=...`). Without the disk each of those paths is an
**empty directory on the SSD**: a service that starts there works on, and writes into, the wrong place, and the output is
hidden once the disk comes back.

**Current layout (2026-11-11):** `move-from-backups.sh` brought all 25 bound trees back to the SSD as plain directories and
commented their fstab lines out (`# [move-from-backups ...]`); the disk itself is still mounted at
`/run/media/<user>/backups` and holds the old copies as a reserve. Nothing is bound now, so no unit is gated, the retry
timer is disabled and the `chattr +i` flags are off. Everything below describes what applies **while fstab binds a
tree** (the user half reads the fstab when `install` runs, see "Which units are gated").

Two halves, both idempotent, both with `--dry-run` and a revert:

| half | script | needs root |
| --- | --- | --- |
| system | `harden-backups-mount.sh` (copy at `<home>/harden-backups-mount.sh`) | yes |
| user services | `backups-user-gates.sh` (installed as `~/.local/bin/backups-user-gates`) | no |

## Gaps found, and what closes each

| gap | fix |
| --- | --- |
| units, tmux-boot (15 panes into xp/work), docker start in the empty dirs | user half: `ConditionPathIsMountPoint=` drop-ins; root half: `chattr +i` on the empty dirs so nothing can create files there |
| late plug-in or slow spin-up never remounts | root half: udev rule -> `backups-attach.service` mounts the disk and every fstab bind for it, then kicks the user retry |
| units skipped without the disk stay stopped (a skipped start is silent and `Restart=` does not retry it) | user half: `backups-mount-retry.timer` (+ the attach kick) starts exactly the units whose last journal line is "skipped, unmet condition check ConditionPathIsMountPoint" |
| 10 s device timeout (the disk mounted at 9.9 s on the audited boot) | root half: 30 s |
| no `errors=` option | root half: `errors=remount-ro` |
| nothing orders `user@UID` / docker after the mount jobs, nor stops them before the unmount at shutdown | root half: `After=` drop-ins on `user@UID.service` and `docker.service` |

### Decisions, with the evidence

* **No `x-systemd.automount`.** Every bind line `Requires=` the backups *mount unit* directly (`systemctl show
  home-<user>-xp.mount -p Requires` lists `run-media-<user>-backups.mount`), so boot starts the mount eagerly and an
  automount in front of it changes nothing for the binds; after the one failed boot attempt nothing retries those binds,
  which automount cannot fix. It would also make every access to the path (file managers, indexers, `df`) block for the
  device timeout while the disk is missing. The udev rule + `backups-attach.service` fixes the real problem (late
  appearance) and also covers a spin-up slower than 30 s.
* **`After=` only on `user@UID.service` and docker, not `RequiresMountsFor=`/`Wants=`.** A hard dependency would stop the
  whole user manager (and docker) from starting while the disk is unplugged, which locks out the ssh/tmux/GNOME user
  services; `Wants=` would re-run the mount jobs and delay login by another device timeout. On the audited boot the disk
  mounted at 9.9 s and `user@1000` started at 17 s, because `local-fs.target` (which orders `sysinit.target`, hence
  `basic.target`, hence the user manager) waits for the nofail mount jobs to finish; the drop-in makes that explicit and
  also reverses it at shutdown (user manager and docker stop *before* the mounts are unmounted).
* **Conditions on the units, not on `user@UID`.** The empty-directory problem is per-unit; a unit that does not touch the
  trees (sshd sessions, GNOME, `sysmon`) must keep working without the disk.
* **`chattr +i` on the directory below the mount.** A bind mount over an immutable directory is allowed and unaffected,
  and while nothing is mounted not even root-owned code can create entries there (docker's bind-source `mkdir` fails
  instead of creating `xp/...` on the SSD; the container then fails to start, and the user retry starts it once the
  mounts are back).
* **Docker.** Containers with a restart policy that failed to start for a missing/unwritable bind source in a gated tree
  are started by the retry helper once **every** gated tree they bind from is mounted (at most 3 times per boot per
  container, so one that fails for another reason is not restarted forever; sources are parsed from `Binds`
  `source:target[:opts]` and structured `Mounts`). `[INFERENCE]` the daemon leaves such a container with a non-empty
  `State.Error`; the filter relies on it and is a no-op otherwise.

## Which units are gated

A tree is gated only while `/etc/fstab` has an **active** (not commented out) `bind` line for it under `$HOME`:
`backups-user-gates install` reads the fstab and writes a gate per unit for the still-bound trees only, removes the
gate (when it carries this script's marker) of a unit whose trees are all plain directories again, and enables
`backups-mount-retry.timer` only while at least one gate is left (otherwise it disables it; `retry` does nothing
without a gated tree). A gate on a plain directory would be an unmet `ConditionPathIsMountPoint` on **every** start:
the unit would never run. `backups-user-gates list` prints the live gates, or `no gated units`.

`install` changes nothing and exits non-zero when it cannot tell what is bound: the fstab is unreadable or has no
active line, or the user systemd cannot be reached (run it from the login session, not via `sudo -u`/cron). It also
leaves the gate of a mapped unit that cannot be loaded on the host alone (it may only be missing because its file lives on the unplugged disk), and, after the gates are gone, starts every
installed unit whose last start was skipped for a mount condition (a skipped start is retried by nothing without a
gate; a second run finishes an interrupted first one). Timer-driven units and the supervised 3d-printer workers are
left to their own schedule.

`GATES` in `backups-user-gates.sh` is the unit -> tree **map**, kept as it was so that a tree bound again
(`move-to-backups.sh`, then `sudo ./harden-backups-mount.sh` and `./backups-user-gates.sh install`) is gated again.
Each unit below gets `~/.config/systemd/user/<unit>.d/backups-mount-gate.conf` when its tree is bound (a dedicated file
name, so an existing `mounts.conf` of someone else is never overwritten or deleted by revert; drop-in only, no unit
file edited):

* `xp`: `3d-printer-vision@`, `3d-printer-watch@` (templates, cover every instance), `agent-tv-backend` (agent-tv is socket-activated: the socket and proxy stay up, only the backend that works in xp is gated),
  `agenttools-hsw-editor-hsw-editor`, `ai.hyperide.model-freshness`, `aisis-dashboard`, `autoheal`, `hcb-alert-watcher`,
  `hsw-heartbeat`, `hsw-heartbeat-20261021`, `hsw-observer-agent`, `hsw-review`, `router-discover`,
  `router-inventory-update`, `router-scan`, `snap-orca-sync`
* `work`: `conloca-merges-hourly`, `conloca-monitor`, `conloca-watch`
* `.npm`: `agenttools-harness-dashboard`
* `xp` **and** `work`: `tmux-boot` (restores the saved panes into those trees; with a missing mount it is skipped and
  the tmux server is not started with panes in empty dirs)

Found by reading every user unit, its drop-ins and the scripts it execs, resolving `~/.local/bin/*` symlinks into `xp`.
Not gated: units with no exec path, working directory or script reference inside a bind-mounted tree. `ai.hyperide.sysmon`
names `~/xp`, `~/work` as scan roots in its config; it is left alone here (its own tickets own that unit).
Timer-driven units that are skipped simply run on their next tick (the retry helper does not duplicate them).
`3d-printer-watch@` / `3d-printer-vision@` are on-demand workers started by a supervisor (`3d-printer-ondemand@`, 3d-cli
ticket 747): they keep the gate, but `NO_RETRY_START` in `backups-user-gates.sh` keeps `retry` from starting them (the
supervisor retries a skipped start itself; a retry-start would leave an idle worker running). When the supervisor
units run from `xp`, add `3d-printer-ondemand@.service xp` to `GATES`.
A unit gated by `ConditionPathIsMountPoint` is silently skipped at boot without the disk; `systemctl --user status`
shows `inactive (dead)` with `Condition: start condition unmet`, and `journalctl --user -u <unit>` the reason.

## Owner commands

```bash
cd <path of this directory>   # .files: ubuntu/machines/ubuntu-pc/scripts/backups-mount
./harden-backups-mount.sh --dry-run          # no root needed: shows every change
sudo ./harden-backups-mount.sh               # apply (one run, idempotent)
sudo ./harden-backups-mount.sh --revert      # undo
# user half: install = converge the gates to the fstab (safe to re-run after a move), revert = remove everything it wrote
./backups-user-gates.sh install | revert [--dry-run]
```

After `sudo ./harden-backups-mount.sh`:

```bash
findmnt -no OPTIONS /run/media/$USER/backups     # errors=remount-ro present
grep '^UUID=b93572e7' /etc/fstab                 # device-timeout=30s,errors=remount-ro
ls /etc/udev/rules.d/90-backups-disk.rules /etc/systemd/system/backups-attach.service
systemctl cat user@$(id -u).service docker.service | grep -c '^After=.*mount'   # drop-ins: backups-mounts.conf
sudo udevadm test /sys/class/block/sda1 2>&1 | grep -E 'SYSTEMD_WANTS=.*backups-attach'   # rule matches the partition
```

Flags on the dirs below the mounts (read through a throw-away view of `/`, nothing is unmounted):

```bash
sudo bash -c 'rv=$(mktemp -d); mount --bind / $rv; lsattr -d $rv/home/$SUDO_USER/{xp,work,.codex,Downloads,Pictures,.rustup,.npm}; umount $rv; rmdir $rv'
```

Every line must show an `i`.

## Tests without a reboot

1. **User half, real, no mount touched** (already run, output in the ticket):
   `backups-user-gates selftest` makes a throw-away unit whose start is skipped for a missing mount, proves `retry`
   leaves it stopped, then points the condition at `/dev/shm` (always a mount point: "the disk came back") and proves
   `retry` starts it. `retry` is restricted to the test unit (unique name per run) and docker recovery is off, so no real
   workload is touched. It changes real systemd state, so it has no `--dry-run`; it removes its unit again. It first
   checks the fstab logic on a throw-away fstab and unit dir, with no systemd call (commented, non-bind and unknown trees
   are not gated, trailing and doubled slashes and `\040` are normalised, a stale gate of ours is removed and the unit
   listed for a start, a foreign drop-in stays, only the bound tree is written, an unreadable fstab or one without an
   active line aborts `install` before it changes anything, the retry timer follows the gate count). The live part
   then proves that a unit skipped by a stale gate is started once the gate is gone, also on a second run.
2. **Empty-mountpoint behaviour, in a private mount namespace** (the host mounts are not affected; needs the root half applied):
   ```bash
   sudo unshare -m --propagation private bash -c '
     umount /home/$SUDO_USER/xp
     systemd-analyze condition "ConditionPathIsMountPoint=/home/$SUDO_USER/xp"   # must FAIL: units are skipped
     touch /home/$SUDO_USER/xp/probe; echo "touch rc=$?"                         # must fail: Operation not permitted (chattr +i)
     mount --bind /run/media/$SUDO_USER/backups/home-$SUDO_USER/xp /home/$SUDO_USER/xp
     systemd-analyze condition "ConditionPathIsMountPoint=/home/$SUDO_USER/xp"   # must succeed'
   ```
3. **Late-plug-in path.** `SYSTEMD_WANTS` fires only when a device *becomes* active, so replaying `add` on the
   already-mounted disk proves nothing about activation. Test the parts separately:
   - rule matching: the `udevadm test` line above must print `SYSTEMD_WANTS=backups-attach.service`;
   - the helper's execution, disk mounted so it must be a no-op: `sudo systemctl start backups-attach.service; journalctl -u backups-attach.service -b --no-pager | tail`
     (exit 0, no "FAILED"; it also kicks `backups-mount-retry.service` in the user manager);
   - the activation itself needs a real device transition: the "plug in later" step of the reboot checklist.
4. **Real late plug-in** is the reboot checklist below (it needs the disk to be absent).

## Reboot-test checklist

Only meaningful while fstab binds trees (see "Current layout"). Do each with `~/.local/bin/backups-user-gates list` before and after, and this check for the binds:
`awk '$4 ~ /bind/ && $1 !~ /^#/ {print $2}' /etc/fstab | while read -r m; do mountpoint -q "$m" || echo "NOT MOUNTED $m"; done` (prints nothing when all are up).

*Disk attached.* Reboot, wait for login.
- [ ] `findmnt /run/media/$USER/backups` is mounted and the bind check above prints nothing.
- [ ] `systemctl --user --failed` has nothing new; `systemctl --user status tmux-boot` is `active (exited)` and `tmux ls` shows the restored sessions.
- [ ] `journalctl -b -u backups-attach.service` shows no FAILED lines.

*Disk unplugged before boot.* Reboot (login takes ~30 s longer: the device timeout).
- [ ] Desktop/ssh login works (`user@UID` is not hard-dependent on the disk).
- [ ] `backups-user-gates list` shows `DOWN`; `systemctl --user is-active tmux-boot agent-tv-backend aisis-dashboard conloca-watch` are `inactive`; `tmux ls` has no server.
- [ ] `journalctl --user -b | grep 'unmet condition check ConditionPathIsMountPoint'` lists the skipped units.
- [ ] `touch ~/xp/x` fails with "Operation not permitted"; nothing exists under `~/xp ~/work` (`ls -A`).
- [ ] `docker ps` works; a container with a bind under `xp` (`aisis-openclaw-gateway`) is not running and `~/xp` is still empty.

*Plug in later* (no commands): within a few seconds
- [ ] `journalctl -u backups-attach.service -b` shows `mounted ...` lines; `findmnt /run/media/$USER/backups` and the binds are up.
- [ ] Within ~2 min (usually at once: the attach service kicks it) the skipped units are `active`, `tmux ls` shows the restored sessions, the container is running (`journalctl --user -u backups-mount-retry.service`).

## Interplay and caveats

* **After every move of a tree, in either direction, run `./backups-user-gates.sh install`** (it converges the gates to
  the fstab; nothing does it on its own: `retry` and the timer never read the fstab). Skipping it after
  `move-to-backups.sh` leaves the newly bound tree ungated (units start in the empty directory when the disk is missing);
  skipping it after `move-from-backups.sh` leaves the old gate on a plain directory (the unit is skipped on every
  start; `install` removes the gate and starts the units whose start was skipped).
* `move-to-backups.sh` adds new bind targets: re-run `sudo ./harden-backups-mount.sh` afterwards (it re-reads fstab; the
  drop-ins and flags for the new targets are added, everything else reports "already in place"). To remove or merge an
  existing bind (`move` with nested binds, whole-tree merges) release the flag first: `sudo ./harden-backups-mount.sh --revert`, change, re-apply.
* A non-empty underlying directory is reported and **not** flagged: those files were written while the disk was missing
  and need a human decision (move into the disk copy, then re-run).
* Conditions are evaluated at start. Units already running are unaffected, and a unit whose disk disappears *while it
  runs* is not stopped (out of scope: needs `BindsTo`, which would stop everything on a USB glitch).
* `errors=remount-ro` makes the disk (and every bind of it) read-only after a filesystem error: writes then fail loudly instead of corrupting more.
* `--revert` restores what the first apply found, from the ledger in `/var/lib/harden-backups-mount`: the original
  `x-systemd.device-timeout` (or none), `errors=remount-ro` removed only if the script added it, and `chattr -i` only on
  the directories it made immutable (a flag that was already set stays). Without the ledger it leaves the fstab line
  and the flags alone and says what to do by hand.
* The retry service is bounded (`TimeoutStartSec=5min`, 8 s per docker call, the docker pass ends after 200 s). The attach service has no start timeout on
  purpose: it waits for the mount job, which waits for `fsck`, and nothing would restart it after a kill.
* fstab paths are decoded (`\040` space, `\011` tab) before they become mount-unit names; the disk line may use
  `UUID=<uuid>` or `/dev/disk/by-uuid/<uuid>`, nothing else (the script stops before changing anything).
* A tmux server started by something else (ssh `tmux`, a terminal) before the disk is up is not gated: its panes sit in the
  empty (immutable) directories; `tmux-boot` itself never restores into them.
