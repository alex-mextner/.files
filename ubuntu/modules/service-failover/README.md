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

The one exception is explicit and per unit: `install.sh --system UNIT=always|on-failure` (see "What the owner must run with sudo") writes
`StartLimitIntervalSec=0` and the whole restart backoff into that unit's own drop-in, for units that must never be refused a start.

## What is installed

| Piece | Where | What it does |
|---|---|---|
| `service.d/10-failover.conf` | user manager (every user service of the PC and the odroid) and, with sudo, `/etc/systemd/system/service.d/` | `RestartSteps=8`, `RestartMaxDelaySec=5min`: the delay between restarts grows from the unit's own `RestartSec` to 5 minutes over 8 steps. systemd counts the steps in `NRestarts`, which only a manual start or `reset-failed` clears, so a service that crashes rarely drifts toward the 5 minute delay over weeks |
| `service.d/15-failover-repair.conf` | user manager of the PC only (needs omp) | `OnFailure=service-failover-repair@%n.service`. systemd fires it on every crash, also while it is still restarting the unit; the script below decides. The instance is the unit name (it may hold colons), or `HOST:SCOPE:UNIT` for a unit of an ssh host (`odroidn2:system:caddy.service`; the host is a plain ssh alias, no `user@`) |
| `service-failover-repair@.service` + `.d/20-limits.conf` | PC | oneshot template; `OnFailure=` reset (checked with `systemctl --user show`) so a failing repair cannot start a repair of the repair; `StartLimitIntervalSec=0`, because a fast crash loop of the watched unit starts its repair instance once per crash and systemd's default limit (5 starts in 10 s) refused the start that reports the final `failed` state (2026-11-10 22:55:09, `wireplumber.service`). The script ignores `service-failover-*` units as well. The template's drop-in directory belongs to the module: `install.sh --check` lists a drop-in it did not install as `stale` and an install removes it (a leftover smoke-test drop-in set `SERVICE_FAILOVER_DRY_RUN=1` and made every repair on the PC a dry run) |
| `bin/service-failover-repair` | PC | leaves a unit alone that is healthy (running, or a one-shot that finished with `Result=success`), still inside its restart backoff, stopped on purpose, transient, or one that exited with a status it declares final (`RestartPreventExitStatus=`) (only `failed`, or 10 or more restarts in `auto-restart`, counts as a failure); a vendor or generated unit (the distribution's, or from a generator) is handled like any other, and its brief tells the session not to edit the vendor file; first aid at most once per hour per unit (`restart --no-block`; `reset-failed` first only when `Result=start-limit-hit`, or after a restart that started nothing: same invocation, still `failed`, and the restart added a "Start request repeated too quickly" journal line (the original failure's line does not count; a failed dependency refuses a start the same way and must not trigger `reset-failed`), because `--no-block` exits 0 for it and the limit keeps `Result=exit-code`; `reset-failed` would otherwise hide a failure the restart cannot undo; wait 20 s; a start still in progress is left to systemd; a unit that ends `inactive` with `Result=success` after the restart counts as finished only with exit status 0, a clean stop signal (SIGHUP, SIGINT, SIGPIPE, SIGTERM) or a status the unit lists in SuccessExitStatus=, because a crashed service whose failed state was cleared keeps its crash status; one-shot jobs are left untouched because re-running them may repeat a side effect); if still down, writes a brief (host, scope, unit file, what was tried, a summary of the last 50 journal entries (see *Secrets*) fenced as data, instructions to diagnose, fix at the source, verify for 5 minutes, look for an existing ticket, report) and starts the sanctioned launcher `~/.agents/skills/rig-detached-omp/rig-detached-omp`. One session at a time (`flock`, inherited by the session), one per unit per 12 hours and 4 per 24 hours in total (stamp files in `~/.local/state/service-failover/launches`: systemd forgets a unit's counters once it unloads, so it cannot carry the limit). A session that hangs holds the lock until it ends: later repairs are deferred (the launcher gives the session no time limit), and each deferral is a journal notice (see below). Units stopped on purpose whose stop ended `failed` (a stop timeout) look like failures and are restarted once by first aid. **Tells the user** (see *What the user sees*) |
| `bin/service-failover-journal` | PC (next to the repair script) | builds what a brief or a notification says about a unit's journal and about the error text of systemctl/ssh, from a closed grammar, never copying text; see *Secrets*. python3 only |
| `bin/service-failover-notify-journal` | PC (next to the repair script) | the repair script's default notifier: every notice goes to the user journal (tag `service-failover`), and one that no repair session will act on (launcher missing or failed, daily cap) also to the owner via `tg --tag problem`; no desktop pop-up. See *What the user sees* |
| `service-failover-scan.timer` / `.service` / `bin/service-failover-scan` | PC, every 5 minutes | finds failed units and units in `auto-restart` with 10 or more restarts (a unit that never reaches `failed`, such as one with `StartLimitIntervalSec=0`, or any unit under the backoff) on this host and on each host of `~/.config/service-failover/hosts`, over ssh with no sudo, user and system units, and starts the repair template for each; at the end of every run it delivers the notices that are still pending (`service-failover-repair --flush-notifications`) |
| `docker/autoheal.compose.yaml` | both hosts, option `autoheal` | `willfarrell/autoheal` restarts a container whose healthcheck is `unhealthy` |

`Restart=` is deliberately **not** in the global drop-in: it would override `Restart=always` of units that have it,
and `Restart=always` is refused for `Type=oneshot` units (the unit would not load). Units that lack a `Restart=`
get `Restart=on-failure` from the module option `restart_units` (`<unit>.d/30-failover-restart.conf`).

The PC's `wyoming-whisper.service` (`StartLimitIntervalSec=0`) never reaches `failed`; the scan catches it as a restart
loop instead. Turning an option off removes what it installed: `--check` lists such files as `stale` and an install removes
them (disables the scan timer when `repair = false`; removes the `autoheal` container when `autoheal = false` is set
explicitly, and only a container docker compose created from a copy of this module's compose file, found by its config-file
label, never one that merely has the name, and by its id). A host that never sets the option (the odroid, run by hand with
`--autoheal`) keeps its container when the flag is left out. Options that are not valid JSON abort before anything is touched.
The container is `service-failover-autoheal` (compose service of the same name). The earlier copy of the file named both
`autoheal`; that container, when it carries this module's compose file label, is removed by id once the new one is up
(`docker compose up -d --wait`); if the new one does not start, the old one keeps running. An install that cannot confirm a
removal (the daemon went away) reports it and exits non-zero instead of claiming it. A host without a docker client has
nothing to remove, so `autoheal = false` is satisfied there.

A repair run that has not finished what it started leaves a file under `~/.local/state/service-failover/recheck/` (the
first aid writes it before it clears the unit's failed state), and the next scan starts that repair again, because the scan
itself lists only failed and restart-looping units and would never come back to a unit that first aid left inactive. The
file stays when the run is stopped by its start deadline (`TimeoutStartSec=` of the template), when the query after the first
aid fails (a host that does not answer: the user is told so as well, without journal text, since the journal is out of
reach too), and when the session lock defers the run; a run that launched a session, told the user that nothing more will be
tried, or found the unit healthy removes it. From the moment the launcher is called a stop signal is taken note of and not
acted on (the rest takes seconds), so a launch is never left without its stamp and its message. A unit whose name starts with a
dot (`.foo.service` is a valid name) is keyed with a leading `@` in those state files, so that no glob skips them as hidden
files; its launch and first aid stamps are also looked up under the old name.

## What the user sees

A failed service used to be silent: every outcome of the repair script was one journal line. When a unit is still down after the first
aid, `service-failover-repair` now sends a notice titled `Service down: <unit>` whose body
says what became of it (a repair session was launched, another session is running so this one is deferred, the daily cap of sessions is
reached, the launcher is missing or failed, the host did not answer after the first aid: that one has no journal text) and a `Summary:` of how the unit failed: `exited with status 78, Result=exit-code` from the
properties read before first aid, then what the journal says (see *Secrets*: the last failure the manager itself recorded, for example
`Main process exited, code=exited, status=78/CONFIG`, how many lines the unit's own output had, and the table members recognised in them;
`-- No entries --` and ssh errors are no log line, and then the text is `(no log line)`; a journal that cannot be read says so), 300
bytes of journal at most. A unit is announced once per `NOTIFY_MIN` minutes
(default 720, the same window as the session cooldown): a stamp file in `~/.local/state/service-failover/notify/sent`. The notice that the
host did not answer after the first aid is provisional (the scan keeps looking) and has a stamp of its own, so it never hides the
message that says what finally became of the unit. It is shown only while the unit's recheck file exists: a later run that found
or said what became of the unit takes that file away, and the delivery then drops the notice, whether it still waits for the
notifier or a delivery has just claimed it. (A notifier call already in progress cannot be taken back.) A message the
notifier cannot deliver (the journal socket or Telegram is not reachable) stays in `notify/pending` and leaves no stamp; the scan delivers it
within 5 minutes, unless it is older than `NOTIFY_MIN` (it would announce, hours late, a unit that has had its chance to be fixed since:
it is dropped). Runs overlap (one repair instance per failed unit, plus the scan), so a delivery first claims a message by renaming it to
`NAME.sending.PID`: one run delivers it, the others skip it. A unit that first aid brings back is not announced.

### Where the notice goes: the journal, and Telegram for what no session will fix

The notices are for agents, not for the owner's desktop: the owner ordered the desktop pop-ups gone (2026-11-19,
[#226](https://github.com/alex-mextner/.files/issues/226)). The repair script decides who a notice is for; the default notifier,
`bin/service-failover-notify-journal` (installed next to the repair script, called with notify-send's arguments), only carries it:

- every notice goes to the user journal, tag `service-failover`, priority `crit`: `journalctl --user -t service-failover`;
- a notice that means no repair session is acting on the unit right now (the launcher is missing or failed, the daily cap is reached, or a
  deferral that nothing retries: "restart it by hand") is queued a second time as `<key>+owner` and handed over with `--category=owner`;
  the notifier sends that copy to the owner with `tg --tag problem`, in the problem-report format (context, what is blocked, what was
  tried, what is being done, where to look). Nobody else reads that journal tag. The owner copy has its own stamp, so a journal notice of
  the same unit earlier in the window (a deferral) never hides it, and the owner hears of a unit once per `NOTIFY_MIN` window; an owner
  copy that is due always brings its journal line, so the journal holds what the owner was told.
- the outcome says what happens next, and the owner's message follows it: while the scan still sees the unit (it is failed or
  restart-looping) it requests a repair every 5 minutes and a session starts on its own once the cap frees a slot or the launcher works
  again, so the message says what blocks it and warns against starting a second session by hand; only when nothing will retry the unit
  does it ask the owner to restart it by hand.

The two copies are delivered and retried apart: journal messages first, owner messages after them, and a failure stops further attempts
on its own channel only. A Telegram outage, or a send that hangs, leaves the owner copies pending while the journal notices of every unit
are still written (once each: a Telegram retry writes no second journal line). A Telegram send can wait on `tg`'s online check for more
than 10 seconds under load, so one delivery (all notifier calls of one run together) may take `SERVICE_FAILOVER_NOTIFY_TIMEOUT` seconds
(default 90; each call is stopped with `timeout -k 5`); what does not fit waits for the next run. The units' start deadlines leave room
for that: `TimeoutStartSec=240` for the repair template (120 before), `300` for the scan (180 before). The notifier runs without the
session lock's descriptor, so nothing it leaves behind can hold the lock, and a failed delivery logs the notifier's exit status.
`SERVICE_FAILOVER_NOTIFY` still names another notifier called like `notify-send` (a smoke test, another channel; notify-send would show an
owner notice twice, once per copy), and `SERVICE_FAILOVER_TG` another `tg` command. Nothing of the module needs `notify-send` or
`libnotify-bin` any more. `tg` (installed by rig) is a Bun script: it and the interpreter its `#!` line names must resolve on the units'
`PATH` (`~/.local/bin`, `~/.bun/bin`, Linuxbrew, ...; `install.sh` reads it from the scan unit), or `install.sh --check` reports drift and
an install says `needs manual` (exit 10), since without it nobody is told of a unit no session is acting on. A deferral that the scan
retries ("another repair session is running, deferred") stays a journal notice: a session that hangs for long is seen there, not on the
phone.

The pop-ups were first switched off on 2026-11-19 by hand, in the installed copy only (an edited repair script, the notifier and two
`30-notify-journal.conf` drop-ins), and misfiled as [ubuntu-tweaks#261](https://github.com/alex-mextner/ubuntu-tweaks/issues/261). The
installer removes a drop-in it did not declare in a directory named after a module unit (see below), so those drop-ins go with the first
install of this version, whose units and repair script carry the same settings.

`install.sh --check` also reports as `stale` (an install removes it) any file or symlink in a drop-in directory named after a module unit
(`service-failover-*.d/`: the repair template's, the scan's, one for a single instance) that the module did not install, and a
`SERVICE_FAILOVER_*` variable in the user manager's environment (`systemctl --user set-environment`; an install unsets it): each one can turn
every repair into a dry run, and a file or a variable nobody declared is exactly what the module exists to find.

### Incident: wireplumber.service, 2026-10-31 to 2026-11-11

`wireplumber.service` exited with status 78 on every start (a hand-installed `/usr/local` portal configuration named a module the
distribution's wireplumber does not search), the unit hit its start limit and no audio device existed. The module was installed on
2026-11-10 16:11, so nothing before that date could have caught it; after that:

1. The repair script saw the unit `failed` (2026-11-11 01:36:50) and logged `vendor or generated unit (/usr/lib/systemd/user/wireplumber.service),
   not repaired`: no first aid, no session, no message. The vendor rule is gone.
2. On 2026-11-10 22:55:09 systemd refused to start the repair instance for `wireplumber.service` itself (`Start request repeated too quickly`):
   the instance had the default start limit. It has none now.
3. The live repair template carried a drop-in `90-smoke-dry.conf` (`SERVICE_FAILOVER_DRY_RUN=1`) that this module never installed: every
   session the scan requested on the PC was a dry run (`DRY RUN: would launch`). The installer now owns the template's drop-in directory.
4. Nothing reached the user. See above.

Known gap: the failed state of `wireplumber.service` was gone three seconds after it appeared (`StateChangeTimestamp` 01:36:52, now
`ActiveState=inactive`, `Result=success`, `ExecMainStatus=78`). The unit has `BindsTo=pipewire.service`, so a stop of pipewire would do that;
what did it was not established. The scan lists only `failed` units, so a failure whose state is erased before the next scan is seen only by the
`OnFailure=` run at the moment of failure, which is why that run sends the notification itself. The `audio-health` module of ubuntu-tweaks is the
independent check for this loss: it asks PipeWire, not systemd, whether a card exists.

## Secrets

The brief goes to an autonomous agent session and the notice to the journal and, for some, to Telegram; the journal of a failed service may hold any
credential in any shape. Masking by patterns never converged (ten review rounds of #197 each found one more shape), so nothing is
masked: `bin/service-failover-journal` (python3; `journalctl -b -o json`, a few fields) **builds** what the brief and the notification
say, and every part of it comes from a closed grammar G: the unit name the script passed, integers in checked ranges, the timestamps
journald sets, members of fixed tables (unit and job results, exit and signal names, errno names and texts, exception class names)
and the program's own sentences. The input chooses which members of G are written, never what they are.

- An entry the **manager** wrote (journald's `_COMM`/`_EXE` say systemd's binary and `_PID` is the manager's own: 1 for the system
  manager, the user manager's process for user units, read from `$MANAGERPID` or `user@UID.service`; the `SYSLOG_IDENTIFIER` a service
  chooses is not looked at, and a process of the same user can run systemd's binary under a name of its own, but cannot write another
  process's id) is kept only when it fits a known message (`Main process exited, code=..., status=N/NAME`, `Failed with result '...'`,
  `Scheduled restart job, restart counter is at N`, `Start request repeated too quickly`, `Failed to start UNIT`, a timeout, the OOM
  killer, ...) naming the unit, and is rebuilt from the message's pieces (a number up to 255, a word of a table): a unit's description and
  anything around the pieces is dropped, a word that is in no table is dropped from the line. Other entries of the manager are counted.
- Of the unit's own output **no text is copied**. The brief says how many lines there were (and how many at error priority) and lists the
  table members found in them (`ECONNREFUSED`, `Address already in use`, `ConnectionError`), labelled as hints the service reported:
  the service can suggest a member, not write a word. The session reads the rest with `journalctl` on the host; the brief names the
  exact command for the unit's scope and host.
- What `systemctl` and ssh printed (in "what was tried" and in the repair log) becomes one fixed phrase (`permission refused`, `host
  unreachable`, `unit not found`, `timed out`, ...; `reason withheld` when none fits). The states, results and numbers read from
  `systemctl show` keep only the characters of their own kind.
- The cause in the notification comes from the properties read **before** first aid (`exited with status 78, Result=exit-code`), and the
  brief and the notification read the journal once (same boot, last 50 entries). A start limit counts as hit only when the manager's own
  entry says so, not a line of the unit's output (`journal limit-hit`).
- Failing closed: when python3 or the helper is missing, or the probe timeout (30 s) stops it, nothing comes out (the brief says
  `(no journal summary...)`, the notification `(no log line)`); a journalctl that cannot be read says `exit N`. A line of the service's
  output is scanned for table members up to 4096 characters (cut at a space, ANSI colour sequences removed), an entry over 1 MiB is not
  read, and neither are more than 1000 entries or 16 MiB.

**Review rule for this part:** a finding blocks only when some input makes an output line leave G (the grammar is written out in
`JournalTest.grammar` of the tests, which fill every slot of every known message with text of the slot's alphabet and feed the corpus of all
shapes found in review). A finding that needs a contrived input, or that only says the summary is less useful, is recorded in #197 as
hardening. Limits that remain: a unit name that is itself a secret is shown (the brief names the unit anyway); a secret that equals a table
member is listed as that member; the agent's own reads of the journal send the raw lines into its session (the brief protects the brief,
the notification and the prompt, not the model provider).

## Why odroidn2 is scanned from the PC

The odroid has no omp, no `tg` and no launcher, and no ssh key to the PC. `OnFailure=` there could not start a
session, so the odroid gets only the restart backoff (`--restart-only`), and the PC's scan reads its failed units over
ssh every 5 minutes. The repair session runs on the PC and reaches the odroid with `ssh odroidn2`. It has no sudo
there: whatever needs root goes into a ticket with the exact command.

## Install

PC (profile module, `[modules.service-failover] enabled = true` in `machines/ubuntu-pc/machine.toml`):
`ubuntu-tweak apply --commit`, or by hand `bash install.sh` (`--plan`, `--check` are read-only). It writes user files,
runs `daemon-reload`, enables the scan timer and starts it again when it is enabled but stopped. It restarts nothing: a unit
takes the new backoff at its next start. A repair run bounds every `systemctl`/`journalctl`/ssh query (30 s) and
closes its lock descriptor for them, so a hung host cannot keep the one-session lock. The install holds one lock,
`~/.cache/service-failover-install.lock`, whatever `XDG_RUNTIME_DIR` is (a login session has one and an ssh session has
not); an install that cannot create or open it stops before it changes anything.

Odroid, user level, once (no repository checkout there, so copy the module):

```sh
rsync -a --delete ubuntu/modules/service-failover/ odroidn2:.local/share/service-failover/
ssh odroidn2 'bash ~/.local/share/service-failover/install.sh --restart-only --autoheal'
```

### What the owner must run with sudo (odroid, once)

The system units live in `/etc/systemd/system`, owned by root. This installs the same backoff for every system
service, `Restart=on-failure` for `caddy.service` (it has no `Restart=`), and for the two units that must never be
refused a start a drop-in that sets everything explicitly; nothing is restarted:

```sh
ssh odroidn2
sudo bash ~/.local/share/service-failover/install.sh --system caddy.service \
  ha-host-actions.service=always hcb-odroid-watchdog.service=on-failure
```

`UNIT=always` / `UNIT=on-failure` writes `<unit>.d/30-failover-restart.conf` with `StartLimitIntervalSec=0`,
`Restart=<policy>`, `RestartSec=2`, `RestartSteps=8`, `RestartMaxDelaySec=5min`. Every value is set in the drop-in
because a drop-in beats the unit file and the unit file's own values (`ha-host-actions.service` had
`StartLimitIntervalSec=infinity`, `hcb-odroid-watchdog.service` had the default 5 starts in 10 s) are what failed:

- `infinity` never forgets a start: the unit is refused for good after `StartLimitBurst` starts in its whole life (the
  2026-11-09 outage above). It is not "no limit"; `0` is.
- The default 10 s / 5 starts refuses a fast crash loop or a burst of manual starts, and the refused unit stays `failed` with
  `Result=start-limit-hit` until `reset-failed` or a later start once the window has passed. `hcb-odroid-watchdog.service` is a
  `Type=oneshot` started by a timer every 2 minutes and was seen in that state (#186). `Restart=always` is refused for a
  one-shot unit, hence `on-failure` there.

`systemctl show` prints a disabled limit as `StartLimitIntervalUSec=0` (`infinity` for an infinite window), with
`StartLimitBurst=5` left over and unused. Afterwards `systemctl cat ha-host-actions.service` lists
`30-failover-restart.conf`. A unit that is already `failed` is cleared with `sudo systemctl reset-failed <unit>`; that starts nothing:
a timer-driven unit runs again at its next tick, a service needs `sudo systemctl start <unit>` (`Restart=` acts only on a unit that
was running). The odroid's system `ha-host-actions.service` once failed against the
user unit that holds port 8787: decide which of the two is canonical and disable the other.

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
