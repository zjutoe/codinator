"""Optional budget/checkpoint integration using local fake agents only."""
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_engine
from codinator.agents import worker
from codinator.checkpoints import Checkpoints
from codinator.config import deadline_timestamp, load_manifest
from codinator.files import Problem


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class CheckpointEngineTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def publish(self, **changes):
        path = self.base / 'bounded.json'
        path.write_text(json.dumps(self.raw | {'id': 'bounded'} | changes))
        manifest = load_manifest(path)
        self.engine.submit(manifest)
        return manifest

    def test_queue_delay_cannot_extend_absolute_deadline(self):
        cutoff = 1_900_000_000
        with patch('codinator.engine.time.time', return_value=cutoff - 100):
            self.publish(deadline_utc=utc(cutoff))
        with patch('codinator.engine.time.time', return_value=cutoff - 10):
            self.engine.run('bounded')
        task = self.store.get('bounded')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual(task['started'], cutoff - 10)
        self.assertEqual(task['deadline'], cutoff)
        budget = json.loads((self.engine.attempt_path(task) / 'budget.json').read_text())
        self.assertEqual(budget['deadline'], cutoff)

    def test_relative_budget_can_end_before_absolute_ceiling(self):
        cutoff = 1_900_000_000
        self.publish(deadline_utc=utc(cutoff))
        with patch('codinator.engine.time.time', return_value=cutoff - 200):
            self.engine.run('bounded')
        task = self.store.get('bounded')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual(task['deadline'], cutoff - 200 + self.manifest['max_seconds'])

    def test_expired_absolute_deadline_never_starts_an_attempt(self):
        cutoff = 1_900_000_000
        self.publish(deadline_utc=utc(cutoff))
        with patch('codinator.engine.time.time', return_value=cutoff), \
             patch('codinator.engine.worker', side_effect=AssertionError('expired task dispatched')):
            self.engine.run('bounded')
        task = self.store.get('bounded')
        self.assertEqual((task['state'], task['attempt']), ('blocked', 0))
        self.assertIn('budget exhausted', task['reason'])
        self.assertFalse(self.engine.attempt_path(task).exists())

    def test_explicit_extra_budget_still_respects_frozen_absolute_ceiling(self):
        cutoff = 1_900_000_000
        manifest = self.publish(deadline_utc=utc(cutoff))
        self.store.update('bounded', state='paused', deadline=cutoff - 20)
        with patch('codinator.engine.time.time', return_value=cutoff - 10):
            self.engine.resume('bounded', extra_seconds=1000)
        task = self.store.get('bounded')
        self.assertEqual(task['deadline'], cutoff)
        self.assertEqual(task['manifest'], manifest)

    def test_resume_cannot_reduce_attempt_limit_to_checkpoint_interval(self):
        self.publish(checkpoint_seconds=10)
        self.store.update('bounded', state='paused')
        before = self.store.get('bounded')
        with self.assertRaisesRegex(Problem, 'checkpoint_seconds'):
            self.engine.resume('bounded', attempt_seconds=10)
        self.assertEqual(self.store.get('bounded'), before)

    def test_optional_worker_mount_only_reports_writable_and_keeps_contract_readonly(self):
        manifest = self.publish(checkpoint_seconds=10)
        task = self.store.get('bounded') | {'round': 1, 'attempt': 1}
        context = self.base / 'worker-attempt'
        context.mkdir()
        private = self.base / 'private'
        private.mkdir()
        calls = []
        class InspectSandbox:
            def wrap(self, argv, root, allowed, writable, readonly):
                calls.append((argv, allowed, writable, readonly))
                return argv
        with patch('codinator.agents.run_process') as run:
            worker(task, context, private, InspectSandbox(), {'timeout': 20}, self.agent)
        argv, allowed, writable, readonly = calls[0]
        self.assertEqual(allowed, manifest['allowed_paths'])
        self.assertIn(context.parent, readonly)
        self.assertIn(self.workspace / '.git', writable)
        self.assertIn(context / 'checkpoints/reports', writable)
        self.assertNotIn(context / 'checkpoints/requests', writable)
        self.assertNotIn(context / 'checkpoints/acks', writable)
        instructions = argv[argv.index('--append-system-prompt') + 1]
        self.assertIn('Checkpoint contract:', instructions)
        self.assertIn('blocked DELIVERY', instructions)
        self.assertIsNotNone(run.call_args.kwargs['protocol'].checkpoints)

    def test_checkpoint_violation_retains_agent_work_without_controller_commit_or_review(self):
        self.publish(checkpoint_seconds=10)
        def violating(task, context, *args):
            (self.workspace / 'product.py').write_text('VALUE = 17\n')
            checkpoints = Checkpoints(task, context, 10)
            checkpoints.start(100)
            checkpoints.tick(110, lambda message: None)
            checkpoints.enforce(411, set())
        with patch('codinator.engine.worker', side_effect=violating) as implementation, \
             patch('codinator.engine.reviewer') as review:
            self.engine.run('bounded')
        task = self.store.get('bounded')
        self.assertEqual((task['state'], task['attempt'], task['round']), ('blocked', 1, 1))
        self.assertIn('response_timeout', task['reason'])
        self.assertEqual(task['expected_digest'], self.initial)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.assertEqual((self.workspace / 'product.py').read_text(), 'VALUE = 17\n')
        self.assertFalse(list(self.engine.attempt_path(task).glob('git-*')))
        implementation.assert_called_once()
        review.assert_not_called()

    def test_v2_final_delivery_requires_packet_and_binds_all_three_files(self):
        from codinator.files import file_info
        task = self.task() | {'attempt': 1}
        for valid in (False, True):
            with self.subTest(valid_packet=valid):
                context = self.base / ('valid-checkpoint' if valid else 'missing-checkpoint')
                context.mkdir()
                delivery = context / 'delivery'
                delivery.mkdir()
                (delivery / 'summary.md').write_text('Agent check evidence claim.\n')
                identity = {k: task[k] for k in ('round', 'attempt')} | {'task_id': task['id']}
                (delivery / 'completion.json').write_text(json.dumps(identity | {'status': 'awaiting_review'}))
                if valid:
                    packet = {'version': 1, **identity,
                              'git': self.manifest['git'] | {'commit': self.initial},
                              'checks': [check | {'commit': self.initial, 'status': 'passed',
                                          'exit_code': 0, 'evidence': 'fixture output; protocol only'}
                                         for check in self.manifest['checks']]}
                    for check in packet['checks']:
                        check.pop('timeout_seconds', None)
                    (delivery / 'evidence.json').write_text(json.dumps(packet))
                checkpoints = Checkpoints(task, context, 10)
                checkpoints.start(100)
                checkpoints.tick(110, lambda message: None)
                if not valid:
                    with self.assertRaisesRegex(Problem, 'missing_final_delivery'):
                        checkpoints.finish(111)
                    self.assertFalse((context / 'checkpoints/resolutions/0001.json').exists())
                else:
                    checkpoints.finish(111)
                    resolved = json.loads((context / 'checkpoints/resolutions/0001.json').read_text())
                    self.assertEqual(resolved['source'], 'final_delivery')
                    self.assertEqual(resolved['final_delivery']['files'],
                                     {name: file_info(delivery / name) for name in
                                      ('summary.md', 'completion.json', 'evidence.json')})


class DeadlineConfigTests(unittest.TestCase):
    def test_explicit_utc_only_and_real_calendar_validation(self):
        self.assertEqual(deadline_timestamp('2026-10-02T06:00:09Z'),
                         deadline_timestamp('2026-10-02T06:00:09+00:00'))
        for value in (None, True, 123, '2026-10-02T06:00:09', '2026-10-02',
                      '2026-10-02T06:00:09+08:00', '2026-10-02T06:00:09-00:00',
                      '2026-02-30T06:00:09Z', '2026-10-02T25:00:09Z'):
            with self.subTest(value=value), self.assertRaises(Problem):
                deadline_timestamp(value)


if __name__ == '__main__':
    unittest.main()
