# aliases.zsh: zsh aliases and functions (git, docker, ...). Sourced by .zshrc, so interactive zsh only.
# Does NOT belong here: exports/PATH (-> .profile), sh-compatible aliases shared with bash (-> aliases.sh).

function take-webcam-picture() {
  #imagesnap -w 1 "$HOME/Pictures/webcam/$(date '+%Y-%m-%d-%H.%M.jpg')"
  imagesnap -w 1 "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Photos/webcam/$(date '+%Y-%m-%d-%H.%M.jpg')"
}
alias camcap=take-webcam-picture

alias py=python3

alias ll='ls -Foalh'

# git

alias gs='git status'
alias gca='git commit -v --no-ed --amend'
alias gcaa='git commit -va --no-ed --amend'
alias gcf='git c --fixup'
alias gf='git fetch --all --prune 2>&1 | suggest_remove_branch' # fetch & clear old branches
#alias grd='gf;git rebase origin/master' # rebase trunk
alias gr='git remote -v' # show remotes
alias gp='git push'
# alias gpf='git push --force'
alias gl='git lol'
alias gla='git lola'
# git log for all my branches
function glm() {
  git branch | sed 's/^[* ]*//' | awk '{print $0 " origin/" $0}' | xargs git lol --ignore-missing 
}
# git-rebase-branch
function grb() {
  NEW_TARGET=$1
  CURRENT_BRANCH=$(git-get-branch)
  OLD_BRANCH=$(git-get-common-parent $CURRENT_BRANCH $NEW_TARGET)
  CMD="git rb --onto $NEW_TARGET $OLD_BRANCH $CURRENT_BRANCH"
  echo $CMD
  eval $CMD
}
alias gsync='git pull --rebase && git push'
alias gpr='hub pull-request'
# alias gles='/usr/local/bin/gl'

function git-status-edit() {
  $EDITOR $(git-get-changed)
}
function git-diff-edit() {
  # optional, shows last commit if no argument is passed
  BASE_COMMIT=${1:-HEAD^}
  echo "BASE_COMMIT=$BASE_COMMIT"
  $EDITOR $(git-get-changed $BASE_COMMIT)
}

alias ggc=git-get-changed # print list of any changed files in stage/unstage
alias gse=git-status-edit # git status: edit all changed files in stage/unstage
alias gde=git-diff-edit # git diff: edit all changed files in last commit

#alias go='git open'
alias gup=gh-get-url-by-path

alias ggr=git-get-root
alias ggb=git-get-branch

# git push --force with pre-checks
function gpf() {
    BR=$1
    git fetch
    if [ "$(git show-ref refs/heads/$BR)" = "$(git show-ref refs/remotes/origin/$BR)" ]; then
        git push --force origin $BR
    else
        echo 'В origin-ветке что-то новенькое, проверь!'
    fi
}

color_blue_normal='\033[94m'
color_blue_bold='\033[1;94m'
color_reset='\033[0m'

function match_local_branches() {
  cat - | while read br; do
    if [[ "$( git branch --no-color --no-column --list $br --format '%(refname)' )" == "refs/heads/$br" ]]; then
      echo -n " $br"
    fi
  done
}

function suggest_remove_branch() {
  gf_output="$(cat -)"
  echo "$gf_output" | grep -vE '^\[\d+\]'

  deleted_brs="$(echo "$gf_output" | grep -- '- \[deleted\]' | grep -E -o -- '-> origin/.+$' | gsed 's|-> origin/||g')"
  matched_brs="$(echo "$deleted_brs" | match_local_branches)"
  if [[ "$matched_brs" ]]; then
    echo "\n${color_blue_normal}💡 Pro Tip: ${color_blue_bold}git branch -D$matched_brs${color_reset}\n"
  fi
}

# todo: удалять старую, только если она перед этим была удалена в репе
function gn() {
  NEW_BR=$1
  _ rm
  gf
  git co -b $NEW_BR origin/master
  git br -D `git-get-br`
}

function gc__() {
    msg="$@"; git commit -vam "$msg" && take-webcam-picture > /dev/null
}
alias gc='noglob gc__'
function gcn() {
    msg="[$(ggb)] $@"; git commit -vnm "$msg" && take-webcam-picture > /dev/null
}
function gcm() {
    msg="$@"; git commit -vm "$msg"
}
function gco() {
    _ rm; git checkout $*
}
function grsc() {
    git reset @ $*; git checkout $*
}

function get_diff_exclude() {
  echo "':(exclude)package-lock.json' ':(exclude)$(git-get-root)/public/webpack/admin'"
}
function gd() {
  eval git diff --relative --ignore-space-change $* -- . $(get_diff_exclude)
}
function gdc() {
  eval git diff --relative --cached --ignore-space-change $* -- . $(get_diff_exclude)
}
function gdd() {
  eval git diff --relative origin/master --ignore-space-change $* -- . $(get_diff_exclude)
}

