# aliases.sh: sh-compatible aliases shared by bash (.bashrc block) and zsh (.zshrc). Interactive shells only.
# Does NOT belong here: exports/PATH (-> .profile), zsh-only aliases or functions (-> aliases.zsh).

alias sops='SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/keys.txt sops'

# Copy idiot's formatted shell-code w/o a pain
alias %=' '
