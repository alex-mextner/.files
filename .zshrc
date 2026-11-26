# ~/.zshrc: zsh INTERACTIVE shells only (login or not; a login shell reads .zprofile first). Linux and macOS.
# Belongs here: history, options, keybindings, aliases (aliases.sh, aliases.zsh), functions, completion,
# prompt (oh-my-posh, else spaceship), zplug plugins, tool completions.
# Does NOT belong here: exports that scripts or other programs need (PATH, EDITOR, ... -> .profile), zsh login
# env (-> .zprofile), anything a non-interactive shell needs, e.g. ssh host cmd (-> ~/.zshenv, not managed).

# Docker group fix: a session that predates `usermod -aG docker` re-executes itself under the group. Only for a user
# that is in the group by name but whose session lacks it: on a machine without the group or the membership
# `exec sg docker` would replace the shell with a failing or prompting command.
if [[ $OSTYPE == linux* && -o interactive && -z ${DOCKER_GRP-} ]] && (( $+commands[sg] )) \
    && id -nG "$USER" 2>/dev/null | grep -qw docker && ! groups | grep -qw docker; then
  export DOCKER_GRP=1
  exec sg docker -c "SHELL=$SHELL DOCKER_GRP=1 $SHELL"
fi

# fix after update zsh: __zplug::utils::ansi::remove:17: maximum nested function level reached; increase FUNCNEST?
FUNCNEST=1500

#
# Options and built-in aliases (aliases.sh / aliases.zsh below override these)
#
setopt AUTO_CD
setopt CORRECT
setopt NO_BEEP
alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'
alias grep='grep --color=auto'

# keybindings, history, misc, spectrum, then the aliases: the repository wins over everything above
for _files_part in keybindings.zsh history.zsh misc.zsh spectrum.zsh aliases.sh aliases.zsh; do
  [[ -r $HOME/.files/$_files_part ]] && source "$HOME/.files/$_files_part"
done
unset _files_part

#
# zplug (optional): Homebrew's copy on macOS, ~/.zplug on Ubuntu (install-ubuntu.sh clones it there).
# Without either, `zplug` is a no-op and nothing below errors.
#
for _files_zplug in "${HOMEBREW_PREFIX:+$HOMEBREW_PREFIX/opt/zplug}" "$HOME/.zplug"; do
  if [[ -n $_files_zplug && -r $_files_zplug/init.zsh ]]; then
    export ZPLUG_HOME=$_files_zplug
    break
  fi
done
unset _files_zplug
if [[ -n ${ZPLUG_HOME-} ]]; then
  source "$ZPLUG_HOME/init.zsh"
else
  zplug() { :; }
fi

#
# completion
#

zplug "lukechilds/zsh-better-npm-completion", defer:2
zplug "zsh-users/zsh-completions"
# autoload -U compinit && compinit
autoload -U select-word-style
select-word-style bash

# cached completion https://blog.callstack.io/supercharge-your-terminal-with-zsh-8b369d689770
autoload -Uz compinit
typeset -i updated_at=$(date +'%j' -r ~/.zcompdump 2>/dev/null || stat -f '%Sm' -t '%j' ~/.zcompdump 2>/dev/null)
if [ $(date +'%j') != $updated_at ]; then
  compinit -i
else
  compinit -C -i
fi

# menu list from where we can highlight and select completion results
zmodload -i zsh/complist

#
# prompt
#

# An installed oh-my-posh owns the prompt (initialised after the plugins, below); the spaceship/PS1 setup is for
# machines without it.
typeset -i _files_has_omp=$+commands[oh-my-posh]
if (( ! _files_has_omp )); then
  #setopt PROMPT_SUBST
  #export PS1='%S%B %2~ ❯ %s%b '
  export PS1='%2~ ❯ %s'

  setopt PROMPT_SUBST
  # export RPROMPT='$(~/.cabal/bin/git-prompt green)'
  export RPROMPT='$(git-get-branch 2>/dev/null)'
fi

