"""Shared fixtures for the stub-based unit tests of the module's scripts (save-now, alert, watchdog, hooks, boot guard).

Nothing here touches the real tmux server, systemd, alert channel or the real $HOME: every script runs against a temp
HOME with stub `tmux` and `systemctl` on PATH, whose state lives in files under $STUB.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_exec(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


# A tmux whose options live in files ($STUB/opt-NAME) and whose hook table is the file $STUB/hooks (`name[idx] command`).
#   list-sessions / list-windows / list-panes  answer from $STUB/server, $STUB/windows, $STUB/panes
#   show-option -gqv NAME   set-option -g NAME VALUE   set-option -gu NAME   run-shell CMD   show-hooks -g   set-hook
TMUX = """#!/bin/sh
echo "tmux $* [TMUX=${TMUX-unset}]" >> "$STUB/calls.log"
case "$1" in
  list-sessions) [ -e "$STUB/server" ]; exit $? ;;
  has-session) [ -e "$STUB/server" ]; exit $? ;;
  list-windows) [ -e "$STUB/server" ] || exit 1; cat "$STUB/windows" 2>/dev/null; exit 0 ;;
  list-panes) [ -e "$STUB/server" ] || exit 1; cat "$STUB/panes" 2>/dev/null; exit 0 ;;
  show-option) cat "$STUB/opt-$3" 2>/dev/null; exit 0 ;;
  set-option|set)
    case "$2" in
      -gu|-ug) rm -f "$STUB/opt-$3" ;;
      -g) printf '%s\\n' "$4" > "$STUB/opt-$3" ;;
    esac
    exit 0 ;;
  set-hook)
    touch "$STUB/hooks"
    if [ "$3" = -u ]; then key=$4; awk -v k="$key " 'index($0, k) != 1' "$STUB/hooks" > "$STUB/hooks.tmp"; mv "$STUB/hooks.tmp" "$STUB/hooks"
    else key=$3; awk -v k="$key " 'index($0, k) != 1' "$STUB/hooks" > "$STUB/hooks.tmp"; mv "$STUB/hooks.tmp" "$STUB/hooks"
      printf '%s %s\\n' "$key" "$(printf '%s' "$4" | tr -d "'")" >> "$STUB/hooks"; fi
    exit 0 ;;
  show-hooks)
    found=0
    while IFS= read -r l; do case "$l" in "$3["*) echo "$l"; found=1 ;; esac; done < "$STUB/hooks" 2>/dev/null
    [ "$found" = 1 ] || echo "$3"
    exit 0 ;;
  new-session) : > "$STUB/server"; exit 0 ;;
  run-shell) sh -c "$2" ;;
esac
exit 0
"""

# tmux-resurrect's save script, as far as the guard cares: write the snapshot in $STUB/body as tmux_resurrect_<stamp>.txt
# (the stamp is $STUB/stamp), repoint `last` when it differs from it, delete it when identical, rewrite the pane archive
# and what the post-save hook would write (rig's omp session map: the number of omp panes in the snapshot).
SAVER = """#!/bin/sh
echo "saver ran $* [ACTIVE=${TMUX_SAVE_NOW_ACTIVE-unset}]" >> "$STUB/calls.log"
echo "start $(date +%s.%N)" >> "$STUB/saver-times.log"
[ -e "$STUB/saver-sleep" ] && sleep "$(cat "$STUB/saver-sleep")"
if [ -e "$STUB/saver-fail" ]; then echo "end $(date +%s.%N)" >> "$STUB/saver-times.log"; exit 3; fi
d=$FAKE_RESURRECT_DIR
f="$d/tmux_resurrect_$(cat "$STUB/stamp").txt"
cp "$STUB/body" "$f"
if ! cmp -s "$f" "$d/last"; then ln -fs "$(basename "$f")" "$d/last"; else rm "$f"; fi
echo "archive of $(cat "$STUB/stamp")" > "$d/pane_contents.tar.gz"
if [ -n "$FAKE_MAP_DIR" ]; then
  mkdir -p "$FAKE_MAP_DIR"
  echo "omp-panes $(grep -c '	omp	' "$STUB/body")" > "$FAKE_MAP_DIR/omp-sessions.map"
