"""Regression and fault tests for relay cleanup; no live model or production state."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codinator import delivery, handoff
from codinator.agents import repair_delivery
from codinator.config import load_manifest
from codinator.files import Problem, write_bytes, write_json
from codinator.store import Store
import test_harness
import test_handoff_delivery
import test_observations


class CompactRetryTests(unittest.TestCase):
    setUp = test_harness.HarnessTests.setUp
    publish = test_harness.HarnessTests.publish
    compact = test_harness.HarnessTests.compact
    submit_progress = test_harness.HarnessTests.submit_progress

    def test_identical_compact_retry_and_conflict_preserve_original_evidence(self):
        cp, _ = self.compact()
        value = {'completed': [], 'checks': [], 'blockers': [], 'next_step': 'implement',
                 'needs_guidance': False, 'candidate_commit': None}
        self.assertEqual(self.submit_progress(cp, value), 0)
        cp.observe(111)
        def originals():
            return {str(p.relative_to(cp.directory)): (p.read_bytes(), p.stat().st_ino,
                    p.stat().st_mtime_ns) for p in cp.directory.rglob('*') if p.is_file()}
        before = originals()
        self.assertEqual(self.submit_progress(cp, value), 0)
        self.assertEqual(originals(), before)
        self.assertEqual(self.submit_progress(cp, value | {'next_step': 'different'}), 2)
        self.assertEqual(originals(), before)


class PublicationFaultTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_partial_write_file_sync_and_link_failures_leave_no_artifact(self):
        fdopen = os.fdopen
        class PartialWriter:
            def __init__(self, fd, mode):
                self.stream = fdopen(fd, mode)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def write(self, data):
                self.stream.write(data[:2])
                raise OSError('injected partial write')
        publishers = (write_bytes, delivery._write_once, handoff._write_once)
        for publish in publishers:
            for target, fault in (('os.fdopen', PartialWriter), ('os.fsync', OSError('sync failure')),
                                  ('os.link', OSError('link failure'))):
                with self.subTest(publish=publish.__module__, target=target):
                    with patch('codinator.files.' + target, side_effect=fault):
                        with self.assertRaises(OSError):
                            publish(self.root / 'artifact', b'evidence')
                    self.assertEqual(list(self.root.iterdir()), [])

    def test_directory_sync_failure_is_loud_and_retry_never_overwrites(self):
        sync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('directory sync failure')
            sync(fd)
        path = self.root / 'artifact'
        with patch('codinator.files.os.fsync', side_effect=fail_directory):
            with self.assertRaisesRegex(OSError, 'directory sync failure'):
                write_bytes(path, b'original')
        original = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        with self.assertRaises(FileExistsError):
            write_bytes(path, b'replacement')
        self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), original)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_json_wire_bytes_are_unchanged(self):
        path = self.root / 'record.json'
        value = {'description': '中文', 'result': None}
        write_json(path, value)
        self.assertEqual(path.read_bytes(), (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())

    def test_legacy_publication_rejected_before_workspace_access(self):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps({'version': 1, 'workspace': '/nonexistent/legacy-workspace'}))
        with self.assertRaisesRegex(Problem, 'read-only history'):
            load_manifest(path)

    def test_existing_unused_database_column_and_values_are_preserved(self):
        store = Store(self.root / 'state')
        self.addCleanup(store.db.close)
        store.add({'version': 2, 'id': 'T'}, 'a' * 40)
        columns = {r[1] for r in store.db.execute('PRAGMA table_info(tasks)')}
        self.assertNotIn('last_issues', columns)
        store.db.execute("ALTER TABLE tasks ADD COLUMN last_issues TEXT NOT NULL DEFAULT ''")
        store.db.execute("UPDATE tasks SET last_issues='historical-issue' WHERE id='T'")
        store.db.commit()
        reopened = Store(store.root)
        self.addCleanup(reopened.db.close)
        reopened.update('T', feedback='new feedback')
        self.assertEqual(reopened.get('T')['last_issues'], 'historical-issue')


class DeliveryReadTests(unittest.TestCase):
    setUp = test_handoff_delivery.HandoffDeliveryTests.setUp
    submit = test_handoff_delivery.HandoffDeliveryTests.submit

    def test_status_and_packet_share_one_read_and_next_boundary_detects_tampering(self):
        self.assertEqual(self.submit()[0], 0)
        with patch('codinator.delivery._read', wraps=delivery._read) as read:
            status, packet = delivery.read_delivery(self.delivery, self.task)
        self.assertEqual((status, packet), ('awaiting_review', self.packet))
        paths = [call.args[0] for call in read.call_args_list]
        for path in (self.attempt / 'delivery-contract.json', *self.delivery.iterdir()):
            self.assertEqual(paths.count(path), 1, str(path))
        changed = json.loads((self.delivery / 'evidence.json').read_text())
        changed['git']['commit'] = 'd' * 40
        changed['checks'][0]['commit'] = 'd' * 40
        (self.delivery / 'evidence.json').write_text(json.dumps(changed))
        with self.assertRaises(delivery.DeliveryError) as error:
            delivery.read_delivery(self.delivery, self.task)
        self.assertFalse(error.exception.repairable)


class WorkerInstructionTests(unittest.TestCase):
    setUp = test_observations.ObserveWorkerPromptTests.setUp

    def test_worker_has_one_authoritative_instruction_block_and_scoped_task_message(self):
        from codinator import agents
        self.task['feedback'] = 'Verified blocker: fix only the missing addition.'
        with patch.object(agents, '_pi_worker') as worker:
            agents.worker(self.task, self.attempt_dir, self.private_dir, object(), {'timeout': 30})
        prompt = worker.call_args.args[0]
        instructions = worker.call_args.kwargs['delivery_instructions']
        self.assertNotIn(instructions, prompt)
        self.assertIn(self.task['feedback'], prompt)
        self.assertIn(str(self.attempt_dir.parent / 'handoff.md'), prompt)
        self.assertIn(json.dumps(self.task['manifest']['allowed_paths']), prompt)
        self.assertIn(json.dumps(self.task['manifest']['checks']), prompt)
        for required in ('Bound submission command:', 'Commit allowed changes before',
                         'Missing checks use not_run/null', '--status needs_guidance',
                         'Correct any submission tool error', 'Passive output observation',
                         'Do not switch branches', 'Do not change model/provider',
                         'Frozen summary template', 'Frozen help template'):
            self.assertIn(required, instructions)
        self.assertNotIn('Bound submission command:', prompt)
        self.assertNotIn('Frozen summary template', prompt)
        self.assertTrue(worker.call_args.kwargs['git_write'])
        self.assertIsNotNone(worker.call_args.kwargs['closeout'])

    def test_repair_keeps_readonly_constraints_and_current_binding_without_duplication(self):
        from codinator import agents
        error = delivery.DeliveryError([{'code': 'missing', 'path': 'summary.md',
                    'expected': 'summary', 'actual': 'missing'}], repairable=True)
        with patch.object(agents, '_pi_worker') as worker:
            repair_delivery(self.task, self.attempt_dir, self.private_dir, object(), {'timeout': 30}, error)
        prompt = worker.call_args.args[0]
        instructions = worker.call_args.kwargs['delivery_instructions']
        self.assertNotIn(instructions, prompt)
        self.assertIn(json.dumps(error.errors), prompt)
        self.assertIn(str(self.attempt_dir), prompt)
        self.assertIn(str(self.attempt_dir / 'delivery-repair' / 'delivery-contract.json'), instructions)
        for required in ('read-only', 'run Git', 'ONLY from original Pi tool events',
                         'at most five minutes', '--status needs_guidance', 'submit blocked',
                         'Correct any submission tool error'):
            self.assertIn(required, instructions)
        self.assertFalse(worker.call_args.kwargs.get('git_write', False))
        self.assertEqual(worker.call_args.kwargs.get('allowed', ()), ())
        self.assertEqual(worker.call_args.kwargs['readonly'], [self.attempt_dir.parent])


class LiveSmokePreparationTests(unittest.TestCase):
    def test_v2_clean_fixture_and_default_no_dispatch(self):
        spec = importlib.util.spec_from_file_location('live_smoke',
            Path(__file__).resolve().parents[1] / 'validation/live_smoke.py')
        smoke = importlib.util.module_from_spec(spec)
        with patch('subprocess.check_output', side_effect=AssertionError('import ran Git')), \
             patch('codinator.engine.Engine.run', side_effect=AssertionError('import dispatched agent')):
            spec.loader.exec_module(smoke)
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp, \
             patch.object(smoke, 'Engine', side_effect=AssertionError('preparation dispatched agent')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(smoke.main(['--output-parent', tmp]), 0)
            base = next(Path(tmp).iterdir())
            m = load_manifest(base / 'task.json')
            self.assertEqual(m['version'], 2)
            self.assertEqual(m['handoff_protocol'], 1)
            self.assertEqual((m['max_rounds'], m['max_seconds'], m['attempt_seconds']), (2, 2400, 1200))
            ws = Path(m['workspace'])
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=ws, text=True).strip()
            self.assertEqual(git('status', '--porcelain'), '')
            self.assertEqual(git('rev-parse', 'HEAD'), m['git']['base_commit'])
            self.assertEqual(git('branch', '--show-current'), m['git']['branch'])
            self.assertIn('no agents dispatched', output.getvalue())
            self.assertFalse((base / 'state').exists())
