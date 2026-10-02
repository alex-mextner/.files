#!/bin/bash
# Detached uploader: offload screenshots to Google Drive (gdrive remote).
# Started 2026-08-06 by agent after rclone OAuth completion. Does NOT delete anything local.
set -u
LOG=/Users/ultra/.files/rclone-upload.log
COMMON=(--transfers 4 --checkers 8 --drive-chunk-size 64M --log-file "$LOG" --log-level INFO --stats 30s --stats-log-level NOTICE)

echo "=== $(date '+%F %T') runner start (pid $$) ===" >> "$LOG"

rclone copy /Users/ultra/Documents/Screenshots "gdrive:MacBook-offload/Screenshots" "${COMMON[@]}"
echo "=== $(date '+%F %T') Screenshots dir copy exit: $? ===" >> "$LOG"

rclone copy /Users/ultra/Downloads "gdrive:MacBook-offload/Downloads-screenshots" \
  --files-from /Users/ultra/.files/downloads-screenshots-list-rel.txt "${COMMON[@]}"
echo "=== $(date '+%F %T') Downloads screenshots copy exit: $? ===" >> "$LOG"

echo "=== $(date '+%F %T') runner done ===" >> "$LOG"
