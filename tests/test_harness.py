"""Real bound receipts and local fake agents, never real model connectivity."""
import contextlib
import io
import json
import time
import subprocess
import unittest
from unittest.mock import patch

import test_engine
from test_handoff_delivery import document
from codinator.checkpoints import Checkpoints, main as progress, status as checkpoint_status
from codinator.config import load_manifest
from codinator.delivery import main as delivery
from codinator.external import begin, finish, stop
from codinator.files import Problem, write_json
from codinator.handoff import template
from codinator.stages import external_marker, stage_status
from codinator.status import report
from codinator.store import Store


class HarnessTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def publish(self, task_id, **options):
        # Fixtures prepare Git before publication; production controller never does so.
        (self.workspace / 'handoff.md').write_text(template('handoff'))
        self.git('add', 'handoff.md')
        self.git('commit', '--allow-empty', '-qm', 'Prepare frozen protocol fixture')
        current = self.git('rev-parse', 'HEAD')
        raw = self.raw | {'id': task_id, 'handoff_protocol': 1,
                          'git': {'branch': 'task', 'base_commit': current}} | options
        path = self.base / (task_id + '.json')
        path.write_text(json.dumps(raw))
        m = load_manifest(path)
        self.engine.submit(m)
        return m

    def compact(self):
        m = self.publish('compact', checkpoint_seconds=10, checkpoint_format='compact',
                         first_checkpoint='A real boundary test passes; cite command/exit/log.',
                         counterexamples=['Valid input that exceeds the authorized date range.'])
        context = self.base / 'compact-attempt'
        context.mkdir()
        task = self.store.get(m['id']) | {'attempt': 1}
        cp = Checkpoints(task, context, 10, timeout=500)
        cp.start(100)
        sent = []
        cp.tick(110, sent.append)
        return cp, sent

    def submit_progress(self, cp, value):
        path = self.base / 'progress.json'
        path.write_text(json.dumps(value))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return progress(['--request', '1', '--report', str(path)],
                            contract_path=cp.context / 'checkpoint-contract.json')

    def test_compact_receipt_needs_no_markdown_and_preserves_claims(self):
        cp, sent = self.compact()
        claims = {'completed': ['reading only; behavioral goal not yet met'], 'checks': [],
                  'blockers': [], 'next_step': 'write one real boundary test',
                  'needs_guidance': False, 'candidate_commit': None}
        self.assertEqual(self.submit_progress(cp, claims), 0)
        cp.observe(111)
        cp.response({'id': 'checkpoint-0001', 'success': True})
        with patch('codinator.checkpoints.time.monotonic', return_value=112):
            cp.message_observed({'type': 'message_end', 'message': {'role': 'user',
                'content': [{'type': 'text', 'text': sent[0]['message']}]}})
        result = checkpoint_status(cp.context, 'compact', 1)
        self.assertEqual(result['latest_report']['progress'], claims)
        self.assertTrue(result['latest_request']['agent_message_observed'])
        self.assertEqual(result['latest_request']['agent_message_latency_seconds'], 2)
        self.assertEqual(result['latest_request']['report_latency_seconds'], 1)
        self.assertIn('not acceptance', (cp.directory / 'stage-summary-0001.md').read_text())
        (cp.directory / 'stage-summary-0001.md').write_text('substituted')
        cp.observe(113)
        self.assertEqual(cp.violation['reason'], 'checkpoint_evidence_changed')

    def test_compact_invalid_candidate_and_late_report_do_not_avoid_stop(self):
        cp, _ = self.compact()
        value = {'completed': [], 'checks': [], 'blockers': [], 'next_step': 'wait',
                 'needs_guidance': False, 'candidate_commit': 'moving-branch'}
        self.assertEqual(self.submit_progress(cp, value), 2)
        cp.observe(411)
        value['candidate_commit'] = None
        self.assertEqual(self.submit_progress(cp, value), 0)
        with self.assertRaisesRegex(Problem, 'response_timeout'):
            cp.enforce(412, set())
        self.assertFalse(cp.resolutions)

    def test_compact_requires_concrete_goal_and_counterexamples(self):
        for changes in ({'first_checkpoint': ''}, {'counterexamples': []},
                        {'checkpoint_seconds': None}):
            with self.subTest(changes=changes):
                raw = self.raw | {'checkpoint_format': 'compact', 'handoff_protocol': 1,
                    'checkpoint_seconds': 10, 'first_checkpoint': 'prove boundary',
                    'counterexamples': ['wide valid receipt']} | changes
                path = self.base / 'invalid.json'
                path.write_text(json.dumps(raw))
                # Validation rejects before controller dispatch; headings aren't the claim.
                with self.assertRaises(Problem):
                    load_manifest(path)

    def test_stage_shares_dispatch_rounds_and_fixed_deadline_across_ids(self):
        spec = {'id': 'stage', 'max_seconds': 60, 'max_rounds': 2}
        first = self.publish('one', stage=spec)
        self.publish('two', stage=spec)
        self.publish('three', stage=spec)
        with patch('codinator.engine.time.time', return_value=1900000000), \
             patch('codinator.engine.worker', side_effect=Problem('startup failure')):
            self.engine.run('one')
        self.assertEqual(stage_status(self.store, first)['rounds_used'], 1)
        with patch('codinator.engine.time.time', return_value=1900000030), \
             patch('codinator.engine.worker', side_effect=Problem('second failed dispatch')):
            self.engine.run('two')
        self.assertEqual(self.store.get('two')['deadline'], 1900000060)
        self.assertEqual(stage_status(self.store, first)['rounds_remaining'], 0)
        with patch('codinator.engine.time.time', return_value=1900000031), \
             patch('codinator.engine.worker') as worker:
            self.engine.run('three')
        worker.assert_not_called()
        self.assertEqual(self.store.get('three')['attempt'], 0)
        self.assertIn('Shared stage', self.store.get('three')['reason'])
        with self.assertRaisesRegex(Problem, 'conflicts'):
            self.publish('extended', stage=spec | {'max_rounds': 3})

    def test_external_prepare_does_not_start_clock_or_dispatch_pi(self):
        m = self.publish('external', implementation='external',
                         stage={'id': 'external-stage', 'max_seconds': 120, 'max_rounds': 2})
        self.assertEqual(self.store.get('external')['state'], 'external_ready')
        self.assertIsNone(self.store.get('external')['started'])
        with patch('codinator.engine.worker') as worker:
            receipt = begin(self.engine, 'external')
        worker.assert_not_called()
        self.assertTrue(external_marker(m).is_file())
        self.addCleanup(lambda: external_marker(m).unlink(missing_ok=True))
        with self.assertRaisesRegex(Problem, 'stop its tools'):
            self.store.request_control('external', 'pause')
        other = Store(self.base / 'other-state')
        self.addCleanup(other.db.close)
        with self.assertRaisesRegex(Problem, 'another task/state'):
            other.add(m | {'id': 'competitor'}, m['git']['base_commit'])
        self.assertEqual(stage_status(self.store, m)['rounds_used'], 1)
        self.assertEqual(report(self.store, self.store.get('external'))['current_handler'], 'main_codex')
        self.assertIn('bound_delivery_command', receipt)

    def external_delivery(self):
        m = self.publish('external', implementation='external',
                         stage={'id': 'external-stage', 'max_seconds': 120, 'max_rounds': 2})
        receipt = begin(self.engine, 'external')
        self.addCleanup(lambda: external_marker(m).unlink(missing_ok=True))
        task = self.store.get('external')
        context = self.engine.attempt_path(task)
        (context / 'external-initial-refs.txt').write_text(self.git('show-ref'))
        (self.workspace / 'product.py').write_text('VALUE = 42\n')
        self.git('add', 'product.py'); self.git('commit', '-qm', 'External author implementation')
        sha = self.git('rev-parse', 'HEAD')
        summary = self.base / 'summary.md'
        results = []
        for check in m['checks']:
            result = subprocess.run(check['argv'], cwd=self.workspace, capture_output=True, timeout=check['timeout_seconds'])
            self.assertEqual(result.returncode, 0, result.stderr)
            log = context / (check['name'] + '.author.stdout.log'); log.write_bytes(result.stdout)
            (context / (check['name'] + '.author.stderr.log')).write_bytes(result.stderr)
            results.append({'name': check['name'], 'argv': check['argv'], 'commit': sha,
                            'status': 'passed', 'exit_code': 0, 'evidence': str(log)})
        summary.write_text(document('summary', 'External candidate; real author check logs and refs: ' + str(context)))
        packet = {'version': 1, 'task_id': 'external', 'round': task['round'], 'attempt': task['attempt'],
                  'git': {'branch': 'task', 'base_commit': m['git']['base_commit'], 'commit': sha},
                  'checks': results}
        evidence = self.base / 'evidence.json'; evidence.write_text(json.dumps(packet))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = delivery(['--summary', str(summary), '--status', 'awaiting_review',
                             '--evidence', str(evidence)], contract_path=context / 'delivery-contract.json')
        self.assertEqual(code, 0)
        return m, context, sha

    def test_external_delivery_enters_real_fake_reviewer_without_pi_or_extra_round(self):
        m, source, sha = self.external_delivery()
        self.assertEqual(json.loads((source / 'delivery/completion.json').read_text())['message']['author'], 'main_codex')
        finish(self.engine, 'external')
        self.assertEqual(self.store.get('external')['state'], 'review_ready')
        self.assertFalse(external_marker(m).exists())
        self.assertFalse((source / 'pi').exists())
        with patch('codinator.engine.worker', side_effect=AssertionError('Pi dispatched for external review')):
            self.engine.run('external')
        task = self.store.get('external')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual(task['expected_digest'], sha)
        self.assertEqual(stage_status(self.store, m)['rounds_used'], 1)
        review = self.engine.attempt_path(task)
        self.assertTrue((review / 'codex/result.json').exists())
        self.assertIn('External main Codex', (review / 'review-prompt.txt').read_text())
        self.assertEqual(report(self.store, task)['stage']['members'][0]['state'], 'accepted')

    def test_external_overdue_packet_kept_and_explicit_stop_releases_checkout(self):
        m, source, _ = self.external_delivery()
        original = {p: p.read_bytes() for p in (source / 'delivery').iterdir()}
        start = json.loads((source / 'external-start.json').read_text())
        with patch('codinator.external.time.time', return_value=start['implementation_deadline'] + 1):
            with self.assertRaisesRegex(Problem, 'budget exhausted'):
                finish(self.engine, 'external')
        self.assertEqual(self.store.get('external')['state'], 'external_implementing')
        self.assertTrue(external_marker(m).exists())
        summary = self.base / 'stop-summary.md'; summary.write_text(document('summary', 'Tools stopped; overdue candidate unaccepted.'))
        stop(self.engine, 'external', summary)
        self.assertFalse(external_marker(m).exists())
        self.assertEqual(self.store.get('external')['state'], 'blocked')
        self.assertTrue(all(p.read_bytes() == data for p, data in original.items()))

    def test_external_tampered_delivery_refuses_review_and_preserves_owner(self):
        m, source, _ = self.external_delivery()
        finish(self.engine, 'external')
        (source / 'delivery/summary.md').write_text(document('summary', 'Changed after selection'))
        with patch('codinator.engine.reviewer') as reviewer:
            self.engine.run('external')
        reviewer.assert_not_called()
        self.assertEqual(self.store.get('external')['state'], 'blocked')
        self.assertIn('changed', self.store.get('external')['reason'].lower())
