"""The e2e cleanup kills only the omp processes of its fixtures, never an inspector that mentions a fixture path."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_procs  # noqa: E402

SESSION = "/tmp/tmux-e2e-1/p1/2026-11-08T12-00-00-000Z_0123abcd.jsonl"
DURABLE = "omp-recover-0123456789abcdef0123"


class Predicate(unittest.TestCase):
    def owned(self, args):
        return e2e_procs.is_owned(args, {SESSION}, {DURABLE})

    def test_omp_abduco_and_the_harness_host_of_a_fixture_are_owned(self):
        for args in (
            f"omp --resume {SESSION}",
            f"/home/ultra/.local/bin/omp --resume {SESSION}",
            f"abduco -A {DURABLE} rig harness run omp --resume {SESSION}",
            f"/home/linuxbrew/.linuxbrew/bin/abduco -n {DURABLE} omp",  # abduco by full path
            f"python3 /usr/local/bin/rig harness run omp --resume {SESSION}",
            f"/usr/bin/python3.13 -m riglib harness run omp --resume {SESSION}",
            f"bash -c abduco -n {DURABLE} omp",
            f"bash -c /home/linuxbrew/.linuxbrew/bin/abduco -A {DURABLE} rig harness run omp --resume {SESSION}",
            f"sh -c abduco -A {DURABLE} rig harness run omp --resume {SESSION}",
            f"bun /home/ultra/xp/oh-my-pi/packages/coding-agent/src/cli.ts --resume {SESSION}",
            f"bun /home/ultra/xp/oh-my-pi/packages/coding-agent/scripts/../src/cli.ts --resume {SESSION}",  # the dev launcher's spelling
            f"node /home/ultra/xp/oh-my-pi/packages/coding-agent/scripts/../src/cli.ts {SESSION}",
        ):
            with self.subTest(args=args):
                self.assertTrue(self.owned(args))

    def test_an_inspector_of_a_fixture_path_is_never_owned(self):
        for args in (
            f"vim {SESSION}",
            f"tail -f {SESSION}",
            f"tail -F /tmp/{DURABLE}.log",
            f"less {SESSION}",
            f"grep -r {DURABLE} /tmp",
            f"grep abduco {SESSION}",  # abduco as a mere argument
            f"cat {SESSION}",
            f"bash -c tail -f {SESSION}",
            f"bash -c tail -f /tmp/{DURABLE}.log",  # a shell inspecting a log named after the durable session
            f"bash -c abduco-doc --serve {DURABLE}",  # abduco only as a prefix of another word
            f"sh -c grep abduco /tmp/{DURABLE}.log",  # a shell whose command merely mentions abduco
            f"python3 -m json.tool {SESSION}",
            f"python3 viewer.py {SESSION}",
            f"/usr/bin/python3.13 /home/ultra/bin/tail_log.py /tmp/{DURABLE}.log",
            f"bun viewer.ts {SESSION}",
            f"node /home/ultra/tools/show.js {SESSION}",
            f"rig status {SESSION}",
            f"omp read {SESSION}",  # omp inspector subcommands, the rule tl_pane_agents applies
            f"omp ps",
            f"omp stats {SESSION}",
            f"omp models list --resume {SESSION}",
            f"/home/ultra/.local/bin/omp -p hello {SESSION}",  # a headless flag, not an interactive agent
            f"omp --version",
            f"python3 viewer.py {SESSION.replace('p1', 'p9')}",  # another fixture
        ):
            with self.subTest(args=args):
                self.assertFalse(self.owned(args))

    def test_the_caller_is_excluded_and_the_result_is_sorted(self):
        rows = [(30, 1, f"omp --resume {SESSION}"), (10, 1, f"vim {SESSION}"), (20, 1, f"omp --resume {SESSION}")]
        self.assertEqual(e2e_procs.owned_pids(rows, {SESSION}, {DURABLE}, exclude_pid=20), [30])


class LiveProcesses(unittest.TestCase):
    """The same rule against real processes in the real process table."""

    def spawn(self, argv):
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap, proc)
        return proc

    @staticmethod
    def reap(proc):
        proc.send_signal(signal.SIGKILL)
        proc.wait()

    def table(self):
        out = subprocess.run(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True, check=True).stdout.splitlines()[1:]
        return [(int(p[0]), int(p[1]), p[2]) for p in (line.split(None, 2) for line in out) if len(p) == 3]

    def test_tail_and_vim_on_a_fixture_survive_while_the_fake_omp_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = os.path.join(tmp, "p1", "2026-11-08T12-00-00-000Z_feedbeef.jsonl")
            os.makedirs(os.path.dirname(session))
            with open(session, "w") as fh:
                fh.write("{}\n")
            inspector = self.spawn(["tail", "-f", session])
            # a process whose command line is `omp -c <script> x --resume <session>`: bash under another argv[0]
            # (the multicall coreutils `sleep` would pick its tool from argv[0], so bash is the safe fake)
            fake_omp = self.spawn(["bash", "-c", f"exec -a omp bash -c 'sleep 60; :' x --resume {session}"])
            # poll: under load the exec may not have happened yet when the first ps snapshot is taken
            found = []
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                found = e2e_procs.owned_pids(self.table(), {session}, set(), os.getpid())
                if fake_omp.pid in found:
                    break
                time.sleep(0.1)
            self.assertIn(fake_omp.pid, found)
            self.assertNotIn(inspector.pid, found)


if __name__ == "__main__":
    unittest.main()
