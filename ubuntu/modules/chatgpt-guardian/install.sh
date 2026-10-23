#!/usr/bin/env bash
# chatgpt-guardian profile module (user level, never root): the ChatGPT guardian of agent-tools. It watches
# chats in two DEDICATED, logged-in ordinary Chrome profiles (CDP on loopback) and, with --apply, sends the
# one word "go" to a chat whose latest turn is a verified `Thinking failed`. It works through the ChatGPT web
# UI session of those profiles: no OpenAI API key is created, asked for or stored by this module.
#
#   install.sh           make every fact below true
#   install.sh --plan    read-only: what install is going to do
#   install.sh --check   read-only: exit 0 = all facts hold, 3 = drift (one line each), 1 = the check broke
#
# Exit codes (the ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual step
# (the last output line says which), anything else is a failure.
#
# Facts, in order:
#   1. the checkout <dir> of agent-tools exists (cloned from <repo> when missing) and holds the package
#   2. ~/.local/share/agent-tools/chatgpt-guardian-venv has agenttools_chatgpt (+ playwright) identical to the package
#   3. ~/.local/bin/chatgpt-guardian links to the venv's CLI
#   4. Chrome: <chrome> is an executable (chrome = "user": a dpkg -x copy of google-chrome-stable under ~/.local/opt)
#   5. Xvfb, xauth and mcookie (the glide.vc private display) are found
#   6. per workspace (home, glide.vc): control token + state exist, the deployment file
#      ~/.config/agent-tools/chatgpt-guardian/<workspace>.json exists (the units' ConditionPathExists), the
#      configured chats are enrolled (an explicit unwatch is respected: only a chat never seen is watched)
#   7. the seven chatgpt-guardian-* units and the browsers' lifecycle drop-ins (from the package's own templates)
#      are installed as rendered here, stale per-machine drop-ins (90-pc.conf, 95-port.conf) are gone; displays,
#      browsers and observers are enabled and started; a copied guardian-tunnel.service is gated off (it needs a
#      platform API key)
#   8. each browser profile is logged in to ChatGPT (cannot be declared: reported as a manual step); only then
#      its --apply guardian is enabled and started, because a guardian that meets the login page latches a
#      durable human-challenge stop that only an explicit `resume` clears
#
# Options ([modules.chatgpt-guardian] of profile.toml / machine.toml, JSON in UBUNTU_TWEAK_MODULE_CONFIG):
#   repo        git URL of agent-tools   (default https://git.hyperide.ai/ultrabricks/agent-tools.git)
#   dir         where its checkout lives (default ~/xp/agent-tools)
#   package     the package directory    (default <dir>/lib/agenttools_chatgpt)
#   chrome      absolute path of Chrome, or "user" for the rootless install (default /usr/bin/google-chrome)
#   home_port   DevTools port of the home account browser (default 9226: omp's browser relay owns 9224)
#   glide_port  DevTools port of the glide.vc browser     (default 9223)
#   xvfb        absolute path of Xvfb    (default: /usr/bin/Xvfb, else the chatgpt-native-ha gui-runtime copy)
#   chats_home, chats_glide   lists of https://chatgpt.com/... chat URLs to enrol (default none)
set -euo pipefail

