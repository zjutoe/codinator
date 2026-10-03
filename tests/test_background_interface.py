"""Separate service/frontend processes with fake agents; no model connectivity claim."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_engine
from codinator.files import Problem, write_json
from codinator.process import process_start
from codinator.store import Store


class BackgroundInterfaceTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def test_pause_or_cancel_wins_over_late_verdict(self):
        for control in ('pause', 'cancel'):
            with self.subTest(control=control):
                def review(task, *args, **kwargs):
                    self.store.update('test', control=control)
                    return {'verdict': 'accepted', 'summary': 'Late fixture verdict', 'issues': []}
                if control == 'cancel':
                    self.engine.resume('test')
                with patch('codinator.engine.reviewer', side_effect=review):
                    self.engine.run('test')
                self.assertEqual(self.store.get('test')['state'], 'paused' if control == 'pause' else 'cancelled')
                self.assertFalse((self.engine.attempt_path(self.store.get('test')) / 'outcome.json').exists())

    def test_publication_and_concurrent_pause_are_serialized(self):
        started, finished = threading.Event(), threading.Event()
        results = []
        def pause():
            other = Store(self.store.root, read_only=True)
            # Use an existing schema with a normal writable connection; startup
            # migrations must not hide the request race behind their own lock.
            other.db.close()
            other.db = sqlite3.connect(other.root / 'state.sqlite', timeout=5)
            other.db.row_factory = sqlite3.Row
            started.set()
            try:
                other.request_control('test', 'pause')
                results.append('paused')
            except Problem as exc:
                results.append(str(exc))
            finally:
                other.db.close()
                finished.set()
        thread = threading.Thread(target=pause)
        def publish(path, value):
            if path.name == 'outcome.json':
                thread.start()
                self.assertTrue(started.wait(5))
                self.assertFalse(finished.wait(.1), 'pause raced past publication transaction')
            return write_json(path, value)
        try:
            with patch('codinator.engine.write_json', side_effect=publish):
                self.engine.run('test')
        finally:
            if thread.ident is not None:
                thread.join(10)
        self.assertTrue(finished.is_set())
        self.assertEqual(results, ['Task already terminal'])
        task = self.store.get('test')
        self.assertEqual(task['state'], 'accepted')
        self.assertIsNone(task['control'])

    @contextmanager
    def service(self, gate):
        env = os.environ | {'FAKE_MODE': 'gated-rework', 'FAKE_GATE': str(gate),
                            'PYTHONPATH': os.pathsep.join((str(Path(__file__).resolve().parents[1] / 'src'),
                                                        str(Path(__file__).resolve().parent))),
                            'PYTHONDONTWRITEBYTECODE': '1'}
        # Production has no fake-sandbox flag. Only this fixture replaces it.
        code = ('from unittest.mock import patch; from test_engine import FakeSandbox; '
                'from codinator.cli import main; import sys\n'
                'with patch("codinator.engine.Sandbox", FakeSandbox):\n'
                '    sys.exit(main(sys.argv[1:]))\n')
        with (self.base / 'service.log').open('ab') as log:
            service = subprocess.Popen([sys.executable, '-B', '-c', code, '--state-dir', str(self.store.root),
                                        '--pi-bin', self.agent, '--codex-bin', self.agent, 'serve'],
                                       env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
            try:
                yield service, env
            finally:
                if service.poll() is None:
                    service.terminate()
                service.wait(timeout=10)

    def test_service_reworks_after_frontend_exits_and_queries_do_not_pause(self):
        from legacy_fixture import submit_legacy
        legacy = {k: v for k, v in self.manifest.items() if k != 'git'}
        legacy.update(version=1, id='legacy', excludes=[])
        submit_legacy(self.engine, legacy)
        gate = self.base / 'gate'
        with self.service(gate) as (service, env):
            deadline = time.monotonic() + 25
            while self.store.get('test')['state'] != 'implementing':
                self.assertIsNone(service.poll(), (self.base / 'service.log').read_text())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            # Short-lived CLI calls represent a frontend which closes after each read.
            events_before = self.store.db.execute('SELECT COALESCE(MAX(seq), 0) FROM events').fetchone()[0]
            for _ in range(2):
                frontend = subprocess.run([sys.executable, '-B', '-m', 'codinator', '--state-dir',
                                           str(self.store.root), 'status', 'test'], env=env,
                                          capture_output=True, text=True, timeout=5)
                self.assertEqual(frontend.returncode, 0, frontend.stderr)
                self.assertEqual(json.loads(frontend.stdout)[0]['state'], 'implementing')
            self.assertIsNone(self.store.get('test')['control'])
            # Ignore the independent process-start event while the service enters Pi.
            new_events = list(self.store.db.execute('SELECT payload FROM events WHERE seq > ?', (events_before,)))
            self.assertTrue(all(json.loads(row[0]).keys() <= {'pid', 'pid_start', 'updated'} for row in new_events))
            gate.touch()
            while self.store.get('test')['state'] != 'accepted':
                self.assertIsNone(service.poll(), (self.base / 'service.log').read_text())
                self.assertNotIn(self.store.get('test')['state'], ('paused', 'blocked'))
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            task = self.store.get('test')
            self.assertEqual((task['round'], task['attempt']), (2, 2))
            self.assertTrue((self.store.root / 'tasks/test/attempt-0001/outcome.json').exists())
            self.assertTrue((self.store.root / 'tasks/test/attempt-0002/outcome.json').exists())
            self.assertIsNone(task['manifest'].get('notify_thread'))
            self.assertEqual(self.store.get('legacy')['state'], 'ready')
            self.assertEqual(self.store.get('legacy')['attempt'], 0)

    def test_service_restart_requires_explicit_resume_and_preserves_attempt(self):
        gate = self.base / 'gate'
        attempt = self.store.root / 'tasks/test/attempt-0001'
        events = attempt / 'pi/stdout.jsonl'
        with self.service(gate) as (service, env):
            deadline = time.monotonic() + 15
            # Interrupt after prompt acknowledgement, when replay would be uncertain.
            while not events.exists() or '"id": "prompt"' not in events.read_text():
                self.assertIsNone(service.poll(), (self.base / 'service.log').read_text())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            task = self.store.get('test')
            pid, start = task['pid'], task['pid_start']
            service.terminate()
            self.assertEqual(service.wait(timeout=10), 130)
        self.assertNotEqual(process_start(pid), start)
        task = self.store.get('test')
        self.assertEqual((task['state'], task['attempt'], task['pid']), ('paused', 1, None))
        original = {p.relative_to(attempt): p.read_bytes() for p in attempt.rglob('*') if p.is_file()}
        self.assertIn('KeyboardInterrupt', json.loads(original[Path('pi/result.json')])['failure'])
        gate.touch()
        with self.service(gate) as (service, env):
            # Let the restarted service complete a polling interval without a resume.
            time.sleep(3.5)
            self.assertIsNone(service.poll(), (self.base / 'service.log').read_text())
            task = self.store.get('test')
            self.assertEqual((task['state'], task['attempt']), ('paused', 1))
            self.assertFalse((attempt.parent / 'attempt-0002').exists())
            frontend = subprocess.run([sys.executable, '-B', '-m', 'codinator', '--state-dir',
                                       str(self.store.root), 'resume', 'test'], env=env,
                                      capture_output=True, text=True, timeout=5)
            self.assertEqual(frontend.returncode, 0, frontend.stderr)
            deadline = time.monotonic() + 20
            while self.store.get('test')['state'] != 'accepted':
                self.assertIsNone(service.poll(), (self.base / 'service.log').read_text())
                self.assertNotIn(self.store.get('test')['state'], ('paused', 'blocked'))
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            self.assertEqual(self.store.get('test')['attempt'], 2)
        self.assertEqual({p.relative_to(attempt): p.read_bytes() for p in attempt.rglob('*') if p.is_file()}, original)
