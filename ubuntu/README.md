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
  settings/<panel>.ini      # dconf keyfile per GNOME Settings panel, shared by all machines
  files/                    # mirrors $HOME: files/.config/mimeapps.list -> ~/.config/mimeapps.list
  packages.toml             # apt / snap / flatpak names
  system.toml               # root-level: timezone, locale, console keyboard
  machines/<hostname>/      # overlay, one dir per machine (hostname = /etc/hostname, or --machine)
    machine.toml            # same schema as profile.toml; wins over it (no [machine] table: hostname = dir name)
    settings/<panel>.ini    # key-level overlay on top of the shared panel files
    files/                  # path-level overlay: same relative path wins over shared files/
    packages.toml  system.toml   # same schema, merged (lists union, scalars replace)
```

Only `profile.toml` and `machines/home/machine.toml` exist initially; the rest appears
when `ubuntu-tweak capture --commit` writes it.

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

Every mutating command previews by default; only `--commit` changes anything.

On the machine where you changed settings:

1. `ubuntu-tweak capture` - preview of exactly what the profile would receive.
2. `ubuntu-tweak capture --commit` - writes it into this working tree (no git commit).
3. Review `git diff` in `~/.files`, then commit and open a PR.

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
- `include_files`: extends the built-in file allow-list.
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