# gsed on macOS (GNU sed from brew), plain sed on Linux where it already is GNU sed.
_git_init_sed=${commands[gsed]:+gsed}
alias git-init="git init && echo '# MacOS #' > .gitignore && curl https://raw.githubusercontent.com/github/gitignore/master/Global/macOS.gitignore >> .gitignore && ${_git_init_sed:-sed} -i '/Icon/d' .gitignore && echo '# Node #' >> .gitignore && curl https://raw.githubusercontent.com/github/gitignore/master/Node.gitignore >> .gitignore"

# /git

# misc
if (( $+commands[pbcopy] )); then alias copy=pbcopy
elif (( $+commands[wl-copy] )); then alias copy=wl-copy
elif (( $+commands[xclip] )); then alias copy='xclip -selection clipboard -in'
fi
#alias vim='mvim -v'

function take() {
  mkdir $1; cd $1
}
function gcopen() {
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --app="$*"
}

function touchf() {
    file_path=$1
    dir_path="$(echo $file_path | grep -oE '^.+/')"

    # file_name="$(echo $file_path | grep -oE '[^/]+$')"
    [ "$(echo $dir_path)" ] && mkdir -p $dir_path
    # cd $dir_path
    touch $file_path
}

function mvf() {
    touchf "$2"
    mv "$1" "$2"
}

function vimf() {
    touchf "$1"
    vim "$1"
}

# /misc

# npm maintenance bash scripts
# alias patch='pre-version && npm version patch && post-version'
# alias minor='pre-version && npm version minor && post-version'
# alias major='pre-version && npm version major && post-version'
# alias pre-version='git diff --exit-code && npm test'
# alias post-version='(git add package-lock.json && git add package-lock.json && git commit --amend --no-ed || :) && npm run --if-present build && git add . && git commit -m build && git diff --exit-code && git push && git push --tags && npm publish'
# /npm maintenance bash scripts


alias port='sudo lsof -iTCP -sTCP:LISTEN -n -P'

# ---------------------------------------------------
# ZSH SPECIFIC Aliases and functions
# ---------------------------------------------------

alias zshc="vim ~/.zshrc; source ~/.zshrc"
# alias ohmyzsh="~/.oh-my-zsh"

# ^Z instead of `fg`
fancy-ctrl-z () {
    if [[ $#BUFFER -eq 0 ]]; then
        fg
        zle redisplay
    else
        zle push-input
    fi
}
zle -N fancy-ctrl-z
bindkey '^Z' fancy-ctrl-z

# ^P — run pwd
#function pwd-wrapper() { pwd }; zle -N pwd pwd-wrapper; bindkey '^P' pwd;

# Who doesn't want home and end to work?
bindkey '\e[1~' beginning-of-line
bindkey '\e[4~' end-of-line

alias -g '|/'='| grep -i' # / foo |/ bar #= grep foo | grep bar

alias /='rg'

# claude with permission checks disabled — for sandboxes / trusted dirs
alias cc='claude --allow-dangerously-skip-permissions --permission-mode bypassPermissions'

# Moshi iOS client detection — trigger adjustments when connected from the app
# (e.g. narrower prompts, skip nerd-font glyphs, alt keybindings).
# Tmux-side status-bar clearing lives in .tmux.conf.
if [ -n "$MOSHI_CLIENT" ]; then
  : # placeholder; customize when a concrete need appears
fi

# moshi DIR — create/attach tmux session rooted at DIR with standard window
# layout (agent/review/tests/servers/misc). Handy before connecting from a
# mobile mosh+tmux client so the selector has something to pick.
moshi() {
  local dir="${1:-$PWD}"
  if [[ ! -d "$dir" ]]; then
    echo "Directory not found: $dir" >&2
    return 1
  fi

  local abs
  abs="$(cd "$dir" && pwd)"

  local session
  session="$(basename "$abs" | tr -cs '[:alnum:]_-' '-')"
  session="${session#-}"
  session="${session%-}"
  [[ -n "$session" ]] || session="main"

  if ! tmux has-session -t "$session" 2>/dev/null; then
    tmux new-session -d -s "$session" -c "$abs" -n agent
    tmux new-window -t "$session":2 -c "$abs" -n review
    tmux new-window -t "$session":3 -c "$abs" -n tests
    tmux new-window -t "$session":4 -c "$abs" -n servers
    tmux new-window -t "$session":5 -c "$abs" -n misc
  fi

  tmux attach -t "$session"
}