fi
echo "end $(date +%s.%N)" >> "$STUB/saver-times.log"
exit 0
"""

ALERT = """#!/bin/sh
printf '%s\\n' "$1" >> "$STUB/alerts.log"
exit "${ALERT_EXIT:-0}"
"""


def snapshot(windows=1, panes_per_window=1, harness=0, session="main"):
    """A tmux-resurrect snapshot with this many windows/panes; the first `harness` panes run omp, the rest zsh."""
    lines = []
    left = harness
    for w in range(windows):
        for p in range(panes_per_window):
            cmd = "omp" if left > 0 else "zsh"
            left -= 1
            lines.append(f"pane\t{session}\t{w}\t{1 if w == 0 else 0}\t:*\t{p}\ttitle\t:/work/{w}\t1\t{cmd}\t:")
    for w in range(windows):
        lines.append(f"window\t{session}\t{w}\t:win{w}\t{1 if w == 0 else 0}\t:*\tlayout\toff")
    lines.append(f"state\t{session}\t")
    return "\n".join(lines) + "\n"


class StubBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stub = os.path.join(self.tmp, "stub")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        self.resurrect = os.path.join(self.tmp, "resurrect")
        self.maps = os.path.join(self.tmp, "maps")
        self.state = os.path.join(self.tmp, "state")
        for d in (self.home, self.stub, self.fakebin, self.resurrect):
            os.makedirs(d)
        write_exec(os.path.join(self.fakebin, "tmux"), TMUX)
        self.saver = os.path.join(self.tmp, "save.sh")
        write_exec(self.saver, SAVER)
        self.alert_cmd = os.path.join(self.tmp, "alert-sender")
        write_exec(self.alert_cmd, ALERT)
        self.touch("server")
        self.option("@resurrect-dir", self.resurrect)
        self.option("@resurrect-save-script-path", self.saver)
        self.set_stamp("20261108T100000")

    def env(self, **extra):
        return {
            **os.environ,
            "HOME": self.home,
            "STUB": self.stub,
            "PATH": f"{self.fakebin}:{MODULE}:/usr/bin:/bin",
            "TMUX_BOOT_STATE_DIR": self.state,
            "TMUX_SAVE_NOW_SAVER": self.saver,
            "TMUX_SAVE_NOW_MAP_DIR": self.maps,
            "FAKE_RESURRECT_DIR": self.resurrect,
            "FAKE_MAP_DIR": self.maps,
            "TMUX_ALERT_CMD": self.alert_cmd,
            **extra,
        }

    def run_script(self, script, *args, **extra):
        return subprocess.run(
            ["bash", os.path.join(MODULE, script), *args], env=self.env(**extra), capture_output=True, text=True, check=False
        )

    def popen(self, script, *args, **extra):
        return subprocess.Popen(
            ["bash", os.path.join(MODULE, script), *args], env=self.env(**extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

    def calls(self):
        path = os.path.join(self.stub, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def alerts(self):
        path = os.path.join(self.stub, "alerts.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def touch(self, name):
        with open(os.path.join(self.stub, name), "w"):
            pass

    def drop(self, name):
        os.remove(os.path.join(self.stub, name))

    def put(self, name, text):
        with open(os.path.join(self.stub, name), "w") as fh:
            fh.write(text)

    def option(self, name, value):
        self.put(f"opt-{name}", value + "\n")

    def get_option(self, name):
        path = os.path.join(self.stub, f"opt-{name}")
        if not os.path.exists(path):
            return ""
        with open(path) as fh:
            return fh.read().strip()

    def set_stamp(self, stamp):
        self.put("stamp", stamp)

    def next_body(self, text):
        self.put("body", text)

    def read(self, *parts):
        with open(os.path.join(*parts)) as fh:
            return fh.read()

    def last_name(self):
        link = os.path.join(self.resurrect, "last")
        return os.path.basename(os.readlink(link)) if os.path.islink(link) else None

    def snapshots(self):
        return sorted(n for n in os.listdir(self.resurrect) if n.startswith("tmux_resurrect_") and n.endswith(".txt"))

    def save_now(self, *args, **extra):
        return self.run_script("tmux-save-now", *args, **extra)

    def age(self, path, seconds):
        past = time.time() - seconds
        os.utime(path, (past, past), follow_symlinks=False)
