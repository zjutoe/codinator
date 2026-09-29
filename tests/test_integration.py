"""Real Git, deterministic fake model subprocesses; not model connectivity evidence."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from codinator import integration
from codinator.config import load_manifest
from codinator.engine import Engine
from codinator.files import Problem, digest, snapshot
from codinator.store import Store
from test_engine import FakeSandbox


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target = self.base / 'main'
        self.target.mkdir()
        integration.git(self.target, 'init', '-b', 'master')
        integration.git(self.target, 'config', 'user.name', 'Fixture')
        integration.git(self.target, 'config', 'user.email', 'fixture@example.invalid')
        (self.target / 'README.md').write_text('base\n')
        integration.git(self.target, 'add', 'README.md')
        integration.git(self.target, 'commit', '-m', 'Baseline')
        self.head = integration.text(self.target, 'rev-parse', 'HEAD')
        self.workspace = self.base / 'source'
        integration.git(self.target, 'worktree', 'add', '-b', 'task', str(self.workspace))
        (self.workspace / 'handoff.md').write_text('Implement product.py VALUE=42.\n')
        (self.target / 'README.md').write_text('uncommitted planning\n')
        self.agent = self.base / 'agent'
        shutil.copyfile(Path(__file__).parent / 'fake_agent.py', self.agent)
        self.agent.chmod(0o755)
        self.store = Store(self.base / 'state')
        self.addCleanup(self.store.db.close)
        self.engine = Engine(self.store, sandbox=FakeSandbox(), pi_bin=str(self.agent), codex_bin=str(self.agent))
        self.manifest_path = self.base / 'task.json'
        self.manifest_path.write_text(json.dumps({
            'version': 1, 'id': 'test', 'workspace': str(self.workspace), 'handoff': 'handoff.md',
            'allowed_paths': ['product.py'], 'max_seconds': 120, 'attempt_seconds': 20,
            'checks': [{'name': 'unit', 'argv': [sys.executable, '-B', '-c', 'from product import VALUE; assert VALUE == 42']}],
            'integration': {'target_workspace': str(self.target), 'target_branch': 'master',
                            'base_commit': self.head, 'planning_paths': ['handoff.md', 'README.md']}}))
        self.manifest = load_manifest(self.manifest_path)
        self.env = patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.base / 'empty-pi'),
                                          'CODEX_HOME': str(self.base / 'empty-codex'),
                                          'FAKE_MODE': 'accept', 'FAKE_INTEGRATION': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.engine.submit(self.manifest)

    def task_dir(self):
        return self.store.root / 'tasks/test'

    def target_unchanged(self):
        self.assertEqual(integration.text(self.target, 'rev-parse', 'HEAD'), self.head)
        self.assertFalse((self.target / 'product.py').exists())
        self.assertEqual((self.target / 'README.md').read_text(), 'uncommitted planning\n')

    def accepted(self):
        task = self.store.get('test')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        commit = task['integration']['merged_commit']
        self.assertEqual(integration.text(self.target, 'rev-parse', 'HEAD'), commit)
        self.assertEqual(integration.text(self.target, 'rev-parse', 'HEAD^'), self.head)
        self.assertEqual((self.target / 'product.py').read_text(), 'VALUE = 42\n')
        self.assertEqual((self.target / 'README.md').read_text(), 'uncommitted planning\n')
        self.assertEqual(digest(snapshot(self.workspace, self.manifest['excludes'])), task['expected_digest'])
        return task

    def test_review_pi_commit_and_merge_preserve_source_and_dirty_planning(self):
        self.engine.run('test')
        task = self.accepted()
        self.assertEqual(task['attempt'], 1)
        self.assertEqual(task['integration']['attempt'], 1)
        self.assertTrue((self.task_dir() / 'integration-0001/export/candidate.bundle').is_file())

    def test_invalid_pi_commits_never_reach_master_and_resume_only_integration(self):
        for mode in ('wrong-tree', 'no-body', 'crash'):
            with self.subTest(mode=mode):
                os.environ['FAKE_INTEGRATION'] = mode
                self.engine.run('test')
                task = self.store.get('test')
                self.assertEqual((task['state'], task['phase']), ('blocked', 'integration'), task['reason'])
                self.assertIsNone(task['integration']['candidate'])
                self.target_unchanged()
                self.engine.resume('test')
                self.assertEqual(self.store.get('test')['state'], 'integration_ready')
        os.environ['FAKE_INTEGRATION'] = ''
        self.engine.run('test')
        task = self.accepted()
        self.assertEqual((task['attempt'], task['integration']['attempt']), (1, 4))
        self.assertTrue((self.task_dir() / 'integration-0003/pi/result.json').is_file())

    def test_promotion_failure_reuses_completed_pi_candidate(self):
        with patch('codinator.integration.promote', side_effect=Problem('fixture transport interrupted')):
            self.engine.run('test')
        self.target_unchanged()
        self.engine.resume('test')
        with patch('codinator.integration.integrator', side_effect=AssertionError('Pi replayed')):
            self.engine.run('test')
        self.accepted()

    def test_crash_after_merge_reconciles_without_repeating_commit_or_merge(self):
        original = self.store.finish
        def crash(task_id, message, **fields):
            if fields.get('state') == 'accepted':
                raise SystemExit('controller killed after Git merge')
            return original(task_id, message, **fields)
        with patch.object(self.store, 'finish', side_effect=crash), self.assertRaises(SystemExit):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'integrating')
        self.engine.resume('test')
        real_git = integration.git
        def no_merge(root, *args, **kw):
            self.assertNotIn('merge', args)
            return real_git(root, *args, **kw)
        with patch('codinator.integration.integrator', side_effect=AssertionError('Pi replayed')), patch('codinator.integration.git', side_effect=no_merge):
            self.engine.run('test')
        self.accepted()

    def test_pause_and_cancel_before_publication_never_merge(self):
        original = integration.promote
        def pause(engine, task, entries):
            self.store.request_control('test', 'pause')
            return original(engine, task, entries)
        with patch('codinator.integration.promote', side_effect=pause):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'paused')
        self.target_unchanged()
        self.engine.resume('test')
        def cancel(engine, task, entries):
            self.store.request_control('test', 'cancel')
            return original(engine, task, entries)
        with patch('codinator.integration.promote', side_effect=cancel):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'cancelled')
        self.target_unchanged()

    def test_expired_budget_never_promotes_completed_commit(self):
        original = integration.promote
        def expired(engine, task, entries):
            self.store.update('test', deadline=1)
            return original(engine, self.store.get('test'), entries)
        with patch('codinator.integration.promote', side_effect=expired):
            self.engine.run('test')
        self.assertIn('budget exhausted', self.store.get('test')['reason'])
        self.target_unchanged()

    def test_target_drift_overlap_or_staged_changes_block_before_pi(self):
        original = integration.run
        def changed(engine, task_id, timeout):
            (self.target / 'product.py').write_text('user work\n')
            original(engine, task_id, timeout)
        with patch('codinator.integration.run', side_effect=changed):
            self.engine.run('test')
        self.assertIn('overlaps', self.store.get('test')['reason'])
        self.assertFalse((self.task_dir() / 'integration-0001').exists())
        self.assertEqual((self.target / 'product.py').read_text(), 'user work\n')

    def test_pinned_accepted_evidence_tamper_blocks_resume(self):
        with patch('codinator.integration.promote', side_effect=Problem('fixture pause')):
            self.engine.run('test')
        path = self.task_dir() / 'attempt-0001/codex/stdout.txt'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(Problem, 'evidence'):
            self.engine.resume('test')
        self.target_unchanged()

    def test_candidate_evidence_tamper_never_promotes(self):
        with patch('codinator.integration.promote', side_effect=Problem('fixture pause')):
            self.engine.run('test')
        self.engine.resume('test')
        (self.task_dir() / 'integration-0001/delivery/summary.md').write_text('changed')
        self.engine.run('test')
        self.assertIn('evidence changed', self.store.get('test')['reason'])
        self.target_unchanged()

    def test_needs_changes_stays_in_implementation_until_review_accepts(self):
        os.environ['FAKE_MODE'] = 'rework'
        self.engine.run('test')
        task = self.accepted()
        self.assertEqual((task['round'], task['attempt'], task['integration']['attempt']), (2, 2, 1))

    def test_uncommitted_implementation_dependency_cannot_be_published(self):
        (self.workspace / 'hidden_dependency.py').write_text('dependency = 1\n')
        with self.assertRaisesRegex(Problem, 'Uncommitted baseline'):
            integration.publication_preflight(self.manifest)

    def test_acceptance_published_before_crash_can_resume_integration(self):
        with patch('codinator.integration.accepted_checkpoint', side_effect=SystemExit('crash after accepted outcome')):
            with self.assertRaises(SystemExit):
                self.engine.run('test')
        self.assertIsNone(self.store.get('test')['integration'])
        self.engine.resume('test')
        self.engine.run('test')
        self.accepted()

    def test_published_intake_cannot_be_changed_to_omit_accepted_files(self):
        with patch('codinator.integration.promote', side_effect=Problem('fixture pause')):
            self.engine.run('test')
        path = self.task_dir() / 'intake.json'
        intake = json.loads(path.read_text())
        submission = json.loads((self.task_dir() / 'attempt-0001/submission.json').read_text())
        intake['files']['product.py'] = submission['files']['product.py']
        path.write_text(json.dumps(intake))
        with self.assertRaisesRegex(Problem, 'intake evidence changed'):
            self.engine.resume('test')
        self.target_unchanged()

    def test_moving_master_or_staging_unrelated_changes_blocks_promotion(self):
        with patch('codinator.integration.promote', side_effect=Problem('fixture pause')):
            self.engine.run('test')
        integration.git(self.target, 'add', 'README.md')
        self.engine.resume('test')
        self.engine.run('test')
        self.assertIn('unstaged Git index', self.store.get('test')['reason'])
        self.target_unchanged()
        integration.git(self.target, 'commit', '-m', 'Concurrent master planning commit')
        concurrent = integration.text(self.target, 'rev-parse', 'HEAD')
        self.engine.resume('test')
        self.engine.run('test')
        self.assertIn('HEAD changed', self.store.get('test')['reason'])
        self.assertEqual(integration.text(self.target, 'rev-parse', 'HEAD'), concurrent)
        self.assertFalse((self.target / 'product.py').exists())
