# See .profile

#export PKG_CONFIG_PATH="/opt/local/lib/pkgconfig:$PKG_CONFIG_PATH"

#export NODE_PATH="$(npm config get prefix):$(npm config get prefix)/lib/node_modules:./node_modules:$NODE_PATH"

#export PS1=' ❯ '

#export PATH="$HOME/.cargo/bin:$PATH"
#source /Users/mxtnr/emsdk-portable/emsdk_env.sh

# Homebrew prefixes differ per OS (macOS arm: /opt/homebrew, Linux: /home/linuxbrew/.linuxbrew); use the one that exists.
if [ -d /opt/homebrew/bin ]; then
  export HOMEBREW_PREFIX=/opt/homebrew
  export PATH="/opt/homebrew/bin:$PATH"
  [ -d /opt/homebrew/opt/gnu-sed/libexec/gnubin ] && export PATH="/opt/homebrew/opt/gnu-sed/libexec/gnubin:$PATH"
elif [ -x /home/linuxbrew/.linuxbrew/bin/brew ]; then
  export HOMEBREW_PREFIX=/home/linuxbrew/.linuxbrew
  export PATH="$HOMEBREW_PREFIX/bin:$PATH"
elif command -v brew >/dev/null 2>&1; then
  export HOMEBREW_PREFIX="$(brew --prefix)"
fi
export PATH="./node_modules/.bin:$PATH"
