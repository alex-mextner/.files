#!/usr/bin/env bash
# harden-backups-mount.sh — make the bind mounts of the "backups" USB disk robust at boot, on unplug and on late
# plug-in (rig-cli ticket 858). Root half; the user-service half is backups-user-gates (no root).
#
#   sudo ./harden-backups-mount.sh --dry-run   print every change it would make, change nothing (works without root)
#   sudo ./harden-backups-mount.sh             apply (idempotent: a second run reports "already in place")
#   sudo ./harden-backups-mount.sh --revert    undo everything this script did (--dry-run shows it first)
#
# What it changes, and why:
#  1. /etc/fstab, the line that mounts the disk (backed up first, only that line is rewritten):
#       x-systemd.device-timeout 10s -> 30s   the disk was mounted 9.9 s into a boot: a 10 s timeout fails it on
#                                             a slightly slower spin-up and every bind mount then fails with it
#       + errors=remount-ro                   a filesystem error must stop writes, not keep writing to a bad disk
#     NO x-systemd.automount, on evidence: the 20+ bind lines Require the mount UNIT directly (x-systemd.requires-
#     mounts-for), so boot starts it eagerly anyway and an automount adds nothing for them; and after a failed boot
#     attempt nothing retries those binds, which automount cannot fix. It would also make every access to the
#     path (file managers, indexers, `df`, `find`) block for the device timeout while the disk is missing.
#  2. Late plug-in / slow spin-up: /etc/udev/rules.d/90-backups-disk.rules starts backups-attach.service when the
#     partition with this UUID appears; it mounts the disk, then every bind mount fstab lists for it (read at run
#     time, so binds added later by move-to-backups.sh are covered), then kicks the user-level retry that starts
#     the units whose start was skipped without the mounts. A no-op when everything is already mounted.
#  3. /etc/systemd/system/{user@UID,docker}.service.d/backups-mounts.conf: After= the disk and every bind mount unit, so
#     the user manager and docker start only once those mount jobs have finished (successfully or not). After= only,
#     NOT Requires/RequiresMountsFor: a hard dependency would stop the user manager (login, tmux, ssh sessions' user
#     services) and docker from starting at all while the disk is unplugged, and Wants= would re-run the mount jobs
#     and delay login by another device timeout. The empty-directory protection comes from 4. and the user gates.
#  4. chattr +i on the EMPTY mountpoint directories on the SSD (xp, work, .codex, Downloads, ... every bind target
#     in fstab): while the disk is missing nothing - a service, docker's bind-source mkdir, a stray `mkdir -p` -
#     can create files there. The bind mount over the directory is not affected by the flag on the directory below.
#     Reached WITHOUT unmounting anything: `mount --bind / <tmp>` (non-recursive, so the submounts are not carried
#     along) shows the underlying directories; nothing has to be stopped. A non-empty underlying directory is
#     reported (those are files that were written while the disk was missing) and left alone.
#
# Every edited file is copied to <file>.bak-harden-<timestamp> first; created files carry the marker
# "harden-backups-mount". --revert removes the created files, reverses the fstab edit and clears the flags.
#
# Test without a reboot, and the reboot checklist: README.md next to this script (.files repo).
set -euo pipefail

MP="${BACKUPS_MP:-/run/media/ultra/backups}"          # where the disk is mounted
USER_NAME="${BACKUPS_USER:-ultra}"                     # owner of the home that holds the bind targets
MARK="harden-backups-mount"
TS=$(date +%Y%m%dT%H%M%S)
DRY=0
REVERT=0

for a in "$@"; do
  case $a in
    --dry-run) DRY=1 ;;
    --revert) REVERT=1 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown option: $a (use --dry-run, --revert)" >&2; exit 2 ;;
  esac
done

if [ "$DRY" = 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo $0 ${*:-}   (add --dry-run to preview without root)" >&2; exit 1
fi

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then say "  [dry-run] $*"; else "$@"; fi; }

MP_UNIT=$(systemd-escape -p --suffix=mount "$MP")
USER_UID=$(id -u "$USER_NAME")
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
UDEV_RULE=/etc/udev/rules.d/90-backups-disk.rules
ATTACH_BIN=/usr/local/sbin/backups-attach
ATTACH_UNIT=/etc/systemd/system/backups-attach.service
# a dedicated file name, so an existing mounts.conf of someone else is never overwritten and --revert never deletes it
DROPINS=("/etc/systemd/system/user@$USER_UID.service.d/backups-mounts.conf")
if systemctl cat docker.service >/dev/null 2>&1; then DROPINS+=("/etc/systemd/system/docker.service.d/backups-mounts.conf"); fi

