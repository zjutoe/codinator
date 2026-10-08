"""Deterministic local protocol evidence tests; no models or project commands."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codinator.checkpoints import Checkpoints, Closeout, main, status
from codinator.delivery import main as submit_delivery, prepare_contract
from codinator.files import Problem
from codinator.handoff import freeze_protocol, template


PROGRESS = {'completed': [], 'checks': [], 'blockers': [],
            'next_step': 'Continue the same authorized task', 'needs_guidance': False,
            'summary': template('summary')}


class LocalContext(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='closeout-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.context = Path(self.temp.name)
        self.task = {'id': 'T', 'round': 1, 'attempt': 1, 'expected_digest': 'a' * 40,
                     'manifest': {'version': 2, 'id': 'T', 'handoff_protocol': 1,
                         'git': {'branch': 'task/T', 'base_commit': 'a' * 40},
                         'checks': [{'name': 'unit', 'argv': ['true'], 'timeout_seconds': 1}]}}
        self.events = []


class CloseoutTests(LocalContext):
    def adapter(self, timeout=1000):
        adapter = Closeout(self.task, self.context, timeout)
        adapter.start(100)
        return adapter

    def test_one_request_uses_original_deadline_and_preserves_immutable_bytes(self):
        adapter = self.adapter()
        adapter.tick(799, self.events.append)
        self.assertEqual(self.events, [])
        adapter.tick(800, self.events.append)
        request_path = adapter.directory / 'request.json'
        original = request_path.read_bytes()
        for now in (801, 1099, 1100, 1200):
            adapter.tick(now, self.events.append)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(request_path.read_bytes(), original)
        request = json.loads(original)
        self.assertEqual(request['elapsed_seconds'], 700)
        self.assertEqual(request['response_deadline_elapsed_seconds'], 1000)
        self.assertFalse((self.context / 'delivery/summary.md').exists())

    def test_short_budget_requests_at_halfway_and_never_extends_hard_cap(self):
        adapter = self.adapter(120)
        adapter.tick(159, self.events.append)
        self.assertFalse(self.events)
        adapter.tick(160, self.events.append)
        self.assertEqual(adapter.request['response_deadline_elapsed_seconds'], 120)
        adapter.observe(219)
        self.assertTrue(adapter.response({'id': 'closeout-0001', 'success': True}))
        self.assertTrue(json.loads((adapter.directory / 'ack.json').read_text())['within_deadline'])

    def test_invalid_duplicate_and_rejected_acknowledgements_preserve_first_evidence(self):
        adapter = self.adapter()
        self.assertFalse(adapter.response({'id': 'prompt', 'success': True}))
        with self.assertRaises(Problem):
            adapter.response({'id': 'closeout-0001', 'success': True})
        adapter.tick(800, self.events.append)
        for event in ({'id': 'closeout-other', 'success': True},
                      {'id': 'closeout-0001', 'success': 1}):
            with self.subTest(event=event), self.assertRaises(Problem):
                adapter.response(event)
        with self.assertRaisesRegex(Problem, 'rejected closeout'):
            adapter.response({'id': 'closeout-0001', 'success': False, 'error': 'unavailable'})
        ack_path = adapter.directory / 'ack.json'
        original = ack_path.read_bytes()
        with self.assertRaisesRegex(Problem, 'duplicate'):
            adapter.response({'id': 'closeout-0001', 'success': True})
        self.assertEqual(ack_path.read_bytes(), original)

    def test_ack_at_or_after_original_deadline_is_retained_but_rejected(self):
        adapter = self.adapter(120)
        adapter.tick(160, self.events.append)
        adapter.observe(220)
        with self.assertRaisesRegex(Problem, 'Late closeout'):
            adapter.response({'id': 'closeout-0001', 'success': True})
        self.assertFalse(json.loads((adapter.directory / 'ack.json').read_text())['within_deadline'])

    def test_no_request_after_deadline_or_terminal_completion(self):
        adapter = self.adapter(120)
        adapter.tick(220, self.events.append)
        self.assertFalse(self.events)
        adapter.finish(221)
        adapter.tick(222, self.events.append)
        self.assertFalse(self.events)
        self.assertFalse((adapter.directory / 'request.json').exists())


class StageSummaryTests(LocalContext):
    def setUp(self):
        super().setUp()
        self.checkpoints = Checkpoints(self.task, self.context, 10, timeout=1000)
        self.checkpoints.start(100)
        self.checkpoints.tick(110, self.events.append)

    def submit(self, progress=None):
        path = self.context / 'progress.json'
        path.write_text(json.dumps(progress or PROGRESS))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(['--request', '1', '--report', str(path)],
                        contract_path=self.context / 'checkpoint-contract.json')

    def help_delivery(self):
        self.task['expected_digest'] = 'a' * 40
        self.task['manifest'].update({'id': 'T', 'git': {'branch': 'task/T', 'base_commit': 'a' * 40},
            'checks': [{'name': 'unit', 'argv': ['true'], 'timeout_seconds': 1}]})
        frozen = self.context / 'frozen'
        frozen.mkdir()
        (frozen / 'manifest.json').write_text(json.dumps(self.task['manifest']))
        (frozen / 'handoff.md').write_text(template('handoff'))
        freeze_protocol(frozen, self.task['manifest'])
        delivery = self.context / 'delivery'
        delivery.mkdir()
        prepare_contract(self.task, self.context, delivery, task_dir=frozen)
        summary = self.context / 'help-input.md'
        summary.write_text(template('help'))
        evidence = self.context / 'evidence-input.json'
        evidence.write_text(json.dumps({'version': 1, 'task_id': 'T', 'round': 1, 'attempt': 1,
            'git': {'branch': 'task/T', 'base_commit': 'a' * 40, 'commit': 'a' * 40},
            'checks': [{'name': 'unit', 'argv': ['true'], 'commit': 'a' * 40,
                        'status': 'not_run', 'exit_code': None, 'evidence': '未执行'}]}))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = submit_delivery(['--summary', str(summary), '--status', 'needs_guidance',
                                    '--evidence', str(evidence)],
                                   contract_path=self.context / 'delivery-contract.json')
        self.assertEqual(code, 0, stderr.getvalue())
        return delivery

    def test_missing_headings_are_repairable_and_vague_structural_content_is_accepted(self):
        self.assertEqual(self.submit(PROGRESS | {'summary': 'All done'}), 2)
        self.assertFalse((self.checkpoints.directory / 'reports/0001.json').exists())
        summary = '\n'.join('## ' + name + '\n无\n' for name in (
            'Status', 'Completed', 'Incomplete', 'Attempts and results',
            'Artifacts and evidence', 'Deviations and unknowns', 'Next step'))
        self.assertEqual(self.submit(PROGRESS | {'summary': summary}), 0)
        self.checkpoints.observe(111)
        result = status(self.context, 'T', 1)
        path = Path(result['latest_stage_summary']['path'])
        self.assertEqual(path.read_text(), summary)
        self.assertIn('sha256', result['latest_stage_summary'])
        self.assertFalse((self.context / 'delivery/completion.json').exists())

    def test_resolved_report_and_summary_mutation_are_detected_with_original_resolution_retained(self):
        self.assertEqual(self.submit(), 0)
        self.checkpoints.observe(111)
        resolution_path = self.checkpoints.directory / 'resolutions/0001.json'
        original = resolution_path.read_bytes()
        report_path = self.checkpoints.directory / 'reports/0001.json'
        report = json.loads(report_path.read_text())
        report_path.write_text(json.dumps(report | {'progress': PROGRESS | {'next_step': 'Altered'}}))
        with self.assertRaisesRegex(Problem, 'changed after resolution'):
            status(self.context, 'T', 1)
        self.assertEqual(resolution_path.read_bytes(), original)
        report_path.write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode() + b'\n')
        stage_path = self.checkpoints.directory / 'stage-summary-0001.md'
        stage_path.write_text(PROGRESS['summary'] + '\nTampered')
        with self.assertRaisesRegex(Problem, 'changed after resolution'):
            status(self.context, 'T', 1)
        self.checkpoints.observe(112)
        self.assertEqual(self.checkpoints.violation['reason'], 'checkpoint_evidence_changed')
        self.assertEqual(resolution_path.read_bytes(), original)

    def test_idempotent_report_does_not_rewrite_stage_summary(self):
        self.assertEqual(self.submit(), 0)
        self.checkpoints.observe(111)
        path = self.checkpoints.directory / 'stage-summary-0001.md'
        original = path.read_bytes(), path.stat().st_ino
        self.assertEqual(self.submit(), 0)
        self.checkpoints.observe(112)
        self.assertEqual((path.read_bytes(), path.stat().st_ino), original)

    def test_guidance_steers_once_and_allows_bound_grace_before_stopping_at_safe_boundary(self):
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True}), 0)
        self.checkpoints.observe(111)
        self.checkpoints.enforce(112, set())
        self.assertIsNone(self.checkpoints.violation)
        self.checkpoints.tick(112, self.events.append)
        self.checkpoints.tick(113, self.events.append)
        helps = [event for event in self.events if event['id'].startswith('checkpoint-help-')]
        self.assertEqual(len(helps), 1)
        self.assertIn('needs_guidance', helps[0]['message'])
        self.checkpoints.observe(412)
        self.checkpoints.enforce(412, {'long-tool'})
        self.assertFalse(self.checkpoints.stopped)
        with self.assertRaisesRegex(Problem, 'guidance_delivery_timeout'):
            self.checkpoints.enforce(413, set())
        self.assertTrue(self.checkpoints.stopped)
        self.assertTrue((self.checkpoints.directory / 'stage-summary-0001.md').exists())
        self.assertFalse((self.context / 'delivery/summary.md').exists())

    def test_guidance_deadline_is_capped_by_original_process_timeout(self):
        self.checkpoints.timeout = 20
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True}), 0)
        self.checkpoints.observe(111)
        self.assertEqual(self.checkpoints.guidance['deadline_elapsed_seconds'], 20)
        self.checkpoints.enforce(119, set())
        with self.assertRaisesRegex(Problem, 'guidance_delivery_timeout'):
            self.checkpoints.enforce(121, set())

    def test_final_process_completion_without_help_delivery_is_blocked(self):
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True}), 0)
        self.checkpoints.observe(111)
        with self.assertRaisesRegex(Problem, 'missing_final_help_delivery'):
            self.checkpoints.finish(112)
        self.assertIsNotNone(status(self.context, 'T', 1)['latest_stage_summary'])

    def test_valid_final_help_is_observed_before_grace_but_requires_process_finish(self):
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True}), 0)
        self.checkpoints.observe(111)
        delivery = self.help_delivery()
        self.checkpoints.observe(112)
        self.assertIsNotNone(self.checkpoints.guidance_delivery)
        self.assertFalse(self.checkpoints.stopped)
        self.assertIsNone(self.checkpoints.violation)
        original = (self.checkpoints.directory / 'guidance-delivery.json').read_bytes()
        self.checkpoints.enforce(450, {'still-running-tool'})
        self.checkpoints.finish(451)
        self.assertIsNone(self.checkpoints.violation)
        self.assertEqual((self.checkpoints.directory / 'guidance-delivery.json').read_bytes(), original)
        self.assertTrue((delivery / 'completion.json').exists())

    def test_help_appearing_after_grace_does_not_erase_missing_delivery_evidence(self):
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True}), 0)
        self.checkpoints.observe(111)
        self.checkpoints.observe(412)
        original = (self.checkpoints.directory / 'violation.json').read_bytes()
        self.help_delivery()
        with self.assertRaisesRegex(Problem, 'guidance_delivery_timeout'):
            self.checkpoints.finish(413)
        self.assertIsNone(self.checkpoints.guidance_delivery)
        self.assertEqual((self.checkpoints.directory / 'violation.json').read_bytes(), original)

    def test_tampered_final_help_cannot_pass_process_completion(self):
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True}), 0)
        self.checkpoints.observe(111)
        delivery = self.help_delivery()
        self.checkpoints.observe(112)
        (delivery / 'summary.md').write_text(template('help') + '\nChanged after observation')
        with self.assertRaisesRegex(Problem, 'final_help_evidence_changed'):
            self.checkpoints.finish(113)

    def test_guidance_ack_is_bound_duplicate_checked_and_late_ack_preserved(self):
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True}), 0)
        self.checkpoints.observe(111)
        self.checkpoints.tick(112, self.events.append)
        with self.assertRaises(Problem):
            self.checkpoints.response({'id': 'checkpoint-help-0002', 'success': True})
        self.checkpoints.observe(412)
        with self.assertRaisesRegex(Problem, 'Late checkpoint help'):
            self.checkpoints.response({'id': 'checkpoint-help-0001', 'success': True})
        ack_path = self.checkpoints.directory / 'help-acks/0001.json'
        original = ack_path.read_bytes()
        self.assertFalse(json.loads(original)['within_deadline'])
        with self.assertRaises(Problem):
            self.checkpoints.response({'id': 'checkpoint-help-0001', 'success': True})
        self.assertEqual(ack_path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
