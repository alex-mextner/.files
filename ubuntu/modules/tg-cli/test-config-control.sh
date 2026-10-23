#!/usr/bin/env bash
# Fixture test of config_control.py (the one piece of the tg-cli module that rewrites a user file):
# every supported config shape ends with `control.enabled: false` and keeps every other key; every shape
# it cannot edit safely is refused (exit 4) and left untouched.  Run: bash test-config-control.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
file="$tmp/config.yaml"
fail() { echo "FAIL: $1" >&2; exit 1; }
cc() { python3 -B "$here/config_control.py" "$file" "$1"; }
exit_of() { local rc=0; "$@" 2>/dev/null || rc=$?; echo "$rc"; }

expect() { # expect NAME INPUT EXPECTED-AFTER-APPLY
  printf '%b' "$2" > "$file"
  [ "$(exit_of cc check)" = 3 ] || fail "$1: check must report drift (3)"
  cc apply
  cc check || fail "$1: check fails after apply"
  [ "$(cat "$file")" = "$(printf '%b' "$3")" ] || fail "$1: got $(cat "$file")"
  cc apply   # idempotent
  [ "$(cat "$file")" = "$(printf '%b' "$3")" ] || fail "$1: second apply changed the file"
}
refuse() { # refuse NAME INPUT: exit 4 from check and apply, file untouched
  printf '%b' "$2" > "$file"
  [ "$(exit_of cc check)" = 4 ] || fail "$1: check must exit 4"
  [ "$(exit_of cc apply)" = 4 ] || fail "$1: apply must exit 4"
  [ "$(cat "$file")" = "$(printf '%b' "$2")" ] || fail "$1: the file changed"
}

expect "empty file" "" "control:\n  enabled: false"
expect "control block without the key" "control:\n  private_topics: true\nvoice:\n  enabled: true\n" "control:\n  enabled: false\n  private_topics: true\nvoice:\n  enabled: true"
expect "enabled: true" "control:\n  enabled: true\n  x: 1\n" "control:\n  enabled: false\n  x: 1"
expect "no control block" "voice:\n  enabled: true\n" "control:\n  enabled: false\nvoice:\n  enabled: true"
expect "voice.enabled is not control.enabled" "voice:\n  enabled: true\ncontrol:\n  private_topics: true\n" "voice:\n  enabled: true\ncontrol:\n  enabled: false\n  private_topics: true"
expect "4-space indent" "control:\n    private_topics: true\n" "control:\n    enabled: false\n    private_topics: true"
expect "a nested enabled is not control.enabled" "control:\n  topics:\n    enabled: true\n" "control:\n  enabled: false\n  topics:\n    enabled: true"
refuse "flow mapping" "control: {enabled: true}\n"
refuse "quoted key" "\"control\":\n  enabled: true\n"
refuse "list body" "control:\n  - a\n"
refuse "anchored enabled value" "control:\n  enabled: &on true\n"
refuse "document marker" "---\nvoice:\n  a: 1\n"
refuse "BOM" "\xef\xbb\xbfvoice:\n  a: 1\n"
refuse "flow body on its own line" "control:\n  { enabled: true }\n"
refuse "scalar body" "control:\n  some text\n"
refuse "duplicate control" "control:\n  a: 1\ncontrol:\n  b: 2\n"
# a symlinked config is edited in place, the link survives
printf 'voice:\n  a: 1\n' > "$tmp/real.yaml"; ln -sf "$tmp/real.yaml" "$tmp/link.yaml"
python3 -B "$here/config_control.py" "$tmp/link.yaml" apply
[ -L "$tmp/link.yaml" ] || fail "the symlink was replaced by a file"
grep -q 'enabled: false' "$tmp/real.yaml" || fail "the link target was not edited"
printf 'control:\n  private_topics: true\n  enabled: false\n' > "$file"
cc check || fail "already false must pass check"
[ "$(exit_of cc rewrite)" = 2 ] || fail "an unknown action must exit 2 and not write"
echo "ok"
