"""Delivery fault injection with fake agents and immutable original evidence."""
import json
import os
import time
import unittest
from unittest.mock import patch

import test_engine
from codinator.agents import repair_delivery as real_repair, worker as real_worker
from codinator.files import Problem


class DeliveryRepairTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def test_malformed_identity_is_repaired_once_then_independently_reviewed(self):
        with patch('codinator.engine.worker', wraps=real_worker) as workers, \
             patch('codinator.engine.repair_delivery', wraps=real_repair) as repairs:
            task, out = self.run_mode('malformed-then-repair')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 1, 1), task['reason'])
        self.assertEqual((workers.call_count, repairs.call_count), (1, 1))
        self.assertLessEqual(repairs.call_args.args[4]['timeout'], 20)
        original = json.loads((out / 'delivery/completion.json').read_text())
        self.assertEqual((original['task_id'], original['status']), ('research-id', 'completed'))
        selected = json.loads((out / 'delivery-selection.json').read_text())
        self.assertEqual(selected['directory'], 'delivery-repair/delivery')
        self.assertTrue((out / 'delivery-repair/delivery/evidence.json').exists())
        self.assertFalse((out / 'checks').exists())
        self.assertIn(str(out / 'delivery-repair/delivery/summary.md'), (out / 'review-prompt.txt').read_text())
        self.assertEqual(json.loads((out / 'delivery-repair/delivery/completion.json').read_text()),
                         {'task_id': 'test', 'round': 1, 'attempt': 1, 'status': 'awaiting_review'})
        for field in ('task_id', 'round', 'status', 'run_id'):
            self.assertIn(field, (out / 'delivery-error.json').read_text())
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '1')
        for context in (out, out / 'delivery-repair'):
            argv = json.loads((context / 'pi/launch.json').read_text())['argv']
            rules = argv[argv.index('--append-system-prompt') + 1]
            self.assertIn(str(context / 'delivery-contract.json'), rules)
            self.assertIn(str(context / 'submit-delivery.py'), rules)
        repair_events = self.events('pi', out / 'delivery-repair')
        self.assertFalse(any(e['type'] in ('fixture_commit', 'fixture_check') for e in repair_events))

    def test_missing_normalized_delivery_can_reuse_preserved_agent_evidence(self):
        task, out = self.run_mode('missing-then-repair')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual({p.name for p in (out / 'delivery').iterdir()}, {'agent-evidence.json'})
        self.assertTrue((out / 'delivery-repair/pi/result.json').exists())

    def test_misplaced_delivery_is_preserved_and_independent_codex_rejects_dirty_scope(self):
        # Source scanning is the reviewer's responsibility. Repair only normalizes
        # retained evidence; it cannot erase the misplaced source directory.
        task, out = self.run_mode('misplaced-then-repair')
        self.assertEqual(task['state'], 'blocked')
        self.assertTrue((self.workspace / 'delivery/completion.json').exists())
        self.assertTrue((out / 'codex/result.json').exists())
        self.assertIn('not clean', task['reason'])

    def test_linked_or_deep_delivery_never_starts_repair(self):
        for mode in ('symlink-delivery', 'deep-completion'):
            with self.subTest(mode=mode):
                with patch('codinator.engine.repair_delivery', side_effect=AssertionError('unsafe artifact')):
                    task, out = self.run_mode(mode)
                self.assertEqual(task['state'], 'blocked')
                self.assertFalse((out / 'delivery-repair').exists())
                self.assertFalse((out / 'codex').exists())
                # Agent completed a commit, but no valid claim was accepted.
                self.store.update('test', expected_digest=self.git('rev-parse', 'HEAD'))
                self.engine.resume('test')

    def test_explicit_blocked_and_malformed_blocked_never_repaired(self):
        for mode in ('blocked', 'malformed-blocked'):
            with self.subTest(mode=mode):
                with patch('codinator.engine.repair_delivery', side_effect=AssertionError('explicit blocked')):
                    task, out = self.run_mode(mode)
                self.assertEqual(task['state'], 'blocked')
                self.assertFalse((out / 'delivery-repair').exists())
                self.assertFalse((out / 'codex').exists())
                self.store.update('test', expected_digest=self.git('rev-parse', 'HEAD'))
                self.engine.resume('test')

    def test_repair_failure_does_not_retry_or_review(self):
        with patch('codinator.engine.repair_delivery', wraps=real_repair) as repairs:
            task, out = self.run_mode('bad-completion-shape')
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(repairs.call_count, 1)
        self.assertFalse((out / 'delivery-selection.json').exists())
        self.assertFalse((out / 'codex').exists())
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '1')

    def test_truncated_implementation_never_starts_repair(self):
        with patch('codinator.engine.repair_delivery', side_effect=AssertionError('uncertain implementation')):
            task, out = self.run_mode('truncated')
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse((out / 'delivery-repair').exists())

    def test_truncated_repair_never_reviews(self):
        task, out = self.run_mode('repair-truncated')
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse((out / 'codex').exists())
        self.assertFalse((out / 'delivery-selection.json').exists())

    def test_repair_mutation_is_rejected_by_independent_codex(self):
        # FakeSandbox permits malicious writes to exercise independent verification;
        # real bwrap rejects the write itself in test_git_engine.
        task, out = self.run_mode('repair-mutation')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('not clean', task['reason'])
        submitted = json.loads((out / 'submission.json').read_text())
        self.assertEqual(task['expected_digest'], submitted['commit'])
        self.assertEqual(submitted['commit'], self.git('rev-parse', 'HEAD'))
        self.assertTrue((out / 'codex').exists())
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '1')

    def test_interrupted_repair_recovery_never_replays_or_adopts_mutation(self):
        os.environ['FAKE_MODE'] = 'malformed-then-repair'
        with patch('codinator.engine.repair_delivery', side_effect=SystemExit('controller crash')):
            with self.assertRaises(SystemExit):
                self.engine.run('test')
        frozen = self.task()
        self.assertEqual((frozen['state'], frozen['phase']), ('checking', 'delivery-repair'))
        (self.workspace / 'product.py').write_text('VALUE = -1\n')
        with patch('subprocess.Popen', side_effect=AssertionError('never replay or run Git')):
            self.engine.recover()
        task = self.task()
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('interrupted', task['reason'])
        self.assertEqual(task['expected_digest'], frozen['expected_digest'])
        self.assertEqual((self.workspace / 'product.py').read_text(), 'VALUE = -1\n')

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

    def test_repair_mounts_source_git_and_original_evidence_read_only(self):
        with patch.object(self.engine.sandbox, 'wrap', wraps=self.engine.sandbox.wrap) as calls:
            task, out = self.run_mode('malformed-then-repair')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        repair = next(c for c in calls.call_args_list if '--mode' in c.args[0] and c.args[2] == ())
        self.assertEqual(repair.args[3][0], out / 'delivery-repair/delivery')
        self.assertNotIn(self.workspace / '.git', repair.args[3])
        self.assertEqual(repair.kwargs['readonly'], [out.parent])
        implementation = next(c for c in calls.call_args_list if '--mode' in c.args[0] and c.args[2] == ['product.py'])
        self.assertIn(self.workspace / '.git', implementation.args[3])
        self.assertEqual(implementation.kwargs['readonly'], [out.parent])

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

    def test_v2_review_retry_never_falls_back_to_unbound_legacy_delivery(self):
        task, out = self.run_mode('codex-unavailable')
        for name in ('delivery-selection.json', 'delivery-contract.json', 'submit-delivery.py'):
            (out / name).unlink()
        with self.assertRaises(Problem):
            self.engine.resume('test', review_only=True)
