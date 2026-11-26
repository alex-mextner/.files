# exports.sh: Homebrew prefix detection and PATH. sh-compatible, sourced by .profile (login env, not interactive).
# Does NOT belong here: aliases (-> aliases.sh / aliases.zsh), anything zsh-only or interactive (-> .zshrc).

#export PKG_CONFIG_PATH="/opt/local/lib/pkgconfig:$PKG_CONFIG_PATH"

#export NODE_PATH="$(npm config get prefix):$(npm config get prefix)/lib/node_modules:./node_modules:$NODE_PATH"

#export PS1=' ❯ '

#export PATH="$HOME/.cargo/bin:$PATH"
#source /Users/mxtnr/emsdk-portable/emsdk_env.sh

# Homebrew: Apple Silicon, Linuxbrew, Intel; whichever exists first (nothing breaks when there is none)
for _brew_prefix in /opt/homebrew /home/linuxbrew/.linuxbrew /usr/local; do
  if [ -x "$_brew_prefix/bin/brew" ]; then
    export PATH="$_brew_prefix/bin:$PATH"
    export HOMEBREW_PREFIX="$_brew_prefix"
    [ -d "$_brew_prefix/opt/gnu-sed/libexec/gnubin" ] && export PATH="$_brew_prefix/opt/gnu-sed/libexec/gnubin:$PATH"
    break
  fi
done
unset _brew_prefix
export PATH="./node_modules/.bin:$PATH"
