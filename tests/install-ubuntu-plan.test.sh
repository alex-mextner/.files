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
dirs_before=$(cd "$home" && find . -path ./.files -prune -o -print | sort)

# no network anywhere in this file: every submodule probe answers "reachable" unless a test says otherwise (#43)
always=$(mktemp)
printf '#!/bin/sh\nexit 0\n' > "$always"
chmod +x "$always"
export INSTALL_UBUNTU_PROBE="$always"
run() { HOME="$home" XDG_CONFIG_HOME="$home/.config" bash "$repo/install-ubuntu.sh" "$@" 2>&1; }

plan=$(run --plan)
check "plan proposes the bashrc block" grep -q "pending: add the .files:bashrc block" <<< "$plan"
check "plan proposes the gitconfig include" grep -q "pending: include ~/.files/.gitconfig.shared" <<< "$plan"
if python3 -I -c 'import yaml' 2>/dev/null; then
  check "plan proposes the rig config merge" grep -q "pending: merge rig/config.yaml" <<< "$plan"
fi
after=$(cd "$home" && find . -path ./.files -prune -o -type f -print0 | sort -z | xargs -0 sha256sum)
check "plan writes no file" test "$before" = "$after"
check "plan creates no directory" test "$dirs_before" = "$(cd "$home" && find . -path ./.files -prune -o -print | sort)"

# a duplicate block is reported for refresh, an unpaired marker is a manual step and is never rewritten
begin='# >>> .files:bashrc (managed by ~/.files/install-ubuntu.sh; edit there) >>>'
end='# <<< .files:bashrc <<<'
printf '%s\nx\n%s\n%s\ny\n%s\n' "$begin" "$end" "$begin" "$end" >> "$home/.bashrc"
check "duplicate block is refreshed" grep -q "pending: refresh the .files:bashrc block" <<< "$(run --plan)"

printf '%s\nz\n' "$begin" >> "$home/.bashrc"
unpaired=$(run --plan)
check "unpaired markers are a manual step" grep -q "manual: .*unpaired .files:bashrc markers" <<< "$unpaired"
check "unpaired markers do not offer a rewrite" bash -c '! grep -q "refresh the .files:bashrc" <<< "$1"' _ "$unpaired"

# an end marker before its start marker has equal counts but is still unpaired
printf '# machine bashrc\n%s\nA\n%s\nB\n' "$end" "$begin" > "$home/.bashrc"
check "end marker before start marker is a manual step" grep -q "manual: .*unpaired .files:bashrc markers" <<< "$(run --plan)"

# a ~/.profile that is a symlink into the repository (the engine links it) is never edited
rm -f "$home/.profile"
ln -s "$repo/.profile" "$home/.profile"
check "a linked ~/.profile gets no block" grep -q "ok: .*/.profile is linked into ~/.files" <<< "$(run --plan)"

check "--check exits 1 while something is pending" bash -c 'HOME="$1" XDG_CONFIG_HOME="$1/.config" bash "$2/install-ubuntu.sh" --check >/dev/null 2>&1; [ $? -eq 1 ]' _ "$home" "$repo"

# a submodule whose repository this machine cannot read is a manual step naming its owner, never pending work:
# no run can fix it, so it must not keep --check at "pending" forever (#43). The probe is a stub: no network here.
reach=$(mktemp)
# the real ~/.files has every submodule initialised, so the submodule cases run in a scratch repository that declares
# vscode-configs (url copied from the real .gitmodules) as an uninitialised gitlink
subhome=$(mktemp -d)
trap 'rm -rf "$home" "$always" "$reach" "$subhome"' EXIT
sub_url=$(git -C "$repo" config -f .gitmodules --get submodule.vscode-configs.url)
git init -q "$subhome/.files"
cp "$repo/.gitmodules" "$repo/install-ubuntu.sh" "$subhome/.files/"
git -C "$subhome/.files" update-index --add --cacheinfo 160000,8a80c68a2aebae8c9bcba29cea49fa48565d973f,vscode-configs
sub_run() { HOME="$subhome" XDG_CONFIG_HOME="$subhome/.config" bash "$subhome/.files/install-ubuntu.sh" "$@" 2>&1; }
printf '#!/bin/sh\ncase "$1" in *alex-mextner/vscode-configs*) exit 1 ;; esac\nexit 0\n' > "$reach"
chmod +x "$reach"
blocked=$(INSTALL_UBUNTU_PROBE="$reach" sub_run --plan)
check "an unreachable submodule is a manual step naming its owner" \
  grep -q "manual: submodule vscode-configs: $sub_url is not reachable (owner: GitHub account alex-mextner" <<< "$blocked"
