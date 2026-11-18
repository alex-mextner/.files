#!/usr/bin/env bash
# The one human step of the dc-mcp module, `finish-setup --step dc-mcp-cloudflare`:
#
#   finish.sh check   exit 0 = `computer chatgpt doctor` passes end to end AND the public URL reaches THIS machine
#   finish.sh run     interactive: `computer chatgpt login` (Cloudflare login in a browser, tunnel, DNS route), then
#                     `computer chatgpt start`, then the same proof as check, then `computer chatgpt connect --no-wait` prints
#                     the fields ChatGPT needs
#
# Why it is human: the Cloudflare login authorizes this machine for a zone in a browser; its credentials are secrets that never
# enter the repository. The CLI keeps them in ~/.config/computer/chatgpt/cloudflared.
# Why "reaches THIS machine" is its own proof: `login` exits 0 when the owner keeps a DNS record of the hostname that points at
# another machine, and doctor's public check accepts any 401 with the challenge, also another dc-mcp machine's. So a request to a
# random path of the public URL must show up in this machine's shim access log.
set -u

cli=
command -v uv >/dev/null 2>&1 && cli=$(uv tool dir --bin 2>/dev/null)/computer
[ -n "$cli" ] && [ -x "$cli" ] || { echo "dc-mcp: the computer CLI is not installed: run ubuntu-tweak apply --commit first" >&2; exit 1; }

# setting KEY: one value of the CLI's config.toml, with ~ expanded
setting() {
  python3 -B - "$1" <<'PY'
import os, sys, tomllib
path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "computer", "chatgpt", "config.toml")
with open(path, "rb") as fh:
    print(os.path.expanduser(str(tomllib.load(fh).get(sys.argv[1], ""))))
PY
}

reaches_here() {
  local probe log
  # the shim logs every request under /.idp/ by its full path (discovery paths are collapsed per minute, so they cannot carry a nonce)
  probe=/.idp/dc-mcp-probe-$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 16)
  log=$(setting state_dir)/access.log
  curl -s -o /dev/null --max-time 10 "https://$(setting hostname)$probe" || return 1
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    grep -qF " $probe " "$log" 2>/dev/null && return 0
    sleep 0.5
  done
  return 1
}

# The engine kills a finish check after 60 s; doctor is bounded to 35 s and the probe to 10 + 5 s, so a slow network fails the
# check with a reason instead of being killed.
proven() {
  timeout 35 "$cli" chatgpt doctor || return 1
  reaches_here || { echo "dc-mcp: the public URL answers, but not from this machine: its DNS record points elsewhere" >&2; return 1; }
}

case "${1:-}" in
  check) proven >/dev/null 2>&1; exit $? ;;
  run) ;;
  *) echo "usage: $0 check|run" >&2; exit 2 ;;
esac

"$cli" chatgpt login || exit 1
"$cli" chatgpt start || exit 1
if ! proven; then
  echo "dc-mcp: fix what is named above (a DNS record kept on purpose, or DNS not propagated yet) and run this step again" >&2
  exit 1
fi
"$cli" chatgpt connect --no-wait --no-open --no-copy
