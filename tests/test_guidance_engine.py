"""Guidance lifecycle with native Git/checks inside fake agents, never real models."""
import json
import os
import subprocess
import time
import unittest
from unittest.mock import patch

import test_engine
from codinator.agents import (guidance as real_guidance, reviewer as real_reviewer,
                              verify_reply, worker as real_worker)
from codinator.config import load_manifest
from codinator.engine import Engine
from codinator.files import Problem
from codinator.handoff import template, validate_document
from codinator.process import run_process as real_run_process
from codinator.status import report
from codinator.store import Store


class GuidanceEngineTests(unittest.TestCase):
    def setUp(self):
        test_engine.EngineTests.setUp(self)
        handoff = template('handoff').replace('Describe the concrete problem and expected outcome.',
                                             'Implement product.py with VALUE=42; commit before tests.')
        (self.workspace / 'handoff.md').write_text(handoff)
        self.git('add', 'handoff.md')
        self.git('commit', '-qm', 'Publish protocol fixture contract')
        self.initial = self.git('rev-parse', 'HEAD')
        self.raw.update(id='protocol', handoff_protocol=1, max_rounds=3,
                        git={'branch': 'task', 'base_commit': self.initial})
        self.path.write_text(json.dumps(self.raw))
        self.manifest = load_manifest(self.path)
        self.engine.submit(self.manifest)

    def task(self):
        return self.store.get('protocol')

    def run_mode(self, mode='guidance'):
        os.environ['FAKE_MODE'] = mode
        self.engine.run('protocol')
        return self.task(), self.engine.attempt_path(self.task())

    def source(self, attempt=1):
        return self.store.root / 'tasks/protocol' / f'attempt-{attempt:04d}'

    def bounded_task(self):
        task_id = 'bounded'
        self.engine.submit(self.manifest | {'id': task_id, 'max_rounds': 2})
        return task_id

    def pause_queued(self, task_id, queued_state, mode):
        original_update = self.store.update
        observed = []

        def update(current_id, **fields):
            result = original_update(current_id, **fields)
            if current_id == task_id and fields.get('state') == queued_state and fields.get('round') == 2:
                self.store.request_control(task_id, 'pause')
                observed.append(self.store.get(task_id))
            return result

        os.environ['FAKE_MODE'] = mode
        with patch.object(self.store, 'update', side_effect=update):
            self.engine.run(task_id)
        self.assertEqual(len(observed), 1)
        task = self.store.get(task_id)
        self.assertEqual((task['state'], task['round'], task['attempt']), ('paused', 2, 1), task['reason'])
        source = self.engine.attempt_path(task)
        return task, source, {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}

    def test_pause_after_guidance_queue_preserves_unused_round_on_resume(self):
        task_id = self.bounded_task()
        paused, source, evidence = self.pause_queued(task_id, 'ready', 'guidance')
        self.assertTrue((source / 'guidance-outcome.json').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.engine.resume(task_id)
        queued = self.store.get(task_id)
        self.assertEqual((queued['state'], queued['round'], queued['attempt']), ('ready', 2, 1))
        self.assertEqual(queued['deadline'], paused['deadline'])
        os.environ['FAKE_MODE'] = 'guidance'
        self.engine.run(task_id)
        final = self.store.get(task_id)
        self.assertEqual((final['state'], final['round'], final['attempt']), ('accepted', 2, 2), final['reason'])
        self.assertEqual(final['deadline'], paused['deadline'])
        self.assertTrue(all(path.read_bytes() == data for path, data in evidence.items()))

    def test_pause_after_rework_queue_preserves_unused_round_on_resume(self):
        task_id = self.bounded_task()
        paused, source, evidence = self.pause_queued(task_id, 'needs_changes', 'rework')
        self.assertEqual(json.loads((source / 'outcome.json').read_text())['verdict'], 'needs_changes')
        first_candidate = self.git('rev-parse', 'HEAD')
        self.engine.resume(task_id)
        self.assertEqual(self.store.get(task_id)['round'], 2)
        os.environ['FAKE_MODE'] = 'accept'
        self.engine.run(task_id)
        final = self.store.get(task_id)
        self.assertEqual((final['state'], final['round'], final['attempt']), ('accepted', 2, 2), final['reason'])
        self.assertEqual(self.git('rev-parse', 'HEAD^'), first_candidate)
        self.assertEqual(final['deadline'], paused['deadline'])
        self.assertTrue(all(path.read_bytes() == data for path, data in evidence.items()))

    def test_repeated_resume_pause_without_dispatch_does_not_consume_rounds(self):
        task_id = self.bounded_task()
        paused, source, evidence = self.pause_queued(task_id, 'ready', 'guidance')
        for _ in range(3):
            self.engine.resume(task_id)
            queued = self.store.get(task_id)
            self.assertEqual((queued['round'], queued['attempt']), (2, 1))
            self.assertEqual(queued['deadline'], paused['deadline'])
            self.store.request_control(task_id, 'pause')
        self.engine.resume(task_id)
        os.environ['FAKE_MODE'] = 'guidance'
        self.engine.run(task_id)
        final = self.store.get(task_id)
        self.assertEqual((final['state'], final['round'], final['attempt']), ('accepted', 2, 2), final['reason'])
        self.assertEqual(final['deadline'], paused['deadline'])
        self.assertTrue(all(path.read_bytes() == data for path, data in evidence.items()))

    def test_crash_after_dispatch_before_worker_start_consumes_round(self):
        task_id = self.bounded_task()
        with patch('codinator.engine.worker', side_effect=Problem('Injected crash after dispatch')):
            self.engine.run(task_id)
        stopped = self.store.get(task_id)
        self.assertEqual((stopped['state'], stopped['round'], stopped['attempt']), ('blocked', 1, 1))
        source = self.engine.attempt_path(stopped)
        self.assertFalse((source / 'delivery-contract.json').exists())
        evidence = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
        self.engine.resume(task_id)
        self.assertEqual(self.store.get(task_id)['round'], 2)
        os.environ['FAKE_MODE'] = 'accept'
        self.engine.run(task_id)
        final = self.store.get(task_id)
        self.assertEqual((final['state'], final['round'], final['attempt']), ('accepted', 2, 2), final['reason'])
        self.assertEqual(final['deadline'], stopped['deadline'])
        self.assertTrue(all(path.read_bytes() == data for path, data in evidence.items()))

    def test_interrupted_dispatched_pi_consumes_round_on_normal_resume(self):
        task_id = self.bounded_task()
        original_options = self.engine.options

        def options(current_id, timeout):
            result = original_options(current_id, timeout)
            original_start = result['on_start']

            def start(pid, identity):
                original_start(pid, identity)
                if current_id == task_id and self.store.get(task_id)['state'] == 'implementing':
                    self.store.request_control(task_id, 'pause')

            result['on_start'] = start
            return result

        os.environ['FAKE_MODE'] = 'accept'
        with patch.object(self.engine, 'options', side_effect=options):
            self.engine.run(task_id)
        stopped = self.store.get(task_id)
        self.assertEqual((stopped['state'], stopped['round'], stopped['attempt']), ('paused', 1, 1))
        source = self.engine.attempt_path(stopped)
        self.assertIn('Pause/cancel', json.loads((source / 'pi/result.json').read_text())['failure'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        evidence = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
        self.engine.resume(task_id)
        self.assertEqual(self.store.get(task_id)['round'], 2)
        self.engine.run(task_id)
        final = self.store.get(task_id)
        self.assertEqual((final['state'], final['round'], final['attempt']), ('accepted', 2, 2), final['reason'])
        self.assertEqual(final['deadline'], stopped['deadline'])
        self.assertTrue(all(path.read_bytes() == data for path, data in evidence.items()))

    def fixture_events(self, attempt, role):
        name = 'stdout.jsonl' if role == 'pi' else 'stdout.txt'
        return [json.loads(line) for line in (attempt / role / name).read_text().split('\n') if line]

    def assert_stopped_after_help(self, task):
        first = self.source()
        self.assertEqual((task['state'], task['attempt']), ('blocked', 1), task['reason'])
        self.assertFalse(self.source(2).exists())
        self.assertFalse((first / 'codex').exists())
        self.assertTrue((first / 'delivery/completion.json').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        return first

    def test_help_guidance_fresh_pi_then_independent_review(self):
        real_popen = subprocess.Popen
        launched = []

        def agent_only(argv, *args, **kwargs):
            self.assertEqual(argv[0], self.agent, f'Controller executed non-agent command: {argv}')
            launched.append(argv)
            return real_popen(argv, *args, **kwargs)

        with patch('subprocess.Popen', side_effect=agent_only):
            task, final = self.run_mode()
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 2, 2), task['reason'])
        self.assertEqual(len(launched), 4)
        for argv in (launched[1], launched[3]):
            overrides = [argv[i + 1] for i, arg in enumerate(argv) if arg == '-c']
            self.assertNotIn('--sandbox', argv)
            self.assertIn('default_permissions="codinator_review"', overrides)
            self.assertIn('permissions.codinator_review.extends=":read-only"', overrides)
            self.assertIn('permissions.codinator_review.filesystem={"/tmp"="write"}', overrides)
            self.assertIn('permissions.codinator_review.network.enabled=false', overrides)
            self.assertIn('exact level-2 Markdown headings (##)', argv[-1])
        first = self.source()
        help_packet = json.loads((first / 'delivery/evidence.json').read_text())
        help_completion = json.loads((first / 'delivery/completion.json').read_text())
        guidance = json.loads((first / 'guidance-outcome.json').read_text())
        final_completion = json.loads((final / 'delivery/completion.json').read_text())
        verdict = json.loads((final / 'outcome.json').read_text())
        self.assertEqual(help_packet['git']['commit'], self.initial)
        self.assertTrue(all(check['status'] == 'not_run' and check['exit_code'] is None for check in help_packet['checks']))
        self.assertEqual(help_completion['status'], 'needs_guidance')
        self.assertEqual(guidance['result'], 'continue')
        self.assertEqual(guidance['message']['reply_to'], help_completion['message']['message_id'])
        self.assertEqual(final_completion['message']['reply_to'], guidance['message']['message_id'])
        self.assertEqual(verdict['message']['reply_to'], final_completion['message']['message_id'])
        self.assertEqual({value['message']['contract_digest'] for value in
                          (help_completion, guidance, final_completion, verdict)},
                         {help_completion['message']['contract_digest']})
        self.assertNotEqual(guidance['message']['author'], verdict['message']['author'])
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '1')
        self.assertEqual(task['expected_digest'], head)
        self.assertEqual(self.git('status', '--porcelain'), '')
        guide_events = self.fixture_events(first, 'guidance-codex')
        self.assertEqual(next(e for e in guide_events if e['type'] == 'fixture_guide_verification')['commit'], self.initial)
        self.assertFalse(any(e['type'] in ('fixture_commit', 'fixture_check') for e in guide_events))
        for role in ('pi', 'codex'):
            events = self.fixture_events(final, role)
            checked = [e for e in events if e['type'] == 'fixture_check']
            self.assertEqual([(e['role'], e['commit'], e['exit_code']) for e in checked], [(role, head, 0)])
            if role == 'pi':
                self.assertLess(next(i for i, e in enumerate(events) if e['type'] == 'fixture_commit'),
                                next(i for i, e in enumerate(events) if e['type'] == 'fixture_check'))
        self.assertFalse((first / 'outcome.json').exists())
        self.assertFalse((first / 'checks').exists())
        self.assertFalse((final / 'checks').exists())
        for source, kind, name in ((first, 'help', 'delivery/summary.md'),
                                   (final, 'summary', 'delivery/summary.md')):
            validate_document((source / name).read_text(), kind)
        self.assertEqual(json.loads((first / 'budget.json').read_text())['deadline'],
                         json.loads((final / 'budget.json').read_text())['deadline'])
        self.assertIn(guidance['message']['message_id'], (final / 'worker-prompt.txt').read_text())
        self.assertIn('Verified clean candidate', (final / 'worker-prompt.txt').read_text())
        for source, author, prompt_file in ((first, 'pi', 'worker-prompt.txt'),
                                             (first, 'guidance', 'guidance-prompt.txt'),
                                             (final, 'review', 'review-prompt.txt')):
            snapshot = json.loads((source / f'{author}-templates.json').read_text())
            self.assertEqual(snapshot['contract_digest'], help_completion['message']['contract_digest'])
            prompt = (source / prompt_file).read_text()
            if author == 'pi':
                argv = json.loads((source / 'pi/launch.json').read_text())['argv']
                instructions = argv[argv.index('--append-system-prompt') + 1]
            else:
                instructions = prompt
            for name in snapshot['templates']:
                frozen = (source.parent / snapshot['templates'][name]['path']).read_text()
                self.assertIn(frozen, instructions)
                if author == 'pi':
                    self.assertNotIn(frozen, prompt)

    def test_guiding_status_names_handler_request_and_existing_deadline(self):
        observed = []

        def guide(task, *args, **kwargs):
            status = report(self.store, self.task())
            completion = json.loads((self.source() / 'delivery/completion.json').read_text())
            self.assertEqual(status['current_handler'], 'guidance_codex')
            self.assertEqual(status['pending_reply_to'], completion['message']['message_id'])
            self.assertEqual(status['deadline'], task['deadline'])
            self.assertEqual(status['summaries']['final']['status'], 'needs_guidance')
            self.assertFalse(status['summaries']['final_missing'])
            observed.append(status)
            return real_guidance(task, *args, **kwargs)

        with patch('codinator.engine.guidance', side_effect=guide):
            task, out = self.run_mode()
        self.assertEqual(len(observed), 1)
        self.assertEqual(task['state'], 'accepted', task['reason'])

    def test_queued_guidance_status_retains_original_help_summary_identity(self):
        original_update = self.store.update
        observed = []

        def update(task_id, **fields):
            result = original_update(task_id, **fields)
            if task_id == 'protocol' and fields.get('state') == 'ready' and fields.get('round') == 2:
                status = report(self.store, self.task())
                guide = json.loads((self.source() / 'guidance-outcome.json').read_text())
                final = status['summaries']['final']
                self.assertEqual((status['round'], status['attempt']), (2, 1))
                self.assertEqual((final['round'], final['attempt'], final['status']), (1, 1, 'needs_guidance'))
                self.assertEqual(final['path'], str(self.source() / 'delivery/summary.md'))
                self.assertNotIn('final_error', status['summaries'])
                self.assertFalse(status['summaries']['final_missing'])
                self.assertEqual(status['pending_reply_to'], guide['message']['message_id'])
                observed.append(status)
            return result

        with patch.object(self.store, 'update', side_effect=update):
            task, out = self.run_mode()
        self.assertEqual(len(observed), 1)
        self.assertEqual(task['state'], 'accepted', task['reason'])

    def test_interrupted_review_only_status_retains_original_final_summary(self):
        def unavailable(*args, **kwargs):
            with patch.dict(os.environ, {'FAKE_MODE': 'codex-unavailable'}):
                return real_reviewer(*args, **kwargs)

        with patch('codinator.engine.reviewer', side_effect=unavailable):
            task, source = self.run_mode()
        self.assertEqual((task['state'], task['round'], task['attempt']), ('blocked', 2, 2), task['reason'])
        before = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
        self.engine.resume('protocol', review_only=True)
        with patch('codinator.engine.worker', side_effect=AssertionError('Review retry replayed Pi')), \
             patch('codinator.engine.reviewer', side_effect=unavailable):
            self.engine.run('protocol')
        task = self.task()
        self.assertEqual((task['state'], task['round'], task['attempt']), ('blocked', 2, 3), task['reason'])
        status = report(self.store, task)
        final = status['summaries']['final']
        self.assertEqual((final['round'], final['attempt'], final['status']), (2, 2, 'awaiting_review'))
        self.assertEqual(final['path'], str(source / 'delivery/summary.md'))
        self.assertNotIn('final_error', status['summaries'])
        self.assertFalse(status['summaries']['final_missing'])
        self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))

    def test_repeated_help_consumes_original_rounds_without_implementation(self):
        task, out = self.run_mode('guidance-repeat')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('blocked', 3, 3), task['reason'])
        self.assertIn('round budget exhausted', task['reason'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        deadlines = set()
        for number in range(1, 4):
            source = self.source(number)
            deadlines.add(json.loads((source / 'budget.json').read_text())['deadline'])
            self.assertTrue((source / 'guidance-outcome.json').exists())
            self.assertFalse((source / 'codex').exists())
        self.assertEqual(len(deadlines), 1)
        with self.assertRaisesRegex(Problem, 'round budget'):
            self.engine.resume('protocol')

    def test_replayed_reply_from_previous_attempt_is_not_consumed(self):
        task, out = self.run_mode('guidance-replayed-reply')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('blocked', 2, 2), task['reason'])
        self.assertIn('binding_mismatch', task['reason'])
        self.assertTrue((self.source() / 'guidance-outcome.json').exists())
        self.assertFalse((out / 'guidance-outcome.json').exists())
        self.assertFalse(self.source(3).exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)

    def test_deadline_exhaustion_never_starts_continuation_or_resets_budget(self):
        deadline = []

        def expire(task, *args, **kwargs):
            deadline.append(task['deadline'])
            result = real_guidance(task, *args, **kwargs)
            self.store.update('protocol', deadline=time.time() - 1)
            return result

        with patch('codinator.engine.guidance', side_effect=expire):
            task, out = self.run_mode()
        self.assert_stopped_after_help(task)
        self.assertIn('wall-clock budget exhausted', task['reason'])
        self.assertLess(task['deadline'], deadline[0])
        self.assertEqual(json.loads((out / 'budget.json').read_text())['deadline'], deadline[0])

    def test_controller_crash_after_guide_output_recovers_without_replay(self):
        def crash_after_output(task, *args, **kwargs):
            real_guidance(task, *args, **kwargs)
            raise SystemExit('Injected controller loss before publication')

        os.environ['FAKE_MODE'] = 'guidance'
        with patch('codinator.engine.guidance', side_effect=crash_after_output), self.assertRaises(SystemExit):
            self.engine.run('protocol')
        self.assertEqual(self.task()['state'], 'guiding')
        first = self.source()
        before = {path: path.read_bytes() for path in first.rglob('*') if path.is_file()}
        reopened = Store(self.store.root)
        self.addCleanup(reopened.db.close)
        recovered = Engine(reopened, sandbox=test_engine.FakeSandbox(), pi_bin=self.agent, codex_bin=self.agent)
        with patch('subprocess.Popen', side_effect=AssertionError('Recovery replayed an agent')):
            recovered.recover()
        self.assert_stopped_after_help(self.task())
        self.assertIn('No prompt was replayed', self.task()['reason'])
        self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))
        self.assertFalse((first / 'guidance-outcome.json').exists())
        with self.assertRaises(Problem):
            self.engine.run('protocol')
        with self.assertRaises(Problem):
            self.engine.resume('protocol', review_only=True)

    def test_unconfirmed_pi_exit_prevents_guidance_dispatch(self):
        def worker(*args, **kwargs):
            real_worker(*args, **kwargs)
            self.store.update('protocol', pid=99999999, pid_start='unconfirmed')

        with patch('codinator.engine.worker', side_effect=worker), \
             patch('codinator.engine.guidance', side_effect=AssertionError('Unconfirmed Pi consumed')):
            task, out = self.run_mode()
        self.assert_stopped_after_help(task)
        self.assertIn('exit is unconfirmed', task['reason'])
        self.assertEqual(task['pid'], 99999999)
        self.assertFalse((out / 'guidance-codex').exists())

    def test_unconfirmed_guide_exit_prevents_next_pi_dispatch(self):
        def guide(*args, **kwargs):
            result = real_guidance(*args, **kwargs)
            self.store.update('protocol', pid=99999999, pid_start='unconfirmed')
            return result

        with patch('codinator.engine.guidance', side_effect=guide):
            task, out = self.run_mode()
        self.assert_stopped_after_help(task)
        self.assertIn('exit is unconfirmed', task['reason'])
        self.assertEqual(task['pid'], 99999999)
        self.assertFalse((out / 'guidance-outcome.json').exists())

    def test_same_checkout_remains_locked_through_readonly_guidance(self):
        other_store = Store(self.base / 'other-state')
        self.addCleanup(other_store.db.close)
        other = Engine(other_store, sandbox=test_engine.FakeSandbox(), pi_bin=self.agent, codex_bin=self.agent)
        other.submit(self.manifest | {'id': 'other'})

        def guide(*args, **kwargs):
            with self.assertRaises(Problem):
                other.run('other')
            self.assertEqual(other_store.get('other')['attempt'], 0)
            with self.assertRaises(Problem):
                other.submit(self.manifest | {'id': 'third'})
            return real_guidance(*args, **kwargs)

        with patch('codinator.engine.guidance', side_effect=guide):
            task, out = self.run_mode()
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual(other_store.get('other')['state'], 'ready')

    def test_mutated_help_evidence_during_guidance_is_not_consumed(self):
        def mutate_help(*args, **kwargs):
            result = real_guidance(*args, **kwargs)
            (self.source() / 'delivery/summary.md').write_text('Changed after guidance read')
            return result

        with patch('codinator.engine.guidance', side_effect=mutate_help):
            task, out = self.run_mode()
        self.assert_stopped_after_help(task)
        self.assertFalse((out / 'guidance-outcome.json').exists())

    def test_frozen_template_mutation_during_guidance_prevents_continuation(self):
        def mutate_template(*args, **kwargs):
            result = real_guidance(*args, **kwargs)
            (self.source().parent / 'templates/guidance.md').write_text('Altered published template')
            return result

        with patch('codinator.engine.guidance', side_effect=mutate_template):
            task, out = self.run_mode()
        self.assert_stopped_after_help(task)
        self.assertIn('template identity mismatch', task['reason'])
        self.assertFalse((out / 'guidance-outcome.json').exists())

    def test_pause_during_guide_process_preserves_raw_stop_without_continuation(self):
        original_options = self.engine.options

        def options(task_id, timeout):
            result = original_options(task_id, timeout)
            original_start = result['on_start']

            def start(pid, identity):
                original_start(pid, identity)
                if self.task()['state'] == 'guiding':
                    self.store.request_control(task_id, 'pause')

            result['on_start'] = start
            return result

        with patch.object(self.engine, 'options', side_effect=options):
            task, out = self.run_mode()
        self.assertEqual((task['state'], task['attempt']), ('paused', 1))
        stopped = json.loads((out / 'guidance-codex/result.json').read_text())
        self.assertIn('Pause/cancel', stopped['failure'])
        self.assertIsNone(task['pid'])
        self.assertFalse((out / 'guidance-outcome.json').exists())
        self.assertFalse(self.source(2).exists())