# the disk's UUID and ext4 mount line come from fstab, not from this script
FSTAB_LINE=$(awk -v mp="$MP" '!/^[[:space:]]*#/ && NF>=4 && $2==mp && $3=="ext4" {print; exit}' /etc/fstab)
[ -n "$FSTAB_LINE" ] || { echo "no ext4 line for $MP in /etc/fstab" >&2; exit 1; }
FS_UUID=$(printf '%s\n' "$FSTAB_LINE" | awk '{print $1}' | sed 's/^UUID=//')
case $FS_UUID in ??*-*-*-*-*) ;; *) echo "fstab line for $MP does not use UUID=...: $FSTAB_LINE" >&2; exit 1 ;; esac

# bind targets fstab keeps on this disk: every line whose source is under the mount point and has the bind option
bind_targets() { awk -v mp="$MP/" '!/^[[:space:]]*#/ && NF>=4 && index($1, mp)==1 && $4 ~ /(^|,)bind(,|$)/ {print $2}' /etc/fstab; }

# write $2 (content) to $1 with mode $3; back a differing file up; skip when identical
install_file() {
  local path=$1 content=$2 mode=${3:-0644} tmp
  if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ]; then say "  already in place  $path"; return 0; fi
  if [ -f "$path" ]; then say "  backup            $path -> $path.bak-harden-$TS"; run cp -p "$path" "$path.bak-harden-$TS"; fi
  say "  write             $path"
  if [ "$DRY" = 1 ]; then printf '%s\n' "$content" | sed 's/^/      | /'; return 0; fi
  mkdir -p "$(dirname "$path")"
  tmp=$(mktemp "$path.XXXXXX"); printf '%s\n' "$content" > "$tmp"; chmod "$mode" "$tmp"; mv "$tmp" "$path"
}

remove_ours() {
  local path=$1
  if [ ! -e "$path" ]; then say "  already absent    $path"; return 0; fi
  if ! grep -q "$MARK" "$path"; then say "  NOT removed       $path (no '$MARK' marker: not created by this script)"; return 0; fi
  say "  remove            $path"; run rm -f "$path"
  run rmdir --ignore-fail-on-non-empty "$(dirname "$path")" 2>/dev/null || true
}

# ---- 1. fstab ---------------------------------------------------------------------------------------------------
rewrite_fstab_line() { # $1 = apply|revert; prints the whole fstab with only the disk line rewritten
  awk -v mp="$MP" -v mode="$1" '
    /^[[:space:]]*#/ || NF < 4 { print; next }
    $2 == mp && $3 == "ext4" {
      n = split($4, o, ","); out = ""; havet = 0; havee = 0
      for (i = 1; i <= n; i++) {
        x = o[i]
        if (x ~ /^x-systemd\.device-timeout=/) { havet = 1; x = (mode == "apply") ? "x-systemd.device-timeout=30s" : "x-systemd.device-timeout=10s" }
        if (x ~ /^errors=/) { havee = 1; if (mode == "revert" && x == "errors=remount-ro") continue }
        out = out (out == "" ? "" : ",") x
      }
      if (mode == "apply" && !havet) out = out ",x-systemd.device-timeout=30s"
      if (mode == "apply" && !havee) out = out ",errors=remount-ro"
      $4 = out; print; next
    }
    { print }' /etc/fstab
}

# parse errors + errors reported by `findmnt --verify` for a table file
# `findmnt --verify` exits 1 when it finds errors, which is exactly the case being measured (a missing disk is one):
# the status is ignored on purpose so `set -e` / pipefail do not end the script before the counts are compared
fstab_errors() { { findmnt --verify --tab-file "$1" 2>&1 || true; } | awk '/ parse errors?, [0-9]+ errors?/ {print $1 + $4; found=1} END {if (!found) print 999}'; }

