#!/bin/sh
# ghostty module, primary installer (runs as root through the engine's sudo/pkexec): the Ubuntu archive package.
# Not possible without root: the engine then runs install-user.sh (the module's [[fallback]]).
set -eu
command -v apt-get >/dev/null || { echo "install.sh: apt-get not found" >&2; exit 1; }
DEBIAN_FRONTEND=noninteractive exec apt-get install -y ghostty
