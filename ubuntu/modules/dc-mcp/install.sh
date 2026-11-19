#!/usr/bin/env bash
# dc-mcp module installer (user level, never root): a thin wrapper over `computer chatgpt` (repository
# alex-mextner/computer-use-plugin). The CLI owns the pinned mcp-auth-proxy and cloudflared, Desktop Commander, the shim, the
# password and the unit text. This script:
#   1. installs the CLI with `uv tool install` at the commit pinned below, and reinstalls it when the uv receipt names another
#      source (ubuntu-tweak's packages phase compares tool names only, so a pin there would never upgrade an installed CLI);
#   2. writes the module's keys into ~/.config/computer/chatgpt/config.toml (other keys, which `init` adds, stay);
#   3. runs `computer mcp install` and `computer chatgpt init` (they start nothing);
#   4. on a machine set up by the first version of this module, copies the Cloudflare login and this tunnel's credentials from
#      ~/.cloudflared into the CLI's cloudflared_dir (mode 600, ~/.cloudflared itself is left as it is), because the unit `init`
#      writes reads them only from there.
#
#   install.sh           install (idempotent: a second run changes nothing)
#   install.sh --plan    read-only: what install would do
#   install.sh --check   read-only: exit 0 = in place, 3 = drift, 10 = needs a manual step
#
# The layout is fixed to the paths the live stack has used since issue #143, so the OAuth state (no new ChatGPT authorisation),
# the password and the access log history carry over: unit_prefix dc-mcp (units dc-mcp-bridge/-shim/-tunnel), state_dir
# ~/.local/state/dc-mcp, data_dir ~/.local/share/mcp-auth-proxy, password_file ~/.config/dc-mcp-auth.env.
#
# Options ([modules.dc-mcp] in machines/<host>/machine.toml), all validated:
#   hostname      REQUIRED, the public name of this machine's connector. ONE HOSTNAME PER MACHINE.
#   tunnel        Cloudflare tunnel name, default dc-<short hostname of the machine>
#   port          loopback port of the proxy, default 8766
#   shim_port     loopback port of the shim (the tunnel's target), default 8767
#   metrics_port  loopback port of cloudflared's metrics and /ready, default 20241
#
# Exit codes follow the ubuntu-tweak system-module protocol: 0 ok, 1 failed, 3 drift, 10 manual, 75 network failure.
# DC_MCP_LEGACY_CLOUDFLARED replaces ~/.cloudflared; it exists for the tests, nothing else sets it.
set -u

# Upgrading the stack is a change of this line (a commit of alex-mextner/computer-use-plugin; computer 0.4.1).
CLI_REPO=https://github.com/alex-mextner/computer-use-plugin.git
CLI_REV=84dda4ddc601cbccb5e74ec7a6e785f9b4dedf4f

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

command -v python3 >/dev/null 2>&1 || { echo "needs manual: python3 is not installed" >&2; exit 10; }
cfg_dir=${XDG_CONFIG_HOME:-$HOME/.config}
config_file=$cfg_dir/computer/chatgpt/config.toml
legacy=${DC_MCP_LEGACY_CLOUDFLARED:-$HOME/.cloudflared}
export DC_MCP_CONFIG_FILE=$config_file

# config <check|write|get KEY>: compare the module's keys with config.toml, merge them in, or print one effective value.
# check prints the differing keys and exits 1 when there are any; an invalid option prints the reason and exits 2.
config() {
  python3 -B - "$@" <<'PY'
import json, os, re, socket, sys, tomllib

known = {"hostname": "", "tunnel": "dc-" + socket.gethostname().split(".")[0], "port": 8766, "shim_port": 8767, "metrics_port": 20241}
options = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}")


def fail(message):
    print("[modules.dc-mcp] " + message, file=sys.stderr)
    sys.exit(2)


extra = sorted(set(options) - set(known))
if extra:
    fail("unknown option(s): " + " ".join(extra))
opt = {**known, **options}
if not isinstance(opt["hostname"], str) or not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", opt["hostname"]):
    fail("hostname is required and must be a lowercase DNS name such as dc-mcp.example.com: %r" % opt["hostname"])
