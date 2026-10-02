#!/bin/bash
# brew-update — refresh Homebrew metadata at login.
# Run by LaunchAgent com.ultra.brew-update (RunAtLoad).
set -euo pipefail

/opt/homebrew/bin/brew update