def _fault_test(mode, reason):
    def test(self):
        task, out = self.run_mode(mode)
        self.assert_stopped_after_help(task)
        self.assertIn(reason, task['reason'])
        self.assertTrue((out / 'guidance-codex/result.json').exists())
        if mode in ('guidance-blocked', 'guidance-candidate-mismatch', 'guidance-dirty'):
            self.assertEqual(json.loads((out / 'guidance-outcome.json').read_text())['result'], 'blocked')
        else:
            self.assertFalse((out / 'guidance-outcome.json').exists())
    return test


for _mode, _reason in (
    ('guidance-blocked', 'External fixture input'),
    ('guidance-candidate-mismatch', 'Candidate SHA'),
    ('guidance-dirty', 'not clean'),
    ('guidance-wrong-task', 'Invalid Codex guidance'),
    ('guidance-stale-sha', 'Invalid Codex guidance'),
    ('guidance-wrong-result', 'Invalid Codex guidance'),
    ('guidance-missing-message', 'Invalid Codex guidance'),
    ('guidance-invalid-message', 'invalid_message'),
    ('guidance-stale-attempt', 'binding_mismatch'),
    ('guidance-stale-round', 'binding_mismatch'),
    ('guidance-wrong-contract', 'binding_mismatch'),
    ('guidance-wrong-reply', 'binding_mismatch'),
    ('guidance-wrong-author', 'binding_mismatch'),
    ('guidance-wrong-kind', 'binding_mismatch'),
    ('guidance-missing-section', 'missing_section'),
    ('guidance-turn-failed', 'successful turn completion'),
    ('guidance-no-completion', 'successful turn completion'),
    ('guidance-nonzero', 'Process exited 9'),
    ('guidance-absent-output', 'Missing/invalid Codex'),
    ('guidance-duplicate-event', 'duplicate_key'),
    ('guidance-identical-duplicate-event', 'duplicate_key'),
):
    setattr(GuidanceEngineTests, 'test_' + _mode.replace('-', '_'), _fault_test(_mode, _reason))


