import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from codinator.files import Problem
from codinator.process import Interrupted, run_process


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_code(self, code, **kw):
        return run_process([sys.executable, '-c', code], cwd=self.root, env=os.environ.copy(),
                           out=self.root / 'run', timeout=kw.pop('timeout', 5), **kw)

    def test_large_stderr_drained_and_nonzero_recorded(self):
        with self.assertRaises(Problem):
            self.run_code("import sys; sys.stderr.write('x'*200000); sys.exit(3)")
        r = json.loads((self.root / 'run/result.json').read_text())
        self.assertEqual(r['exit_code'], 3)
        self.assertEqual((self.root / 'run/stderr.txt').stat().st_size, 200000)

    def test_timeout_and_cancel_leave_results(self):
        with self.assertRaises(Interrupted):
            self.run_code('import time; time.sleep(20)', cancel=lambda: True)
        self.assertIn('Interrupted', json.loads((self.root / 'run/result.json').read_text())['failure'])

    def test_initial_rpc_write_is_already_subject_to_hard_deadline(self):
        class Protocol:
            def start(self, send):
                send({'type': 'prompt', 'message': 'x' * 200000})
            def event(self, event, send):
                return False
            def finish(self):
                raise AssertionError('Incomplete protocol must not finish')
        def watchdog(*_):
            raise TimeoutError('Initial RPC write blocked before the deadline was established')
        previous = signal.signal(signal.SIGALRM, watchdog)
        signal.setitimer(signal.ITIMER_REAL, 6)
        try:
            with self.assertRaisesRegex(Problem, 'wall-clock budget'):
                self.run_code('import time; time.sleep(60)', protocol=Protocol(), timeout=.3)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        result = json.loads((self.root / 'run/result.json').read_text())
        self.assertIn('wall-clock budget', result['failure'])

    def test_orphan_child_killed_before_leader_reaped(self):
        pid_file = self.root / 'child.pid'
        code = ("import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                f"open({str(pid_file)!r},'w').write(str(p.pid))")
        self.run_code(code)
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        live = True
        while time.monotonic() < deadline:
            try:
                state = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()[0]
                live = state != 'Z'
            except FileNotFoundError:
                live = False
            if not live:
                break
            time.sleep(.05)
        self.assertFalse(live, 'detached child remained alive after runner completion')
