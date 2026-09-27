"""Lifecycle fault injection with fake local agents; no model/network claims."""
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import patch

import test_engine
from codidator.files import Problem, digest, snapshot
from codidator.foreground import Bridge, pi_environment
from codidator.interactive import Interactive
from codidator.store import Store

RUNTIME = {'provider': 'bonsai', 'model': 'bonsai2-27b', 'thinking': 'xhigh'}


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        test_engine.EngineTests.setUp(self)
        self.ui = Interactive(self.store.root, 'test', test_engine.FakeSandbox(), self.agent)
        self.addCleanup(self.ui.close)
        self.ui.begin()
        (self.workspace / 'product.py').write_text('VALUE = 42\n')

    def finish(self):
        self.ui.thread.join(15)
        self.assertFalse(self.ui.thread.is_alive())
        return self.ui.status()

    def submit(self, id='one', text='Implemented VALUE. Controller checks follow.'):
        return self.ui.submit(id, text, RUNTIME)

    def test_submit_review_rework_accept_preserves_markdown_and_snapshot(self):
        os.environ['FAKE_MODE'] = 'rework'
        self.submit()
        self.assertEqual(self.finish()['state'], 'needs_changes')
        before = (self.ui.task_dir() / 'attempt-0001/review.md').read_bytes()
        self.ui.begin()
        self.submit('two')
        result = self.finish()
        self.assertEqual((result['state'], result['round'], result['attempt']), ('accepted', 2, 2))
        self.assertEqual((self.ui.task_dir() / 'attempt-0001/review.md').read_bytes(), before)
        self.assertIn('author: Codex', result['feedback'])
        self.assertIsNone(snapshot(self.workspace, [])['git']['head'])

    def test_lost_ack_duplicate_is_idempotent_and_different_body_rejected(self):
        self.submit()
        self.finish()
        with patch('codidator.interactive.reviewer', side_effect=AssertionError('duplicate review')):
            self.assertEqual(self.submit()['attempt'], 1)
        with self.assertRaisesRegex(Problem, 'different content'):
            self.submit(text='changed')

    def test_bridge_has_no_accept_or_arbitrary_command_operation(self):
        with Bridge(self.base / 'bridge', self.ui) as server:
            t = threading.Thread(target=server.serve_forever)
            t.start()
            try:
                for op in ('accept', 'run', 'cancel_all'):
                    with socket.socket(socket.AF_UNIX) as client:
                        client.connect(str(self.base / 'bridge'))
                        client.sendall(json.dumps({'op': op}).encode() + b'\n')
                        result = json.loads(client.recv(10000))
                        self.assertFalse(result['ok'])
            finally:
                server.shutdown()
                t.join()

    def test_scope_or_wrong_runtime_never_launches_review(self):
        with self.assertRaisesRegex(Problem, 'runtime'):
            self.ui.submit('bad', 'summary', RUNTIME | {'model': 'other'})
        (self.workspace / 'handoff.md').write_text('changed requirements')
        with self.assertRaisesRegex(Problem, 'outside allowed'):
            self.submit()
        self.assertIsNone(self.ui.thread)

    def test_check_failure_returns_automatic_rework_without_codex(self):
        (self.workspace / 'product.py').write_text('VALUE = -1\n')
        with patch('codidator.interactive.reviewer', side_effect=AssertionError('must not review')):
            self.submit()
            t = self.finish()
        self.assertEqual(t['state'], 'needs_changes')
        self.assertIn('author: Controller checks', t['feedback'])
        self.assertTrue((self.ui.task_dir() / 'attempt-0001/checks/unit/result.json').exists())

    def test_stale_verdict_and_workspace_mutation_block_acceptance(self):
        os.environ['FAKE_MODE'] = 'mutate-review'
        self.submit()
        t = self.finish()
        self.assertEqual(t['state'], 'blocked')
        self.assertIn('stale', t['reason'])
        self.assertFalse((self.ui.task_dir() / 'attempt-0001/outcome.json').exists())

    def test_review_failure_resumes_only_review_and_preserves_original(self):
        os.environ['FAKE_MODE'] = 'codex-unavailable'
        self.submit()
        self.assertEqual(self.finish()['state'], 'blocked')
        first = self.ui.task_dir() / 'attempt-0001'
        old = self.ui._evidence_digest(first)
        os.environ['FAKE_MODE'] = 'accept'
        with patch('codidator.interactive.run_process', side_effect=AssertionError('checks must not replay')):
            self.ui.resume()
            self.assertEqual(self.finish()['state'], 'accepted')
        self.assertEqual(self.ui._evidence_digest(first), old)
        self.assertEqual(self.ui.status()['attempt'], 2)

    def test_changed_evidence_or_existing_verdict_prevents_resume(self):
        os.environ['FAKE_MODE'] = 'codex-unavailable'
        self.submit()
        self.finish()
        first = self.ui.task_dir() / 'attempt-0001'
        path = first / 'delivery/summary.md'
        old = path.read_bytes()
        path.write_text('fabricated')
        with self.assertRaisesRegex(Problem, 'evidence changed'):
            self.ui.resume()
        path.write_bytes(old)
        (first / 'review-delivery/verdict.json').write_text('{}')
        with self.assertRaisesRegex(Problem, 'reconciliation'):
            self.ui.resume()

    def test_pause_wins_over_late_accepted_review(self):
        entered, release = threading.Event(), threading.Event()
        def fake_review(task, attempt, fingerprint, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'verdict': 'accepted', 'summary': 'late result', 'issues': []}
        with patch('codidator.interactive.reviewer', side_effect=fake_review):
            self.submit()
            self.assertTrue(entered.wait(5))
            self.ui.pause()
            release.set()
            self.assertEqual(self.finish()['state'], 'paused')
        self.assertFalse((self.ui.task_dir() / 'attempt-0001/outcome.json').exists())

    def test_interrupted_implementation_requires_explicit_resume(self):
        self.ui.recover()
        self.assertEqual(self.ui.status()['state'], 'paused')
        self.assertIsNone(self.ui.thread)
        self.ui.resume()
        self.assertEqual(self.ui.status()['state'], 'ready')
        self.ui.begin()
        self.submit()
        self.assertEqual(self.finish()['state'], 'accepted')

    def test_interrupted_check_can_retry_in_new_attempt(self):
        from codidator.process import Interrupted
        with patch('codidator.interactive.run_process', side_effect=Interrupted('interrupted')):
            self.submit()
            self.assertEqual(self.finish()['state'], 'blocked')
        self.ui.resume()
        self.assertEqual(self.finish()['state'], 'accepted')
        self.assertTrue((self.ui.task_dir() / 'attempt-0001/submission.md').exists())

    def test_round_limit_stops_without_acceptance(self):
        os.environ['FAKE_MODE'] = 'no-progress'
        self.submit()
        self.finish()
        self.ui.begin()
        self.submit('two')
        result = self.finish()
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('same issue', result['reason'])

    def test_foreground_proxy_split_does_not_change_parent_environment(self):
        with patch.dict(os.environ, {'https_proxy': 'http://localhost:8888', 'ALL_PROXY': 'proxy'}):
            env = pi_environment('/config', '/bridge')
            self.assertNotIn('https_proxy', env)
            self.assertNotIn('ALL_PROXY', env)
            self.assertEqual(env['NO_PROXY'], '*')
            self.assertEqual(os.environ['https_proxy'], 'http://localhost:8888')

    def test_unexpected_worker_exception_is_durable_blocked(self):
        with patch('codidator.interactive.reviewer', side_effect=TypeError('unexpected boundary failure')):
            self.submit()
            result = self.finish()
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('unexpected boundary', result['reason'])

    def test_crash_before_submission_receipt_preserves_orphan_and_skips_number(self):
        original = Store._update
        def crash(store, task, fields):
            if fields == {'phase': 'checks'}:
                raise RuntimeError('crash before durable receipt')
            return original(store, task, fields)
        with patch.object(Store, '_update', autospec=True, side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'durable receipt'):
                self.submit()
        old = self.ui.task_dir() / 'attempt-0001'
        preserved = self.ui._evidence_digest(old)
        self.ui.recover()
        self.ui.resume()
        self.ui.begin()
        self.submit()  # Same request is not falsely acknowledged as completed.
        result = self.finish()
        self.assertEqual((result['state'], result['attempt']), ('accepted', 2))
        self.assertEqual(self.ui._evidence_digest(old), preserved)
        self.assertEqual(self.submit()['attempt'], 2)

    def test_pause_cannot_be_erased_by_concurrent_spawn(self):
        before_spawn, release_spawn = threading.Event(), threading.Event()
        review_entered, release_review = threading.Event(), threading.Event()
        original = self.ui._spawn
        def spawn():
            before_spawn.set()
            self.assertTrue(release_spawn.wait(5))
            original()
        def review(*args, **kwargs):
            review_entered.set()
            release_review.wait(5)
            return {'verdict': 'accepted', 'summary': 'late', 'issues': []}
        with patch.object(self.ui, '_spawn', side_effect=spawn), patch('codidator.interactive.reviewer', side_effect=review):
            submitter = threading.Thread(target=self.submit)
            submitter.start()
            self.assertTrue(before_spawn.wait(5))
            pauser = threading.Thread(target=self.ui.pause)
            pauser.start()
            release_spawn.set()
            submitter.join(5); pauser.join(5)
            release_review.set()
            self.assertEqual(self.finish()['state'], 'paused')
        self.assertTrue(self.ui.cancel.is_set())
        self.assertFalse((self.ui.task_dir() / 'attempt-0001/outcome.json').exists())

    def test_resume_crash_does_not_reuse_orphan_review_directory(self):
        from codidator.files import write_json as original
        os.environ['FAKE_MODE'] = 'codex-unavailable'
        self.submit(); self.finish()
        def crash(path, value):
            original(path, value)
            if Path(path).name == 'review-source.json':
                raise RuntimeError('crash before resume state commit')
        with patch('codidator.interactive.write_json', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'resume state'):
                self.ui.resume()
        old = (self.ui.task_dir() / 'attempt-0002/review-source.json').read_bytes()
        os.environ['FAKE_MODE'] = 'accept'
        self.ui.resume()
        self.assertEqual(self.finish()['attempt'], 3)
        self.assertEqual(self.ui.status()['state'], 'accepted')
        self.assertEqual((self.ui.task_dir() / 'attempt-0002/review-source.json').read_bytes(), old)

    def test_exit_between_review_and_rework_can_resume(self):
        os.environ['FAKE_MODE'] = 'rework'
        self.submit(); self.finish()
        self.ui.pause(); self.ui.resume()
        self.assertEqual(self.ui.status()['state'], 'needs_changes')
        self.ui.begin(); self.submit('next'); self.finish()
        self.assertEqual(self.ui.status()['state'], 'accepted')
