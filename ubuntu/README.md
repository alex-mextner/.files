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
  settings/**/*.ini         # settings bundles: dconf keyfiles named by purpose (dev-keyboard.ini, laptop/power.ini), shared by all machines
  files/                    # mirrors $HOME: files/.config/mimeapps.list -> ~/.config/mimeapps.list
  packages.toml             # apt / snap / flatpak names
  system.toml               # root-level: timezone, locale, console keyboard
  machines/<hostname>/      # overlay, one dir per machine (hostname = /etc/hostname, or --machine)
    machine.toml            # same schema as profile.toml; wins over it (no [machine] table: hostname = dir name)
    settings/**/*.ini       # key-level overlay bundles on top of the shared ones
    files/                  # path-level overlay: same relative path wins over shared files/
    packages.toml  system.toml   # same schema, merged (lists union, scalars replace)
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
[ubuntu-tweak]                  # optional header, not a dconf section
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

`packages.toml` takes the arrays `apt`, `snap`, `flatpak` plus two optional scalars:
`flatpak_remote = "flathub"` (the remote flatpaks install from) and `apt_update = true` (run
`apt-get update` before the apt batch).

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

## Secrets

Never store secrets here: no tokens, passwords, private keys, keyring data, online-account
or Wi-Fi credentials. The engine skips such keys and refuses `capture --commit` (exit 5) if
its output scan finds anything. The repo CI also runs gitleaks over the tree.
