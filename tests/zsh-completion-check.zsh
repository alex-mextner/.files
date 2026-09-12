#!/usr/bin/env zsh

setopt errexit nounset pipefail

typeset -r ROOT=${0:A:h:h}
typeset -r ZSH=${commands[zsh]:-zsh}
typeset -r ORIGINAL_PATH=$PATH
typeset -r RUN_DIR="$ROOT/tests/.zsh-completion-check.$$"
typeset -r MANAGER="$ROOT/bin/zsh-completion"
typeset -r FIXTURE_DIR="$RUN_DIR/bin"
typeset -r STATE_DIR="$RUN_DIR/state"
typeset -r DATA_DIR="$RUN_DIR/data"
typeset -r HISTORY_FILE="$RUN_DIR/history"
typeset -r PROBE_MARKER="$RUN_DIR/probes"
typeset -r UNSAFE_MARKER="$RUN_DIR/unsafe-probe"
typeset -r SOURCE_MARKER="$RUN_DIR/source-loaded"

cleanup() {
  rm -rf "$RUN_DIR"
}
trap cleanup EXIT INT TERM

fail() {
  print -u2 -- "zsh-completion-check: $*"
  exit 1
}

assert_contains() {
  local needle=$1
  local file=$2
  grep -F -- "$needle" "$file" >/dev/null ||
    fail "expected $(print -r -- "$needle") in $file"
}

mkdir -p "$FIXTURE_DIR" "$STATE_DIR" "$DATA_DIR"

cat > "$FIXTURE_DIR/direct-tool" <<'EOF'
#!/bin/sh
printf x >> "$PROBE_MARKER"
if [ "$1" = --help ]; then
  printf 'completion support\n'
  exit 0
fi
if [ "$1" = completion ] && [ "$2" = zsh ]; then
  printf '#compdef direct-tool\n_direct_tool() { :; }\n'
  exit 0
fi
exit 2
EOF

cat > "$FIXTURE_DIR/flag-tool" <<'EOF'
#!/bin/sh
if [ "$1" = --help ]; then
  printf 'completion support\n'
  exit 0
fi
if [ "$1" = --completion ] && [ "$2" = zsh ]; then
  printf '#compdef flag-tool\n_flag_tool() { :; }\n'
  exit 0
fi
exit 2
EOF

cat > "$FIXTURE_DIR/retry-tool" <<'EOF'
#!/bin/sh
if [ "$1" = --help ]; then
  printf 'completion support\n'
  exit 0
fi
exit 2
EOF
cat > "$FIXTURE_DIR/source-tool" <<'EOF'
#!/bin/sh
if [ "$1" = --help ]; then
  printf 'completion support\n'
  exit 0
fi
if [ "$1" = completion ] && [ "$2" = zsh ]; then
  printf '# generated source completion\ncompdef _source_tool_completion source-tool\n_source_tool_completion() { printf x >> "$SOURCE_MARKER"; }\n'
  exit 0
fi
exit 2
EOF
cat > "$FIXTURE_DIR/no-completion-tool" <<'EOF'
#!/bin/sh
if [ "$1" = --help ]; then
  printf 'ordinary utility\n'
  exit 0