def _late_control_test(control):
    def test(self):
        def reply_after_control(*args, **kwargs):
            result = real_guidance(*args, **kwargs)
            self.store.request_control('protocol', control)
            return result
        with patch('codinator.engine.guidance', side_effect=reply_after_control):
            task, out = self.run_mode()
        self.assertEqual(task['state'], 'paused' if control == 'pause' else 'cancelled')
        self.assertEqual(task['attempt'], 1)
        self.assertFalse(self.source(2).exists())
        self.assertTrue((out / 'guidance-delivery/guidance.json').exists())
        self.assertFalse((out / 'guidance-outcome.json').exists())
    return test


for _control in ('pause', 'cancel'):
    setattr(GuidanceEngineTests, f'test_{_control}_wins_over_late_guide', _late_control_test(_control))


def _reply_integrity_test(name, remove=False):
    def test(self):
        def alter_after_read(*args, **kwargs):
            result = real_guidance(*args, **kwargs)
            path = self.source() / name
            self.assertTrue(path.is_file(), name)
            if remove:
                path.unlink()
            else:
                path.write_bytes(path.read_bytes() + b'\nchanged after verified read')
            return result
        with patch('codinator.engine.guidance', side_effect=alter_after_read):
            task, out = self.run_mode()
        self.assert_stopped_after_help(task)
        self.assertFalse((out / 'guidance-outcome.json').exists())
    return test


