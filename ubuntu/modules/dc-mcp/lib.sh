# Shared by install.sh and finish.sh (sourced, never run): the options of [modules.dc-mcp] as shell variables.
# One python call validates UBUNTU_TWEAK_MODULE_CONFIG and prints the options as shell assignments:
#   opt_hostname  public hostname of THIS machine's connector (required, no default: two machines must never serve one)
#   opt_tunnel    Cloudflare tunnel name (default dc-<short hostname of the machine>)
#   opt_port      loopback port of the OAuth proxy (default 8766)
#   opt_shim_port loopback port of the shim the tunnel points at (default 8767)
#   opt_log_max_kib / opt_log_keep   size cap (KiB) and number of rotated files of the shim access log (2048, 3)
#   opt_tool_log_keep / opt_tool_log_max_age_days   retention of Desktop Commander's rotated tool-call logs (5 files, 30 days)
#   opt_package   npm package the proxy launches over stdio (default @wonderwhy-er/desktop-commander@latest)
# An invalid value is `die`d (exit 1). A missing hostname is NOT an error here: callers decide (manual step).

load_options() {
  local rendered
  command -v python3 >/dev/null 2>&1 || { echo "needs manual: python3 is not installed" >&2; exit 10; }
  rendered=$(python3 -B - <<'PY'
import json, os, re, shlex, socket, sys

known = {"hostname": "", "tunnel": "dc-" + socket.gethostname().split(".")[0], "port": 8766, "shim_port": 8767,
         "log_max_kib": 2048, "log_keep": 3, "tool_log_keep": 5, "tool_log_max_age_days": 30,
         "package": "@wonderwhy-er/desktop-commander@latest"}
config = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}")


def fail(message):
    print("echo %s >&2; exit 1" % shlex.quote("[modules.dc-mcp] " + message))
    sys.exit(0)


extra = sorted(set(config) - set(known))
if extra:
    fail("unknown option(s): " + " ".join(extra))
opt = {**known, **config}
for key in ("hostname", "tunnel", "package"):
    if not isinstance(opt[key], str):
        fail("%s must be a string" % key)
if opt["hostname"] and not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", opt["hostname"]):
    fail("hostname must be a lowercase DNS name such as dc-mcp.example.com: %s" % opt["hostname"])
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", opt["tunnel"]):
    fail("tunnel must be a Cloudflare tunnel name (letters, digits, . _ -): %s" % opt["tunnel"])
for key in ("port", "shim_port"):
    if isinstance(opt[key], bool) or not isinstance(opt[key], int) or not 1024 <= opt[key] <= 65535:
        fail("%s must be a number from 1024 to 65535: %s" % (key, opt[key]))
for key, low, high in (("log_max_kib", 64, 1048576), ("log_keep", 0, 20), ("tool_log_keep", 1, 100), ("tool_log_max_age_days", 1, 3650)):
    if isinstance(opt[key], bool) or not isinstance(opt[key], int) or not low <= opt[key] <= high:
        fail("%s must be a number from %d to %d: %s" % (key, low, high, opt[key]))
if opt["port"] == opt["shim_port"]:
    fail("port and shim_port must differ: %s" % opt["port"])
if not re.fullmatch(r"(@[A-Za-z0-9._~-]+/)?[A-Za-z0-9._~-]+(@[A-Za-z0-9._~-]+)?", opt["package"]):
    fail("package must be an npm package spec such as @scope/name@1.2.3: %s" % opt["package"])
for key in ("hostname", "tunnel", "port", "shim_port", "log_max_kib", "log_keep", "tool_log_keep", "tool_log_max_age_days", "package"):
    print("opt_%s=%s" % (key, shlex.quote(str(opt[key]))))
PY
  ) || exit 1
  eval "$rendered"
}
