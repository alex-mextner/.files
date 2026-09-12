# .files
dotfiles (mac, webdev, frontend, iterm)

#### installation

The optional completion manager is available at `bin/zsh-completion`.

```sh
~/.files/bin/zsh-completion install
```

It scans commands only when explicitly run, trying `completion zsh`,
`completions zsh`, `generate-completion zsh`, `gen-completion zsh`, and
`completion-script zsh`, plus the corresponding `--shell`, `--format`, and
top-level completion flags. The installer lists every missing candidate,
explains which interface found it, shows safe history-based usage counts and
probe/output cost, and asks before installing. Rarely or never used commands
default to **No**; large or slow candidates are marked `LARGE/EXPENSIVE`.
`--yes` (or `--all`) installs all discovered candidates noninteractively.
Discovery is restricted to a small trusted CLI list (or commands named with
`--command NAME`) and first looks for a completion advertisement in
`command --help`; it does not send guessed completion arguments to ordinary
utilities. Use `--trust-path DIR` for a private CLI directory, or
`--scan-all` only when every executable in the current `PATH` is trusted.

Completion state is kept outside the repository:

- `~/.local/state/zsh-completions/registry.tsv` is the explicit registry,
- `~/.local/state/zsh-completions/snapshot.tsv` prevents repeat probes,
- `~/.local/state/zsh-completions/init.zsh` is the atomic startup fragment,
- `~/.local/share/zsh-completions/completions/` contains installed files.

`.zprofile` only sources `init.zsh` before `compinit`; it never probes commands
on shell startup. Failed registrations stay in the registry and are retried by
the next explicit `install`. State writes are lock-protected and atomic.


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