if not isinstance(opt["tunnel"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", opt["tunnel"]):
    fail("tunnel must be a Cloudflare tunnel name (letters, digits, . _ -): %r" % opt["tunnel"])
for key in ("port", "shim_port", "metrics_port"):
    if isinstance(opt[key], bool) or not isinstance(opt[key], int) or not 1024 <= opt[key] <= 65535:
        fail("%s must be a number from 1024 to 65535: %r" % (key, opt[key]))
if len({opt["port"], opt["shim_port"], opt["metrics_port"]}) != 3:
    fail("port, shim_port and metrics_port must differ")

want = {
    "mode": "public",
    "scheme": "https",
    "hostname": opt["hostname"],
    "tunnel_name": opt["tunnel"],
    "proxy_port": opt["port"],
    "shim_port": opt["shim_port"],
    "metrics_port": opt["metrics_port"],
    "unit_prefix": "dc-mcp",
    "state_dir": "~/.local/state/dc-mcp",
    "data_dir": "~/.local/share/mcp-auth-proxy",
    "password_file": "~/.config/dc-mcp-auth.env",
}
path = os.environ["DC_MCP_CONFIG_FILE"]
have = {}
if os.path.exists(path):
    with open(path, "rb") as fh:
        have = tomllib.load(fh)
merged = {**have, **want}
if sys.argv[1] == "get":
    print(merged.get(sys.argv[2]) or "")
    sys.exit(0)
differ = [key for key, value in want.items() if have.get(key) != value]
if sys.argv[1] == "check":
    print(" ".join(differ))
    sys.exit(1 if differ else 0)
for key, value in merged.items():
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        fail("%s holds %r in %s; only flat strings and numbers are expected" % (key, value, path))


def toml_string(text):
    # A TOML basic string. JSON's escapes are valid TOML, but ensure_ascii would write a non-BMP character as a surrogate pair,
    # which TOML rejects, so non-ASCII stays raw; DEL is the one character JSON leaves raw that TOML forbids unescaped.
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


lines = ["# computer chatgpt: non-secret settings (the login password is not here)."]
lines += ["%s = %s" % (key, toml_string(value) if isinstance(value, str) else value) for key, value in merged.items()]
os.makedirs(os.path.dirname(path), exist_ok=True)
temp = path + ".tmp"
with open(temp, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
os.replace(temp, path)
PY
}

differ=$(config check); rc=$?
# An invalid or missing option is a manual step (fix machine.toml); the engine takes only 0 or 3 from --check, so there it is drift.
if [ "$rc" -gt 1 ]; then [ "$mode" = check ] && exit 3; exit 10; fi

# The source the uv receipt of hyper-computer names, as "<git url>?rev=<sha>"; empty when it is not installed by uv.
cli_source() {
  local dir
  dir=$(uv tool dir 2>/dev/null) || return 0
  python3 -B -c 'import sys, tomllib
try:
    with open(sys.argv[1], "rb") as fh:
        reqs = tomllib.load(fh)["tool"]["requirements"]
    print(next(r.get("git", "") for r in reqs if r.get("name") == "hyper-computer"))
except (OSError, KeyError, StopIteration, tomllib.TOMLDecodeError):
    pass' "$dir/hyper-computer/uv-receipt.toml"
}

# The CLI is always the uv entrypoint the receipt belongs to (the units run that path too), never whatever `computer` PATH finds.
have_uv=0; cli=
if command -v uv >/dev/null 2>&1; then have_uv=1; cli=$(uv tool dir --bin 2>/dev/null)/computer; fi
cli_ok() { [ "$have_uv" = 1 ] && [ -x "$cli" ] && [ "$(cli_source)" = "$CLI_REPO?rev=$CLI_REV" ]; }

# doctor_ok NAME...: 0 when every named check of `computer chatgpt doctor --local` passes, 1 when one fails, 2 when doctor cannot
# run or does not report one of the names (a renamed check in a newer CLI). 2 is never "missing piece": treating it so would re-run
# init and restart the running units on every apply.
doctor_ok() {
  "$cli" chatgpt doctor --local --json 2>/dev/null | python3 -c '
import json, sys
try:
    checks = {c["name"]: c["ok"] for c in json.load(sys.stdin)}
except (ValueError, TypeError, KeyError):
    print("dc-mcp: computer chatgpt doctor --local --json gave no readable report", file=sys.stderr)
    sys.exit(2)
unknown = [name for name in sys.argv[1:] if name not in checks]
if unknown:
    print("dc-mcp: computer chatgpt doctor --local reports no check named: " + ", ".join(unknown), file=sys.stderr)
    sys.exit(2)
sys.exit(0 if all(checks[name] for name in sys.argv[1:]) else 1)' "$@"
}

# The pieces `init` owns; units "active" and a fresh Cloudflare login belong to the finish step.
pieces_ok() { doctor_ok "mcp-auth-proxy installed" "cloudflared installed" "desktop-commander installed" "login password" "unit files current"; }

# The CLI's network failure (exit 7) is the engine's transient one (75, retried); a missing command (126/127) is a manual step;
# every other failure stays a failure.
run_cli() {
  "$cli" "$@"; local rc=$?
  case "$rc" in 7) return 75 ;; 126|127) return 10 ;; esac
  return "$rc"
}

