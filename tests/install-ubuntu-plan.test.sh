#!/usr/bin/env bash
# Read-only checks of install-ubuntu.sh against a scratch HOME (no network, nothing outside it):
#   bash tests/install-ubuntu-plan.test.sh
# Covers the marker-block merge: fresh file, current block, duplicate block, unpaired markers, and
# that --plan/--check never write.
set -u

repo=$(cd "$(dirname "$0")/.." && pwd -P)
fail=0
check() { # check <description> <command...>
  local what="$1"; shift
  if "$@"; then echo "ok:   $what"; else echo "FAIL: $what"; fail=1; fi
}

home=$(mktemp -d)
trap 'rm -rf "$home"' EXIT
ln -s "$repo" "$home/.files"
printf '# machine bashrc\nalias ll=ls\n' > "$home/.bashrc"
printf '# machine zshrc\n' > "$home/.zshrc"
: > "$home/.profile"
before=$(cd "$home" && find . -path ./.files -prune -o -type f -print0 | sort -z | xargs -0 sha256sum)

run() { HOME="$home" XDG_CONFIG_HOME="$home/.config" bash "$repo/install-ubuntu.sh" "$@" 2>&1; }

plan=$(run --plan)
check "plan proposes the bashrc block" grep -q "pending: add the .files:bashrc block" <<< "$plan"
check "plan proposes the gitconfig include" grep -q "pending: include ~/.files/.gitconfig.shared" <<< "$plan"
check "plan proposes the rig config merge" grep -q "pending: merge rig/config.yaml" <<< "$plan"
after=$(cd "$home" && find . -path ./.files -prune -o -type f -print0 | sort -z | xargs -0 sha256sum)
check "plan writes nothing" test "$before" = "$after"

# a duplicate block is reported for refresh, an unpaired marker is a manual step and is never rewritten
begin='# >>> .files:bashrc (managed by ~/.files/install-ubuntu.sh; edit there) >>>'
end='# <<< .files:bashrc <<<'
printf '%s\nx\n%s\n%s\ny\n%s\n' "$begin" "$end" "$begin" "$end" >> "$home/.bashrc"
check "duplicate block is refreshed" grep -q "pending: refresh the .files:bashrc block" <<< "$(run --plan)"

printf '%s\nz\n' "$begin" >> "$home/.bashrc"
unpaired=$(run --plan)
check "unpaired markers are a manual step" grep -q "manual: .*unpaired .files:bashrc markers" <<< "$unpaired"
check "unpaired markers do not offer a rewrite" bash -c '! grep -q "refresh the .files:bashrc" <<< "$1"' _ "$unpaired"

check "--check exits 1 while something is pending" bash -c 'HOME="$1" XDG_CONFIG_HOME="$1/.config" bash "$2/install-ubuntu.sh" --check >/dev/null 2>&1; [ $? -eq 1 ]' _ "$home" "$repo"

exit $fail
