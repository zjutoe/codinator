import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codinator.cli import notifications
from codinator.config import load_manifest
from codinator.engine import Engine, _budget_timeout
from codinator.agents import reviewer as real_reviewer, worker as real_worker
from codinator.files import Problem, digest, snapshot, file_info
from codinator.process import run_process as real_run_process
from codinator.store import Store


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
        with patch('codinator.engine.run_process', side_effect=Problem('check failed')):
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
        with patch('codinator.engine.stop_group'), patch('codinator.engine.process_start', return_value='old'):
            self.engine.recover()
            self.assertEqual(self.store.get('test')['state'], 'blocked')
            with self.assertRaises(Problem):
                self.engine.resume('test')
            with self.assertRaises(Problem):
                self.engine.resume('test', review_only=True)
            with self.assertRaises(Problem):
                self.engine.run('test')

    def review_failure(self):
        os.environ['FAKE_MODE'] = 'codex-unavailable'
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['phase']), ('blocked', 'stopped'))
        source = self.engine.attempt_path(task)
        self.assertTrue((source / 'checks/unit/result.json').exists())
        return source

    def test_review_only_reuses_checked_submission_without_worker_or_checks(self):
        source = self.review_failure()
        old = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        self.engine.resume('test', review_only=True)
        self.assertEqual(self.store.get('test')['state'], 'review_ready')
        os.environ['FAKE_MODE'] = 'accept'
        with patch('codinator.engine.worker', side_effect=AssertionError('Pi must not run')), \
             patch('codinator.engine.run_process', side_effect=AssertionError('checks must not run')):
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 1, 2), task['reason'])
        out = self.engine.attempt_path(task)
        self.assertFalse((out / 'pi').exists())
        self.assertFalse((out / 'checks').exists())
        self.assertEqual(json.loads((out / 'review-source.json').read_text())['source_attempt'], 1)
        self.assertIn(str(source), (out / 'review-prompt.txt').read_text())
        self.assertEqual(old, {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()})

    def test_review_only_survives_restart_and_repeated_transport_failure(self):
        self.review_failure()
        self.engine.resume('test', review_only=True)
        # Queue survives a new Store/Engine, as in a service restart.
        reopened = Store(self.store.root)
        self.addCleanup(reopened.db.close)
        restarted = Engine(reopened, sandbox=FakeSandbox(), pi_bin=self.agent, codex_bin=self.agent)
        with patch('codinator.engine.worker', side_effect=AssertionError('Pi must not run')):
            restarted.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('blocked', 1, 2))
        self.engine.resume('test', review_only=True)
        os.environ['FAKE_MODE'] = 'accept'
        with patch('codinator.engine.worker', side_effect=AssertionError('Pi must not run')):
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 1, 3), task['reason'])
        self.assertEqual(json.loads((self.engine.attempt_path(task) / 'review-source.json').read_text())['source_attempt'], 1)

    def test_review_only_refuses_existing_verdict_even_without_outcome(self):
        source = self.review_failure()
        (source / 'review-delivery/verdict.json').write_text('{"verdict":"needs_changes"}')
        with self.assertRaisesRegex(Problem, 'verdict|outcome'):
            self.engine.resume('test', review_only=True)
        self.assertEqual(self.store.get('test')['state'], 'blocked')

    def test_review_only_refuses_incomplete_or_changed_check_evidence(self):
        source = self.review_failure()
        for name, replacement in [('checks/unit/result.json', b'{"exit_code":1}'),
                                  ('checks/unit/stdout.txt', b'changed'),
                                  ('checks/unit/launch.json', b'{"argv":["true"],"cwd":"wrong"}'),
                                  ('delivery/completion.json', b'{"status":"blocked"}'),
                                  ('delivery/completion.json', b'{"task_id":"test","round":true,"attempt":1,"status":"awaiting_review"}'),
                                  ('pi/result.json', b'{"exit_code":0,"failure":"truncated"}')]:
            with self.subTest(name=name):
                p = source / name
                original = p.read_bytes()
                p.write_bytes(replacement)
                try:
                    with self.assertRaises(Problem):
                        self.engine.resume('test', review_only=True)
                finally:
                    p.write_bytes(original)
        self.assertEqual(self.store.get('test')['state'], 'blocked')

    def test_review_only_requires_complete_pi_protocol_not_just_zero_exit(self):
        source = self.review_failure()
        events = source / 'pi/stdout.jsonl'
        events.write_text(events.read_text().replace('"stopReason": "stop"', '"stopReason": "length"'))
        result = source / 'pi/result.json'
        data = json.loads(result.read_text())
        data['streams']['stdout.jsonl'] = file_info(events)
        result.write_text(json.dumps(data))
        with self.assertRaisesRegex(Problem, 'completion protocol'):
            self.engine.resume('test', review_only=True)

    def test_review_only_malformed_snapshot_blocks_without_service_crash(self):
        source = self.review_failure()
        p = source / 'before.json'
        original = p.read_bytes()
        snap = json.loads(original)
        bad = ({}, snap | {'git': []}, snap | {'files': []},
               snap | {'files': {'handoff.md': None}}, snap | {'root_mode': True})
        for malformed in bad:
            with self.subTest(snapshot=malformed):
                # Reject it at both explicit resume and later queue dispatch.
                p.write_text(json.dumps(malformed))
                with self.assertRaisesRegex(Problem, 'snapshot evidence'):
                    self.engine.resume('test', review_only=True)
                p.write_bytes(original)
                self.engine.resume('test', review_only=True)
                p.write_text(json.dumps(malformed))
                with patch('codinator.engine.reviewer', side_effect=AssertionError('review must not run')), \
                     patch('codinator.engine.worker', side_effect=AssertionError('Pi must not run')):
                    self.engine.run('test')
                task = self.store.get('test')
                self.assertEqual((task['state'], task['attempt']), ('blocked', 1))
                self.assertIn('snapshot evidence', task['reason'])
                p.write_bytes(original)

    def test_review_only_rechecks_evidence_at_dispatch(self):
        source = self.review_failure()
        self.engine.resume('test', review_only=True)
        (source / 'delivery/summary.md').write_text('changed after enqueue')
        with patch('codinator.engine.reviewer', side_effect=AssertionError('review must not run')), \
             patch('codinator.engine.worker', side_effect=AssertionError('Pi must not run')):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'blocked')
        self.assertIn('evidence', self.store.get('test')['reason'].lower())

    def test_review_only_rechecks_evidence_after_review(self):
        source = self.review_failure()
        self.engine.resume('test', review_only=True)
        os.environ['FAKE_MODE'] = 'accept'

        def mutate(*args, **kwargs):
            result = real_reviewer(*args, **kwargs)
            (source / 'delivery/summary.md').write_text('concurrent edit during review')
            return result

        with patch('codinator.engine.reviewer', side_effect=mutate):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'blocked')
        self.assertIn('evidence changed', self.store.get('test')['reason'])

    def test_review_only_needs_changes_returns_to_normal_implementation(self):
        self.review_failure()
        self.engine.resume('test', review_only=True)

        def review(*args, **kwargs):
            os.environ['FAKE_MODE'] = 'no-progress' if args[1].name == 'attempt-0002' else 'accept'
            return real_reviewer(*args, **kwargs)

        with patch('codinator.engine.worker', wraps=real_worker) as worker_calls, \
             patch('codinator.engine.reviewer', side_effect=review):
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 2, 3), task['reason'])
        self.assertIsNone(task['review_resume'])
        self.assertEqual(worker_calls.call_count, 1)
        self.assertIn('Need a boundary case', worker_calls.call_args.args[0]['feedback'])
        self.assertTrue((self.engine.attempt_path(task) / 'checks/unit/result.json').exists())

    def test_review_only_recover_crash_after_attempt_allocation(self):
        self.review_failure()
        self.engine.resume('test', review_only=True)
        # Simulate death between the atomic state update and attempt files.
        self.store.update('test', attempt=2, state='reviewing', phase='codex')
        self.engine.recover()
        task = self.store.get('test')
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['review_resume']['source_attempt'], 1)
        self.assertFalse(self.engine.attempt_path(task).exists())
        self.engine.resume('test', review_only=True)
        os.environ['FAKE_MODE'] = 'accept'
        with patch('codinator.engine.worker', side_effect=AssertionError('Pi must not run')):
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'accepted')

    def test_review_only_recovery_rejects_workspace_drift(self):
        self.review_failure()
        self.engine.resume('test', review_only=True)
        self.store.update('test', attempt=2, state='reviewing', phase='codex')
        (self.workspace / 'product.py').write_text('VALUE = -1\n')
        self.engine.recover()
        with self.assertRaisesRegex(Problem, 'checkpoint'):
            self.engine.resume('test', review_only=True)
        self.assertEqual(self.store.get('test')['state'], 'blocked')

    def test_review_only_expired_budget_does_not_launch_and_extension_allows_review(self):
        self.review_failure()
        self.store.update('test', deadline=time.time() - 1)
        self.engine.resume('test', review_only=True)
        with patch('codinator.engine.reviewer', side_effect=AssertionError('budget exhausted')):
            self.engine.run('test')
        self.assertIn('budget exhausted', self.store.get('test')['reason'])
        self.engine.resume('test', review_only=True, extra_seconds=60)
        os.environ['FAKE_MODE'] = 'accept'
        self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'accepted')

    def test_review_only_refuses_attempt_that_never_reached_review(self):
        os.environ['FAKE_MODE'] = 'truncated'
        self.engine.run('test')
        with self.assertRaisesRegex(Problem, 'reached review'):
            self.engine.resume('test', review_only=True)

    def test_review_only_refuses_missing_checks_and_outcomes(self):
        source = self.review_failure()
        p = source / 'checks/unit/result.json'
        original = p.read_bytes()
        p.unlink()
        with self.assertRaisesRegex(Problem, 'Missing'):
            self.engine.resume('test', review_only=True)
        p.write_bytes(original)
        (source / 'outcome.json').write_text('{"verdict":"blocked"}')
        with self.assertRaisesRegex(Problem, 'verdict/outcome'):
            self.engine.resume('test', review_only=True)

    def test_review_only_mounts_original_and_current_evidence(self):
        source = self.review_failure()
        self.engine.resume('test', review_only=True)
        os.environ['FAKE_MODE'] = 'accept'
        with patch.object(self.engine.sandbox, 'wrap', wraps=self.engine.sandbox.wrap) as calls:
            self.engine.run('test')
        reviewer_call = [c for c in calls.call_args_list if 'readonly' in c.kwargs][0]
        self.assertEqual(reviewer_call.kwargs['readonly'], [self.engine.attempt_path(self.store.get('test')), source])

    def test_old_database_migration_preserves_legacy_task_and_checks(self):
        source = self.review_failure()
        self.store.db.close()
        # Recreate the precise pre-feature schema while keeping real old records.
        db = sqlite3.connect(self.store.root / 'state.sqlite')
        db.execute('ALTER TABLE tasks DROP COLUMN review_resume')
        db.execute('ALTER TABLE tasks DROP COLUMN attempt_seconds_override')
        db.commit()
        db.close()
        self.store = Store(self.store.root)
        self.addCleanup(self.store.db.close)
        self.engine.store = self.store
        self.assertIsNone(self.store.get('test')['review_resume'])
        self.assertIsNone(self.store.get('test')['attempt_seconds_override'])
        self.assertEqual(self.store.get('test')['manifest']['attempt_seconds'], 20)
        self.engine.resume('test', review_only=True)
        os.environ['FAKE_MODE'] = 'accept'
        self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'accepted')
        self.assertTrue((source / 'checks/unit/result.json').exists())

    def test_new_default_budget_preserves_explicit_old_budget(self):
        path = self.base / 'default-task.json'
        raw = dict(self.manifest)
        del raw['attempt_seconds']
        path.write_text(json.dumps(raw))
        self.assertEqual(load_manifest(path)['attempt_seconds'], 7200)
        raw['attempt_seconds'] = 3600
        path.write_text(json.dumps(raw))
        self.assertEqual(load_manifest(path)['attempt_seconds'], 3600)

    def test_invalid_resume_budget_does_not_recover_or_mutate(self):
        before = self.store.get('test')
        count = self.store.db.execute('SELECT count(*) FROM events').fetchone()[0]
        with patch.object(self.engine, 'recover', side_effect=AssertionError('must validate first')):
            for value in (0, -1, True, 1.5, '7200'):
                with self.subTest(value=value), self.assertRaises(Problem):
                    self.engine.resume('test', attempt_seconds=value)
            for value in (-1, True, '60'):
                with self.subTest(extra=value), self.assertRaises(Problem):
                    self.engine.resume('test', extra_seconds=value)
        self.assertEqual(self.store.get('test'), before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM events').fetchone()[0], count)

    def test_real_timeout_then_resume_preserves_manifest_and_old_evidence(self):
        # Start with a one-second authorized override so the fake sleeping
        # process exercises actual timeout/termination without a long test.
        self.store.update('test', state='paused')
        self.engine.resume('test', attempt_seconds=1)
        os.environ['FAKE_MODE'] = 'sleep'
        self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('wall-clock', task['reason'])
        source = self.engine.attempt_path(task)
        old_files = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
        manifest_file = source.parent / 'manifest.json'
        original_manifest = manifest_file.read_bytes()
        self.engine.resume('test', attempt_seconds=7200, extra_seconds=14400)
        queued = self.store.get('test')
        self.assertEqual(queued['manifest']['attempt_seconds'], 20)
        self.assertEqual(queued['attempt_seconds_override'], 7200)
        self.assertIn('each Pi/Codex process <= 7200 seconds', queued['feedback'])
        self.assertIn(str(queued['deadline']), queued['feedback'])
        events = [json.loads(r[0]) for r in self.store.db.execute('SELECT payload FROM events WHERE kind=?', ('state',))]
        self.assertTrue(any(e.get('attempt_seconds_override') == 7200 and e.get('state') == 'ready' for e in events))
        os.environ['FAKE_MODE'] = 'accept'
        with patch('codinator.engine.worker', wraps=real_worker) as workers, \
             patch('codinator.engine.reviewer', wraps=real_reviewer) as reviews, \
             patch('codinator.engine.run_process', wraps=real_run_process) as checks:
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['attempt']), ('accepted', 2), task['reason'])
        self.assertEqual(workers.call_args.args[4]['timeout'], 7200)
        self.assertEqual(reviews.call_args.args[3]['timeout'], 7200)
        self.assertEqual(checks.call_args.kwargs['timeout'], self.manifest['checks'][0]['timeout_seconds'])
        budget = json.loads((self.engine.attempt_path(task) / 'budget.json').read_text())
        self.assertEqual(budget['attempt_seconds'], 7200)
        self.assertEqual(budget['manifest_attempt_seconds'], 20)
        self.assertEqual(budget['source'], 'resume_override')
        self.assertEqual(manifest_file.read_bytes(), original_manifest)
        self.assertTrue(all(p.read_bytes() == data for p, data in old_files.items()))

    def test_override_persists_after_restart_and_rework(self):
        self.store.update('test', state='paused')
        self.engine.resume('test', attempt_seconds=7200, extra_seconds=14400)
        self.store.db.close()
        self.store = Store(self.store.root)
        self.addCleanup(self.store.db.close)
        self.engine.store = self.store
        self.store.update('test', state='paused')
        self.engine.resume('test')  # Omission retains the explicit override.
        os.environ['FAKE_MODE'] = 'rework'
        with patch('codinator.engine.worker', wraps=real_worker) as workers, \
             patch('codinator.engine.reviewer', wraps=real_reviewer) as reviews:
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'accepted')
        self.assertEqual([c.args[4]['timeout'] for c in workers.call_args_list], [7200, 7200])
        self.assertEqual([c.args[3]['timeout'] for c in reviews.call_args_list], [7200, 7200])

    def test_override_still_caps_both_agents_by_total_deadline(self):
        self.store.update('test', state='paused', deadline=time.time() + 30)
        self.engine.resume('test', attempt_seconds=7200)
        with patch('codinator.engine.worker', wraps=real_worker) as workers, \
             patch('codinator.engine.reviewer', wraps=real_reviewer) as reviews, \
             patch('codinator.engine.run_process', wraps=real_run_process) as checks:
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'accepted')
        for timeout in (workers.call_args.args[4]['timeout'], reviews.call_args.args[3]['timeout']):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30)
        self.assertLessEqual(checks.call_args.kwargs['timeout'], self.manifest['checks'][0]['timeout_seconds'])
        self.assertLessEqual(checks.call_args.kwargs['timeout'], 30)

    def test_deadline_expiry_between_worker_and_checks_never_launches_next_process(self):
        original_timeout = _budget_timeout
        calls = 0
        def after_pi(deadline, limit):
            nonlocal calls
            calls += 1
            if calls >= 2:
                with patch('codinator.engine.time.time', return_value=deadline):
                    return original_timeout(deadline, limit)
            return original_timeout(deadline, limit)
        with patch('codinator.engine._budget_timeout', side_effect=after_pi), \
             patch('codinator.engine.run_process', side_effect=AssertionError('expired check launched')), \
             patch('codinator.engine.reviewer', side_effect=AssertionError('expired review launched')):
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('blocked', 1, 1))
        self.assertFalse((self.engine.attempt_path(task) / 'outcome.json').exists())
        self.assertFalse((self.engine.attempt_path(task) / 'checks').exists())

    def test_review_only_override_keeps_eligibility_gate_and_skips_pi(self):
        source = self.review_failure()
        self.engine.resume('test', review_only=True, attempt_seconds=7200, extra_seconds=14400)
        os.environ['FAKE_MODE'] = 'accept'
        with patch('codinator.engine.worker', side_effect=AssertionError('Pi must not run')), \
             patch('codinator.engine.reviewer', wraps=real_reviewer) as reviews:
            self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'accepted')
        self.assertEqual(reviews.call_args.args[3]['timeout'], 7200)
        self.assertTrue((source / 'checks/unit/result.json').exists())

    def test_failed_checkpoint_does_not_change_override_or_deadline(self):
        self.review_failure()
        before = self.store.get('test')
        (self.workspace / 'product.py').write_text('unexpected edit\n')
        with self.assertRaisesRegex(Problem, 'checkpoint'):
            self.engine.resume('test', review_only=True, attempt_seconds=7200, extra_seconds=14400)
        self.assertEqual(self.store.get('test'), before)
