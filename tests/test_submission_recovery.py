"""Submission receipts and recoverable preflight failures with fake agents."""
import json
from pathlib import Path
import py_compile
import socket
import threading
import unittest
from unittest.mock import patch

import test_engine
from codinator.files import Problem
from codinator.foreground import Bridge, pi_environment
from codinator.interactive import Interactive, SubmissionRejected

RUNTIME = {'provider': 'bonsai', 'model': 'bonsai2-27b', 'thinking': 'xhigh'}


class SubmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        test_engine.EngineTests.setUp(self)
        self.ui = Interactive(self.store.root, 'test', test_engine.FakeSandbox(), self.agent)
        self.addCleanup(self.ui.close)
        self.ui.begin()
        (self.workspace / 'product.py').write_text('VALUE = 42\n')

    def test_bytecode_is_archived_and_submission_runs_once(self):
        path = Path(py_compile.compile(str(self.workspace / 'product.py'), doraise=True))
        original = path.read_bytes()
        self.ui.submit('one', 'done', RUNTIME)
        self.ui.thread.join(15)
        self.assertEqual(self.ui.status()['state'], 'accepted')
        self.assertEqual(self.ui.status()['attempt'], 1)
        archive, = self.ui.task_dir().glob('cache-cleanup-*')
        self.assertEqual((archive / 'removed' / path.relative_to(self.workspace)).read_bytes(), original)
        self.assertFalse(path.exists())
        with patch.object(self.ui, '_spawn', side_effect=AssertionError('duplicate submission')):
            self.assertEqual(self.ui.submit('one', 'done', RUNTIME)['attempt'], 1)

    def test_paused_implementation_resume_cleans_only_new_bytecode(self):
        path = Path(py_compile.compile(str(self.workspace / 'product.py'), doraise=True))
        self.ui.pause('Submission failed: bytecode outside scope')
        result = self.ui.resume()
        self.assertEqual((result['state'], result['round'], result['attempt']), ('ready', 1, 0))
        self.assertFalse(path.exists())
        self.assertEqual((self.workspace / 'product.py').read_text(), 'VALUE = 42\n')
        self.assertIsNone(self.ui.thread)

    def test_definite_rejection_has_evidence_but_no_receipt_or_new_attempt(self):
        (self.workspace / 'outside.txt').write_text('not allowed')
        with self.assertRaises(SubmissionRejected) as caught:
            self.ui.submit('one', 'done', RUNTIME)
        self.assertEqual(caught.exception.status['state'], 'implementing')
        self.assertEqual(caught.exception.status['attempt'], 0)
        self.assertIn('outside.txt', caught.exception.status['reason'])
        evidence, = self.ui.task_dir().glob('rejected-*')
        self.assertTrue((evidence / 'snapshot.json').exists())
        self.assertEqual(json.loads((evidence / 'request.json').read_text())['markdown'], 'done')
        self.assertFalse(list(self.ui.task_dir().glob('attempt-*')))
        self.assertFalse(list(self.store.db.execute("select * from events where kind='interactive_submitted'")))
        self.assertIsNone(self.ui.thread)

    def test_pause_cause_survives_session_exit(self):
        self.ui.pause('提交结果不确定：socket timeout')
        self.ui.close()
        self.assertEqual(self.ui.status()['reason'], '提交结果不确定：socket timeout')
        with self.assertRaises(Problem):
            self.ui.pause('')

    def test_late_worker_cannot_overwrite_pause_cause(self):
        entered, release = threading.Event(), threading.Event()
        def review(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            raise Problem('late worker error')
        with patch('codinator.interactive.reviewer', side_effect=review):
            self.ui.submit('one', 'done', RUNTIME)
            self.assertTrue(entered.wait(5))
            self.ui.pause('用户明确暂停')
            release.set()
            self.ui.thread.join(10)
        self.assertEqual(self.ui.status()['reason'], '用户明确暂停')

    def test_bridge_tags_only_definite_rejections_and_preserves_reason(self):
        with Bridge(self.base / 'bridge', self.ui) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            def request(value):
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(self.base / 'bridge'))
                    client.sendall(json.dumps(value).encode() + b'\n')
                    return json.loads(client.makefile('rb').readline())
            try:
                (self.workspace / 'outside.txt').write_text('not allowed')
                req = {'op': 'submit', 'request_id': 'one', 'markdown': 'done', 'runtime': RUNTIME}
                result = request(req)
                self.assertEqual(result['code'], 'submission_rejected')
                self.assertEqual(result['status']['state'], 'implementing')
                with patch.object(self.ui, 'submit', side_effect=Problem('late persistence failure')):
                    self.assertNotIn('code', request(req))
                request({'op': 'pause', 'reason': '具体提交失败原因'})
                self.assertEqual(self.ui.status()['reason'], '具体提交失败原因')
                self.assertFalse(request({'op': 'pause', 'reason': 'bad', 'state': 'accepted'})['ok'])
            finally:
                server.shutdown()
                thread.join()

    def test_native_pi_cache_prefix_is_outside_workspace(self):
        env = pi_environment('/private/pi', '/bridge')
        self.assertEqual(env['PYTHONPYCACHEPREFIX'], '/private/pi/pycache')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