do_fstab() {
  local mode tmp
  mode=apply; [ "$REVERT" = 1 ] && mode=revert
  step "1. /etc/fstab: disk line ($mode)"
  tmp=$(mktemp); rewrite_fstab_line "$mode" > "$tmp"
  if cmp -s /etc/fstab "$tmp"; then say "  already in place  /etc/fstab"; rm -f "$tmp"; return 0; fi
  diff -u --label /etc/fstab --label /etc/fstab.new /etc/fstab "$tmp" | sed 's/^/      | /' || true
  # the rewritten table must not report more findmnt --verify errors than the live one does
  local before after
  before=$(fstab_errors /etc/fstab); after=$(fstab_errors "$tmp")
  if [ "$after" -gt "$before" ]; then
    echo "  ABORT: findmnt --verify reports $after errors for the new fstab (live one: $before); nothing written" >&2; rm -f "$tmp"; exit 1
  fi
  say "  findmnt --verify  $after errors (live fstab: $before)"
  say "  backup            /etc/fstab -> /etc/fstab.bak-harden-$TS"
  run cp -p /etc/fstab "/etc/fstab.bak-harden-$TS"
  say "  write             /etc/fstab"
  if [ "$DRY" = 0 ]; then cat "$tmp" > /etc/fstab; fi
  rm -f "$tmp"
  run systemctl daemon-reload
  if [ "$mode" = apply ]; then
    # applies errors=remount-ro to the mounted disk now; the new device timeout is used from the next mount job on
    say "  remount           $MP (picks up errors=remount-ro; failure is not fatal)"
    run mount -o remount "$MP" || say "  WARNING: remount failed, the options apply at the next mount"
  else
    say "  note: errors=remount-ro stays on the mounted disk until the next mount; nothing was remounted"
  fi
}

# ---- 2. udev rule + attach service ------------------------------------------------------------------------------
attach_script() {
  cat <<EOF
#!/usr/bin/env bash
# $MARK: installed by harden-backups-mount.sh (rig-cli ticket 858). Started by backups-attach.service, which the
# udev rule $UDEV_RULE wants when the backups partition appears (late plug-in, slow spin-up, hot swap) and
# which is harmless at boot: everything already mounted means nothing to do.
set -u
MP='$MP'
USER_NAME='$USER_NAME'
log() { echo "backups-attach: \$*"; }
rc=0
systemctl start '$MP_UNIT' || { log "cannot mount \$MP"; exit 1; }
mountpoint -q "\$MP" || { log "\$MP is not a mount point after start"; exit 1; }
# every bind mount fstab keeps on this disk, read now so binds added later are covered without editing this file
while read -r target; do
  [ -n "\$target" ] || continue
  mountpoint -q "\$target" && continue
  unit=\$(systemd-escape -p --suffix=mount "\$target")
  if systemctl start "\$unit"; then log "mounted \$target"; else log "FAILED to mount \$target"; rc=1; fi
done < <(awk -v mp="\$MP/" '!/^[[:space:]]*#/ && NF>=4 && index(\$1, mp)==1 && \$4 ~ /(^|,)bind(,|\$)/ {print \$2}' /etc/fstab)
# start the user units whose start was skipped (ConditionPathIsMountPoint) while the mounts were missing
uid=\$(id -u "\$USER_NAME")
if [ -S "/run/user/\$uid/bus" ]; then
  runuser -u "\$USER_NAME" -- env XDG_RUNTIME_DIR="/run/user/\$uid" systemctl --user start --no-block backups-mount-retry.service || true
fi
exit \$rc
EOF
}

attach_unit() {
  cat <<EOF
# $MARK (rig-cli ticket 858): not wanted by any target, only started by $UDEV_RULE
[Unit]
Description=Mount the backups disk and its bind mounts (late plug-in or slow spin-up)
After=local-fs.target

[Service]
Type=oneshot
ExecStart=$ATTACH_BIN
# infinity: the start waits for the mount job, which waits for fsck; a long check of a 1 TB disk after an unclean
# unplug must not kill this helper before it has mounted the binds (nothing would restart it). The mount job itself
# has its own timeout, so a hung mount still ends the service.
TimeoutStartSec=infinity
EOF
}

udev_rule() {
  cat <<EOF
# $MARK (rig-cli ticket 858): when the backups partition shows up (after boot too), start backups-attach.service
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_UUID}=="$FS_UUID", TAG+="systemd", ENV{SYSTEMD_WANTS}+="backups-attach.service"
EOF
}

do_attach() {
  step "2. late plug-in: udev rule, backups-attach.service, $ATTACH_BIN"
  if [ "$REVERT" = 1 ]; then
    remove_ours "$UDEV_RULE"; remove_ours "$ATTACH_UNIT"; remove_ours "$ATTACH_BIN"
    run udevadm control --reload; run systemctl daemon-reload
    return 0
  fi
  install_file "$ATTACH_BIN" "$(attach_script)" 0755
  install_file "$ATTACH_UNIT" "$(attach_unit)" 0644
  install_file "$UDEV_RULE" "$(udev_rule)" 0644
  run udevadm control --reload
  run systemctl daemon-reload
  say "  bash -n on the generated script: $(attach_script | bash -n && echo OK)"
}

