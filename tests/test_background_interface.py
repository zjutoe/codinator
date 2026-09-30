"""Separate service/frontend processes with fake agents; no model connectivity claim."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import py_compile
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_engine
from codinator.agents import worker as real_worker
from codinator.files import Problem, digest, snapshot, write_json
from codinator.process import process_start
from codinator.store import Store


class BackgroundInterfaceTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def test_completed_worker_archives_new_caches_before_freezing(self):
        cache = {}
        def worker(*args):
            result = real_worker(*args)
            path = Path(py_compile.compile(str(self.workspace / 'product.py'), doraise=True))
            cache.update(path=path, data=path.read_bytes())
            return result
        with patch('codinator.engine.worker', side_effect=worker):
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        archive, = (self.store.root / 'tasks/test').glob('cache-cleanup-*')
        self.assertEqual((archive / 'removed' / cache['path'].relative_to(self.workspace)).read_bytes(), cache['data'])
        self.assertFalse(cache['path'].exists())
        self.assertEqual(task['expected_digest'], digest(snapshot(self.workspace, self.manifest['excludes'])))

    def test_another_violation_prevents_cleanup_and_review(self):
        def worker(*args):
            result = real_worker(*args)
            py_compile.compile(str(self.workspace / 'product.py'), doraise=True)
            (self.workspace / 'outside.txt').write_text('retain evidence')
            return result
        with patch('codinator.engine.worker', side_effect=worker), \
             patch('codinator.engine.reviewer', side_effect=AssertionError('invalid submission')):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'blocked')
        self.assertTrue(list(self.workspace.glob('__pycache__/*.pyc')))
        self.assertFalse(list((self.store.root / 'tasks/test').glob('cache-cleanup-*')))
        evidence = self.engine.attempt_path(self.store.get('test')) / 'implementation.json'
        self.assertIn('outside.txt', json.loads(evidence.read_text())['files'])

    def test_archive_failure_blocks_before_review_and_retains_bytes(self):
        def worker(*args):
            result = real_worker(*args)
            py_compile.compile(str(self.workspace / 'product.py'), doraise=True)
            return result
        with patch('codinator.engine.worker', side_effect=worker), \
             patch('codinator.bytecode.preserve', side_effect=OSError('archive unavailable')), \
             patch('codinator.engine.reviewer', side_effect=AssertionError('must not review')):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'blocked')
        self.assertIn('archive unavailable', self.store.get('test')['reason'])
        self.assertTrue(list(self.workspace.glob('__pycache__/*.pyc')))

    def test_frozen_review_never_cleans_new_caches(self):
        def review(task, attempt, fingerprint, *args, **kwargs):
            py_compile.compile(str(self.workspace / 'product.py'), doraise=True)
            return {'verdict': 'accepted', 'summary': 'Stale fixture verdict', 'issues': []}
        with patch('codinator.engine.reviewer', side_effect=review):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'blocked')
        self.assertIn('changed during review', self.store.get('test')['reason'])
        self.assertTrue(list(self.workspace.glob('__pycache__/*.pyc')))

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

    def test_pause_during_preflight_prevents_dispatch(self):
        def preflight(*args):
            result = snapshot(*args)
            if self.store.get('test')['state'] == 'ready':
                self.store.request_control('test', 'pause')
            return result
        with patch('codinator.engine.snapshot', side_effect=preflight), \
             patch('codinator.engine.worker', side_effect=AssertionError('paused before dispatch')):
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['attempt']), ('paused', 0))

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