for _name in ('guidance-delivery/guidance.json', 'guidance-codex/stdout.txt',
              'guidance-codex/stderr.txt', 'guidance-codex/result.json',
              'guidance-codex/launch.json', 'guidance-prompt.txt', 'guidance-schema.json',
              'guidance-selection.json', 'guidance-templates.json'):
    _label = _name.replace('/', '_').replace('.', '_').replace('-', '_')
    setattr(GuidanceEngineTests, 'test_tampered_reply_' + _label, _reply_integrity_test(_name))

for _name in ('guidance-delivery/guidance.json', 'guidance-codex/stdout.txt', 'guidance-selection.json'):
    _label = _name.replace('/', '_').replace('.', '_').replace('-', '_')
    setattr(GuidanceEngineTests, 'test_missing_reply_' + _label, _reply_integrity_test(_name, remove=True))


def _review_integrity_test(name):
    def test(self):
        def alter_after_review(task, attempt, *args, **kwargs):
            result = real_reviewer(task, attempt, *args, **kwargs)
            path = attempt / name
            self.assertTrue(path.is_file(), name)
            path.write_bytes(path.read_bytes() + b'\nchanged after verified review')
            return result
        with patch('codinator.engine.reviewer', side_effect=alter_after_review):
            task, out = self.run_mode()
        self.assertEqual((task['state'], task['attempt']), ('blocked', 2), task['reason'])
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '1')
        self.assertTrue((self.source() / 'guidance-outcome.json').exists())
        self.assertFalse((out / 'outcome.json').exists())
        self.assertFalse(self.source(3).exists())
    return test