check "an unreachable submodule is not offered as pending work" \
  bash -c '! grep -q "initialise submodule vscode-configs" <<< "$1"' _ "$blocked"
check "a reachable submodule is still offered for cloning" \
  grep -q "pending: initialise submodule vscode-configs" <<< "$(INSTALL_UBUNTU_PROBE="$always" sub_run --plan)"

# a repository that moved (#87): the url recorded in .git/config and the origin kept in .git/modules win over .gitmodules, so
# a plain `git submodule update` would fetch the new commit from the OLD repository. Local repositories stand in for the two
# GitHub repositories (file transport allowed through the environment, only here).
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=protocol.file.allow GIT_CONFIG_VALUE_0=always
mk_repo() { git init -q -b main "$1" && git -C "$1" -c user.name=t -c user.email=t@t commit -q --allow-empty -m "$2"; }
moved_scene() { # moved_scene <dir>: a populated submodule of the old repository, then .gitmodules and the gitlink move to the new one
  local dir=$1 files=$1/home/.files
  mk_repo "$dir/old" old; mk_repo "$dir/new" new
  mkdir -p "$dir/home"; git init -q -b main "$files"
  cp "$repo/install-ubuntu.sh" "$files/"
  git -C "$files" submodule add -q "$dir/old" vscode-configs
  git -C "$files" config -f .gitmodules submodule.vscode-configs.url "$dir/new"
  git -C "$files" update-index --cacheinfo "160000,$(git -C "$dir/new" rev-parse HEAD),vscode-configs"
}
# the installer also clones zplug, downloads vim-plug and runs bun: shims keep this file off the network (a clone of an https
# url, curl and bun fail; every other git call, the local file clones of the submodules included, runs the real git)
shims=$(mktemp -d)
real_git=$(command -v git)
printf '#!/bin/sh\ncase " $* " in *" clone "*https://*) exit 1 ;; esac\nexec "$REAL_GIT" "$@"\n' > "$shims/git"
printf '#!/bin/sh\nexit 1\n' > "$shims/curl"
printf '#!/bin/sh\nexit 1\n' > "$shims/bun"
chmod +x "$shims/git" "$shims/curl" "$shims/bun"
moved_run() { HOME="$1/home" XDG_CONFIG_HOME="$1/home/.config" PATH="$shims:$PATH" REAL_GIT="$real_git" bash "$1/home/.files/install-ubuntu.sh" 2>&1; }

rewritten=$(mktemp -d)
moved_scene "$rewritten"
# already moved, except that git rewrites the stored origin on read (`remote get-url` would show the rewritten value)
git -C "$rewritten/home/.files/vscode-configs" config url."ssh://rewrite.invalid/x.git".insteadOf "$rewritten/new"
git -C "$rewritten/home/.files/vscode-configs" config remote.origin.url "$rewritten/new"
git -C "$rewritten/home/.files" config submodule.vscode-configs.url "$rewritten/new"
check "an origin that an insteadOf rule rewrites is not pending drift" bash -c '! grep -q "point submodule" <<< "$1"' _ "$(HOME="$rewritten/home" XDG_CONFIG_HOME="$rewritten/home/.config" PATH="$shims:$PATH" REAL_GIT="$real_git" bash "$rewritten/home/.files/install-ubuntu.sh" --plan 2>&1)"
rm -rf "$rewritten"

initialised=$(mktemp -d)
moved_scene "$initialised"
out=$(moved_run "$initialised")
check "an initialised submodule of a moved repository is re-pointed" grep -q "apply: point submodule vscode-configs at $initialised/new" <<< "$out"
check "the recorded url and the origin name the new repository" test \
  "$(git -C "$initialised/home/.files" config --get submodule.vscode-configs.url)|$(git -C "$initialised/home/.files/vscode-configs" remote get-url origin)" = "$initialised/new|$initialised/new"
