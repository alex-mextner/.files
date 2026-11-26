# misc.zsh: interactive zsh options (jobs, comments, directory stack). Sourced by .zshrc.
# Does NOT belong here: exports (PAGER, LESS, PATH -> .profile), zplug (-> .zshrc).

## jobs
setopt long_list_jobs

# recognize comments
setopt interactivecomments

# directories

## cd - <tab>
setopt auto_pushd
setopt pushd_ignore_dups
setopt pushdminus
