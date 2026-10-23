#!/usr/bin/env bash
# install-ubuntu.sh — apply this dotfiles repository to an Ubuntu (Linux) home directory, user level.
#
#   install-ubuntu.sh            apply: submodules, links, merges, zplug, vim-plug, tool deps
#   install-ubuntu.sh --plan     read-only: print what apply would change
#   install-ubuntu.sh --check    read-only: exit 1 when something is pending (10: a manual step remains)
#
# The macOS bootstrap lives in installer.sh (brew, casks); this is its Linux counterpart. Idempotent:
# a second run changes nothing. It never needs root and never overwrites a machine-specific file:
#   linked        .zprofile .vimrc .gitattributes .hyper.js  and the .tmux plugin dir
#                 (an existing different file is moved to <name>.pre-dotfiles-<timestamp> first)
#   merged        ~/.profile ~/.bashrc ~/.zshrc (one marked block each), ~/.gitconfig (an [include] of
#                 .gitconfig.shared at the top: keys in ~/.gitconfig win over it, keys in
#                 ~/.config/git/config, which git reads earlier, do not) and the rig global config
#                 (rig/config.yaml, see rig/merge-config.py: the repository wins for every key it
#                 declares, so change a setting there, not with `rig config set`), so the machine's own
#                 identity, credential helper and hooksPath stay as they are
#   not touched   ~/.tmux.conf — tmux is provisioned by rig (`tmux:` block of the rig config)
# Exit codes: 0 done / nothing pending, 1 pending (--check), 2 usage, 10 a manual step remains
# (--check included: the first run on a fresh machine ends with 10 until bun is installed).
set -u

mode=apply
case "${1:-}" in
  "") ;;
  --plan) mode=plan ;;
  --check) mode=check ;;
  -h|--help) sed -n '2,/^[^#]/{/^[^#]/!p}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

here=$(cd "$(dirname "$0")" && pwd -P)
files="$HOME/.files"
if [ ! -d "$files" ] || [ "$(cd "$files" && pwd -P)" != "$here" ]; then
  echo "the repository must be checked out at ~/.files (its shell files source ~/.files/...); this one is $here" >&2
  exit 2
fi

pending=0
manuals=0
stamp=$(date +%Y%m%d-%H%M%S)

applying() { [ "$mode" = apply ]; }

# todo <what>: an action is needed. In apply mode the caller performs it right after.
todo() {
  pending=$((pending + 1))
  if applying; then echo "apply: $*"; else echo "pending: $*"; fi
}
ok() { echo "ok: $*"; }
manual() { manuals=$((manuals + 1)); echo "manual: $*"; }

# replace_file <dest>: replace the contents of <dest> (kept in place, so a symlink stays a symlink)
# with stdin, but only once all of stdin has been read.
replace_file() {
  local dest="$1" tmp rc=0
  tmp=$(mktemp "$dest.XXXXXX") || return 1
  cat > "$tmp" && cat "$tmp" > "$dest" || rc=1
  rm -f "$tmp"
  return $rc
}

# link_into_home <repo-relative path>: ~/<name> becomes a symlink to ~/.files/<path>
link_into_home() {
  local rel="$1" name dest want
  name=$(basename "$rel")
  dest="$HOME/$name"
  want="$files/$rel"
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$want" ]; then ok "~/$name -> $want"; return; fi
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    todo "back up ~/$name to $name.pre-dotfiles-$stamp and link it to $want"
    applying || return 0
    mv "$dest" "$dest.pre-dotfiles-$stamp" || { manual "could not move ~/$name aside"; return 0; }
  else
    todo "link ~/$name -> $want"
    applying || return 0
  fi
  ln -s "$want" "$dest" || manual "could not link ~/$name"
}

# merge_block <file> <id> <body>: keep exactly one marked block in <file>, created when the file is absent.
merge_block() {
  local file="$1" id="$2" body="$3" begin end current expected
  begin="# >>> .files:$id (managed by ~/.files/install-ubuntu.sh; edit there) >>>"
  end="# <<< .files:$id <<<"
  expected=$(printf '%s\n%s\n%s' "$begin" "$body" "$end")
  if [ -f "$file" ] && grep -qxF "$begin" "$file"; then
    # Every start marker needs its own end marker, or the rewrite below would drop the rest of the file.
    if [ "$(grep -cxF "$begin" "$file")" != "$(grep -cxF "$end" "$file")" ]; then
      manual "$file has unpaired .files:$id markers (hand-edited?); fix or remove the block, then re-run"
      return 0
    fi
    # The block exactly once, and as expected; a duplicate or a stale block is rewritten to one fresh block.
    current=$(awk -v b="$begin" -v e="$end" '$0 == b { p = 1 } p { print } $0 == e { p = 0 }' "$file")
    if [ "$current" = "$expected" ]; then ok "$file has the .files:$id block"; return; fi
    todo "refresh the .files:$id block in $file"
    applying || return 0
    BODY="$body" awk -v b="$begin" -v e="$end" '
      $0 == b { if (!seen) { print b; print ENVIRON["BODY"]; print e; seen = 1 } skip = 1; next }
      skip && $0 == e { skip = 0; next }
      !skip { print }' "$file" | replace_file "$file" || manual "could not rewrite $file"
    return
  fi
  todo "add the .files:$id block to $file"
  applying || return 0
  if [ -s "$file" ] && [ "$(tail -c1 "$file")" != "" ]; then echo >> "$file"; fi
  printf '%s\n' "$expected" >> "$file"
}

