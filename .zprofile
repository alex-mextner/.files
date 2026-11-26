# ~/.zprofile: zsh LOGIN shells only (read after ~/.zshenv, before .zshrc). Session environment, nothing interactive.
# Belongs here: zsh-syntax PATH/env. The shared env (brew shellenv, EDITOR, BUN_INSTALL, cargo, ...) is in .profile,
# which is sourced below so a zsh login and a bash login see the same environment.
# Does NOT belong here: aliases, functions, keybindings, history, completion, prompt, plugins, zplug (-> .zshrc);
# anything a non-interactive shell needs, e.g. ssh host cmd (-> ~/.zshenv, not managed by the engine).

[ -r "$HOME/.profile" ] && source "$HOME/.profile"

# Google Cloud SDK: path.zsh.inc is zsh syntax and only extends PATH (its completion is in .zshrc)
[ -r "$HOME/google-cloud-sdk/path.zsh.inc" ] && source "$HOME/google-cloud-sdk/path.zsh.inc"

# rig's harness wrappers (omp, claude, codex, opencode) must come before the real binaries; last, so nothing above can shadow them
[ -r "$HOME/.files/harness-wrappers.sh" ] && . "$HOME/.files/harness-wrappers.sh"