usage() { awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"; }

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

die() { echo "[modules.chatgpt-guardian] $1" >&2; exit 1; }
manual() { echo "needs manual: $1" >&2; exit 10; }

command -v python3 >/dev/null || manual "python3 is not installed"

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# -- options -----------------------------------------------------------------------------------
# One python call validates UBUNTU_TWEAK_MODULE_CONFIG and prints its options as shell assignments.
options=$(python3 -B - <<'PY'
import json, os, re, shlex, sys

known = {"repo": "https://git.hyperide.ai/ultrabricks/agent-tools.git", "dir": "~/xp/agent-tools", "package": "",
         "chrome": "/usr/bin/google-chrome", "home_port": 9226, "glide_port": 9223, "xvfb": "",
         "chats_home": [], "chats_glide": []}
config = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}")


def fail(message):
    print("die " + shlex.quote(message))
    sys.exit(0)


extra = sorted(set(config) - set(known))
if extra:
    fail("unknown option(s): " + " ".join(extra))
opt = {**known, **config}
for port_key in ("home_port", "glide_port"):
    if not re.fullmatch(r"[0-9]+", str(opt[port_key])):
        fail("ports must be numbers: %s" % opt[port_key])
    if not 1024 <= int(opt[port_key]) <= 65535:
        fail("port out of range: %s" % opt[port_key])
if int(opt["home_port"]) == int(opt["glide_port"]):
    fail("home_port and glide_port must differ")
for key in ("repo", "dir", "package", "chrome", "xvfb"):
    if not isinstance(opt[key], str):
        fail("%s must be a string" % key)
if opt["repo"].startswith("-"):
    fail("repo must not start with '-': %s" % opt["repo"])
for key in ("chats_home", "chats_glide"):
    value = opt[key]
    if not isinstance(value, list) or not all(isinstance(u, str) and re.fullmatch(r"https://chatgpt\.com/\S+", u) for u in value):
        fail("%s must be a list of https://chatgpt.com/... URLs" % key)


def expand(path):
    return os.path.expanduser(path) if path.startswith("~/") else path


for key in ("dir", "package", "chrome", "xvfb"):
    opt[key] = expand(opt[key])
if not opt["dir"].startswith("/"):
    fail("dir must be absolute or start with ~/: %s" % opt["dir"])
if opt["chrome"] != "user" and not opt["chrome"].startswith("/"):
    fail('chrome must be an absolute path or "user": %s' % opt["chrome"])
if not opt["package"]:
    opt["package"] = opt["dir"] + "/lib/agenttools_chatgpt"
for key in ("repo", "dir", "package", "chrome", "xvfb", "home_port", "glide_port"):
    print("opt_%s=%s" % (key, shlex.quote(str(opt[key]))))
for key in ("chats_home", "chats_glide"):
    print("opt_%s=(%s)" % (key, " ".join(shlex.quote(u) for u in opt[key])))
PY
)
eval "$options"

repo_url=$opt_repo
dir=$opt_dir
package=$opt_package
chrome_opt=$opt_chrome
home_port=$opt_home_port
glide_port=$opt_glide_port

venv="$HOME/.local/share/agent-tools/chatgpt-guardian-venv"
vpy="$venv/bin/python"
cli_link="$HOME/.local/bin/chatgpt-guardian"
# The units hardcode %h/.config/agent-tools/... in their ConditionPathExists, so no XDG_CONFIG_HOME indirection here.
units_dir="$HOME/.config/systemd/user"
dep_dir="$HOME/.config/agent-tools/chatgpt-guardian"
chrome_user_dir="$HOME/.local/opt/google-chrome"
chrome_user_bin="$chrome_user_dir/opt/google/chrome/chrome"
# A dedicated wrapper name: a generic ~/.local/bin/google-chrome would be picked up by xdg-open and every launcher.
chrome_wrapper="$HOME/.local/bin/chatgpt-guardian-chrome"
if [ "$chrome_opt" = user ]; then chrome="$chrome_wrapper"; else chrome=$chrome_opt; fi

# Xvfb: the apt package, else the copy the chatgpt-native-ha tool extracts under its gui-runtime (a rootless machine
# has no apt); that layout belongs to that tool, so name the path with the xvfb option when it moves.
xvfb=$opt_xvfb
if [ -z "$xvfb" ]; then
  if [ -x /usr/bin/Xvfb ]; then xvfb=/usr/bin/Xvfb
  elif [ -x "$HOME/.local/share/chatgpt-native-ha/gui-runtime/root/usr/bin/Xvfb" ]; then
    xvfb="$HOME/.local/share/chatgpt-native-ha/gui-runtime/root/usr/bin/Xvfb"
  fi
