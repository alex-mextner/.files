## jobs
setopt long_list_jobs

## pager
export PAGER="less"
export LESS="-R"

# recognize comments
setopt interactivecomments

# directories

## cd - <tab>
setopt auto_pushd
setopt pushd_ignore_dups
setopt pushdminus


# zplug: when the Homebrew copy .zprofile looks for is absent, use ~/.zplug (install-ubuntu.sh clones
# it there). Sourced before the zplug declarations of .zprofile, so they register against it.
if [[ ! -r ${ZPLUG_HOME:-/nonexistent}/init.zsh && -r $HOME/.zplug/init.zsh ]]; then
  (( $+functions[zplug] )) && unfunction zplug
  export ZPLUG_HOME=$HOME/.zplug
  source $ZPLUG_HOME/init.zsh
fi