# -- submodules (tmux plugins, git-worktree-runner, tg-cli, vscode-configs) -----------------------------
# .gitmodules uses ssh URLs for private repos. When git on this machine authenticates to GitHub
# through the gh credential helper (the usual Ubuntu setup), those are cloned over https instead and
# the https URL is recorded for the submodule and its origin, so a later plain `git submodule update`
# keeps working. Nested submodules keep whatever URL their own .gitmodules has.
gh_https=false
if git config --get-all credential.https://github.com.helper 2>/dev/null | grep -q 'gh auth git-credential'; then gh_https=true; fi
gitm() {
  if $gh_https; then git -C "$files" -c url."https://github.com/".insteadOf=git@github.com: "$@"
  else git -C "$files" "$@"; fi
}
while IFS= read -r line; do
  [ -n "$line" ] || continue
  flag=${line:0:1}
  read -r _ path _ <<< "${line:1}"
  if [ "$flag" = "-" ]; then
    todo "initialise submodule $path"
    applying || continue
    gitm submodule update --init --recursive -- "$path" >/dev/null 2>&1 \
      || manual "submodule $path did not clone (check access to its repository), then re-run"
  else
    ok "submodule $path"
  fi
done < <(gitm submodule status 2>/dev/null)
if $gh_https && applying; then
  while read -r key path; do
    name=${key#submodule.}; name=${name%.path}
    url=$(git -C "$files" config -f .gitmodules --get "submodule.$name.url")
    case "$url" in
      git@github.com:*)
        https="https://github.com/${url#git@github.com:}"
        if git -C "$files" config --get "submodule.$name.url" >/dev/null 2>&1; then
          git -C "$files" config "submodule.$name.url" "$https"
        fi
        if [ -e "$files/$path/.git" ]; then git -C "$files/$path" remote set-url origin "$https"; fi ;;
    esac
  done < <(git -C "$files" config -f .gitmodules --get-regexp '^submodule\..*\.path$')
fi

# -- links ----------------------------------------------------------------------------------------------
for f in .zprofile .vimrc .gitattributes .hyper.js .tmux; do link_into_home "$f"; done

# -- merges ---------------------------------------------------------------------------------------------
# bash/zsh only: ~/.profile is also read by dash (sh) logins, which cannot parse .files/.profile.
profile_body='if [ -n "$BASH_VERSION" ] || [ -n "$ZSH_VERSION" ]; then
  [ -f "$HOME/.files/.profile" ] && . "$HOME/.files/.profile"
  # tools installed on this machine win over the copies in ~/.files/bin
  [ -d "$HOME/.local/bin" ] && PATH="$HOME/.local/bin:$PATH"
fi'
merge_block "$HOME/.profile" profile "$profile_body"

bashrc_body='[ -f "$HOME/.files/aliases.sh" ] && . "$HOME/.files/aliases.sh"
case ":$PATH:" in *":$HOME/.files/bin:"*) ;; *) PATH="$PATH:$HOME/.files/bin:$HOME/.files/ts-import-trace/bin" ;; esac'
merge_block "$HOME/.bashrc" bashrc "$bashrc_body"

# Login zsh shells read the linked ~/.zprofile; every other interactive zsh sources it here.
zshrc_body='[[ -o login ]] || source "$HOME/.files/.zprofile"'
merge_block "$HOME/.zshrc" zshrc "$zshrc_body"

# First in the file, like the include in the macOS .gitconfig: the machine's own keys win on overlap.
inc='~/.files/.gitconfig.shared'
if git config --global --get-all include.path 2>/dev/null | grep -qxF "$inc"; then
  ok "~/.gitconfig includes $inc"
else
  todo "include $inc at the top of ~/.gitconfig"
  if applying; then
    { printf '[include]\n\tpath = %s\n' "$inc"; [ -f "$HOME/.gitconfig" ] && cat "$HOME/.gitconfig"; } \
      | replace_file "$HOME/.gitconfig" || manual "could not write ~/.gitconfig"
  fi
