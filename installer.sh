#!/usr/bin/env bash
# installer.sh: bootstrap these dotfiles on macOS or Ubuntu (Linux). One script, branches on `uname -s`.
#
#   installer.sh [--dry-run] [--yes]
#
#   --dry-run   print what would be done and change nothing
#   --yes       unattended: no `gh auth login` prompt; on Linux also run `ubuntu-tweak init --commit`
#
# Both systems: ensure ~/.files (clone when missing), link .zprofile .zshrc .profile into $HOME (an existing
# different file is moved to <name>.pre-dotfiles-<timestamp> first), install Homebrew when absent (Homebrew's
# own installer; /opt/homebrew or /usr/local on macOS, /home/linuxbrew/.linuxbrew on Linux) and its formulae.
# macOS also: the casks, npm and VS Code lists, the other links, zplug plugins, fonts.
# Linux: the formulae come from `brew` in ubuntu/packages.toml; apt, GNOME, services and the rest stay with the
# ubuntu-tweak engine, which this script runs (or tells you how to get) at the end.
# Idempotent: a second run changes nothing. INSTALLER_OS=Darwin|Linux overrides `uname -s` (for --dry-run tests);
# INSTALLER_APPS_DIR / INSTALLER_APPS_REPO name the macOS package lists (default ~/xp/apps from a-x/apps).
set -eu

dry=0
yes=0
for arg in "$@"; do
  case $arg in
    --dry-run) dry=1 ;;
    --yes) yes=1 ;;
    -h|--help) sed -n '2,/^set -eu/{/^set -eu/!s/^# \{0,1\}//p;}' "$0"; exit 0 ;;
    *) echo "usage: $0 [--dry-run] [--yes]" >&2; exit 2 ;;
  esac
done

os=${INSTALLER_OS:-$(uname -s)}
case $os in
  Darwin|Linux) ;;
  *) echo "unsupported system: $os (macOS and Linux only)" >&2; exit 2 ;;
esac

here=$(cd "$(dirname "$0")" && pwd -P)
files=$HOME/.files
stamp=$(date +%Y%m%d-%H%M%S)
failed=""

