# ~/.profile: the login environment shared by every shell. sh-compatible syntax only (dash, bash, zsh).
# Read by login sh/bash/dash and by the Ubuntu desktop session; .zprofile sources it for zsh logins.
# Belongs here: PATH and exports (brew shellenv, EDITOR, BUN_INSTALL, cargo, GOPATH, ...).
# Does NOT belong here: aliases (aliases.sh / aliases.zsh), functions, prompt, plugins, completion, history,
# keybindings (-> .zshrc); zsh-only syntax (-> .zprofile); machine-local values (-> ~/.profile.local).

[ -r "$HOME/.files/exports.sh" ] && . "$HOME/.files/exports.sh"

#
# Front
#
export N_PREFIX="$HOME/n"  # Added by n-install (see http://git.io/n-install-repo).
case ":$PATH:" in *":$N_PREFIX/bin:"*) ;; *) PATH="$PATH:$N_PREFIX/bin" ;; esac
export PATH="./node_modules/.bin/:$PATH"

# nvm.sh is sh-compatible; its bash completion is loaded by .zshrc
export NVM_DIR="$HOME/.nvm"
[ -s "${HOMEBREW_PREFIX:-/nonexistent}/opt/nvm/nvm.sh" ] && . "$HOMEBREW_PREFIX/opt/nvm/nvm.sh" # This loads nvm

#
# General Purpose Exports
#

export EMAIL=invntrm@gmail.com
#export EDITOR='mvim -v'
#export EDITOR='vim'
# code on machines that have it, else vim (EDITOR=code with no `code` binary breaks `git commit`, crontab -e, ...).
if command -v code >/dev/null 2>&1; then export EDITOR='code'; else export EDITOR='vim'; fi
export PAGER="less"
export LESS="-R"
export REACT_EDITOR=code
export LANG=en_US.UTF-8
export BASH_SILENCE_DEPRECATION_WARNING=1
export BUN_INSTALL="$HOME/.bun"
_sublime_bin=
if [ "$(uname -s)" = Darwin ]; then _sublime_bin="/Applications/Sublime Text.app/Contents/SharedSupport/bin:"; fi
export PATH="$BUN_INSTALL/bin:$HOME/.files/bin:${_sublime_bin}$PATH"
unset _sublime_bin
export PATH="$PATH:$HOME/.files/ts-import-trace/bin"
export GOPATH="$HOME/go"
export PATH="$PATH:$GOPATH/bin"

# Chrome's sandbox is unavailable to agent-browser on Ubuntu (AppArmor restricts user namespaces)
if [ "$(uname -s)" = Linux ]; then export AGENT_BROWSER_ARGS="--no-sandbox"; fi

[ -n "${HOMEBREW_PREFIX:-}" ] && [ -x "$HOMEBREW_PREFIX/bin/brew" ] && eval "$("$HOMEBREW_PREFIX/bin/brew" shellenv)"

[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"

#
# Linux login shells (the Ubuntu default ~/.profile did these; a bash login reads only this file)
#
if [ -n "${BASH_VERSION:-}" ] && [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi
[ -d "$HOME/bin" ] && PATH="$HOME/bin:$PATH"
[ -d "$HOME/.local/bin" ] && PATH="$HOME/.local/bin:$PATH"

# Machine-local settings (not in this repository: ubuntu-tweaks links ~/.profile.local from the profile's
# machines/<host>/files/, or write it by hand)
if [ -f "$HOME/.profile.local" ]; then . "$HOME/.profile.local"; fi

# rig's harness wrappers (omp, claude, codex, opencode) must come before the real binaries; last, so nothing above can shadow them
[ -r "$HOME/.files/harness-wrappers.sh" ] && . "$HOME/.files/harness-wrappers.sh"
