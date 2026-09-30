"""Delivery fault injection with fake model subprocesses, never real credentials."""
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_engine
from codinator.agents import repair_delivery as real_repair, worker as real_worker
from codinator.files import Problem, digest, snapshot


class DeliveryRepairTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def run_mode(self, mode):
        os.environ['FAKE_MODE'] = mode
        self.engine.run('test')
        task = self.store.get('test')
        return task, self.engine.attempt_path(task)

    def test_malformed_identity_is_repaired_once_then_checked_and_reviewed(self):
        with patch('codinator.engine.worker', wraps=real_worker) as workers, \
             patch('codinator.engine.repair_delivery', wraps=real_repair) as repairs:
            task, out = self.run_mode('malformed-then-repair')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 1, 1), task['reason'])
        self.assertEqual((workers.call_count, repairs.call_count), (1, 1))
        self.assertLessEqual(repairs.call_args.args[4]['timeout'], 20)
        original = json.loads((out / 'delivery/completion.json').read_text())
        self.assertEqual(original['task_id'], 'research-id')
        self.assertEqual(original['status'], 'completed')
        selected = json.loads((out / 'delivery-selection.json').read_text())
        self.assertEqual(selected['directory'], 'delivery-repair/delivery')
        self.assertTrue((out / 'checks/unit/result.json').exists())
        self.assertIn(str(out / 'delivery-repair/delivery/summary.md'), (out / 'review-prompt.txt').read_text())
        self.assertEqual(json.loads((out / 'delivery-repair/delivery/completion.json').read_text()),
                         {'task_id': 'test', 'round': 1, 'attempt': 1, 'status': 'awaiting_review'})
        errors = (out / 'delivery-error.json').read_text()
        for field in ('task_id', 'round', 'status', 'run_id'):
            self.assertIn(field, errors)
        for context in (out, out / 'delivery-repair'):
            argv = json.loads((context / 'pi/launch.json').read_text())['argv']
            rules = argv[argv.index('--append-system-prompt') + 1]
            self.assertIn(str(context / 'delivery-contract.json'), rules)
            self.assertIn(str(context / 'submit-delivery.py'), rules)

    def test_missing_delivery_in_scope_can_be_repaired(self):
        task, out = self.run_mode('missing-then-repair')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual(list((out / 'delivery').iterdir()), [])
        self.assertTrue((out / 'delivery-repair/pi/result.json').exists())

    def test_misplaced_workspace_delivery_preserved_and_never_repaired(self):
        with patch('codinator.engine.repair_delivery', side_effect=AssertionError('scope violation')):
            task, out = self.run_mode('misplaced-delivery')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('outside allowed', task['reason'])
        self.assertTrue((self.workspace / 'delivery/completion.json').exists())
        self.assertTrue((out / 'implementation.json').exists())
        self.assertFalse((out / 'delivery-repair').exists())
        self.assertFalse((out / 'checks').exists())

    def test_linked_delivery_never_repaired(self):
        with patch('codinator.engine.repair_delivery', side_effect=AssertionError('unsafe artifact')):
            task, out = self.run_mode('symlink-delivery')
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse((out / 'checks').exists())

    def test_deep_delivery_blocks_task_without_stopping_later_dispatch(self):
        with patch('codinator.engine.repair_delivery', side_effect=AssertionError('unsafe JSON')):
            task, out = self.run_mode('deep-completion')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('json_too_deep', task['reason'])
        self.assertFalse((out / 'delivery-repair').exists())
        self.engine.submit(self.manifest | {'id': 'next-task'})
        os.environ['FAKE_MODE'] = 'accept'
        self.engine.run('next-task')
        self.assertEqual(self.store.get('next-task')['state'], 'accepted')

    def test_explicit_blocked_is_not_changed_to_awaiting_review(self):
        with patch('codinator.engine.repair_delivery', side_effect=AssertionError('explicit blocked')):
            task, out = self.run_mode('blocked')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('Pi reported blocked', task['reason'])
        self.assertFalse((out / 'checks').exists())

    def test_malformed_blocked_is_not_repaired(self):
        with patch('codinator.engine.repair_delivery', side_effect=AssertionError('explicit blocked')):
            task, out = self.run_mode('malformed-blocked')
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse((out / 'delivery-repair').exists())

    def test_repair_failure_does_not_retry_or_run_checks(self):
        with patch('codinator.engine.repair_delivery', wraps=real_repair) as repairs:
            task, out = self.run_mode('bad-completion-shape')
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(repairs.call_count, 1)
        self.assertTrue((out / 'delivery-repair/delivery-error.json').exists())
        self.assertFalse((out / 'delivery-selection.json').exists())
        self.assertFalse((out / 'checks').exists())

    def test_truncated_implementation_never_starts_repair(self):
        with patch('codinator.engine.repair_delivery', side_effect=AssertionError('uncertain implementation')):
            task, out = self.run_mode('truncated')
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse((out / 'delivery-repair').exists())

    def test_truncated_repair_never_reviews(self):
        task, out = self.run_mode('repair-truncated')
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse((out / 'checks').exists())
        self.assertFalse((out / 'delivery-selection.json').exists())

    def test_repair_mutation_does_not_replace_frozen_checkpoint(self):
        task, out = self.run_mode('repair-mutation')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('changed during delivery repair', task['reason'])
        submitted = json.loads((out / 'submission.json').read_text())
        self.assertEqual(task['expected_digest'], digest(submitted))
        self.assertNotEqual(task['expected_digest'], digest(snapshot(self.workspace, [])))
        self.assertFalse((out / 'checks').exists())

    def test_interrupted_repair_recovery_never_replays_or_adopts_mutation(self):
        os.environ['FAKE_MODE'] = 'malformed-then-repair'
        with patch('codinator.engine.repair_delivery', side_effect=SystemExit('controller crash')):
            with self.assertRaises(SystemExit):
                self.engine.run('test')
        frozen = self.store.get('test')
        self.assertEqual((frozen['state'], frozen['phase']), ('checking', 'delivery-repair'))
        (self.workspace / 'product.py').write_text('VALUE = -1\n')
        with patch('codinator.engine.repair_delivery', side_effect=AssertionError('never replay')):
            self.engine.recover()
        task = self.store.get('test')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('Frozen submission changed', task['reason'])
        self.assertEqual(task['expected_digest'], frozen['expected_digest'])

    def test_pause_before_repair_prevents_second_prompt(self):
        def worker(*args):
            real_worker(*args)
            self.store.request_control('test', 'pause')
        with patch('codinator.engine.worker', side_effect=worker), \
             patch('codinator.engine.repair_delivery', side_effect=AssertionError('paused')):
            task, out = self.run_mode('malformed-then-repair')
        self.assertEqual(task['state'], 'paused')
        self.assertFalse((out / 'delivery-repair').exists())

    def test_expired_shared_budget_prevents_repair(self):
        def worker(task, *args):
            real_worker(task, *args)
            task['deadline'] = time.time() - 1
        with patch('codinator.engine.worker', side_effect=worker), \
             patch('codinator.engine.repair_delivery', side_effect=AssertionError('budget exhausted')):
            task, out = self.run_mode('malformed-then-repair')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('budget exhausted', task['reason'])
        self.assertFalse((out / 'delivery-repair').exists())

    def test_repair_mounts_workspace_and_original_evidence_read_only(self):
        with patch.object(self.engine.sandbox, 'wrap', wraps=self.engine.sandbox.wrap) as calls:
            task, out = self.run_mode('malformed-then-repair')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        call = next(c for c in calls.call_args_list if c.kwargs.get('readonly') == [out]
                    and '--mode' in c.args[0] and c.args[2] == ())
        self.assertEqual(call.args[2], ())
        self.assertEqual(call.args[3][0], out / 'delivery-repair/delivery')
        worker_call = next(c for c in calls.call_args_list if '--mode' in c.args[0]
                           and c.args[2] == ['product.py'])
        self.assertEqual(worker_call.kwargs['readonly'], [out])

    def test_repaired_delivery_supports_review_only_retry(self):
        task, out = self.run_mode('repair-unavailable')
        self.assertEqual(task['state'], 'blocked')
        original = {p.relative_to(out): p.read_bytes() for p in out.rglob('*') if p.is_file()}
        self.engine.resume('test', review_only=True)
        with patch('codinator.engine.worker', side_effect=AssertionError('do not implement again')), \
             patch('codinator.engine.repair_delivery', side_effect=AssertionError('do not repair again')):
            task, new = self.run_mode('accept')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 1, 2), task['reason'])
        self.assertIn(str(out / 'delivery-repair/delivery/summary.md'), (new / 'review-prompt.txt').read_text())
        self.assertEqual(original, {p.relative_to(out): p.read_bytes() for p in out.rglob('*') if p.is_file()})

    def test_review_retry_rejects_changed_repair_protocol(self):
        task, out = self.run_mode('repair-unavailable')
        result = out / 'delivery-repair/pi/result.json'
        value = json.loads(result.read_text())
        value['failure'] = 'transport uncertain'
        result.write_text(json.dumps(value))
        with self.assertRaisesRegex(Problem, 'successful process'):
            self.engine.resume('test', review_only=True)

    def test_legacy_review_evidence_remains_readable(self):
        task, out = self.run_mode('codex-unavailable')
        for name in ('delivery-selection.json', 'delivery-contract.json', 'submit-delivery.py'):
            (out / name).unlink()
        self.engine.resume('test', review_only=True)
        task, _ = self.run_mode('accept')
        self.assertEqual(task['state'], 'accepted', task['reason'])