say() { printf '%s\n' "$*"; }
# run <command...>: execute, or only print under --dry-run
run() {
  if [ "$dry" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi
}
# try <command...>: like run, but a failure is collected and reported at the end instead of stopping the script
try() {
  run "$@" || failed="$failed [$*]"
}
# items <list file>: one entry per line, comments and blanks removed
items() { sed -E 's/#.*//; s/^[[:space:]]+//; s/[[:space:]]+$//; /^$/d' "$1"; }

#
# 1. The checkout: this script normally runs from it; a copy elsewhere clones ~/.files first.
#
if [ "$here" != "$files" ] && [ ! -e "$files/.git" ]; then
  say "clone: ~/.files is missing"
  if command -v gh >/dev/null 2>&1; then
    run gh repo clone alex-mextner/.files "$files"
  else
    run git clone https://github.com/alex-mextner/.files "$files"
  fi
fi

#
# 2. Links into $HOME. The source list comes from this checkout; the links point at ~/.files.
#
link_into_home() {
  name=$1
  [ -e "$here/$name" ] || { say "skip: $name is not in the repository"; return 0; }
  dest=$HOME/$name
  target=$files/$name
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$target" ]; then
    say "ok: ~/$name -> $target"
    return 0
  fi
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    say "backup: ~/$name -> ~/$name.pre-dotfiles-$stamp"
    run mv "$dest" "$dest.pre-dotfiles-$stamp"
  fi
  say "link: ~/$name -> $target"
  run ln -sfn "$target" "$dest"
}

# the rc files the ubuntu-tweak engine links too (SHELL_RC_FILES of its dotfiles_rc.py)
for name in .zprofile .zshrc .profile; do link_into_home "$name"; done
if [ "$os" = Darwin ]; then
  for name in .gitconfig .gitattributes .tmux .tmux.conf .vimrc .hyper.js; do link_into_home "$name"; done
fi

#
# 3. Homebrew
#
find_brew() {
  if [ "$os" = Linux ]; then candidates="/home/linuxbrew/.linuxbrew/bin/brew"
  else candidates="/opt/homebrew/bin/brew /usr/local/bin/brew"; fi
  for candidate in $candidates; do
    if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
  done
  command -v brew 2>/dev/null || true
}

brew_bin=$(find_brew)
if [ -n "$brew_bin" ]; then
  say "ok: Homebrew at $brew_bin"
else
  say "install: Homebrew"
  if [ "$os" = Linux ]; then
    # the official installer refuses to run without a compiler and these tools
    for pkg in build-essential procps curl file git; do
      dpkg -s "$pkg" >/dev/null 2>&1 || missing_pkgs="${missing_pkgs:-} $pkg"
    done
    if [ -n "${missing_pkgs:-}" ]; then
      # shellcheck disable=SC2086 # the list is words on purpose
      run sudo apt-get install -y $missing_pkgs
    fi
  fi
  if [ "$dry" = 1 ]; then
    # shellcheck disable=SC2016 # printed literally on purpose
    echo '+ NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  else
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    brew_bin=$(find_brew)
  fi
fi
if [ -n "$brew_bin" ]; then eval "$("$brew_bin" shellenv)"; fi

# brew_install <formula|--cask name>: skip what is installed
brew_install() {
  if [ -n "$brew_bin" ]; then
    if [ "$1" = --cask ]; then
      if brew list --cask "$2" >/dev/null 2>&1; then say "ok: cask $2"; return 0; fi
    elif brew list --formula "$1" >/dev/null 2>&1; then say "ok: $1"; return 0
    fi
  fi
  try brew install "$@"
}

#
# 4. Packages
#
if [ "$os" = Linux ]; then
  # the CLI formulae the Ubuntu profile declares (one place for them: the engine installs the same list)
  list=$here/ubuntu/packages.toml
  if [ -r "$list" ]; then
    formulae=$(awk '/^brew = \[/ { on = 1; next } on && /^\]/ { exit }
      on && match($0, /"[^"]+"/) { print substr($0, RSTART + 1, RLENGTH - 2) }' "$list")
    for formula in $formulae; do brew_install "$formula"; done
  else
    say "skip: ubuntu/packages.toml not found, no formulae to install"
  fi
else
  apps=${INSTALLER_APPS_DIR:-$HOME/xp/apps}
  repo=${INSTALLER_APPS_REPO:-https://github.com/a-x/apps.git}
  if [ ! -d "$apps" ]; then
    say "clone: package lists $repo"
    run mkdir -p "$(dirname "$apps")"
    run git clone "$repo" "$apps" || say "warning: $repo is not reachable (the original a-x/apps is gone); set INSTALLER_APPS_REPO or INSTALLER_APPS_DIR to a copy of the lists"
  fi
  if [ -r "$apps/brew.list" ]; then
    for formula in $(items "$apps/brew.list"); do brew_install "$formula"; done
  fi
  if [ -r "$apps/brew-cask.list" ]; then
    for cask in $(items "$apps/brew-cask.list"); do brew_install --cask "$cask"; done
  fi
  if [ -r "$apps/npm.list" ]; then
    for pkg in $(items "$apps/npm.list"); do try npm -g install "$pkg"; done
  fi
  if [ -r "$apps/vscode.list" ]; then
    for ext in $(items "$apps/vscode.list"); do try code --install-extension "$ext"; done
  fi
  [ -d "$HOME/.nvm" ] || run mkdir "$HOME/.nvm"
  brew_install --cask font-fira-code
  brew_install --cask font-input
fi

#
# 5. GitHub login (the ubuntu-tweak engine finds the dotfiles repository through gh)
#
if command -v gh >/dev/null 2>&1 && ! gh auth status >/dev/null 2>&1; then
  if [ "$yes" = 1 ] || [ "$dry" = 1 ]; then say "manual: run \`gh auth login\`"; else gh auth login; fi
fi

#
# 6. zplug plugins (macOS: Homebrew's zplug; on Linux install-ubuntu.sh clones zplug and installs them)
#
if [ "$os" = Darwin ] && command -v zsh >/dev/null 2>&1 && [ "$dry" = 0 ]; then
  zsh -c 'source "$HOME/.files/.zshrc" >/dev/null 2>&1; zplug check || zplug install' || true
elif [ "$os" = Darwin ]; then
  say "zplug: source ~/.files/.zshrc and run zplug install"
fi

#
# 7. The rest
#
if [ "$os" = Linux ]; then
  engine=$(command -v ubuntu-tweak 2>/dev/null || true)
  if [ -z "$engine" ] && [ -x "$HOME/ubuntu-tweaks/bin/ubuntu-tweak" ]; then engine=$HOME/ubuntu-tweaks/bin/ubuntu-tweak; fi
  if [ -n "${engine:-}" ]; then
    if [ "$yes" = 1 ]; then run "$engine" init --commit; else run "$engine" init; fi
  else
    bootstrap='curl -fsSL https://raw.githubusercontent.com/alex-mextner/ubuntu-tweaks/main/scripts/bootstrap.sh | sh -s --'
    if [ "$yes" = 1 ] && [ "$dry" = 0 ]; then
      curl -fsSL https://raw.githubusercontent.com/alex-mextner/ubuntu-tweaks/main/scripts/bootstrap.sh | sh -s -- --yes
    else
      say "next: apt packages, GNOME and services are the ubuntu-tweak engine's; install it with"
      say "  $bootstrap --yes"
      say "  (without --yes it only previews)"
    fi
  fi
else
  say "manual: vim plugins: curl -fLo ~/.vim/autoload/plug.vim --create-dirs https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim, then run vim and :PlugInstall"
fi

if [ -n "$failed" ]; then
  say "failed (re-run to retry):$failed"
  exit 1
fi
say "done"