fi
printf x >> "$UNSAFE_MARKER"
exit 0
EOF
chmod +x "$FIXTURE_DIR"/*

printf ': 1:0;direct-tool\n: 2:0;direct-tool\nflag-tool\n' > "$HISTORY_FILE"

run_manager() {
  local action=$1
  shift
  env \
    PATH="$FIXTURE_DIR:$ORIGINAL_PATH" \
    PROBE_MARKER="$PROBE_MARKER" \
    UNSAFE_MARKER="$UNSAFE_MARKER" \
    SOURCE_MARKER="$SOURCE_MARKER" \
    ZSH_COMPLETION_SCAN_PATH="$FIXTURE_DIR" \
    "$ZSH" -df "$MANAGER" "$action" "$@"
}

run_trusted_manager() {
  local action=$1
  shift
  run_manager "$action" \
    --command direct-tool --command flag-tool --command retry-tool --command source-tool \
    --trust-path "$FIXTURE_DIR" \
    "$@"
}

run_trusted_manager discover --state-dir "$STATE_DIR" --data-dir "$DATA_DIR" \
  --history-file "$HISTORY_FILE" --timeout 1 >/dev/null

assert_contains $'direct-tool\t_direct-tool\t' "$STATE_DIR/registry.tsv"
assert_contains $'\tcompletion\tdirect-tool completion zsh\tpending\t' "$STATE_DIR/registry.tsv"
assert_contains $'\t2\t' "$STATE_DIR/registry.tsv"
assert_contains $'\t--completion\tflag-tool --completion zsh\tpending\t' "$STATE_DIR/registry.tsv"
assert_contains $'retry-tool\t_retry-tool\t' "$STATE_DIR/registry.tsv"
grep -Eq $'^retry-tool\t_retry-tool\t.*\tnone\t\tfailed\t' "$STATE_DIR/registry.tsv" ||
  fail "failed registration lost its empty invocation column"
[[ ! -e "$UNSAFE_MARKER" ]] || fail "ordinary utility received completion probe arguments"

typeset first_probe_count=$(wc -c < "$PROBE_MARKER" | tr -d ' ')
run_manager discover --state-dir "$STATE_DIR" --data-dir "$DATA_DIR" \
  --history-file "$HISTORY_FILE" --timeout 1 >/dev/null
typeset second_probe_count=$(wc -c < "$PROBE_MARKER" | tr -d ' ')
[[ "$first_probe_count" == "$second_probe_count" ]] ||
  fail "unchanged snapshot was probed again"

run_manager install --yes --state-dir "$STATE_DIR" --data-dir "$DATA_DIR" \
  --history-file "$HISTORY_FILE" --timeout 1 >/dev/null
[[ -s "$DATA_DIR/completions/_direct-tool" ]] || fail "direct completion was not installed"
[[ -s "$DATA_DIR/completions/_flag-tool" ]] || fail "flag completion was not installed"
[[ -s "$DATA_DIR/completions/_source-tool" ]] || fail "source completion wrapper was not installed"
[[ -s "$DATA_DIR/sources/source-tool.zsh" ]] || fail "source completion script was not preserved"
assert_contains "source $DATA_DIR/sources/source-tool.zsh" "$DATA_DIR/completions/_source-tool"
assert_contains $'\tinstalled\t' "$STATE_DIR/registry.tsv"
assert_contains "$DATA_DIR/completions" "$STATE_DIR/init.zsh"
"$ZSH" -df -c \
  'fpath=(/existing); source "$1"; [[ "$fpath[1]" == "$2" ]]' \
  zsh "$STATE_DIR/init.zsh" "$DATA_DIR/completions"
env SOURCE_MARKER="$SOURCE_MARKER" "$ZSH" -df -c \
  'compdef() { :; }; fpath=("$1"); autoload -Uz _source-tool; _source-tool; [[ -s "$2" ]]' \
  zsh "$DATA_DIR/completions" "$SOURCE_MARKER"

run_manager status --state-dir "$STATE_DIR" --data-dir "$DATA_DIR" > "$RUN_DIR/status"
assert_contains "direct-tool — found via completion; rarely used (2 time(s))" "$RUN_DIR/status"
assert_contains "flag-tool — found via --completion" "$RUN_DIR/status"

cat > "$FIXTURE_DIR/retry-tool" <<'EOF'
#!/bin/sh
if [ "$1" = --help ]; then
  printf 'completion support\n'
  exit 0
fi
if [ "$1" = completion ] && [ "$2" = zsh ]; then
  printf '#compdef retry-tool\n_retry_tool() { :; }\n'
  exit 0
fi
exit 2
EOF
chmod +x "$FIXTURE_DIR/retry-tool"
run_manager discover --force --state-dir "$STATE_DIR" --data-dir "$DATA_DIR" \
  --history-file "$HISTORY_FILE" --timeout 1 >/dev/null
run_manager install --yes --state-dir "$STATE_DIR" --data-dir "$DATA_DIR" \
  --history-file "$HISTORY_FILE" --timeout 1 >/dev/null
[[ -s "$DATA_DIR/completions/_retry-tool" ]] || fail "failed registration was not retried"

print -- "zsh-completion checks passed"
