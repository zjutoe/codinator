import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codidator.cli import notifications
from codidator.config import load_manifest
from codidator.engine import Engine
from codidator.files import Problem, digest, snapshot
from codidator.store import Store


class FakeSandbox:
    """Only for fake agents in tests; never selectable through the public CLI."""
    def wrap(self, argv, *args, **kw):
        return argv


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.workspace)], check=True)
        (self.workspace / 'handoff.md').write_text('Implement product.py with VALUE=42; tests mandatory.\n')
        executable = self.base / 'agent'
        shutil.copyfile(Path(__file__).parent / 'fake_agent.py', executable)
        executable.chmod(0o755)
        self.agent = str(executable)
        self.store = Store(self.base / 'state')
        self.addCleanup(self.store.db.close)
        self.engine = Engine(self.store, sandbox=FakeSandbox(), pi_bin=self.agent, codex_bin=self.agent)
        self.manifest = {'version': 1, 'id': 'test', 'workspace': str(self.workspace), 'handoff': 'handoff.md',
                         'allowed_paths': ['product.py'], 'checks': [{'name': 'unit', 'argv': [sys.executable, '-c', 'from product import VALUE; assert VALUE == 42']}],
                         'max_seconds': 120, 'attempt_seconds': 20}
        path = self.base / 'task.json'
        path.write_text(json.dumps(self.manifest))
        self.manifest = load_manifest(path)
        # Keep production Pi credentials completely out of these tests.
        self.env = patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.base / 'empty-pi'),
                                          'CODEX_HOME': str(self.base / 'empty-codex'), 'FAKE_MODE': 'accept'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.engine.submit(self.manifest)

    def test_complete_acceptance_evidence_and_no_git_commit(self):
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        out = self.engine.attempt_path(task)
        self.assertTrue((out / 'checks/unit/result.json').exists())
        self.assertTrue((out / 'pi-runtime.json').exists())
        self.assertTrue((out / 'submission.json').exists())
        self.assertIsNone(snapshot(self.workspace, [])['git']['head'])
        self.assertIn('--sandbox', json.loads((out / 'codex/launch.json').read_text())['argv'])

    def test_rework_is_automatic_and_preserves_prior_attempt(self):
        os.environ['FAKE_MODE'] = 'rework'
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual((task['round'], task['attempt']), (2, 2))
        first = self.store.root / 'tasks/test/attempt-0001/outcome.json'
        self.assertEqual(json.loads(first.read_text())['verdict'], 'needs_changes')

    def test_two_rounds_without_progress_stop(self):
        os.environ['FAKE_MODE'] = 'no-progress'
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['attempt'], 2)
        self.assertIn('same issue', task['reason'])

    def test_scope_failure_never_reviews(self):
        os.environ['FAKE_MODE'] = 'scope'
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('outside allowed', task['reason'])
        self.assertFalse((self.engine.attempt_path(task) / 'codex').exists())

    def test_truncated_or_missing_delivery_never_reviews(self):
        for mode in ('truncated', 'missing-delivery', 'bad-completion-shape'):
            with self.subTest(mode=mode):
                os.environ['FAKE_MODE'] = mode
                self.engine.run('test')
                task = self.store.get('test')
                self.assertEqual(task['state'], 'blocked')
                self.assertFalse((self.engine.attempt_path(task) / 'codex').exists())
                self.engine.resume('test')

    def test_bad_review_exit_or_digest_or_concurrent_mutation_never_accepts(self):
        for mode in ('stale-verdict', 'codex-exit-failure', 'codex-turn-failure', 'bad-review-shape', 'mutate-review'):
            with self.subTest(mode=mode):
                os.environ['FAKE_MODE'] = mode
                self.engine.run('test')
                task = self.store.get('test')
                self.assertEqual(task['state'], 'blocked')
                if mode != 'mutate-review':
                    self.engine.resume('test')
        with self.assertRaises(Problem):
            self.engine.resume('test')

    def test_failed_required_check_does_not_accept(self):
        with patch('codidator.engine.run_process', side_effect=Problem('check failed')):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'blocked')

    def test_pi_bypasses_proxy_and_codex_retains_it(self):
        with patch.dict(os.environ, {'FAKE_MODE': 'proxy-routing', 'https_proxy': 'http://codex-proxy.invalid:8888',
                                     'HTTP_PROXY': 'http://codex-proxy.invalid:8888', 'ALL_PROXY': 'socks5://invalid:1'}):
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'accepted', task['reason'])

    def test_codex_reconnection_diagnostic_then_terminal_success(self):
        os.environ['FAKE_MODE'] = 'codex-reconnect'
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'accepted', task['reason'])

    def test_pause_before_dispatch(self):
        self.store.update('test', control='pause')
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['attempt']), ('paused', 0))

    def test_recovery_marks_uncertain_without_replaying(self):
        self.store.update('test', state='implementing', attempt=1)
        self.engine.recover()
        task = self.store.get('test')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('uncertain', task['reason'])
        self.assertFalse((self.workspace / 'product.py').exists())
        self.engine.resume('test')
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual(task['attempt'], 2)

    def test_notification_failure_does_not_rollback_acceptance(self):
        self.engine.run('test')
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET thread='explicit-user-authorized-target'")
        os.environ['FAKE_MODE'] = 'notify-fail'
        notifications(self.store, self.agent)
        self.assertEqual(self.store.get('test')['state'], 'accepted')
        row = self.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual((row['delivered'], row['attempts']), (0, 1))
        os.environ['FAKE_MODE'] = 'accept'
        notifications(self.store, self.agent)
        row = self.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual((row['delivered'], row['attempts']), (1, 2))

    def test_terminal_state_and_outbox_are_atomic(self):
        with patch.object(self.store, '_notify', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                self.store.finish('test', 'done', state='accepted')
        self.assertEqual(self.store.get('test')['state'], 'ready')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_recovery_does_not_adopt_review_mutations(self):
        self.store.update('test', state='reviewing', attempt=1)
        attempt = self.engine.attempt_path(self.store.get('test'))
        attempt.mkdir()
        (attempt / 'before.json').write_text(json.dumps(snapshot(self.workspace, [])))
        (self.workspace / 'product.py').write_text('unauthorized concurrent change')
        self.engine.recover()
        with self.assertRaises(Problem):
            self.engine.resume('test')

    def test_unconfirmed_old_process_blocks_resume_and_dispatch(self):
        self.store.update('test', state='implementing', pid=123456, pid_start='old')
        with patch('codidator.engine.stop_group'), patch('codidator.engine.process_start', return_value='old'):
            self.engine.recover()
            self.assertEqual(self.store.get('test')['state'], 'blocked')
            with self.assertRaises(Problem):
                self.engine.resume('test')
            with self.assertRaises(Problem):
                self.engine.run('test')