for _name in ('review-delivery/verdict.json', 'codex/stdout.txt', 'review-selection.json'):
    _label = _name.replace('/', '_').replace('.', '_').replace('-', '_')
    setattr(GuidanceEngineTests, 'test_tampered_review_' + _label, _review_integrity_test(_name))


def _duplicate_artifact_test(phase, field, value, nested=False):
    def test(self):
        injected = []

        def alter_before_parse(*args, **kwargs):
            result = real_run_process(*args, **kwargs)
            process = kwargs['out']
            if process.name == ('guidance-codex' if phase == 'guidance' else 'codex'):
                artifact = process.parent / ('guidance-delivery/guidance.json' if phase == 'guidance'
                                              else 'review-delivery/verdict.json')
                text = artifact.read_text()
                duplicate = json.dumps(field) + ':' + json.dumps(value) + ','
                if nested:
                    self.assertIn('"message": {', text)
                    text = text.replace('"message": {', '"message": {' + duplicate, 1)
                else:
                    text = '{' + duplicate + text[1:]
                artifact.write_text(text)
                injected.append(artifact)
            return result

        with patch('codinator.agents.run_process', side_effect=alter_before_parse):
            task, out = self.run_mode()
        self.assertEqual(len(injected), 1)
        self.assertIn('duplicate_key', task['reason'])
        if phase == 'guidance':
            self.assert_stopped_after_help(task)
            self.assertFalse((out / 'guidance-outcome.json').exists())
        else:
            self.assertEqual((task['state'], task['attempt']), ('blocked', 2), task['reason'])
            self.assertFalse((out / 'outcome.json').exists())
            self.assertFalse(self.source(3).exists())
    return test


