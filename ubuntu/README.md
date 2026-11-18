# ubuntu/

Personal data store for the `ubuntu-tweak` engine
([`alex-mextner/ubuntu-tweaks`](https://github.com/alex-mextner/ubuntu-tweaks), public).
This directory holds only personal settings; the engine code lives in that repo, never here.
The mac-era dotfiles at the root of `.files` (zsh, zplug, brew) are not touched or managed by it.

The directory is managed by the engine: `capture` writes into it and `apply` reads from it.
Files under `files/` are the symlink targets of the live config, so editing
`ubuntu/files/.config/foo` edits `~/.config/foo` directly.

## Layout

```
ubuntu/
  README.md
  profile.toml              # shared config: modules, capture rules
  coverage.toml  coverage.d/*.toml   # [[coverage]] rules: what is tracked, excluded, secret or ticketed (see Coverage)
  settings/**/*.ini         # settings bundles: dconf keyfiles named by purpose (dev-keyboard.ini, laptop/power.ini), shared by all machines
  files/                    # mirrors $HOME: files/.config/mimeapps.list -> ~/.config/mimeapps.list
  packages.toml             # apt / snap / flatpak / brew / uv / npm / bun / cargo names, each with a `# reason`
  units.toml                # systemd user units to enable (their files are leaves of files/.config/systemd/user)
  system.toml               # root-level: timezone, locale, console keyboard
  machines/<hostname>/      # overlay, one dir per machine (hostname = /etc/hostname, or --machine)
    machine.toml            # same schema as profile.toml; wins over it (no [machine] table: hostname = dir name)
    settings/**/*.ini       # key-level overlay bundles on top of the shared ones
    files/                  # path-level overlay: same relative path wins over shared files/
    packages.toml  system.toml  units.toml   # same schema, merged (lists union, scalars replace)
```

Only `profile.toml` and `machines/home/machine.toml` exist initially; the rest appears
when `ubuntu-tweak capture --commit` writes it (by default one bundle per Settings panel:
`settings/keyboard.ini`, `settings/appearance.ini`, ...).

## Settings bundles and reasons

Any `settings/**/*.ini` file is a *bundle*: one file per purpose, holding keys from any dconf path,
merged with the other bundles at load (the idea is Home Assistant's `packages:`). The name is the
path without `.ini`; `machines/<hostname>/settings/**/*.ini` are bundles of the overlay and win key by
key. The same key with different values in two bundles of one layer is an error (exit 2, both
files and lines named); identical values only warn. `ubuntu-tweak bundles` shows what applies here
and the shadowed / duplicated / conflicting keys.

```ini
# settings/dev-keyboard.ini
# optional header, not a dconf section:
[ubuntu-tweak]
description = "Keys for working on the dev keyboard"
machines = ["home"]             # apply only on these hostnames (also: requires-module = "hyperdock")

[org/gnome/desktop/wm/keybindings]
# Super+arrows belong to the tiler, so workspaces move on Alt   <- the reason: the comment above a key
switch-to-workspace-left=['<Super><Alt>Left']
```

A reason is the full-line `#` comment directly above a key (or, in `packages.toml`, a trailing
comment: `"tmux",  # shell tools`). `apply` prints it next to the key, `status` shows it for modified
keys, and `capture --commit` rewrites only the changed values, keeping every comment and the order.
`ubuntu-tweak note <bundle>:<dconf key> "reason"` (also `packages:apt:<name>`, `system:<key>`) adds one;
`ubuntu-tweak bundle-move <bundle> '<key-or-glob>'...` regroups keys with their comments. Both are
previews until `--commit`.

Capture only writes values you changed (never schema defaults). Keys the system or an app registered
by itself (notification app ids, Ptyxis profile uuids, input-source MRU order, ...) are listed in an
`auto` section of the preview instead; promote any of them with `[capture] include`.
`ubuntu-tweak capture --dry-run` is the explicit no-write preview (SUMMARY block first, `--summary`
prints only that).

`packages.toml` takes the arrays `apt`, `snap`, `snap_classic`, `flatpak`, `brew` and the user-level tool lists
`uv`, `npm`, `bun`, `cargo`, `pipx` (install specs: `"glances[web]"`, `"name @ git+https://..."`), plus two optional
scalars: `flatpak_remote = "flathub"` (the remote flatpaks install from) and `apt_update = true` (run
`apt-get update` before the apt batch). `units.toml` lists the systemd user units that must be enabled; a unit stays out
of it while the code it runs is not reproduced by the profile (https://github.com/alex-mextner/.files/issues/58), though its
file is still linked.

## Fresh machine bootstrap

```sh
gh repo clone alex-mextner/.files ~/.files
git clone --recurse-submodules https://github.com/alex-mextner/ubuntu-tweaks ~/xp/ubuntu-tweaks
~/xp/ubuntu-tweaks/bin/ubuntu-tweak init      # doctor -> submodules -> apply preview -> --commit
```

`init` also symlinks the CLI into `~/.local/bin/ubuntu-tweak` (only at `--commit`), which is what makes the
bare `ubuntu-tweak` in the daily workflow work. Until then call it by full path, or link it yourself
(make sure `~/.local/bin` is on `PATH`):

```sh
mkdir -p ~/.local/bin && ln -sfnT ~/xp/ubuntu-tweaks/bin/ubuntu-tweak ~/.local/bin/ubuntu-tweak
```

The profile is found at `~/.files/ubuntu`; override with `--profile DIR` or `$UBUNTU_TWEAK_PROFILE`.

## Daily workflow

Every mutating command previews by default; only `--commit` changes anything. The loop is preview, commit, review the git diff here, PR.

On the machine where you changed settings:

1. `ubuntu-tweak capture` (or `capture --dry-run`) - preview of exactly what the profile would receive.
2. `ubuntu-tweak capture --commit` - writes it into this working tree (no git commit).
3. Review `git diff` in `~/.files`, then commit and open a PR. Regroup with `bundle-move`, annotate
   with `note`.

On another machine, after pulling:

1. `ubuntu-tweak apply` - preview of the plan.
2. `ubuntu-tweak apply --commit` - applies it.

## Per-machine overlays

Merge order, last wins: module defaults < shared profile < `machines/<hostname>/`.
`profile.toml` + `machine.toml`: tables merge, scalars replace, **lists replace** (a machine's
`[capture] exclude = [...]` replaces the shared list, it does not extend it). `packages.toml` and
`system.toml` (shared + overlay): lists **union**, scalars replace. A machine without an overlay
directory uses the shared files only.

### `[capture]` and the engine defaults

The engine ships its own capture rules (`ubuntu_tweaks/capture/defaults.toml`); the profile's
`[capture]` table (after the merge above) is evaluated together with them, not instead:

- `exclude`: checked first and always wins; it adds to the built-in secret/identity/hardware excludes
  and can never weaken them.
- `machine`: adds to the built-in machine rules (power, touchpad, mouse, audio device keys), so
  listing a key there only routes it to `machines/<hostname>/`; defaults keep working.
- `include`: globs that lift the `state` and `auto` skips for the keys they match.
- `bundles`: a table, bundle name -> dconf globs; where *new* keys go (first matching entry wins).
  A key that a bundle already holds stays there; with no match a key goes to its panel's bundle.
  Tables merge, so `machine.toml` may add entries; a bundle name present in both replaces its list.
- `include_files` / `machine_files`: extend the built-in file allow-list (the second adopts into
  `machines/<hostname>/files/`).
- Unknown keys under `[capture]` are a config error.

Because lists replace between `profile.toml` and `machine.toml`, a machine that sets its own
`[capture] machine = [...]` must repeat any shared entries it still wants.

Example: power keys differ between a laptop and a desktop. `capture` routes the power plugin
keys to the machine overlay through the engine's built-in default `machine` rules (extend them with `[capture] machine` in `profile.toml`), so each machine keeps its own:

```ini
# machines/laptop/settings/power.ini
[org/gnome/settings-daemon/plugins/power]
sleep-inactive-battery-type='suspend'
sleep-inactive-battery-timeout=900

# machines/home/settings/power.ini  (desktop: never suspend)
[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
```

Shared keys in `settings/power.ini` (if any) are overridden key by key; keys absent from the
overlay keep the shared value. The same applies to `files/` (same relative path wins) and
to modules (`[modules.memguard] enabled = false` in a machine's `machine.toml`).

## Coverage

`ubuntu-tweak coverage` scans everything on the machine that holds configuration (dconf keys, files under
`$HOME`, apt/snap/flatpak and tool packages, systemd user units, `/etc`, GNOME extensions) and puts every finding
in one of four classes. The profile's goal is that nothing stays unaccounted for:

| Class | Meaning | How a finding gets there |
|---|---|---|
| `tracked` | declared here and reconciled by `apply` | a key in `settings/**/*.ini`, a file under `files/`, a name in `packages.toml`, an item of a module, or a `[[coverage]] class = "tracked"` rule |
| `excluded` | not configuration, with a written reason (cache, state, generated, machine-bound) | a `[[coverage]]` rule with `reason`, or an engine default |
| `secret` | a credential; the reason says where it lives and how it is restored, never its content | a `[[coverage]]` rule with `reason` |
| `untracked` | cannot be declared yet; allowed only with a `ticket` (counted as `N untracked (M ticketed)`) | a `[[coverage]] class = "untracked"` rule with `ticket` and `reason` |

Rules are `[[coverage]]` tables (`match` = globs of item ids such as `dconf:/org/...`, `file:~/.config/x`,
`etc:/etc/...`, `unit:x.service`, `apt:name`, `extension:uuid`). They live in modules' `module.toml`,
`coverage.toml` and `coverage.d/*.toml` (one file per topic, so authors do not conflict), and in
`machines/<hostname>/coverage.toml` and `machines/<hostname>/coverage.d/*.toml`; machine beats profile beats
module beats engine defaults, and a declared item is tracked whatever a rule says. Current files:
`coverage.d/dconf.toml` (dock keys that the hyperdock module migrates; the RDP TLS paths as secret, with the restore steps), `coverage.d/etc.toml` (stock grub; the two
root-only `/etc` areas, ticketed), `machines/ubuntu-pc/coverage.d/extensions.toml` (the gsconnect claim).

Two gates:

- **On the machine (the live gate).** `ubuntu-tweak coverage` exits 0 when no untracked finding lacks a ticket;
  `ubuntu-tweak status` ends with the same summary line, and the autoheal timer re-runs `apply`, which
  reconciles everything tracked. The scan needs the live machine (its dconf, `$HOME`, packages, units, `/etc`), so
  it cannot run on a CI runner.
- **In CI (the static gate).** `.github/workflows/coverage.yml` runs
  `ubuntu-tweak coverage --check-profile --online --profile ubuntu` of the engine (checked out at a pinned commit)
  on every PR and push that touches `ubuntu/`. It scans no machine; it fails (exit 2, every problem listed) when a
  rule file does not parse, an `excluded` or `secret` rule has no reason, an `untracked` rule has no ticket or no
  reason, a glob cannot match any item id, a pattern is listed twice, or a ticket is missing or closed. Bump the
  engine commit in the workflow together with the profile when an engine change is wanted.

Open engine gaps the profile waits on: root-only `/etc` areas cannot be classified
(alex-mextner/ubuntu-tweaks#100), and the extensions adapter does not claim an extension a builtin module enables
(alex-mextner/ubuntu-tweaks#101; remove the gsconnect rule when it lands).

## Manual steps

What only a human can do. `ubuntu-tweak apply` never prompts, stores a secret or touches hardware; every
step below is also reported as `needs manual` by the module that owns it, and `ubuntu-tweak status` keeps it
Missing until it is done. After a step, run `ubuntu-tweak apply --commit` again.

**One command for all of it: `finish-setup`** (also the "Finish setup" / "Завершить настройку" entry in the app grid, which
opens a terminal). It is the profile's launcher of `ubuntu-tweak finish`: it lists what is still left on this machine in
the order it can be done (the engine's steps from *Manual steps* of the engine spec, the `[[finish]]` steps of the profile's
own modules and the machine's `[[finish.steps]]`), runs the machine part of each step itself and checks the result; run it
again whenever it says something is left. `finish-setup --dry-run` only prints the list. The rows below name what each step
does by hand. The PC's own values are in `machines/ubuntu-pc/machine.toml` (`[finish.apply]`: `apply` waits while a pull
request of the dock repository is open; `build-server-cleanup`: two scratch directories on the build server).

| Step | Module / owner | Exact command or action |
|---|---|---|
| root part of a fresh machine (apt packages, ssh server, polkit, Firefox policy, memguard, mounts) | engine (`apply --commit`) | `ubuntu-tweak apply --commit` from a normal terminal: it asks for the sudo password once and runs every root action. An agent shell with `no_new_privs` cannot, so it lists the root remainder and exits 6 |
| iCloud sign-in (Apple ID password, trusted-device code, device passcode) | `passwords-app` | `passwords-app signin`, then `ubuntu-tweak status --only passwords-app` |
| ChatGPT sign-in of the glide.vc account in its guardian browser (the profile of the other account is signed in) | `chatgpt-guardian` | `finish-setup --step glide-chatgpt-login`, which runs `modules/chatgpt-guardian/relogin run`: it stops the glide guardian units, opens that profile in a visible Chrome window, waits until you have signed in and closed it, starts the units again and checks them. `relogin check` tells without asking whether the glide.vc account is signed in (through the browser's DevTools port, else the marker `~/.local/state/ubuntu-tweaks/chatgpt-guardian-glide-login`) |
| optional: keep the guardian profiles signed in from the dock Chrome (instead of typing the password into a guardian browser) | `chatgpt-guardian` (opt-in, apply never runs it) | `chatgpt-guardian-profile-sync` (written to `~/.local/bin` by apply; the file is `modules/chatgpt-guardian/profile-sync`). Workflow: sign in to ChatGPT in the dock Chrome with the account the guardian should use, **close that Chrome**, then `chatgpt-guardian-profile-sync pull home` (or `pull glide`); `--dry-run` shows the plan and changes nothing. `pull` takes the per-profile lock that `relogin run` takes too, stops that workspace's guardian units, backs the target `Cookies` database up (`~/.local/state/ubuntu-tweaks/chatgpt-guardian-profile-sync/<workspace>/`, mode 0600, newest three kept), replaces only the chatgpt.com and openai.com cookie rows (through the sqlite backup API, one transaction; no value is printed), starts the units and asks the browser through DevTools which account is signed in (glide: the `glide_email_domain`; home: the `home_email_domain` when the machine declares one, else a visible email domain other than the glide one, or exactly `--expect-domain`; the home guardian unit refuses to start for any other domain, so `--expect-domain` may only repeat a declared one); the observer and the guardian start only after the browser alone showed the right account; when that is not confirmed it restores the backup (exit 4). `chatgpt-guardian-profile-sync status` shows, read-only, the account domain of each guardian browser and whether the dock Chrome is closed. Exit codes: 0 ok, 1 failed (target unchanged or restored), 2 usage/config, 3 a Chrome still holds the source or target profile or another pull runs, 4 not confirmed and rolled back, 5 restore failed (no unit is started again and the backup path is printed). The dock profile holds ONE ChatGPT account at a time, so pull `home`, sign in to the other account, pull `glide`. Limits: this moves live session cookies between profiles of the same user (the same Chrome binary and cookie key are what lets a copied row decrypt); a session that ChatGPT or Cloudflare bound to the browser instance may be rejected, and ChatGPT may sign out the other copy of a session, so a later pull can be needed; if the guardian Chrome cannot decrypt a copied row the browser shows ChatGPT signed out, the check fails and the backup comes back. Local Storage is not copied (the sign-in is the session cookie) |
| two scratch directories on the build server | `machines/ubuntu-pc` (optional) | `finish-setup --step build-server-cleanup`: shows the two paths on `ultra@65.21.40.51`, asks y/N, removes them |
| ChatGPT to this PC's local tools: Cloudflare login, tunnel and DNS name of the dc-mcp connector (Desktop Commander MCP behind an OAuth proxy) | `dc-mcp` (opt-in per machine: `[modules.dc-mcp] enabled = true`, `hostname = "..."`, optional `tunnel`, `port`, `shim_port`, `metrics_port` in `machines/<host>/machine.toml`) | A thin installer over `computer chatgpt`: `apply --commit` installs the `computer` CLI with `uv tool install` at the commit pinned in `modules/dc-mcp/install.sh` (alex-mextner/computer-use-plugin), writes the module's keys into `~/.config/computer/chatgpt/config.toml` and runs `computer mcp install` and `computer chatgpt init`, which installs the pinned, checksum-verified `mcp-auth-proxy` and `cloudflared`, keeps or generates the password in `~/.config/dc-mcp-auth.env` (mode 600, never printed) and writes `dc-mcp-bridge.service`, `dc-mcp-shim.service` and `dc-mcp-tunnel.service`; `init` starts nothing, but when it changed the unit text, apply **restarts the dc-mcp units that are already running** (bridge, shim, then tunnel) so they run the new text; stopped units stay stopped and an unchanged machine is not restarted. A machine set up by the first version of the module gets its Cloudflare login and tunnel credentials copied from `~/.cloudflared`. Then `finish-setup --step dc-mcp-cloudflare` runs `computer chatgpt login` (browser; tunnel and DNS route), `computer chatgpt start` and prints the connector fields. In ChatGPT: Settings, Connectors, custom MCP server, URL `https://<hostname>/mcp`, Authentication OAuth, log in with the password from `~/.config/dc-mcp-auth.env`. **One hostname per machine**: two machines serving the same name split the requests, so each machine sets its own `hostname`. Logs for "did ChatGPT reach this computer, and which tool ran": `computer chatgpt logs` reads the shim's rotated access log `~/.local/state/dc-mcp/access.log`, and the daily `dc-mcp-tool-log-prune.timer` keeps Desktop Commander's own rotated `claude_tool_call_*.log` files to the newest 5 within 30 days. Secrets (`~/.config/dc-mcp-auth.env`, `~/.config/computer/chatgpt/cloudflared/`, `~/.local/share/mcp-auth-proxy`) are listed in `coverage.d/secrets.toml`. Not the guardian tunnel below: other units and credentials |
| ChatGPT window launcher (`chatgpt.desktop`, icon `chatgpt-window`, script `chatgpt-window`) | `files/` (`apply` links them; no step) | uses `google-chrome-stable` when installed, else the rootless Chrome of the `chatgpt-guardian` module (`chrome = "user"`); sign in to ChatGPT once in that window (its profile `~/.config/chatgpt-app` is a secret, never in the repository) |
| the guardian tunnel (ChatGPT to a local MCP) | not declared | needs a platform API key with Tunnels Read+Use; without it only the ChatGPT-to-local `pm chatgpt mcp` connector is missing. Store the key with `keychain set <name>`; the key is never in this repository |
| the Bluetooth dongle of the ODROID and its HDMI cable | hardware (Home Assistant, Kodi) | re-plug the dongle, plug HDMI into the ODROID; then Home Assistant, Developer tools, States, filter `unavailable` shows no leftovers |
| GitHub merge policy of this repository, and the settings GitHub does not offer on this plan | rig (`rig.yaml` `github:`) | merge policy and Actions: `cd ~/.files && rig apply commit` with the admin token of `gh` (the user has admin on this repository). Rulesets, secret scanning with push protection and CodeQL default setup are plan-gated for a private repository on the free plan (`rig.yaml` explains): they converge by themselves with `rig apply commit` once the repository is public or the plan is upgraded |
| tg-cli bot credentials, AMO signing key, Home Assistant token | `tg-cli`, `firefox`, `firefox-passkey-qr` | exact commands are in each module's `manual` text and in the engine spec, *Manual steps*: `tg auth login`, `secret-tool store --label='AMO JWT issuer' application ubuntu-tweaks purpose amo-jwt-issuer` (and `amo-jwt-secret`), `secret-tool store --label='Home Assistant token' application ubuntu-tweaks purpose ha-token` |
| Spoolman and the PrintOps client of 3d-cli (`~/.config/3d-cli/printops.yaml`: `api:` and `token:`) | `3d-cli` | `apply` links `~/.local/bin/3d` to the checkout at `~/xp/3d-cli` (nothing happens without one) and, while the client is not configured, runs only `3d init --env --no-input --spoolman auto`: it never prompts, installs or deploys, it prints where things stand and the command to run. The decision stays yours: `3d init --env` asks where Spoolman should live (a configured printer, this computer, a server over ssh, or a script to copy to a server by hand). A machine that does not print declines it with `[modules.3d-cli] spoolman = "skip"` in its `machine.toml` |
| AISIS voice stack (adapter, STT, PM, embed, kit, LAN proxy, HA CLI, clip feed, dashboard, failover, whisper-prompt timer, wyoming-whisper) | `aisis` (opt-in per machine: `[modules.aisis] enabled = true`, set in `machines/ubuntu-pc/machine.toml`: its drop-ins are this PC's, a CUDA Whisper and the `~/xp` backups-mount gate) | `apply --commit` clones `alex-mextner/aisis` to `~/xp/aisis` when it is missing (an existing checkout is never pulled) and runs its `scripts/install-systemd-units.sh`, which copies the units and drop-ins versioned there into `~/.config/systemd/user`, enables them and starts what is not running (a running unit is never restarted). The units are not kept in this repository. What the repositories do not hold is the runtime state: restore `~/.local/share/aisis` (venvs, models) and `~/.config/aisis` (`runtime.env`, `fish.key`, per-machine files: secrets) from backup, then run apply again; units that lack it fail to start and are retried by systemd |

Browser logins (Firefox, Chrome profiles of this user, 1Password) are state, not configuration: they are
never in this repository; sign in once on a new machine.

## Not applicable on Linux

Things the macOS machine did that have no meaning here. Each is declared, not forgotten: where a tool owns the
concept, that tool already reports it as unsupported off macOS (`rig status`, `rig activate` say so).

| Item | Why it does not apply | Linux equivalent |
|---|---|---|
| rig `tg_ctl` LaunchAgent (`ai.hyperide.tg-ctl`) | launchd; `rig status`: "unsupported (macOS-only; no-op off darwin)" | profile module `tg-cli`: the systemd user unit `agenttools-tg-ctl-tg-ctl.service` |
| rig `tgctl_reaper` (`com.hyperide.tgctl-omp-acp-reaper`) | launchd StartInterval sweep of orphan omp/ACP processes; rig: macOS-only | systemd stops the whole cgroup of a unit on restart, so the daemon leaves no orphans of its own |
| rig `tcc_watch` (`ai.hyperide.tcc-watch`) | watches macOS TCC permission dialogs; rig: macOS-only | none needed: Linux has no TCC |
| rig `spotlight` sweep (`ai.hyperide.spotlight-exclude`, `.metadata_never_index`) | keeps the Spotlight indexer out of dependency directories | none needed; the ignore entry stays in the global git ignore, harmlessly |
| rig `tmux.boot` / `tmux.autosave` (`ai.hyperide.tmux-boot`, `ai.hyperide.tmux-autosave`, `com.ultra.ctmux-ensure`) | launchd agents; switching them on would make `rig status` expect a plist that cannot exist here | profile module `tmux-boot` starts the server at boot, and its `tmux-autosave.timer` fires tmux-continuum's autosave every 5 minutes (a save every 15) independently of tmux's status line: continuum's own trigger lives in `status-right`, which the Moshi client wipes on every attach |
| `com.ultra.container-gc` | its script (`container-gc`, in the Mac copy of `.files/bin`) stops the builder of Apple's `container` CLI and prunes its storage | none: Docker (`docker.io`) is what this PC runs, and no job for it existed on the Mac |
| `com.alex.tuya-ir-reminder` | a one-shot reminder (`tuya-ir-reminder.sh`) that deletes its own plist after it fired | none |
| `com.alex.rustdesk-autostart`, `app.openremotecommander.*`, `com.alex.openremotecommander.*` | macOS remote-control apps | `ssh-server` module, mosh, GNOME Remote Desktop (RDP is on in this PC's dconf) and GSConnect |
| `com.openssh.ssh-agent`, `com.apple.*`, `com.google.*` updater agents | the macOS system and Google's macOS updater | the GNOME session's `gcr-ssh-agent`; apt keeps Chrome current |
| AeroSpace (`~/.aerospace.toml`) | a tiling window manager for macOS; the file in the Mac copy is AeroSpace's unmodified sample config | GNOME's tiling-assistant keys in `settings/` and the Ptyxis/ctmux key bindings |
| macOS Dock, Finder and system preference plists (`com.apple.*.plist`) | not on the Mac disk at all (only LaunchAgents are there) | GNOME settings bundles (`settings/`), `hyperdock` |
| `org.virtualbox.vboxwebsrv`, `homebrew.mxcl.redis` (present on the Mac, not loaded at backup time) | Mac-side services nothing on Linux depends on | none; `redis-server` is not installed on this PC |

## Apt repositories and docker

`profile.toml` is the catalogue of every extra apt repository (`[modules.apt-sources.sources.<name>]`: URIs, suites,
the key URL and the **pinned primary-key fingerprint**; a key whose fingerprint differs is refused, `apt-key` is never
used). Every source is off there. A machine opts in to the ones it needs with `enabled = true` and a reason in its
`machine.toml`, and says why it declines the others (the table form merges key by key, so that is all it has to
write). `ubuntu-tweak apply` previews the exact files and diffs; `machines/ubuntu-pc/coverage.toml` lists the source
files the module owns for that machine and has to follow its `machine.toml`. Docker's `daemon.json` and the memory
cap of all containers (`[modules.docker-daemon]`) are per machine because the cap depends on its RAM.
Both are engine modules that are disabled by default; `profile.toml` enables `apt-sources` and `machine.toml` enables
`docker-daemon`. Order inside one `apply`: apt sources, then the apt packages of `packages.toml`, then docker.

## Secrets

Never store secrets here: no tokens, passwords, private keys, keyring data, online-account
or Wi-Fi credentials. The engine skips such keys and refuses `capture --commit` (exit 5) if
its output scan finds anything. The repo CI also runs gitleaks over the tree.

Credential entries of `$HOME` (`~/.ssh`, `~/.config/gh`, `~/.config/gcloud`, browser profiles, `.env`, ...) are listed
one by one in `coverage.d/secrets.toml` with the command or keychain item that brings each back on a fresh machine
(`ubuntu-tweak coverage --class secret` prints them). An entry that mixes a portable file with credentials
(`~/.config/tasks-backup`, `~/.config/ralphex`) is tracked through the portable file under `files/`; the credential
files inside it stay local. Files that carry a reviewed false positive of the secret scan (`guardian-connect`,
`tg-remote-test`, `ha-host-actions.py`, `odroid-remote-log`, `omp`) are in `files/` but not in `[capture] include_files`.

## Laptop root files: verdicts

The laptop `home` carried hand-made root files (units, scripts, `/etc` defaults); copies are in
`~/migration-from-home/home-live/system/`. Every one of them has a verdict below: a **module** (declarative, `--check` /
`--dry-run`, reproduced by `ubuntu-tweak apply --commit`) or **not applicable** with the reason and the evidence, checked on
the PC (Gigabyte B550M DS3H, Ryzen 5 5600, RTX 4060, 62 GB RAM, 32 GB swap file) on 23 Oct 2026.

| File(s) on the laptop | Verdict | Reason and evidence |
|---|---|---|
| `/etc/default/earlyoom` | **module** `earlyoom` (engine, `docs/specs/earlyoom.md`) | The package, the typed file and the service; the laptop's `-m 4 -s 10 -r 60 --prefer ... --avoid ... -n` is set for the PC in `machines/ubuntu-pc/machine.toml` with a reason per value (`chrome` dropped from `prefer`, the PC's own no-stop list added, `-n` off: no `systembus-notify` package in Ubuntu 26.04). |
| `mem-sentinel.service`, `/usr/local/sbin/mem-sentinel` | **module** `mem-sentinel` (this profile, `modules/mem-sentinel/`) | Installed from an agent-tools checkout, not copied here: the laptop's installed script is byte-identical to agent-tools `main` (sha256 equal, 23 Oct 2026), so nothing was lost by not keeping a copy. The desktop user is rendered from the account that ran `apply` instead of the sentinel's literal `ultra` default (agent-tools ticket 986). PC thresholds in `machines/ubuntu-pc/machine.toml`. |
| `swap-drain.service`, `swap-drain.timer`, `/usr/local/sbin/swap-drain`, `/usr/local/bin/memguard` (+ `/usr/local/lib/memguard`, `/etc/memguard/memguard.toml`, the polkit rule) | **module** `memguard` (engine), enabled | Diffed against the engine module (after `ruff format`): the engine copy is a superset (it resolves the desktop user, ships the heavy gate and locks off, validates the config). The laptop's gate values are the `[modules.memguard.swap]` options in `profile.toml` (engine ticket 86 adds the options). `memguard --check` reports drift on this PC until `apply --commit`: expected, nothing is installed yet. |
| `docker-limited.slice`, `docker/daemon.json` | **module** of the apt/docker slice (another agent): only noted | The slice caps containers (`MemoryHigh=6G`, `MemoryMax=8G`) and `daemon.json` points `cgroup-parent` at it; the docker-daemon module owns both. 6/8 GB were chosen for a 48 GB laptop; the PC value is that module's decision. |
| `apt/sources.list.d/*`, `apt/keyrings/*`, `apt/preferences.d/*` | **module** of the apt/docker slice (another agent): only noted | Third-party repositories and their keys. |
| `gnome-shell-leak-guard.service`, `.timer`, `/usr/local/sbin/gnome-shell-leak-guard` | **not applicable** | It restarts `gdm` when gnome-shell passes 3 GB RSS and no Kodi player is active (it polls Kodi's JSON-RPC on `127.0.0.1:8080`): a laptop that is an HTPC with a 24/7 autologin session. Kodi now lives on the odroid, and `systemctl restart gdm` ends this PC's desktop session with every open GUI app. Evidence: gnome-shell here is at 471 MB after 2 days 13 h, nowhere near 3 GB; its `omp`/terminal neighbours are on earlyoom's `avoid` list. A session-safe version does not exist: GNOME on Wayland cannot restart the shell without ending the session. Revisit with a RSS-logging user timer only if gnome-shell ever grows here. |
| `hang-probe.service`, `/usr/local/sbin/hang-probe` | **not applicable** | Forensics for the laptop's hangs, built on laptop hardware: `BAT1`/`ADP1` power supply, `i915` debugfs (PSR, DMC, runtime PM), `turbostat`, Intel `thermal_throttle`, MSI WMI. The PC has no battery and no i915 (an NVIDIA card), so most of its probes would read nothing. The memory side of such hangs is covered by the memory-protection stack above and `rig sysmon` (`ai.hyperide.sysmon`, a census every 60 s). If this PC hangs, write a probe for its own sensors (`k10temp`, `nvidia-smi`, `/sys/class/powercap` RAPL) with the same shape. |
| `hw-watchdog.service` (`modprobe iTCO_wdt`), `/etc/systemd/system.conf.d/90-hw-watchdog.conf` (`RuntimeWatchdogSec=120s`, `RebootWatchdogSec=5min`; not in the copy, read over ssh) | **not applicable** | `iTCO_wdt` is the Intel PCH watchdog. This PC is AMD: `/sys/class/watchdog` is empty, there is no `/dev/watchdog*`, no `sp5100-tco` platform device and `sp5100_tco` is not loaded (it exists as a module for this kernel, `modinfo sp5100_tco`), and arming `RuntimeWatchdogSec` without a device does nothing. Whether this board's BIOS exposes the AMD TCO timer needs root: `sudo modprobe sp5100_tco && ls /sys/class/watchdog && dmesg \| grep -i sp5100`. If a watchdog appears, the module is two files (`modules-load.d/sp5100_tco.conf` and a `system.conf.d` drop-in); until then it would be dead configuration. |
| `/usr/local/bin/determinate-nixd` | **not applicable** | Not a dead leftover (checked on the laptop over ssh): it is the daemon of **Determinate Nix** installed on 6 Oct 2026 (`/nix`, 6.1 GB, 13 289 store paths, `nix-daemon` and `determinate-nixd.socket` active), used for `devenv` and `secretspec` through `nix profile`. `which nix` is empty only because `/nix/var/nix/profiles/default/bin` is not on that shell's PATH. This PC has no `/nix`, no `devenv` or `secretspec`, and no `devenv.nix` or `devenv.yaml` under `~/xp` or `~/dev`, so there is nothing to reproduce. If `devenv` is wanted here, it is a module that runs the Determinate installer, not this binary. |
| `/usr/local/bin/mosh-server` (symlink into linuxbrew) | **packages** (`packages.toml`: apt `mosh`) | On the laptop `mosh` comes from Homebrew (`/home/linuxbrew/.linuxbrew/Cellar/mosh/1.4.0_38`), so sshd needed a symlink on its non-login PATH. The apt package puts `mosh-server` in `/usr/bin` (`apt-cache policy mosh`: 1.4.0-1ubuntu5), where sshd finds it; no symlink. |
| `/usr/local/bin/music-accounts`, `yandex-music-token`, `ytm-cookie` | **not applicable** | Dangling symlinks on the laptop into `~/homeassistant/scripts/ma_*.sh`. The scripts exist (`~/xp/ha-tinyprint-config/scripts/ma_accounts.sh`, `ma_yandex_token.sh`, `ma_ytm_cookie.sh`), but they are Music Assistant maintenance commands for the Home Assistant host: each one runs `docker exec ... homeassistant-homeassistant-1 python3 /config/scripts/ma_accounts.py`, and are used as `ssh root@home music-accounts ...`. That container now runs on the odroid (Home Assistant at `100.116.57.66:8123`), and this PC has no such container (`docker ps -a` lists none), so a link here would only fail. They belong to the `ha-tinyprint-config` repository. |
| `/usr/local/bin/haflow` | **open**, [#46](https://github.com/alex-mextner/.files/issues/46) | A 150-byte script, mode 700, owned by root, from 25 Sep 2026: unreadable without sudo on the laptop, so it was not copied and cannot be judged. It needs someone with root on the laptop to print it (without secrets) and decide. |
