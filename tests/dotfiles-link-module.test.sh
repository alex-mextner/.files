#!/usr/bin/env bash
# Exit-code mapping of the dotfiles-link profile module, against a stub installer and a stub rig:
#   bash tests/dotfiles-link-module.test.sh
set -u

repo=$(cd "$(dirname "$0")/.." && pwd -P)
fail=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/tree/ubuntu/modules/dotfiles-link" "$tmp/bin"
cp "$repo/ubuntu/modules/dotfiles-link/install.sh" "$tmp/tree/ubuntu/modules/dotfiles-link/install.sh"
# the installer stub exits with $STUB_RC and logs the mode it was called with
cat > "$tmp/tree/install-ubuntu.sh" <<'EOF'
#!/usr/bin/env bash
echo "installer ${1:-apply}" >> "$STUB_LOG"
exit "${STUB_RC:-0}"
EOF
# the rig stub: `status` exits $RIG_STATUS_RC, `apply commit` exits $RIG_APPLY_RC
cat > "$tmp/bin/rig" <<'EOF'
#!/usr/bin/env bash
echo "rig $*" >> "$STUB_LOG"
case "$1" in status) exit "${RIG_STATUS_RC:-0}" ;; apply) exit "${RIG_APPLY_RC:-0}" ;; esac
EOF
chmod +x "$tmp/tree/install-ubuntu.sh" "$tmp/bin/rig"

module="$tmp/tree/ubuntu/modules/dotfiles-link/install.sh"
export STUB_LOG="$tmp/log"

# expect <description> <expected exit> <env assignments...> -- <module args...>
expect() {
  local what="$1" want="$2"; shift 2
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  : > "$STUB_LOG"
  env "${envs[@]}" PATH="$tmp/bin:$PATH" bash "$module" "$@" >/dev/null 2>&1
  local got=$?
  if [ "$got" -eq "$want" ]; then echo "ok:   $what"; else echo "FAIL: $what (exit $got, want $want)"; fail=1; fi
}
ran() { grep -q "$1" "$STUB_LOG"; }

expect "--check: clean installer and clean rig is 0" 0 STUB_RC=0 RIG_STATUS_RC=0 -- --check
expect "--check: installer pending (1) is drift 3" 3 STUB_RC=1 -- --check
expect "--check: rig drift is 3" 3 STUB_RC=0 RIG_STATUS_RC=3 -- --check
expect "--check: rig drift still counts while the installer has a manual step" 3 STUB_RC=10 RIG_STATUS_RC=3 -- --check
expect "--check: a manual step alone stays 10" 10 STUB_RC=10 RIG_STATUS_RC=0 -- --check
expect "apply: installer ok then rig apply commit is 0" 0 STUB_RC=0 -- ""
ran "rig apply commit" && echo "ok:   apply ran rig apply commit" || { echo "FAIL: apply ran rig apply commit"; fail=1; }
expect "apply: a manual installer step skips rig apply and stays 10" 10 STUB_RC=10 -- ""
ran "rig apply" && { echo "FAIL: rig apply must not run after a manual installer step"; fail=1; } || echo "ok:   no rig apply after a manual installer step"
expect "apply: rig apply failure is a manual step (10)" 10 STUB_RC=0 RIG_APPLY_RC=1 -- ""
expect "usage error is 2" 2 -- --bogus

# without rig on PATH the apply path asks for it
: > "$STUB_LOG"
PATH="/usr/bin:/bin" bash "$module" >/dev/null 2>&1
[ $? -eq 10 ] && echo "ok:   apply without rig is 10" || { echo "FAIL: apply without rig is 10"; fail=1; }

exit $fail