check "a plain submodule update then fetches the new commit" bash -c \
  'git -C "$1/home/.files" submodule update -q vscode-configs && [ "$(git -C "$1/home/.files/vscode-configs" rev-parse HEAD)" = "$(git -C "$1/new" rev-parse HEAD)" ]' _ "$initialised"

deinitialised=$(mktemp -d)
moved_scene "$deinitialised"
git -C "$deinitialised/home/.files" submodule deinit -q -f vscode-configs
moved_run "$deinitialised" > /dev/null
check "a deinitialised submodule keeping the old origin is cloned from the new repository" test \
  "$(git -C "$deinitialised/home/.files/vscode-configs" rev-parse HEAD 2>/dev/null)" = "$(git -C "$deinitialised/new" rev-parse HEAD)"
rm -rf "$initialised" "$deinitialised"

# a fresh checkout whose .gitmodules declares an ssh url, on a machine whose git logs in through gh: the clone goes over https
# and the https url is what gets recorded (the declared ssh url is only the transport rewrite's input). The "github" of this
# test is a local repository: the longer insteadOf rule (the exact ssh url) beats the installer's generic ssh-to-https one.
fresh=$(mktemp -d)
mk_repo "$fresh/gh" remote
mkdir -p "$fresh/home"; git init -q -b main "$fresh/home/.files"
printf '[credential "https://github.com"]\n\thelper = !gh auth git-credential\n' > "$fresh/home/.gitconfig"
cp "$repo/install-ubuntu.sh" "$fresh/home/.files/"
printf '[submodule "vscode-configs"]\n\tpath = vscode-configs\n\turl = git@github.com:o/r.git\n' > "$fresh/home/.files/.gitmodules"
git -C "$fresh/home/.files" update-index --add --cacheinfo "160000,$(git -C "$fresh/gh" rev-parse HEAD),vscode-configs"
GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_1="url.$fresh/gh.insteadOf" GIT_CONFIG_VALUE_1=git@github.com:o/r.git \
  HOME="$fresh/home" XDG_CONFIG_HOME="$fresh/home/.config" PATH="$shims:$PATH" REAL_GIT="$real_git" \
  bash "$fresh/home/.files/install-ubuntu.sh" > /dev/null 2>&1
check "a fresh ssh-declared submodule is cloned and recorded with its https url, in the parent and as its origin" test \
  "$(git -C "$fresh/home/.files" config --get submodule.vscode-configs.url)|$(git -C "$fresh/home/.files/vscode-configs" config --get remote.origin.url)|$(git -C "$fresh/home/.files/vscode-configs" rev-parse HEAD 2>/dev/null)" \
  = "https://github.com/o/r.git|https://github.com/o/r.git|$(git -C "$fresh/gh" rev-parse HEAD)"
rm -rf "$fresh"

# a relative url (../x.git) is resolved by git when it registers the submodule: the repair must leave it alone
relative=$(mktemp -d)
moved_scene "$relative"
git -C "$relative/home/.files" config -f .gitmodules submodule.vscode-configs.url ../new
git -C "$relative/home/.files" config submodule.vscode-configs.url "$relative/new"
git -C "$relative/home/.files/vscode-configs" config remote.origin.url "$relative/new"
check "a relative url in .gitmodules is not treated as drift" bash -c '! grep -q "point submodule" <<< "$1"' _ \
  "$(HOME="$relative/home" XDG_CONFIG_HOME="$relative/home/.config" PATH="$shims:$PATH" REAL_GIT="$real_git" bash "$relative/home/.files/install-ubuntu.sh" --plan 2>&1)"
rm -rf "$relative"

# a repair that cannot be written (a config lock held by another git) is a manual step, never a silent success
locked=$(mktemp -d)
moved_scene "$locked"
: > "$locked/home/.files/.git/config.lock"
locked_out=$(moved_run "$locked"); locked_rc=$?
check "an unwritable url repair is a manual step and the exit status says so" bash -c 'grep -q "manual: could not point submodule vscode-configs" <<< "$1" && [ "$2" -eq 10 ]' _ "$locked_out" "$locked_rc"
rm -rf "$locked" "$shims"

exit $fail
