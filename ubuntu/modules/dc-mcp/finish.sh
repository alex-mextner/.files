#!/usr/bin/env bash
# The one human step of the dc-mcp module, `finish-setup --step dc-mcp-cloudflare`:
#
#   finish.sh check   exit 0 = the three units (proxy, shim, tunnel) are active (read-only, fast, no network)
#   finish.sh run     interactive: Cloudflare login, tunnel, DNS route, start the units, print what ChatGPT needs
#
# Why it is human: `cloudflared tunnel login` authorizes this machine for a Cloudflare zone in a browser, and the credentials
# it brings (~/.cloudflared/cert.pem, ~/.cloudflared/<tunnel id>.json) are secrets that never enter the repository. The proxy
# password is NOT asked for: install.sh generated it; this script only says where it is and never prints it.
#
# One hostname, one machine: `route dns --overwrite-dns` moves the hostname to THIS machine's tunnel (requests would otherwise
# split between two hosts), so `run` says so and asks before it does.
set -u

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=lib.sh
. "$here/lib.sh"
load_options

cf=$HOME/.local/bin/cloudflared
cf_dir=$HOME/.cloudflared
auth_file=${XDG_CONFIG_HOME:-$HOME/.config}/dc-mcp-auth.env
units=(dc-mcp-bridge.service dc-mcp-shim.service dc-mcp-tunnel.service)

if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then export XDG_RUNTIME_DIR="/run/user/$(id -u)"; fi
sc() { systemctl --user "$@"; }
units_active() { local u; for u in "${units[@]}"; do [ "$(sc is-active "$u" 2>/dev/null)" = active ] || return 1; done; }
die() { echo "dc-mcp: $1" >&2; exit 1; }

case "${1:-}" in
  check)
    units_active
    exit $?
    ;;
  run) ;;
  *) echo "usage: $0 check|run" >&2; exit 2 ;;
esac

[ -n "$opt_hostname" ] || die "set [modules.dc-mcp] hostname in machines/<host>/machine.toml first, then run: ubuntu-tweak apply --commit"
[ -x "$cf" ] && [ -x "$HOME/.local/bin/mcp-auth-proxy" ] || die "the binaries are not installed yet: run ubuntu-tweak apply --commit first"
for u in "${units[@]}"; do
  [ -e "$HOME/.config/systemd/user/$u" ] || die "$u is not linked yet: run ubuntu-tweak apply --commit first"
done
auth_has_password() { [ -f "$auth_file" ] && grep -q '^PASSWORD=.' "$auth_file"; }
auth_has_password || die "$auth_file has no password yet: run ubuntu-tweak apply --commit first"

# 1. Cloudflare login (opens a browser; the certificate is a secret)
if [ ! -s "$cf_dir/cert.pem" ]; then
  echo "dc-mcp: step 1/4: Cloudflare login. A browser link appears; pick the zone of $opt_hostname and authorize."
  "$cf" tunnel login || die "cloudflared tunnel login failed"
  [ -s "$cf_dir/cert.pem" ] || die "the login did not leave $cf_dir/cert.pem"
else
  echo "dc-mcp: step 1/4: Cloudflare login already there ($cf_dir/cert.pem)"
fi

# 2. the tunnel: reuse one whose credentials are here, create it when Cloudflare does not know the name
tunnel_id=$("$cf" tunnel list --name "$opt_tunnel" -o json 2>/dev/null \
  | python3 -c 'import json,sys
try:
    rows = json.load(sys.stdin) or []
except ValueError:
    rows = []
print(rows[0]["id"] if rows else "")')
if [ -z "$tunnel_id" ]; then
  echo "dc-mcp: step 2/4: creating tunnel $opt_tunnel"
  "$cf" tunnel create "$opt_tunnel" || die "cloudflared tunnel create $opt_tunnel failed"
  tunnel_id=$("$cf" tunnel list --name "$opt_tunnel" -o json 2>/dev/null | python3 -c 'import json,sys; rows=json.load(sys.stdin) or []; print(rows[0]["id"] if rows else "")')
fi
[ -n "$tunnel_id" ] || die "cannot find tunnel $opt_tunnel after creating it"
if [ ! -s "$cf_dir/$tunnel_id.json" ]; then
  die "tunnel $opt_tunnel exists (id $tunnel_id) but its credentials $cf_dir/$tunnel_id.json are not on this machine: copy that file from the machine that created it, or pick a new name with [modules.dc-mcp] tunnel = \"...\" and run this step again"
fi
echo "dc-mcp: step 2/4: tunnel $opt_tunnel ($tunnel_id) has its credentials here"

# 3. DNS: the hostname becomes a CNAME of this tunnel
echo "dc-mcp: step 3/4: $opt_hostname will point at tunnel $opt_tunnel of THIS machine (an existing record is replaced, so any other machine serving that hostname stops receiving requests)."
printf 'Point %s at this machine? [y/N] ' "$opt_hostname"
read -r answer
case "$answer" in y|Y|yes|YES) ;; *) die "stopped before changing DNS; run this step again when you want it" ;; esac
"$cf" tunnel route dns --overwrite-dns "$opt_tunnel" "$opt_hostname" || die "cloudflared tunnel route dns failed"

# 4. start the three units (proxy, shim, tunnel)
sc daemon-reload
sc enable --now "${units[@]}" || die "systemctl --user enable --now ${units[*]} failed"
for _ in 1 2 3 4 5 6 7 8 9 10; do units_active && break; sleep 2; done
units_active || die "the units did not become active: systemctl --user status ${units[*]}"
echo "dc-mcp: step 4/4: ${units[*]} are active"

cat <<TEXT

ChatGPT connector (Settings, Connectors, add a custom MCP server):
  Server URL      https://$opt_hostname/mcp
  Authentication  OAuth
  Log in with the password stored in $auth_file (line PASSWORD=...; this script does not print it).
Check from here: curl -s -o /dev/null -w '%{http_code}' -X POST https://$opt_hostname/mcp -d '{}'   -> 401 means the login is enforced and the tunnel is up
(a fresh DNS record may need a minute before the name resolves).
TEXT