#
# Plugins
#

(( _files_has_omp )) || zplug "denysdovhan/spaceship-prompt", use:spaceship.zsh, from:github, as:theme
SPACESHIP_PROMPT_SEPARATE_LINE=false
SPACESHIP_CHAR_SYMBOL='❯'
SPACESHIP_CHAR_SUFFIX=' '
SPACESHIP_CHAR_SYMBOL_ROOT='# '

SPACESHIP_GIT_STATUS_SHOW=false

SPACESHIP_HG_SHOW=false
SPACESHIP_NODE_SHOW=false
SPACESHIP_PACKAGE_SHOW=false
# SPACESHIP_KUBECONTEXT_SHOW=false
SPACESHIP_KUBECTL_CONTEXT_SHOW=false
SPACESHIP_DOCKER_SHOW=false
SPACESHIP_RUBY_SYMBOL=

# https://upload.wikimedia.org/wikipedia/commons/1/15/Xterm_256color_chart.svg
SPACESHIP_DIR_COLOR=cyan #027 # some blue
SPACESHIP_GIT_BRANCH_COLOR=244 # some silver
SPACESHIP_RUBY_COLOR=244 # some silver

# ZSH port of Fish shell's history search feature https://github.com/zsh-users/zsh-history-substring-search
zplug "zsh-users/zsh-history-substring-search"

# UX Proactive tips
zplug "djui/alias-tips"

# Fish-like autosuggestions for zsh
zplug "zsh-users/zsh-autosuggestions"

# doesn't work
# zplug "marzocchi/zsh-notify"

# # Enable zsh-autosuggestions
# zle-line-init() {
#     zle autosuggest-start
# }
# zle -N zle-line-init
AUTOSUGGESTION_ACCEPT_RIGHT_ARROW=1
ZSH_AUTOSUGGEST_USE_ASYNC=1 # async

# zsh-history-substring-search
# bind UP and DOWN arrow keys
# zmodload zsh/terminfo
# bindkey "$terminfo[kcuu1]" history-substring-search-up
# bindkey "$terminfo[kcud1]" history-substring-search-down
#
# # bind UP and DOWN arrow keys (compatibility fallback
# # for Ubuntu 12.04, Fedora 21, and MacOSX 10.9 users)
bindkey '^[[A' history-substring-search-up
bindkey '^[[B' history-substring-search-down


#autoload -U zmv # Mass renaming files with zmv

#source ~/.bin/tmuxinator.zsh

unsetopt EXTENDED_GLOB
setopt noclobber # >, >! — rewrite safety

# MUST BE AT END OF .ZSHRC
# Fish shell like syntax highlighting for Zsh
zplug "zdharma/fast-syntax-highlighting"
# zplug "zsh-users/zsh-syntax-highlighting"

[[ $OSTYPE == darwin* && -e $HOME/.iterm2_shell_integration.zsh ]] && source "$HOME/.iterm2_shell_integration.zsh"


# Then, source plugins and add commands to $PATH
zplug load #--verbose

# setopt correct_all # autocorrect commands
setopt auto_list # automatically list choices on ambiguous completion
setopt auto_menu # automatically use menu completion
setopt always_to_end # move cursor to end if word had one match

zstyle ':completion:*' menu select # select completions with arrow keys
zstyle ':completion:*' matcher-list 'm:{a-z}={A-Za-z}' # lower case matches upper case
zstyle ':completion:*' group-name '' # group results by category
zstyle ':completion:::::' completer _expand _complete _ignored _approximate # enable approximate matches for completion

# Prompt: oh-my-posh with the theme this machine has cached, else its default theme
if (( _files_has_omp )); then
  if [[ -r $HOME/.cache/oh-my-posh/themes/jandedobbeleer.omp.json ]]; then
    eval "$(oh-my-posh init zsh --config "$HOME/.cache/oh-my-posh/themes/jandedobbeleer.omp.json")"
  else
    eval "$(oh-my-posh init zsh)"
  fi
fi
unset _files_has_omp