fi

# -- zplug (zsh plugin manager; Homebrew's on macOS, ~/.zplug here) -------------------------------------
if [ -r "$HOME/.zplug/init.zsh" ]; then
  ok "zplug at ~/.zplug"
else
  todo "clone zplug into ~/.zplug"
  if applying; then
    # pinned to the 2.4.2 release commit (supply chain: no moving master)
    { git clone --quiet https://github.com/zplug/zplug "$HOME/.zplug" \
        && git -C "$HOME/.zplug" checkout --quiet 8f14b4850d8e410f00db92afcd87b88c0c90f771; } \
      || manual "zplug did not clone (network?), then re-run"
  fi
fi
zplug_ok() { zsh -c 'source "$HOME/.files/.zprofile" >/dev/null 2>&1; zplug check' </dev/null >/dev/null 2>&1; }
if [ -r "$HOME/.zplug/init.zsh" ] && command -v zsh >/dev/null 2>&1; then
  if zplug_ok; then
    ok "zplug plugins installed"
  else
    todo "zplug install (plugins declared in .zprofile are missing)"
    if applying; then
      # `zplug install` exits 1 even when every plugin installed; the check afterwards is the verdict.
      zsh -c 'source "$HOME/.files/.zprofile" >/dev/null 2>&1; zplug install' </dev/null >/dev/null 2>&1
      zplug_ok || manual "zplug plugins did not install; run: zsh -ic 'zplug install'"
    fi
  fi
fi

# -- vim-plug and the plugins of .vimrc -----------------------------------------------------------------
plug="$HOME/.vim/autoload/plug.vim"
if [ -r "$plug" ]; then
  ok "vim-plug"
else
  todo "download vim-plug to ~/.vim/autoload/plug.vim"
  if applying; then
    # pinned to the 0.14.0 release commit
    curl -fsSLo "$plug" --create-dirs https://raw.githubusercontent.com/junegunn/vim-plug/d80f495fabff8446972b8695ba251ca636a047b0/plug.vim \
      || manual "vim-plug did not download (network?), then re-run"
  fi
fi
if [ -r "$plug" ] && command -v vim >/dev/null 2>&1; then
  if [ -d "$HOME/.vim/plugged/vim-sensible" ]; then
    ok "vim plugins"
  else
    todo "vim +PlugInstall"
    if applying; then
      vim -es -u "$HOME/.vimrc" -i NONE -c PlugInstall -c qa </dev/null >/dev/null 2>&1
      [ -d "$HOME/.vim/plugged/vim-sensible" ] || manual "vim plugins did not install; run vim and :PlugInstall"
    fi
  fi
fi

# -- ts-import-trace (bin/ts-import-trace is a bun script that needs its node_modules) -------------------
if command -v bun >/dev/null 2>&1; then
  if [ -d "$files/ts-import-trace/node_modules" ]; then
    ok "ts-import-trace dependencies"
  else
    todo "bun install in ts-import-trace"
    if applying; then
      # --no-save: the repo tracks package-lock.json only; bun would otherwise drop an untracked bun.lock here.
      (cd "$files/ts-import-trace" && bun install --no-save >/dev/null 2>&1) || manual "bun install failed in ~/.files/ts-import-trace"
    fi
  fi
else
  manual "bun is not installed: ts-import-trace needs it (https://bun.sh)"
fi

# -- rig global config (rig/config.yaml -> ~/.config/rig/config.yaml) -----------------------------------
# Converging the machine to that config is `rig apply commit` (not run here: it writes agent hooks,
# skills, services).
rigcfg="${XDG_CONFIG_HOME:-$HOME/.config}/rig/config.yaml"
if [ -f "$files/rig/config.yaml" ] && command -v python3 >/dev/null 2>&1 && python3 -I -c 'import yaml' 2>/dev/null; then
  merge_args=("$files/rig/config.yaml" "$rigcfg")
  applying && merge_args+=(--apply --backup-suffix ".bak-pre-dotfiles-$stamp")
  if verdict=$(python3 -I "$files/rig/merge-config.py" "${merge_args[@]}"); then
    case "$verdict" in
      ok) ok "rig global config has everything in rig/config.yaml" ;;
      changed) todo "merge rig/config.yaml into $rigcfg (then: rig apply commit)" ;;
      *) manual "rig/merge-config.py answered '$verdict'" ;;
    esac
  else
    manual "rig/config.yaml could not be merged into $rigcfg (see the error above)"
  fi
else
  manual "python3 with PyYAML is needed to merge rig/config.yaml into the rig global config"
fi

if [ "$mode" = check ] && [ "$pending" -gt 0 ]; then exit 1; fi
[ "$manuals" -gt 0 ] && exit 10
exit 0
