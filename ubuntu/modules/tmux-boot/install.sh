#!/usr/bin/env bash
# tmux-boot module installer (user level, never root): ~/.local/bin/tmux-boot + the systemd user unit
# tmux-boot.service, enabled, with linger on, so the tmux server and its saved sessions are back after a reboot.
#
#   install.sh           install (idempotent: a second run changes nothing)
#   install.sh --plan    read-only: what install would do
#   install.sh --check   read-only: exit 0 = in place, 3 = drift, 10 = needs a manual step, anything else = broken checker
#
# Exit codes follow the ubuntu-tweak system-module protocol (0 ok, 3 drift, 10 manual, 75 transient).
#
# Failover, decided here and not by the engine's [[fallback]] (which would also fire on an unrelated failure such as a
# full disk): with no user systemd bus (an ssh session without a bus, a container) the install writes an XDG autostart
# entry instead, and the server then starts at graphical login rather than at boot. When the bus is there, the unit
# is the one start path: a stale autostart entry is drift, and install removes it.
#
# TMUX_BOOT_UNIT_DIR / TMUX_BOOT_BIN_DIR / TMUX_BOOT_AUTOSTART_DIR move the three install locations; they exist for
# the tests (a temp HOME). The unit and the autostart entry's Exec always name the script in BIN_DIR as installed.
set -u

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

here=$(cd "$(dirname "$0")" && pwd -P)
unit=tmux-boot.service
bin_dir=${TMUX_BOOT_BIN_DIR:-$HOME/.local/bin}
unit_dir=${TMUX_BOOT_UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}
autostart_dir=${TMUX_BOOT_AUTOSTART_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/autostart}
autostart_file="$autostart_dir/tmux-boot.desktop"

# the user's systemd bus is found through XDG_RUNTIME_DIR: ssh and cron sessions do not set it, the unit still lives
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then export XDG_RUNTIME_DIR="/run/user/$(id -u)"; fi

manual() { echo "needs manual: $1" >&2; exit 10; }
sc() { systemctl --user "$@"; }
same() { [ -f "$2" ] && cmp -s "$1" "$2"; }
linger_ok() { [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" = yes ]; }
bus_ok() { systemctl --user show-environment >/dev/null 2>&1; }
script_ok() { same "$here/tmux-boot" "$bin_dir/tmux-boot" && [ -x "$bin_dir/tmux-boot" ]; }
unit_file_ok() { same "$here/$unit" "$unit_dir/$unit"; }
unit_enabled() { [ "$(sc is-enabled "$unit" 2>/dev/null)" = enabled ]; }
tmux_ok() { command -v tmux >/dev/null 2>&1; }

# the autostart entry is generated: its Exec is the absolute path of the installed script
desktop_text() {
  local line
  while IFS= read -r line; do printf '%s\n' "${line//@BIN@/"$bin_dir/tmux-boot"}"; done < "$here/tmux-boot.desktop"
}
autostart_ok() { [ -f "$autostart_file" ] && [ "$(desktop_text)" = "$(cat "$autostart_file")" ]; }

check_systemd() { # prints one line per unmet fact
  unit_file_ok || echo "$unit_dir/$unit is missing or differs from the module copy"
  unit_enabled || echo "$unit is not enabled (systemctl --user enable $unit)"
  linger_ok || echo "systemd linger is off for $(id -un), so the unit starts at login, not at boot (loginctl enable-linger)"
  [ ! -e "$autostart_file" ] || echo "$autostart_file is stale: the unit is the start path while the user bus is available (install removes it)"
}
check_autostart() {
  autostart_ok || echo "no user systemd bus: $autostart_file is the start path and it is missing or differs"
}

if [ "$mode" = check ]; then
  problems=()
  tmux_ok || problems+=("tmux is not installed (apt: tmux, declared in packages.toml)")
  script_ok || problems+=("$bin_dir/tmux-boot is missing or differs from the module copy")
  if bus_ok; then check=check_systemd; else check=check_autostart; fi
  while IFS= read -r line; do [ -z "$line" ] || problems+=("$line"); done < <($check)
  if [ "${#problems[@]}" -gt 0 ]; then
    printf 'drift: %s\n' "${problems[@]}"
    exit 3
  fi
  exit 0
fi

if [ "$mode" = plan ]; then
  script_ok || echo "install $here/tmux-boot -> $bin_dir/tmux-boot"
  if bus_ok; then
    unit_file_ok || echo "install $here/$unit -> $unit_dir/$unit; systemctl --user daemon-reload"
    unit_enabled || echo "systemctl --user enable $unit"
    linger_ok || echo "loginctl enable-linger $(id -un)   (so the unit starts at boot, before any login)"
    [ ! -e "$autostart_file" ] || echo "remove the stale $autostart_file"
  else
    autostart_ok || echo "no user systemd bus: write the XDG autostart entry $autostart_file (the server starts at graphical login)"
  fi
  exit 0
fi

tmux_ok || manual "tmux is not installed; run \`ubuntu-tweak apply --commit\` (packages.toml installs it) or \`sudo apt-get install -y tmux\`"
mkdir -p "$bin_dir" || exit 1
script_ok || install -m 755 "$here/tmux-boot" "$bin_dir/tmux-boot" || exit 1

if ! bus_ok; then
  echo "tmux-boot: no user systemd bus, installing the XDG autostart entry instead (the server starts at graphical login)"
  mkdir -p "$autostart_dir" || exit 1
  autostart_ok || desktop_text > "$autostart_file" || exit 1
  exit 0
fi

mkdir -p "$unit_dir" || exit 1
unit_file_ok || { install -m 644 "$here/$unit" "$unit_dir/$unit" && sc daemon-reload; } || exit 1
unit_enabled || sc enable "$unit" || exit 1
rm -f "$autostart_file"
# enabling does not start it: the tmux server that may be running now must not be touched, and the script would do
# nothing anyway. The unit runs at the next boot.
linger_ok || loginctl enable-linger "$(id -un)" 2>/dev/null \
  || manual "systemd linger is off: run \`sudo loginctl enable-linger $(id -un)\` once so the unit starts at boot, then run apply again"
exit 0