fi

workspaces=(home glide.vc)
declare -A ws_unit=([home]=home [glide.vc]=glide)
declare -A ws_port=([home]="$home_port" [glide.vc]="$glide_port")
unit_names=(chatgpt-guardian-glide-display chatgpt-guardian-glide-browser chatgpt-guardian-glide chatgpt-guardian-glide-observer
            chatgpt-guardian-home-browser chatgpt-guardian-home chatgpt-guardian-home-observer)
stale_dropins=(90-pc.conf 95-port.conf)
login_attempts=10
login_wait=3
start_wait=60

sc() { systemctl --user "$@"; }
is_active() { [ "$(sc is-active "$1.service" 2>/dev/null || true)" = active ]; }
is_enabled() { [ "$(sc is-enabled "$1.service" 2>/dev/null || true)" = enabled ]; }
chats_of() { local -n chats="opt_chats_${ws_unit[$1]}"; printf '%s\n' "${chats[@]}"; }

# -- rendering ---------------------------------------------------------------------------------
header="# Managed by ubuntu-tweaks (profile module chatgpt-guardian); local edits are overwritten on apply."
render() { # render TEMPLATE KEY=VALUE...: the template with every @KEY@ replaced; any other @TOKEN@ is an error
  local template=$1; shift
  python3 -B - "$template" "$header" "$@" <<'PY'
import re, sys

path, header, *pairs = sys.argv[1:]
text = open(path, encoding="utf-8").read()
for pair in pairs:
    key, _, value = pair.partition("=")
    text = text.replace("@%s@" % key, value)
left = sorted(set(re.findall(r"@[A-Z_]+@", text)))
if left:
    sys.exit("unreplaced template token(s) in %s: %s" % (path, " ".join(left)))
sys.stdout.write(header + "\n" + text)
PY
}
render_unit() { render "$here/units/$1.service" "CHROME=$chrome" "HOME_PORT=$home_port" "GLIDE_PORT=$glide_port" "XVFB=$xvfb"; }
site_pkg() { # the installed package directory in the venv
  local d
  for d in "$venv"/lib/python3*/site-packages/agenttools_chatgpt; do
    [ -d "$d" ] && { echo "$d"; return 0; }
  done
  return 1
}
render_lifecycle() { # render_lifecycle WS: the package's own drop-in template for that browser unit
  local sp
  sp=$(site_pkg) || return 1
  render "$sp/systemd/chatgpt-guardian-${ws_unit[$1]}-browser.service.d/lifecycle.conf.template" \
    "VENV_PYTHON=$vpy" "CDP_PORT=${ws_port[$1]}"
}
tunnel_gate_text() {
  printf '%s\n' "$header"
  cat <<'EOF'
# guardian-tunnel needs an OpenAI platform API key (CONTROL_PLANE_API_KEY); the guardian itself works without it through
# the logged-in browsers. Start the tunnel only after you created that key yourself and ran guardian-connect:
# touch ~/.config/guardian-connect/.credential-ok
[Unit]
ConditionPathExists=%h/.config/guardian-connect/.credential-ok
EOF
}