#
# Tool completions (after compinit; each guarded, a missing tool prints nothing)
#
# nvm (installed by Homebrew; its completion is a bash script)
[[ -n ${HOMEBREW_PREFIX-} && -s $HOMEBREW_PREFIX/opt/nvm/etc/bash_completion.d/nvm ]] && source "$HOMEBREW_PREFIX/opt/nvm/etc/bash_completion.d/nvm"
# bun
[[ -s $HOME/.bun/_bun ]] && source "$HOME/.bun/_bun"
# Google Cloud SDK (its PATH part is in .zprofile)
[[ -r $HOME/google-cloud-sdk/completion.zsh.inc ]] && source "$HOME/google-cloud-sdk/completion.zsh.inc"
# vastai: its generated env.sh extends PATH and loads the zsh completion
[[ -f $HOME/.local/share/vastai/env.sh ]] && source "$HOME/.local/share/vastai/env.sh"

# gh 2.89 shadows the legacy gh-copilot extension; use the wrapper command directly.
if command -v copilot >/dev/null 2>&1; then
  ghcs() {
    local TARGET="shell"
    local GH_DEBUG="$GH_DEBUG"
    local GH_HOST="$GH_HOST"
    local OPT OPTARG OPTIND

    while getopts "dht:-:" OPT; do
      if [ "$OPT" = "-" ]; then
        OPT="${OPTARG%%=*}"
        OPTARG="${OPTARG#"$OPT"}"
        OPTARG="${OPTARG#=}"
      fi

      case "$OPT" in
        debug | d)
          GH_DEBUG=api
          ;;

        help | h)
          copilot suggest --help
          return 0
          ;;

        hostname)
          GH_HOST="$OPTARG"
          ;;

        target | t)
          TARGET="$OPTARG"
          ;;
      esac
    done

    shift "$((OPTIND-1))"

    local TMPFILE FIXED_CMD
    TMPFILE="$(mktemp -t gh-copilotXXXXXX)"
    trap 'rm -f "$TMPFILE"' EXIT
    if GH_DEBUG="$GH_DEBUG" GH_HOST="$GH_HOST" copilot suggest -t "$TARGET" "$@" --shell-out "$TMPFILE"; then
      if [ -s "$TMPFILE" ]; then
        FIXED_CMD="$(cat "$TMPFILE")"
        print -s -- "$FIXED_CMD"
        echo
        eval -- "$FIXED_CMD"
      fi
    else
      return 1
    fi
  }

  ghce() {
    local GH_DEBUG="$GH_DEBUG"
    local GH_HOST="$GH_HOST"
    local OPT OPTARG OPTIND

    while getopts "dh-:" OPT; do
      if [ "$OPT" = "-" ]; then
        OPT="${OPTARG%%=*}"
        OPTARG="${OPTARG#"$OPT"}"
        OPTARG="${OPTARG#=}"
      fi

      case "$OPT" in
        debug | d)
          GH_DEBUG=api
          ;;

        help | h)
          copilot explain --help
          return 0
          ;;

        hostname)
          GH_HOST="$OPTARG"
          ;;
      esac
    done

    shift "$((OPTIND-1))"
    GH_DEBUG="$GH_DEBUG" GH_HOST="$GH_HOST" copilot explain "$@"
  }
fi

(( $+commands[thefuck] )) && eval $(thefuck --alias)

if [[ $RANDOM > 8191 ]] && (( $+commands[get-random-alias-help] )); then
  echo
  echo
  get-random-alias-help
  echo '_____________________________'
  echo '       \'
  echo '        \  .\__/.       '
  echo '         \  (oo)\_______'
  echo '            (_.)\       )\/\'
  echo '                ||----w |'
  echo '                ||     ||'
fi

# rig's harness wrappers (omp, claude, codex, opencode) must come before the real binaries; last, so nothing above can shadow them
[ -r "$HOME/.files/harness-wrappers.sh" ] && . "$HOME/.files/harness-wrappers.sh"
