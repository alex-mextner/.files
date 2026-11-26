#!/usr/bin/env bash
# Checks of harness-wrappers.sh against a scratch HOME (no network, nothing outside it):
#   bash tests/harness-wrappers.test.sh
# Covers the helper itself in bash, zsh and dash (order, no duplicate entry, literal match of a HOME with
# glob characters, no change without the directory, the REVIEW_MAX_CONCURRENCY default) and the startup
# chains it exists for: the profile/bashrc/zshrc blocks install-ubuntu.sh merges, run as login and
# non-login interactive shells, must resolve omp to the wrapper dir and not to ~/.local/bin.
set -u

repo=$(cd "$(dirname "$0")/.." && pwd -P)
fail=0
check() { # check <description> <command...>
  local what="$1"; shift
  if "$@"; then echo "ok:   $what"; else echo "FAIL: $what"; fail=1; fi
}

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
script="$repo/harness-wrappers.sh"

# fake_home <dir>: a HOME with a real omp in ~/.local/bin and the wrapper dir holding the wrapper
fake_home() {
  mkdir -p "$1/.local/bin" "$1/.local/share/rig/bin"
  printf '#!/bin/sh\n' > "$1/.local/bin/omp"
  printf '#!/bin/sh\n' > "$1/.local/share/rig/bin/omp"
  chmod +x "$1/.local/bin/omp" "$1/.local/share/rig/bin/omp"
  ln -s "$repo" "$1/.files"
}

# -- the helper ----------------------------------------------------------------------------------------
home="$scratch/home"
fake_home "$home"
wrappers="$home/.local/share/rig/bin"

# first_path <shell> <starting PATH> [env assignment...]: the PATH after sourcing the helper
first_path() {
  local shell="$1" start="$2"; shift 2
  env -i HOME="$home" "$@" "$shell" -c "PATH='$start'; . '$script'; printf '%s' \"\$PATH\""
}

for shell in bash zsh dash; do
  command -v "$shell" >/dev/null 2>&1 || { echo "skip: $shell is not installed"; continue; }

  out=$(first_path "$shell" "/usr/bin:$home/.local/bin:/bin")
  check "$shell: wrapper dir comes first, the rest keeps its order" test "$out" = "$wrappers:/usr/bin:$home/.local/bin:/bin"

  out=$(first_path "$shell" "$home/.local/bin:/usr/bin:$wrappers:/bin")
  check "$shell: a later entry is moved to the front, once" test "$out" = "$wrappers:$home/.local/bin:/usr/bin:/bin"

  out=$(first_path "$shell" "$wrappers:$wrappers:/usr/bin:$wrappers")
  check "$shell: adjacent and repeated entries collapse to one" test "$out" = "$wrappers:/usr/bin"

  out=$(env -i HOME="$home" REVIEW_MAX_CONCURRENCY=5 "$shell" -c ". '$script'; printf '%s' \"\$REVIEW_MAX_CONCURRENCY\"")
  check "$shell: an explicit REVIEW_MAX_CONCURRENCY wins" test "$out" = 5
  out=$(env -i HOME="$home" "$shell" -c ". '$script'; printf '%s' \"\${REVIEW_MAX_CONCURRENCY-unset}\"")
  check "$shell: REVIEW_MAX_CONCURRENCY defaults to 2" test "$out" = 2

  empty="$scratch/empty"; mkdir -p "$empty"
  out=$(env -i HOME="$empty" "$shell" -c "PATH='/usr/bin:/bin'; . '$script'; printf '%s|%s' \"\$PATH\" \"\${REVIEW_MAX_CONCURRENCY-unset}\"")
  check "$shell: no wrapper dir, nothing changes" test "$out" = "/usr/bin:/bin|unset"

  # a HOME with glob characters must not make the helper drop a different PATH entry
  globhome="$scratch/h[o]me"
  fake_home "$globhome"
  out=$(env -i HOME="$globhome" "$shell" -c "PATH='$scratch/home/.local/share/rig/bin:/usr/bin'; . '$script'; printf '%s' \"\$PATH\"")
  check "$shell: glob characters in HOME are matched literally" test "$out" = "$globhome/.local/share/rig/bin:$scratch/home/.local/share/rig/bin:/usr/bin"
  rm -rf "$globhome"
done

# -- the startup chains install-ubuntu.sh merges ---------------------------------------------------------
# body <name>: the single-quoted body of <name>_body in install-ubuntu.sh, exactly as the installer writes it
body() {
  awk -v name="$1" '
    !on && index($0, name "_body=\047") == 1 { on = 1; sub(/^[a-z]+_body=\047/, "") }
    on { out = $0; done = (out ~ /\047$/); if (done) sub(/\047$/, "", out); print out; if (done) exit }' "$repo/install-ubuntu.sh"
}

chain="$scratch/chain"
fake_home "$chain"
ln -s "$repo/.zprofile" "$chain/.zprofile"
ln -s "$repo/.zshrc" "$chain/.zshrc"
{
  echo '# >>> .files:profile >>>'; body profile; echo '# <<< .files:profile <<<'
} > "$chain/.profile"
{
  echo '# machine bashrc: a PATH edit before the block, as ubuntu-pc has'
  echo 'export PATH="$HOME/.local/bin:$PATH"'
  echo '# >>> .files:bashrc >>>'; body bashrc; echo '# <<< .files:bashrc <<<'
} > "$chain/.bashrc"
for f in .profile .bashrc .zshrc; do
  check "the merged body of $f sources the helper" grep -q 'harness-wrappers.sh' "$chain/$f"
done

resolve() { # resolve <shell> <flags>: where `omp` resolves in that kind of interactive shell
  env -i HOME="$chain" PATH="/usr/bin:/bin" TERM=dumb "$1" "$2" 'command -v omp' 2>/dev/null | tail -1
}
for mode in "bash -ic" "bash -lic" "zsh -ic" "zsh -lic"; do
  shell=${mode%% *}; flags=${mode#* }
  command -v "$shell" >/dev/null 2>&1 || { echo "skip: $shell is not installed"; continue; }
  check "merged startup files, $mode: omp resolves to the wrapper" test "$(resolve "$shell" "$flags")" = "$chain/.local/share/rig/bin/omp"
done

# dash logins read ~/.profile too: the merged block must reach the helper there (the bash/zsh-only part is
# skipped) and must not end the shell
if command -v dash >/dev/null 2>&1; then
  out=$(env -i HOME="$chain" PATH="/usr/bin:/bin" dash -c '. "$HOME/.profile" 2>/dev/null; command -v omp')
  check "dash: the merged profile block puts the wrapper first" test "$out" = "$chain/.local/share/rig/bin/omp"
fi

exit "$fail"
