# service-failover

A service that crashes is restarted with growing delays; a service that is failed or keeps restarting gets a repair
session instead of silence. Machine-level, generic: no service is named in the mechanism. Ticket
[#160](https://github.com/alex-mextner/.files/issues/160).

## Why it exists

On 2026-11-09 the odroid's `ha-host-actions.service` (system unit) failed after five fast restarts with
`Start request repeated too quickly` and stayed down for about 18 hours before anyone noticed. The unit file said
`Restart=always`, `RestartSec=3` and `StartLimitIntervalSec=infinity`. Two things combined:

1. **`infinity` is not "no limit".** The start counter of a unit with an infinite window never forgets a start. The unit
   had been started once at boot (2026-11-08); another user unit of the same name held port 8787
   (`OSError: Address already in use`), so four restarts on the evening of the 9th made five starts in total and the
   sixth was refused. The same unit would have died after any five restarts spread over weeks. A limit of `0` is the
   only way to switch the limit off, and then the unit never reaches `failed`, so nothing could escalate either.
2. **Nothing noticed.** `failed` is a quiet state: no log line is repeated, no alert is sent.

The module does not touch `StartLimitIntervalSec=`/`StartLimitBurst=`. systemd counts every start against them (timer
runs and socket activations too), so a global value would throttle every timer-driven one-shot and would override the limits
that units set in their own files. The scan finds a unit that turned `failed` (whatever its limit was) within 5 minutes,
and first aid clears the counter, which is what would have saved `ha-host-actions.service` on the evening of the 9th.

## What is installed

| Piece | Where | What it does |
|---|---|---|
| `service.d/10-failover.conf` | user manager (every user service of the PC and the odroid) and, with sudo, `/etc/systemd/system/service.d/` | `RestartSteps=8`, `RestartMaxDelaySec=5min`: the delay between restarts grows from the unit's own `RestartSec` to 5 minutes over 8 steps. systemd counts the steps in `NRestarts`, which only a manual start or `reset-failed` clears, so a service that crashes rarely drifts toward the 5 minute delay over weeks |
| `service.d/15-failover-repair.conf` | user manager of the PC only (needs omp) | `OnFailure=service-failover-repair@%n.service`. systemd fires it on every crash, also while it is still restarting the unit; the script below decides |
| `service-failover-repair@.service` + `.d/20-limits.conf` | PC | oneshot template; `OnFailure=` reset (checked with `systemctl --user show`) so a failing repair cannot start a repair of the repair. The script ignores `service-failover-*` units as well |
| `bin/service-failover-repair` | PC | leaves a unit alone that is healthy (running, or a one-shot that finished with `Result=success`), still inside its restart backoff, stopped on purpose, transient, a vendor or generated unit, or one that exited with a status it declares final (`RestartPreventExitStatus=`) (only `failed`, or 10 or more restarts in `auto-restart`, counts as a failure); first aid at most once per hour per unit (`restart --no-block`, preceded by `reset-failed` only when the start limit refused the unit, because `reset-failed` would hide a failure the restart cannot undo; wait 20 s; a start still in progress is left to systemd; one-shot jobs are left untouched because re-running them may repeat a side effect); if still down, writes a brief (host, scope, unit file, what was tried, last 50 journal lines with secrets masked and fenced as untrusted data, instructions to diagnose, fix at the source, verify for 5 minutes, look for an existing ticket, report) and starts the sanctioned launcher `~/.agents/skills/rig-detached-omp/rig-detached-omp`. One session at a time (`flock`, inherited by the session), one per unit per 12 hours and 4 per 24 hours in total (stamp files in `~/.local/state/service-failover/launches`: systemd forgets a unit's counters once it unloads, so it cannot carry the limit). A session that hangs holds the lock until it ends: later repairs are logged as deferred and no alert is sent (the launcher gives the session no time limit). Units stopped on purpose whose stop ended `failed` (a stop timeout) look like failures and are restarted once by first aid |
| `service-failover-scan.timer` / `.service` / `bin/service-failover-scan` | PC, every 5 minutes | finds failed units and units in `auto-restart` with 10 or more restarts (a unit that never reaches `failed`, such as one with `StartLimitIntervalSec=0`, or any unit under the backoff) on this host and on each host of `~/.config/service-failover/hosts`, over ssh with no sudo, user and system units, and starts the repair template for each |
| `docker/autoheal.compose.yaml` | both hosts, option `autoheal` | `willfarrell/autoheal` restarts a container whose healthcheck is `unhealthy` |

`Restart=` is deliberately **not** in the global drop-in: it would override `Restart=always` of units that have it,
and `Restart=always` is refused for `Type=oneshot` units (the unit would not load). Units that lack a `Restart=`
get `Restart=on-failure` from the module option `restart_units` (`<unit>.d/30-failover-restart.conf`).

The PC's `wyoming-whisper.service` (`StartLimitIntervalSec=0`) never reaches `failed`; the scan catches it as a restart
loop instead. Turning an option off removes what it installed: `--check` lists such files as `stale` and an install removes
them (and disables the scan timer when `repair = false`).

## Why odroidn2 is scanned from the PC

The odroid has no omp, no `tg` and no launcher, and no ssh key to the PC. `OnFailure=` there could not start a
session, so the odroid gets only the restart backoff (`--restart-only`), and the PC's scan reads its failed units over
ssh every 5 minutes. The repair session runs on the PC and reaches the odroid with `ssh odroidn2`. It has no sudo
there: whatever needs root goes into a ticket with the exact command.

## Install

PC (profile module, `[modules.service-failover] enabled = true` in `machines/ubuntu-pc/machine.toml`):
`ubuntu-tweak apply --commit`, or by hand `bash install.sh` (`--plan`, `--check` are read-only). It writes user files,
runs `daemon-reload` and enables the scan timer. It restarts nothing: a unit takes the new backoff at its next start.

Odroid, user level, once (no repository checkout there, so copy the module):

```sh
rsync -a --delete ubuntu/modules/service-failover/ odroidn2:.local/share/service-failover/
ssh odroidn2 'bash ~/.local/share/service-failover/install.sh --restart-only --autoheal'
```

### What the owner must run with sudo (odroid, once)

The system units live in `/etc/systemd/system`, owned by root. This installs the same backoff for every system
service and `Restart=on-failure` for `caddy.service` (it has no `Restart=`); nothing is restarted:

```sh
ssh odroidn2
sudo bash ~/.local/share/service-failover/install.sh --system caddy.service
```

Afterwards `systemctl cat ha-host-actions.service` lists `service.d/10-failover.conf`. A unit that is already `failed` is cleared with `sudo systemctl reset-failed <unit>`; the odroid's system
`ha-host-actions.service` is such a unit (it fails against the user unit that holds port 8787: decide which of the two is
canonical and disable the other).

## Docker

Every container on both hosts already has `restart: unless-stopped` (it backs off and never gives up on an exiting
container); `ut-sd` on the PC is a one-shot with `no`. `autoheal` adds the missing half, an `unhealthy` healthcheck.
Containers without a healthcheck are not covered by it. Gap: there is no escalation to a repair session for a
container; docker publishes no event that a unit can hang an `OnFailure=` on without a long-running watcher process.
Containers without a healthcheck on the odroid: home-lan (dhcp, dns-relay, tls-voice), and most of the HA compose
project (music-assistant, avahi-reflector, bgutil-pot, go2rtc, hga-postgres, printer-proxy, ssh-server,
u1-camera-proxy); on the PC: both switches, tls-voice, dns-relay, ha-cli-stage, dozzle, anisette.

## Tests

`python3 -m unittest discover -s ubuntu/modules/service-failover/tests` runs the installer, the drop-in contents, the
repair script and the scan against stand-in `systemctl`, `journalctl`, `ssh` and a launcher stub: no real systemd,
ssh or omp.