# uv's own exit code does not tell a network failure from a bad source, so ask the network: unreachable = 75 (retried), else 1.
uv_failed() { curl -sf -o /dev/null --max-time 10 https://github.com && return 1; return 75; }

# Credentials of the first version of this module that the CLI's cloudflared_dir still lacks.
cf_dir() { local d; d=$(config get cloudflared_dir); d=${d:-$cfg_dir/computer/chatgpt/cloudflared}; printf '%s' "${d/#\~/$HOME}"; }
tunnel=$(config get tunnel_name)
legacy_pending() { [ -s "$legacy/cert.pem" ] && { [ ! -s "$(cf_dir)/cert.pem" ] || [ ! -s "$(cf_dir)/$tunnel.json" ]; }; }
# A tunnel runs on its <id>.json alone, without cert.pem; without the login the id of the name cannot be looked up, so such a
# machine is a manual step, never a silent success with a unit that names a missing key.
legacy_orphan_key() {
  [ ! -s "$legacy/cert.pem" ] && [ ! -s "$(cf_dir)/$tunnel.json" ] && compgen -G "$legacy/*-*-*-*-*.json" >/dev/null
}

# The pending marker: written before the first change (a CLI upgrade, or `init` rewriting unit text) and removed only once the
# installers ran and the units restarted, so an apply that fails or is killed anywhere in between resumes all of it next time.
# It lists the dc-mcp units that were running when the work began: those are restarted (a unit a failed restart left stopped
# included); units that were stopped stay stopped (the finish step starts them).
state_dir=$(config get state_dir); state_dir=${state_dir/#\~/$HOME}
restart_pending=$state_dir/units-restart-pending
mark_pending() {
  [ -e "$restart_pending" ] && return 0
  # an unreachable user manager answers is-active with an error, which must not be recorded as "nothing was running"
  systemctl --user show-environment >/dev/null 2>&1 || { echo "dc-mcp: the systemd user manager does not answer" >&2; return 1; }
  mkdir -p "$state_dir" || return 1
  local unit running=
  for unit in dc-mcp-bridge.service dc-mcp-shim.service dc-mcp-tunnel.service; do
    [ "$(systemctl --user is-active "$unit" 2>/dev/null)" = active ] && running="$running$unit"$'\n'
  done
  printf '%s' "$running" > "$restart_pending.tmp" && mv "$restart_pending.tmp" "$restart_pending"
}

# copy_private SRC DEST: mode 600 through a temp file and a rename, so a failed copy never leaves a truncated DEST behind
copy_private() {
  if install -m 600 "$1" "$2.tmp" && mv "$2.tmp" "$2"; then return 0; fi
  rm -f "$2.tmp"
  return 1
}

# The first version of this module could leave `PASSWORD=` followed by the generated assignment; systemd used the last one,
# the CLI reads the first and would replace an "empty" password. Keep only the effective (last) assignment.
normalize_password() {
  python3 -B - "$(config get password_file)" <<'PY'
import os, sys
path = os.path.expanduser(sys.argv[1])
try:
    with open(path) as fh:
        lines = fh.read().splitlines()
except FileNotFoundError:
    sys.exit(0)
assignments = [line for line in lines if line.startswith("PASSWORD=")]
if len(assignments) < 2:
    sys.exit(0)
kept = [line for line in lines if not line.startswith("PASSWORD=")] + [assignments[-1]]
temp = path + ".tmp"
fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write("\n".join(kept) + "\n")
os.replace(temp, path)
print("dc-mcp: kept the effective (last) PASSWORD= line of " + path)
PY
}

if [ "$mode" = check ]; then
  [ "$rc" -eq 0 ] || { echo "drift: $config_file differs from [modules.dc-mcp] in: $differ"; exit 3; }
  [ "$have_uv" = 1 ] || { echo "drift: uv is not installed yet (the computer CLI is installed with it)"; exit 3; }
  cli_ok || { echo "drift: the computer CLI is missing or not installed from $CLI_REPO at $CLI_REV"; exit 3; }
  pieces_ok; prc=$?
  [ "$prc" -eq 2 ] && exit 1
  [ "$prc" -eq 0 ] || { echo "drift: \`computer chatgpt doctor --local\` reports a missing piece (binaries, Desktop Commander, password or unit files)"; exit 3; }
  legacy_pending && { echo "drift: the Cloudflare login and tunnel credentials in $legacy are not copied to $(cf_dir) yet"; exit 3; }
  legacy_orphan_key && { echo "drift: $legacy holds tunnel credentials but no cert.pem to find tunnel $tunnel's id: run finish-setup --step dc-mcp-cloudflare"; exit 3; }
  [ -e "$restart_pending" ] && { echo "drift: running dc-mcp units still run the unit text before the last computer chatgpt init"; exit 3; }
  exit 0
fi

if [ "$mode" = plan ]; then
  cli_ok || echo "uv tool install --force hyper-computer @ git+$CLI_REPO@$CLI_REV"
  [ "$rc" -eq 0 ] || echo "write $differ into $config_file"
  { cli_ok && pieces_ok; } || echo "run computer mcp install and computer chatgpt init (pinned binaries, Desktop Commander, password, unit files; starts nothing)"
  legacy_pending && echo "copy $legacy/cert.pem and the credentials of tunnel $tunnel into $(cf_dir) (mode 600)"
  [ -e "$restart_pending" ] && echo "restart the running dc-mcp units on the new unit text"
  exit 0
fi

if [ "$have_uv" != 1 ]; then
  echo "needs manual: uv is not installed (packages.toml / the uv installer); the computer CLI is installed with it" >&2
  exit 10
fi
# Node for `computer mcp install`: the profile's rootless copy is in ~/.local/opt/node, which a non-login PATH lacks.
[ -x "$HOME/.local/opt/node/bin/node" ] && PATH=$HOME/.local/opt/node/bin:$PATH
if ! cli_ok; then
  mark_pending || exit 1
  uv tool install --force "hyper-computer @ git+$CLI_REPO@$CLI_REV" || { uv_failed; exit $?; }
  cli_ok || { echo "dc-mcp: $cli is still not the pinned $CLI_REV after uv tool install" >&2; exit 1; }
fi
[ "$rc" -eq 0 ] || config write || exit 1
pieces_ok; prc=$?
[ "$prc" -eq 2 ] && exit 1
if [ "$prc" -ne 0 ] && [ ! -e "$restart_pending" ]; then
  doctor_ok "unit files current"; urc=$?
  [ "$urc" -eq 2 ] && exit 1
  if [ "$urc" -ne 0 ]; then mark_pending || exit 1; fi
fi
# A new CLI revision re-runs both installers (their migrations, say Desktop Commander's lockfile, are not doctor checks) and
# restarts the running units even when the unit text is the same: the shim and the journal filter are the CLI's own code.
if [ "$prc" -ne 0 ] || [ -e "$restart_pending" ]; then
  normalize_password || exit 1
  run_cli mcp install || exit $?
  run_cli chatgpt init || exit $?
fi

if legacy_orphan_key; then
  echo "needs manual: $legacy holds tunnel credentials but no cert.pem to find tunnel $tunnel's id; run finish-setup --step dc-mcp-cloudflare" >&2
  exit 10
fi

if legacy_pending; then
  dir=$(cf_dir)
  mkdir -p "$dir" && chmod 700 "$dir" || exit 1
  [ -s "$dir/cert.pem" ] || copy_private "$legacy/cert.pem" "$dir/cert.pem" || exit 1
  if [ ! -s "$dir/$tunnel.json" ]; then
    # the credentials file is named after the tunnel id; ask Cloudflare which id the name has, with the pinned cloudflared
    data_home=${XDG_DATA_HOME:-$HOME/.local/share}
    install_dir=$(config get install_dir); install_dir=${install_dir:-$data_home/computer/chatgpt}
    cf=${install_dir/#\~/$HOME}/bin/cloudflared
    # an empty --config: a config.yml in ~/.cloudflared or /etc/cloudflared (say, one naming another origin cert) must not apply
    empty=$(mktemp) && printf '{}\n' > "$empty" || exit 1
    listing=$("$cf" tunnel --config "$empty" --origincert "$dir/cert.pem" list --name "$tunnel" -o json); lrc=$?
    rm -f "$empty"
    [ "$lrc" -eq 0 ] || { echo "dc-mcp: cannot ask Cloudflare for tunnel $tunnel" >&2; exit 75; }
    id=$(printf '%s' "$listing" | python3 -c 'import json, sys
rows = json.load(sys.stdin) or []
print(rows[0]["id"] if len(rows) == 1 else "")')
    if [ -z "$id" ] || [ ! -s "$legacy/$id.json" ]; then
      echo "needs manual: no credentials of tunnel $tunnel in $legacy; run finish-setup --step dc-mcp-cloudflare" >&2
      exit 10
    fi
    copy_private "$legacy/$id.json" "$dir/$tunnel.json" || exit 1
    echo "dc-mcp: copied the Cloudflare login and the credentials of tunnel $tunnel into $dir"
  fi
fi

if [ -e "$restart_pending" ]; then
  # an init interrupted before its own daemon-reload would leave systemd on the cached old commands
  systemctl --user daemon-reload || { echo "dc-mcp: systemctl --user daemon-reload failed" >&2; exit 1; }
  while IFS= read -r unit; do
    [ -n "$unit" ] || continue
    systemctl --user restart "$unit" || { echo "dc-mcp: systemctl --user restart $unit failed" >&2; exit 1; }
    echo "dc-mcp: restarted $unit on the unit text of computer chatgpt init"
  done < "$restart_pending"
  rm -f "$restart_pending" || exit 1
fi
exit 0