# -- facts -------------------------------------------------------------------------------------
has_package() { [ -f "$package/pyproject.toml" ] && [ -f "$package/startup.py" ]; }
venv_ok() { [ -x "$vpy" ] && "$vpy" -c 'import playwright, agenttools_chatgpt' 2>/dev/null; }
pkg_same() { # the installed copy holds exactly the package's code, scripts and unit templates
  local sp f rel
  sp=$(site_pkg) || return 1
  shopt -s nullglob
  for f in "$package"/*.py "$package"/*.js "$package"/systemd/*/*.template; do
    rel=${f#"$package"/}
    cmp -s "$f" "$sp/$rel" || return 1
  done
  for f in "$sp"/*.py "$sp"/*.js "$sp"/systemd/*/*.template; do # a file deleted from the package is drift too
    rel=${f#"$sp"/}
    [ -f "$package/$rel" ] || return 1
  done
  shopt -u nullglob
  return 0
}
link_ok() { [ -L "$cli_link" ] && [ "$(readlink -f "$cli_link")" = "$(readlink -f "$venv/bin/chatgpt-guardian")" ]; }
chrome_ok() { [ -x "$chrome" ] && { [ "$chrome_opt" != user ] || [ -x "$chrome_user_bin" ]; }; }
xvfb_ok() { [ -n "$xvfb" ] && [ -x "$xvfb" ]; }
display_tools_ok() { [ -x /usr/bin/xauth ] && [ -x /usr/bin/mcookie ]; } # the glide display unit's ExecStartPre
dep_file() { echo "$dep_dir/$1.json"; }
dep_ok() { [ -f "$(dep_file "$1")" ]; }
state_path() { echo "$HOME/.local/state/agent-tools/chatgpt-guardian/$1/state.sqlite"; }
status_json() { "$cli_link" --workspace "$1" status 2>/dev/null; } # fails when the store cannot be read
watch_value() { # watch_value STATUS_JSON URL: '1', '0' or '' (never seen); fails when the JSON is not the expected shape
  printf '%s' "$1" | python3 -B -c 'import json, sys
print(json.load(sys.stdin)["controls"].get("watch:" + sys.argv[1], ""))' "$2" 2>/dev/null
}
unit_file() { echo "$units_dir/$1.service"; }
unit_ok() { # unit_ok NAME: installed text equals the rendering; a rendering failure counts as drift
  local want
  want=$(render_unit "$1" 2>/dev/null) || return 1
  [ -f "$(unit_file "$1")" ] && [ "$(cat "$(unit_file "$1")")" = "$want" ]
}
lifecycle_file() { echo "$units_dir/chatgpt-guardian-${ws_unit[$1]}-browser.service.d/lifecycle.conf"; }
lifecycle_ok() {
  local want
  want=$(render_lifecycle "$1" 2>/dev/null) || return 1
  [ -f "$(lifecycle_file "$1")" ] && [ "$(cat "$(lifecycle_file "$1")")" = "$want" ]
}
tunnel_unit="$units_dir/guardian-tunnel.service"
tunnel_gate="$units_dir/guardian-tunnel.service.d/90-no-platform-key.conf"
tunnel_gate_ok() { [ ! -f "$tunnel_unit" ] || { [ -f "$tunnel_gate" ] && [ "$(cat "$tunnel_gate")" = "$(tunnel_gate_text)" ]; }; }
stale_list() {
  local n f
  for n in "${unit_names[@]}"; do
    for f in "${stale_dropins[@]}"; do
      if [ -e "$units_dir/$n.service.d/$f" ]; then echo "$units_dir/$n.service.d/$f"; fi
    done
  done
  if [ -e "$units_dir/guardian-tunnel.service.d/90-pc.conf" ]; then echo "$units_dir/guardian-tunnel.service.d/90-pc.conf"; fi
  return 0
}
login_state() { # login_state PORT: yes | no | unreachable (no CDP answer). Opens a short-lived chatgpt.com tab when none is open.
  [ -x "$vpy" ] || { echo unreachable; return 0; }
  # The probe prints exactly one word; any failure before it means no answer (unreachable), and the whole call is time-boxed.
  timeout 90 "$vpy" -B - "$1" <<'PY' 2>/dev/null || echo unreachable
import sys
from playwright.sync_api import sync_playwright

# A challenge page or a non-JSON answer means the profile needs a human: that is "no", not a dead browser.
FETCH = """async () => { try { const r = await fetch('/api/auth/session', {credentials: 'include', signal: AbortSignal.timeout(20000)}); const j = await r.json(); return !!(j && j.user) } catch (e) { return false } }"""
answer = "unreachable"
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:" + sys.argv[1], timeout=8000)
    try:
        context = browser.contexts[0]
        pages = [pg for pg in context.pages if pg.url.startswith("https://chatgpt.com")]
        opened = None
        if not pages:
            opened = context.new_page()
            opened.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=20000)
            pages = [opened]
        try:
            answer = "yes" if pages[0].evaluate(FETCH) else "no"
        finally:
            if opened is not None:
                opened.close()
    finally:
        try:
            browser.close()
        except Exception:
            pass
print(answer)
PY
}

# -- --check: read-only ------------------------------------------------------------------------
if [ "$mode" = check ]; then
  problems=()
  has_package || problems+=("agent-tools package missing at $package")
  if has_package; then
    if ! venv_ok; then
      problems+=("$venv has no agenttools_chatgpt + playwright")
    elif ! pkg_same; then
      problems+=("$venv holds a different agenttools_chatgpt than $package")
    fi
  fi
  link_ok || problems+=("$cli_link is missing or points elsewhere")
  chrome_ok || problems+=("Chrome is not executable at $chrome")
  xvfb_ok || problems+=("Xvfb not found (apt install xvfb)")
  display_tools_ok || problems+=("/usr/bin/xauth or /usr/bin/mcookie missing (apt install xauth; mcookie is util-linux)")
  for ws in "${workspaces[@]}"; do
    dep_ok "$ws" || problems+=("$(dep_file "$ws") is missing")
  done
  # read-only: the store of a workspace without a deployment file is not opened (opening creates it)
  if [ -x "$cli_link" ]; then
    for ws in "${workspaces[@]}"; do
      dep_ok "$ws" || continue
      if ! status=$(status_json "$ws"); then
        problems+=("$ws: chatgpt-guardian status failed: the enrolment cannot be checked")
        continue
      fi
      while IFS= read -r url; do
        [ -n "$url" ] || continue
        if ! value=$(watch_value "$status" "$url"); then
          problems+=("$ws: chatgpt-guardian status is unreadable: the enrolment cannot be checked")
          break
        fi
        [ -n "$value" ] || problems+=("$ws: $url is not enrolled")
      done < <(chats_of "$ws")
    done
  fi
  if has_package && site_pkg >/dev/null; then
    for n in "${unit_names[@]}"; do unit_ok "$n" || problems+=("$(unit_file "$n") is missing or differs"); done
    for ws in "${workspaces[@]}"; do lifecycle_ok "$ws" || problems+=("$(lifecycle_file "$ws") is missing or differs"); done
  else
    problems+=("the package is not installed in the venv yet: the units cannot be compared")
  fi
  tunnel_gate_ok || problems+=("$tunnel_gate is missing or differs (guardian-tunnel needs a platform API key)")
  while IFS= read -r f; do problems+=("stale drop-in $f"); done < <(stale_list)
  if sc show -p Id --value default.target >/dev/null 2>&1; then
    for n in "${unit_names[@]}"; do
      is_enabled "$n" || problems+=("$n.service is not enabled")
    done
    for ws in "${workspaces[@]}"; do
      if dep_ok "$ws"; then
        for n in "chatgpt-guardian-${ws_unit[$ws]}-browser" "chatgpt-guardian-${ws_unit[$ws]}" "chatgpt-guardian-${ws_unit[$ws]}-observer"; do
          is_active "$n" || problems+=("$n.service is not active")
        done
      fi
    done
  else
    echo "systemctl --user is unavailable (no user bus): the check cannot decide" >&2
    exit 1
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'drift: %s\n' "${problems[@]}"
    exit 3
  fi
  echo "chatgpt-guardian is installed and its services run"
  exit 0
fi

# -- --plan: read-only -------------------------------------------------------------------------
if [ "$mode" = plan ]; then
  if has_package; then echo "package: $package present"
  elif [ -e "$dir" ]; then echo "$dir exists but has no lib/agenttools_chatgpt/startup.py: needs manual"
  else echo "git clone $repo_url $dir"; fi
  if venv_ok && has_package && pkg_same; then echo "venv: $venv matches the package"
  else echo "uv venv / pip install $package[browser] into $venv"; fi
  if link_ok; then echo "link: $cli_link ok"; else echo "ln -sfn $venv/bin/chatgpt-guardian $cli_link"; fi
  if chrome_ok; then echo "chrome: $chrome ok"
  elif [ "$chrome_opt" = user ]; then echo "download google-chrome-stable .deb, dpkg-deb -x into $chrome_user_dir, wrapper $chrome_wrapper (--no-sandbox)"
  else echo "chrome: $chrome missing: needs manual (install google-chrome-stable or set chrome = \"user\")"; fi
  if xvfb_ok; then echo "xvfb: $xvfb"; else echo "xvfb: not found: needs manual (sudo apt install xvfb)"; fi
  for ws in "${workspaces[@]}"; do
    if dep_ok "$ws"; then echo "$ws: deployment file present"; else echo "$ws: chatgpt-guardian init, write $(dep_file "$ws")"; fi
    chats_of "$ws" | while IFS= read -r url; do [ -z "$url" ] || echo "$ws: watch $url (when never seen)"; done
  done
  echo "units: render ${#unit_names[@]} units + 2 lifecycle drop-ins into $units_dir (home port $home_port, glide.vc port $glide_port), drop stale 90-pc.conf / 95-port.conf, daemon-reload, enable, start the stopped displays, browsers and observers"
  [ ! -f "$tunnel_unit" ] || echo "guardian-tunnel.service: gate behind ~/.config/guardian-connect/.credential-ok (no platform API key is created)"
  echo "logins: each browser profile must be signed in to ChatGPT (reported, not done); only a signed-in profile gets its guardian enabled and started"
  exit 0
fi

# -- install -----------------------------------------------------------------------------------
command -v git >/dev/null || manual "git is not installed (module base-tools installs it)"
sc show -p Id --value default.target >/dev/null 2>&1 || { echo "systemctl --user is unavailable (no user bus)" >&2; exit 1; }

if ! has_package; then
  [ ! -e "$dir" ] || manual "$dir exists but has no lib/agenttools_chatgpt/startup.py (the guardian is not on that checkout's branch): set [modules.chatgpt-guardian] package to the directory that has it"
  mkdir -p "$(dirname "$dir")"
  echo "cloning $repo_url -> $dir"
  git clone -- "$repo_url" "$dir" || manual "cannot clone $repo_url (git credentials for that forge, e.g. fj auth login, or the network); then re-run"
  has_package || manual "$dir has no lib/agenttools_chatgpt/startup.py on its default branch: check out the branch that has the guardian or set [modules.chatgpt-guardian] package"
fi

if ! venv_ok || ! pkg_same; then
  echo "installing $package into $venv"
  if [ ! -x "$vpy" ]; then
    mkdir -p "$(dirname "$venv")"
    if command -v uv >/dev/null; then uv venv --quiet "$venv"; else python3 -m venv "$venv"; fi
  fi
  if command -v uv >/dev/null; then
    uv pip install --quiet --python "$vpy" --reinstall-package agenttools-chatgpt "$package[browser]"
  else
    "$vpy" -m pip install --quiet --force-reinstall --no-deps "$package"
    "$vpy" -m pip install --quiet "$package[browser]"
  fi
  venv_ok || die "the venv still cannot import agenttools_chatgpt + playwright"
fi

mkdir -p "$(dirname "$cli_link")"
if ! link_ok; then
  [ ! -e "$cli_link" ] || [ -L "$cli_link" ] || manual "$cli_link exists and is not a symlink; move it away"
  ln -sfn "$venv/bin/chatgpt-guardian" "$cli_link"
fi

if ! chrome_ok; then
  [ "$chrome_opt" = user ] || manual "Chrome is not executable at $chrome: install google-chrome-stable (sudo apt) or set [modules.chatgpt-guardian] chrome = \"user\" for the rootless copy"
  if [ ! -x "$chrome_user_bin" ]; then
    command -v curl >/dev/null && command -v dpkg-deb >/dev/null || manual "curl and dpkg-deb are needed for the user-level Chrome"
    tmp=$(mktemp --suffix=.deb)
    unpacked=$(mktemp -d)
    echo "downloading google-chrome-stable (HTTPS from dl.google.com; Google publishes no checksum for the 'current' package)"
    curl -fsSL -o "$tmp" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb || { rm -rf "$tmp" "$unpacked"; manual "cannot download Chrome (network)"; }
    # unpack to a temp dir and move it into place: an interrupted unpack never leaves a half Chrome that looks installed
    dpkg-deb -x "$tmp" "$unpacked" || { rm -rf "$tmp" "$unpacked"; die "cannot unpack the Chrome package"; }
    mkdir -p "$(dirname "$chrome_user_dir")"
    rm -rf "$chrome_user_dir"
    mv "$unpacked" "$chrome_user_dir"
    rm -f "$tmp"
  fi
  {
    echo '#!/bin/sh'
    echo '# Chrome of the chatgpt-guardian module (dpkg -x of google-chrome-stable). --no-sandbox: unprivileged user namespaces are'
    echo '# AppArmor-restricted on recent Ubuntu and there is no setuid sandbox helper without root. Only the two dedicated'
    echo '# guardian profiles run through this wrapper; it is deliberately not named google-chrome.'
    printf 'exec %q --no-sandbox "$@"\n' "$chrome_user_bin"
  } > "$chrome_wrapper"
  chmod 0755 "$chrome_wrapper"
fi

xvfb_ok || manual "Xvfb is not installed: sudo apt install xvfb (the glide.vc browser runs on a private display)"
display_tools_ok || manual "xauth or mcookie is missing: sudo apt install xauth (mcookie comes with util-linux)"

# per workspace: token + state, deployment file (the units' ConditionPathExists), enrolled chats
for ws in "${workspaces[@]}"; do
  if ! dep_ok "$ws"; then
    "$cli_link" --workspace "$ws" init >/dev/null
    status_json "$ws" >/dev/null || die "$ws: chatgpt-guardian could not open its store"   # opens it once, so the schema exists before the deployment file points at it
    mkdir -p "$dep_dir"
    python3 -B -c 'import json, sys
print(json.dumps({"workspace": sys.argv[1], "state": sys.argv[2]}, separators=(",", ":")))' "$ws" "$(state_path "$ws")" > "$(dep_file "$ws").tmp"
    chmod 0600 "$(dep_file "$ws").tmp"
    mv -f "$(dep_file "$ws").tmp" "$(dep_file "$ws")"
    status_json "$ws" >/dev/null || die "$ws: the guardian refuses the deployment file $(dep_file "$ws")"
  fi
  # A failed status must never read as "never seen": that would re-enrol a chat the user deliberately unwatched.
  status=$(status_json "$ws") || die "$ws: chatgpt-guardian status failed (store busy or unreadable); enrolment left alone, run apply again"
  while IFS= read -r url; do
    [ -n "$url" ] || continue
    value=$(watch_value "$status" "$url") || die "$ws: chatgpt-guardian status output is unreadable; enrolment left alone, run apply again"
    [ -n "$value" ] || "$cli_link" --workspace "$ws" watch "$url" </dev/null
  done < <(chats_of "$ws")
done

# units
mkdir -p "$units_dir"
changed=0
declare -A rewritten=()
install_text() { # install_text PATH TEXT
  mkdir -p "$(dirname "$1")"
  printf '%s\n' "$2" > "$1.tmp"
  mv -f "$1.tmp" "$1"
  changed=1
}
for n in "${unit_names[@]}"; do
  if ! unit_ok "$n"; then
    text=$(render_unit "$n") || die "cannot render $n.service"
    install_text "$(unit_file "$n")" "$text"
    rewritten[$n]=1
  fi
done
for ws in "${workspaces[@]}"; do
  if ! lifecycle_ok "$ws"; then
    text=$(render_lifecycle "$ws") || die "cannot render the lifecycle drop-in of $ws (does the installed package ship systemd/*/lifecycle.conf.template?)"
    install_text "$(lifecycle_file "$ws")" "$text"
  fi
done
tunnel_gate_ok || install_text "$tunnel_gate" "$(tunnel_gate_text)"
while IFS= read -r f; do rm -f -- "$f"; changed=1; done < <(stale_list)
if [ "$changed" != 0 ]; then
  sc daemon-reload
  # systemd keeps a running unit on its old definition: say which ones need a restart (a running browser keeps its windows, so this is never done silently)
  restart_needed=()
  for n in "${!rewritten[@]}"; do is_active "$n" && restart_needed+=("$n"); done
  [ ${#restart_needed[@]} -eq 0 ] || echo "changed units run on their old definition until restarted: systemctl --user restart ${restart_needed[*]}"
fi

enable_unit() { # a rewritten unit is re-enabled: a changed [Install] section must drop the old wants link
  if [ -n "${rewritten[$1]:-}" ] && is_enabled "$1"; then sc reenable "$1.service" >/dev/null
  elif ! is_enabled "$1"; then sc enable "$1.service" >/dev/null; fi
}
to_start=()
start_unit() {
  is_active "$1" && return 0
  sc start "$1.service" || echo "warning: $1.service failed to start" >&2
  to_start+=("$1")
}

# Only stopped units are started: a running browser keeps its windows; a changed unit takes effect at its next restart.
for n in chatgpt-guardian-glide-display chatgpt-guardian-glide-browser chatgpt-guardian-home-browser \
         chatgpt-guardian-glide-observer chatgpt-guardian-home-observer; do
  enable_unit "$n"
  start_unit "$n"
done

# logins live in the browser profiles: report, never fake. The --apply guardian of a workspace is enabled and
# started only once its profile is signed in.
signed_out=()
unreachable=()
for ws in "${workspaces[@]}"; do
  state=unreachable
  for _ in $(seq "$login_attempts"); do
    state=$(login_state "${ws_port[$ws]}")
    [ "$state" != unreachable ] && break
    sleep "$login_wait"
  done
  case "$state" in
    yes) enable_unit "chatgpt-guardian-${ws_unit[$ws]}"; start_unit "chatgpt-guardian-${ws_unit[$ws]}" ;;
    no) signed_out+=("$ws") ;;
    *) unreachable+=("$ws") ;;
  esac
done

# the started units must come up (a browser waits for its display; give them time before judging)
down=()
for n in "${to_start[@]}"; do
  for _ in $(seq "$start_wait"); do is_active "$n" && break; sleep 1; done
  is_active "$n" || down+=("$n")
done
if [ ${#down[@]} -gt 0 ]; then
  echo "not running after start: ${down[*]} (journalctl --user -u <unit>; the home browser needs the desktop session's DISPLAY)" >&2
  exit 1
fi
if [ ${#unreachable[@]} -gt 0 ]; then
  echo "the DevTools port of ${unreachable[*]} did not answer: its browser is not up (journalctl --user -u chatgpt-guardian-<workspace>-browser)" >&2
  exit 1
fi
if [ ${#signed_out[@]} -gt 0 ]; then
  manual "sign in to ChatGPT in the guardian browser profile(s): ${signed_out[*]} (home = the browser window on this desktop, DevTools port $home_port; glide.vc = the private display :111, port $glide_port: stop chatgpt-guardian-glide-browser, open the same profile dir ~/.local/share/agent-tools/chatgpt-guardian/profiles/glide.vc in a normal Chrome window once, sign in, close it, start the unit); then run apply again"
fi