# ---- 3. user@ and docker ordering drop-ins ----------------------------------------------------------------------
dropin_content() {
  local u
  cat <<EOF
# $MARK (rig-cli ticket 858): start only after the backups disk and its bind mounts have been tried.
# After= only (no Requires/Wants): with the disk unplugged this unit must still start; the mounts' own jobs
# already run at boot, After= just makes the order explicit instead of relying on local-fs.target alone.
[Unit]
After=$MP_UNIT
EOF
  while read -r t; do
    [ -n "$t" ] || continue
    u=$(systemd-escape -p --suffix=mount "$t"); printf 'After=%s\n' "$u"
  done < <(bind_targets)
}

do_dropins() {
  step "3. ordering drop-ins (user manager and docker start after the mount jobs)"
  local f
  for f in "${DROPINS[@]}"; do
    if [ "$REVERT" = 1 ]; then remove_ours "$f"; else install_file "$f" "$(dropin_content)" 0644; fi
  done
  run systemctl daemon-reload
  [ "$REVERT" = 1 ] || say "  note: a changed drop-in is read now, but user@$USER_UID.service / docker.service are NOT restarted; it applies at the next boot"
}

# ---- 4. chattr +i on the empty mountpoint directories ----------------------------------------------------------
do_chattr() {
  local flag=+i verb="protect"; [ "$REVERT" = 1 ] && { flag=-i; verb="release"; }
  step "4. chattr $flag on the mountpoint directories below the mounts ($verb)"
  local targets; targets=$(bind_targets)
  if [ "$DRY" = 1 ]; then
    local t
    while read -r t; do say "  [dry-run] would chattr $flag  $t   (underlying dir checked empty at run time through 'mount --bind / <tmp>')"; done <<< "$targets"
    return 0
  fi
  if [ "$(stat -c %d /)" != "$(stat -c %d "$HOME_DIR")" ]; then
    say "  SKIPPED: $HOME_DIR is on a different filesystem than /; a bind of / would not reach the mountpoint dirs"; return 0
  fi
  local rv; rv=$(mktemp -d /run/harden-backups-rootview.XXXXXX)
  # shellcheck disable=SC2064
  trap "umount '$rv' 2>/dev/null; rmdir '$rv' 2>/dev/null" EXIT
  mount --bind / "$rv"; mount --make-private "$rv"
  local t d n=0 skipped=0
  while read -r t; do
    [ -n "$t" ] || continue
    d="$rv$t"
    if [ ! -d "$d" ]; then say "  skip (no such dir on the SSD)  $t"; skipped=$((skipped+1)); continue; fi
    # shellcheck disable=SC2012
    if [ "$REVERT" = 0 ] && [ -n "$(ls -A "$d")" ]; then
      say "  NOT PROTECTED, NOT EMPTY on the SSD  $t  ($(ls -A "$d" | wc -l) entries, $(du -sh "$d" 2>/dev/null | cut -f1)):"
      find "$d" -mindepth 1 -maxdepth 1 | awk 'NR <= 5 {print "      | " $0}'   # awk reads it all: no SIGPIPE under pipefail
      say "      -> written while the disk was missing. Review, move/delete, then: chattr +i '$t' (through a root view, see docs)"
      skipped=$((skipped+1)); continue
    fi
    local flags; flags=$(lsattr -d "$d" | awk '{print $1}')   # a variable, not a pipe into grep -q: no SIGPIPE under pipefail
    if [[ $flags == *i* ]]; then
      if [ "$flag" = +i ]; then say "  already immutable  $t"; continue; fi
    elif [ "$flag" = -i ]; then say "  already mutable    $t"; continue; fi
    chattr "$flag" "$d"; say "  chattr $flag         $t"; n=$((n+1))
  done <<< "$targets"
  umount "$rv"; rmdir "$rv"; trap - EXIT
  say "  $n changed, $skipped skipped"
}

# ---- run --------------------------------------------------------------------------------------------------------
say "harden-backups-mount: $([ "$REVERT" = 1 ] && echo REVERT || echo apply)$([ "$DRY" = 1 ] && echo ' (dry-run: nothing is changed)')"
say "disk UUID $FS_UUID, mount point $MP, user $USER_NAME (uid $USER_UID), $(bind_targets | wc -l) bind mounts in fstab"
do_fstab
do_attach
do_dropins
do_chattr
step "done"
if [ "$DRY" = 1 ]; then say "  dry-run only; run without --dry-run (as root) to apply"; else
  say "  verify: findmnt $MP; systemctl cat user@$USER_UID.service | grep After; lsattr -d on a root view (see docs)"
fi
