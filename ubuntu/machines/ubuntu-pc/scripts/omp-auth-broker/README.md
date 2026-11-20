# omp auth-broker: one set of omp logins for every machine

This PC (ubuntu-pc) is the only machine that holds the omp (oh-my-pi) logins. It runs
`omp auth-broker serve` on 127.0.0.1:18765, and a reverse SSH tunnel to each other machine
("consumer") makes that same port appear on the consumer's own 127.0.0.1. The consumer's omp asks
the broker for short-lived access tokens, so OAuth refresh tokens never leave this PC. A copied
refresh token would rotate on first use and log the other copy out, which is why consumers do not
keep logins of their own.

| piece | where |
| --- | --- |
| broker | `files/.config/systemd/user/omp-auth-broker.service` (this overlay) |
| tunnel, one instance per consumer | `files/.config/systemd/user/omp-auth-broker-tunnel@.service`; the instance name is an ssh Host alias, e.g. `omp-auth-broker-tunnel@clvm.service` |
| setup | `install.sh` in this directory |

Consumers today: `clvm` (the build box), `home` (the old workstation). `bread` is planned but has no
login for the `ultra` account with omp yet.

## Setup on this PC

1. Every consumer needs a Host alias with key authentication in this PC's `~/.ssh/config`. That
   file is local and is not in this repository, so addresses and user names stay out of git:

   ```
   Host clvm
       HostName <address>
       User <user>
   ```

   Check it with `ssh -o BatchMode=yes clvm true`. The alias must have no `RemoteCommand`,
   `RemoteForward`, `LocalForward` or `DynamicForward` (a `ClearAllForwardings yes` does not hide them): the tunnel uses
   the alias as is, so these would break it or collide with an interactive session, and the script
   skips such an alias. Give such a host a second, plain alias for the tunnel. Two aliases for the
   same consumer must not both get a tunnel: only one can hold its port. The key must work without
   an ssh agent, because the tunnel runs in the systemd user manager.
2. The consumer must be Linux (the tunnel's scripts read `/proc` and use `ss` and GNU `tail`) and
   have `~/.local/bin/omp` and python3. It should stay on: while a consumer is off, its tunnel
   keeps retrying, and after about 20 minutes service-failover on this PC treats the retry loop as
   a broken unit and starts a repair.
3. Make sure no consumer runs a broker of its own on port 18765, and no machine still runs the old
   `clvm-auth-broker-tunnel.service`: run `systemctl --user disable --now
   omp-auth-broker.service clvm-auth-broker-tunnel.service` on each of them. The script refuses a
   consumer where either is active or enabled.
4. Run `./install.sh --dry-run clvm home`, read the plan, then `./install.sh clvm home`. Run it
   from `~/.files`, not from a worktree: the unit links point into the checkout the script runs
   from (a rerun from `~/.files` moves them back and says so).

The script links the two unit files (`ubuntu-tweak apply` does that too) and starts the broker.
Then, for each alias:

1. It leaves the consumer untouched when its port 18765 is held by anything other than this PC's
   tunnel, or when the consumer still has its own broker or the old tunnel unit active or enabled.
   A dry run makes this check too.
2. On `clvm` it retires the old `clvm-auth-broker-tunnel.service`, which this setup replaces.
3. It enables `omp-auth-broker-tunnel@<alias>.service` and waits until the consumer's record shows
   this instance's own live session on the port. Until then the consumer's config is not touched.
4. When the consumer's copy differs, it sets `auth.broker.url` to `http://127.0.0.1:18765` and
   `auth.broker.token` to the broker's bearer token, in one write of the consumer's omp config
   file. The token travels on ssh's standard input, and a short Python step changes only those two
   lines. It never appears in a command line on either machine, so other accounts on the consumer
   cannot read it from the process list, and it is never printed. The file is set to mode 0600 on
   every run.
5. It checks the result: `omp auth-broker status` on the consumer must print `OK` through this
   instance's session, and the instance must not restart during the check.

The port is plain HTTP on the consumer's loopback, and the design does not protect it. Whenever the
tunnel is down (this PC off, or a restart, which service-failover can delay by up to 5 minutes),
another account on the consumer can take 127.0.0.1:18765 and receive the bearer token from that
consumer's omp. Use consumers whose other accounts you trust.

Running it again changes nothing that is already in place. A new consumer is one more alias on the
command line.

## Checking and removing

- On a consumer: `~/.local/bin/omp auth-broker status` prints `OK http://127.0.0.1:18765 (...)`.
- On this PC: `systemctl --user status 'omp-auth-broker-tunnel@*'`.
- Remove a consumer: `systemctl --user disable --now omp-auth-broker-tunnel@<alias>.service`, delete
  the `broker:` block under `auth:` from the consumer's omp config (its token works on every other
  consumer too), then rotate the token with `omp auth-broker token --regenerate` and run
  `install.sh` for the remaining consumers.
- After rotating the token with `omp auth-broker token --regenerate`, run `install.sh` with no alias:
  it sends the new one to every consumer whose tunnel instance is enabled, and to no other.
