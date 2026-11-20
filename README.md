# .files
dotfiles (mac, webdev, frontend, iterm)

#### installation

Ubuntu settings profile (managed by `ubuntu-tweak`): see [ubuntu/README.md](ubuntu/README.md).


#### Fancy Gallery

<br/>**PS1 version 1**:

<img width="492" alt="screen shot 2017-04-27 at 23 56 02" src="https://cloud.githubusercontent.com/assets/6201068/25504090/3bfb4c68-2ba5-11e7-923a-8ccb8fa18081.png">

<br/>**Start up random tips sometimes (~every 5th start)**

<img width="484" alt="screenshot 2019-02-18 at 16 24 22" src="https://user-images.githubusercontent.com/6201068/52954062-2a598200-339a-11e9-8dfc-5b5616503491.png">

<br/>**the fuck helper**

<img width="826" alt="screenshot 2019-02-18 at 13 09 54" src="https://user-images.githubusercontent.com/6201068/52954074-32192680-339a-11e9-8880-3a46b0e0cda5.png">

<br/>**git lol and git lola — log prettifiers**

<img width="616" alt="screenshot 2019-02-17 at 13 34 55" src="https://user-images.githubusercontent.com/6201068/52954132-642a8880-339a-11e9-8c6f-f99dadf0e461.png">

<br/>**git diff as diff-so-fancy** (by line + by word, best gui diffs like)

<img width="1140" alt="screenshot 2019-02-16 at 00 07 08" src="https://user-images.githubusercontent.com/6201068/52954167-7e646680-339a-11e9-943c-1abf3c569576.png">

<br/>**shell syntax highlighting**

![screenshot 2019-02-14 at 13 55 40](https://user-images.githubusercontent.com/6201068/52954210-a5bb3380-339a-11e9-82aa-84f8fb21c0f4.png)

<br/>**short alias proactive suggest** (force short aliases usage)

<img width="616" alt="screenshot 2019-01-29 at 12 23 34" src="https://user-images.githubusercontent.com/6201068/52954292-d733ff00-339a-11e9-9090-ee91fea98f3c.png">

<br/>**funny shit happens**

![telegram-cloud-file-2-255411053-180556--2134020241385532112](https://user-images.githubusercontent.com/6201068/52954434-3f82e080-339b-11e9-8fb9-eecb16314181.jpg)

<br/>**custom git smart suggest utils**

![telegram-cloud-file-2-257125856-16910-2002596048701755077](https://user-images.githubusercontent.com/6201068/52954455-490c4880-339b-11e9-8bb6-d9b46c00444a.jpg)

#### todo

- path `.files/bin`
- automate symlinks
- track all git init/clone directories for futher system reinstallation/upgrage
- try to use `.ondirc` ([example](https://github.com/tenorok/dotfiles/blob/master/bash_osx#L1-L11))
- add rare used git aliases tip, add rare used bin tip

#### Ubuntu (user level, no root)

```sh
git clone https://github.com/alex-mextner/.files ~/.files
~/.files/install-ubuntu.sh --plan   # what it would change
~/.files/install-ubuntu.sh          # link, merge, zplug, vim-plug, submodules, rig global config
rig apply commit                    # converge the machine to rig/config.yaml
```

It links `.zprofile .vimrc .gitattributes .hyper.js .tmux`, adds one marked block each to the
machine's `~/.profile`, `~/.bashrc`, `~/.zshrc`, includes `.gitconfig.shared` from `~/.gitconfig`
and merges `rig/config.yaml` into `~/.config/rig/config.yaml`. Machine-specific files are merged,
never overwritten; running it twice changes nothing. A private submodule whose repository this machine cannot
read is reported as a `manual:` step (the script names the owner account) instead of failing.

Interactive `omp`, `claude`, `codex` and `opencode` start hosted in abduco under rig's crash supervisor: `rig/config.yaml`
declares `harness_wrappers`, and `harness-wrappers.sh` (sourced last by `.profile`, `.zprofile` and the blocks the installer
merges into `~/.profile`, `~/.bashrc` and `~/.zshrc`) puts `~/.local/share/rig/bin` before the real binaries, since rig
never edits `PATH`. Print mode, subcommands and `--help` pass straight through. `tests/harness-wrappers.test.sh` checks
the helper in bash, zsh and dash and the merged startup files as login and non-login interactive shells.