for _label, _phase, _field, _value, _nested in (
    ('conflicting_guidance_result', 'guidance', 'result', 'blocked', False),
    ('identical_guidance_result', 'guidance', 'result', 'continue', False),
    ('conflicting_review_verdict', 'review', 'verdict', 'blocked', False),
    ('identical_review_verdict', 'review', 'verdict', 'accepted', False),
    ('nested_message_task', 'guidance', 'task_id', 'different-task', True),
    ('nested_message_author', 'guidance', 'author', 'pi', True),
    ('identical_nested_message_attempt', 'guidance', 'attempt', 1, True),
):
    setattr(GuidanceEngineTests, 'test_duplicate_json_' + _label,
            _duplicate_artifact_test(_phase, _field, _value, _nested))


def _duplicate_selection_test(self):
    def duplicate_after_receipt(*args, **kwargs):
        result = real_guidance(*args, **kwargs)
        receipt = self.source() / 'guidance-selection.json'
        text = receipt.read_text()
        receipt.write_text('{"version":1,' + text[1:])
        with self.assertRaises(Problem) as caught:
            verify_reply(self.source(), 'guidance')
        self.assertIn('duplicate_key', str(caught.exception))
        return result

    with patch('codinator.engine.guidance', side_effect=duplicate_after_receipt):
        task, out = self.run_mode()
    self.assert_stopped_after_help(task)
    self.assertIn('duplicate_key', task['reason'])
    self.assertFalse((out / 'guidance-outcome.json').exists())


GuidanceEngineTests.test_duplicate_json_reply_receipt_rejected_by_verify_reply = _duplicate_selection_test


if __name__ == '__main__':
    unittest.main()
