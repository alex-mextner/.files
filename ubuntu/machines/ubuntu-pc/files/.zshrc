# ~/.zshrc: executed by zsh(1) for interactive shells.

# History
HISTFILE=~/.zsh_history
HISTSIZE=10000
SAVEHIST=10000
setopt HIST_IGNORE_DUPS
setopt HIST_IGNORE_SPACE
setopt SHARE_HISTORY
setopt APPEND_HISTORY

# Completion
autoload -Uz compinit
compinit
zstyle ':completion:*' menu select
zstyle ':completion:*' matcher-list 'm:{a-z}={A-Za-z}'

# Options
setopt AUTO_CD
setopt CORRECT
setopt EXTENDED_GLOB
setopt NO_BEEP

# Aliases
alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'
alias grep='grep --color=auto'

# Homebrew
[ -x /home/linuxbrew/.linuxbrew/bin/brew ] && eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv zsh)"

# PATH
export PATH="/home/ultra/.local/bin:$PATH"

# Docker group fix (only for a user that is in the docker group by name but whose session predates it: on a machine
# without the group or the membership `exec sg docker` would replace the shell with a failing or prompting command)
if [ -z "${DOCKER_GRP-}" ] && command -v sg >/dev/null 2>&1 && id -nG "$USER" 2>/dev/null | grep -qw docker; then
    if ! groups | grep -qw docker; then
        export DOCKER_GRP=1
        exec sg docker -c "SHELL=$SHELL DOCKER_GRP=1 $SHELL"
    fi
fi

# Agent browser fix
export AGENT_BROWSER_ARGS="--no-sandbox"

# Forgejo CLI
export FJ_FALLBACK_HOST=https://git.hyperide.ai

# Oh My Posh
command -v oh-my-posh >/dev/null 2>&1 && [ -r ~/.cache/oh-my-posh/themes/jandedobbeleer.omp.json ] && eval "$(oh-my-posh init zsh --config ~/.cache/oh-my-posh/themes/jandedobbeleer.omp.json)"
# bun completions
[ -s "/home/ultra/.bun/_bun" ] && source "/home/ultra/.bun/_bun"

# bun
export BUN_INSTALL="$HOME/.bun"
export PATH="$BUN_INSTALL/bin:$HOME/.cargo/bin:$PATH"

[ -f "/home/ultra/.local/share/vastai/env.sh" ] && . "/home/ultra/.local/share/vastai/env.sh"  # vastai shell setup
# >>> .files:zshrc (managed by ~/.files/install-ubuntu.sh; edit there) >>>
[[ -o login ]] || source "$HOME/.files/.zprofile"
[ -r "$HOME/.files/harness-wrappers.sh" ] && . "$HOME/.files/harness-wrappers.sh"
# <<< .files:zshrc <<<
