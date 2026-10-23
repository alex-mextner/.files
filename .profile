
for file in exports aliases; do source ~/.files/${file}.sh; done
unset file

#
# Front
#
export N_PREFIX="$HOME/n"; [[ :$PATH: == *":$N_PREFIX/bin:"* ]] || PATH+=":$N_PREFIX/bin"  # Added by n-install (see http://git.io/n-install-repo).
export PATH="./node_modules/.bin/:$PATH"
# export NVM_DIR="$HOME/.nvm"
# [ -s "/opt/homebrew/opt/nvm/nvm.sh" ] && \. "/opt/homebrew/opt/nvm/nvm.sh"  # This loads nvm
# [ -s "/opt/homebrew/opt/nvm/etc/bash_completion.d/nvm" ] && \. "/opt/homebrew/opt/nvm/etc/bash_completion.d/nvm"  # This loads nvm bash_completion

export NVM_DIR="$HOME/.nvm"
[ -s "$HOMEBREW_PREFIX/opt/nvm/nvm.sh" ] && \. "$HOMEBREW_PREFIX/opt/nvm/nvm.sh" # This loads nvm
[ -s "$HOMEBREW_PREFIX/opt/nvm/etc/bash_completion.d/nvm" ] && \. "$HOMEBREW_PREFIX/opt/nvm/etc/bash_completion.d/nvm" # This loads nvm bash_completion

#
# General Purpose Exports
#

export EMAIL=invntrm@gmail.com
#export EDITOR='mvim -v'
#export EDITOR='vim'
# code on machines that have it, else vim (EDITOR=code with no `code` binary breaks `git commit`, crontab -e, ...).
if command -v code >/dev/null 2>&1; then export EDITOR='code'; else export EDITOR='vim'; fi
export REACT_EDITOR=code
export LANG=en_US.UTF-8
export BASH_SILENCE_DEPRECATION_WARNING=1
export BUN_INSTALL="$HOME/.bun" 
export PATH="$BUN_INSTALL/bin:$HOME/.files/bin:/Applications/Sublime Text.app/Contents/SharedSupport/bin:$PATH"
export PATH="$PATH:$HOME/.files/ts-import-trace/bin"

#
# HELPERS
#

#eval $(thefuck --alias)
#eval "$(fasd --init auto)"

# added by travis gem
#[ -f /Users/mxtnr/.travis/travis.sh ] && source /Users/mxtnr/.travis/travis.sh

# Copy idiot's formatted shell-code w/o a pain
alias %=' '

#
# Completion
#

# which npm 2>/dev/null 1>&2 && (. <(npm completion))2>/dev/null

# tabtab source for yarn package
# uninstall by removing these lines or running `tabtab uninstall yarn`
# [[ -f /Users/mxtnr/xp/yarn-completions/node_modules/tabtab/.completions/yarn.zsh ]] && . /Users/mxtnr/xp/yarn-completions/node_modules/tabtab/.completions/yarn.zsh



[ -n "${HOMEBREW_PREFIX:-}" ] && [ -x "$HOMEBREW_PREFIX/bin/brew" ] && eval "$("$HOMEBREW_PREFIX/bin/brew" shellenv)"

[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
export LANG=en_US.UTF-8

#
# Linux login shells (the Ubuntu default ~/.profile did these; a bash login reads only this file)
#
if [ -n "${BASH_VERSION:-}" ] && [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi
[ -d "$HOME/bin" ] && PATH="$HOME/bin:$PATH"
[ -d "$HOME/.local/bin" ] && PATH="$HOME/.local/bin:$PATH"

# Machine-local settings (not in this repository: ubuntu-tweaks links ~/.profile.local from the profile's
# machines/<host>/files/, or write it by hand)
if [ -f "$HOME/.profile.local" ]; then . "$HOME/.profile.local"; fi
