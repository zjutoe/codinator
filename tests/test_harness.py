"""Real bound receipts and local fake agents, never real model connectivity."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
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
        return self.deliver_external(m, context)

    def deliver_external(self, m, context):
        task = self.store.get('external')
        (context / 'external-initial-refs.txt').write_text(self.git('show-ref'))
        artifacts = {'before_git': str(context / 'external-initial-refs.txt')}
        (self.workspace / 'product.py').write_text('VALUE = 42\n')
        self.git('add', 'product.py'); self.git('commit', '--allow-empty', '-qm', 'External author implementation')
        sha = self.git('rev-parse', 'HEAD')
        summary = self.base / 'summary.md'
        results = []
        for check in m['checks']:
            result = subprocess.run(check['argv'], cwd=self.workspace, capture_output=True, timeout=check['timeout_seconds'])
            self.assertEqual(result.returncode, 0, result.stderr)
            log = context / (check['name'] + '.author.stdout.log'); log.write_bytes(result.stdout)
            (context / (check['name'] + '.author.stderr.log')).write_bytes(result.stderr)
            artifacts['check_' + check['name'] + '_stdout'] = str(log)
            artifacts['check_' + check['name'] + '_stderr'] = str(context / (check['name'] + '.author.stderr.log'))
            results.append({'name': check['name'], 'argv': check['argv'], 'commit': sha,
                            'status': 'passed', 'exit_code': 0, 'evidence': str(log)})
        after = context / 'external-final-refs.txt'; after.write_text(self.git('show-ref'))
        commands = context / 'external-commands.log'; commands.write_text('Author commit ' + sha + '\nChecks: ' + json.dumps(results))
        artifacts.update(after_git=str(after), commands=str(commands))
        write_json(context / 'author-artifacts.json', artifacts)
        summary.write_text(document('summary', 'External candidate; real author check logs and refs: ' + str(context)))
        packet = {'version': 1, 'task_id': 'external', 'round': task['round'], 'attempt': task['attempt'],
                  'git': {'branch': 'task', 'base_commit': task['expected_digest'], 'commit': sha},
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
        finish(self.engine, 'external', source / 'author-artifacts.json')
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
                finish(self.engine, 'external', source / 'author-artifacts.json')
        self.assertEqual(self.store.get('external')['state'], 'external_implementing')
        self.assertTrue(external_marker(m).exists())
        summary = self.base / 'stop-summary.md'; summary.write_text(document('summary', 'Tools stopped; overdue candidate unaccepted.'))
        stop(self.engine, 'external', summary)
        self.assertFalse(external_marker(m).exists())
        self.assertEqual(self.store.get('external')['state'], 'blocked')
        self.assertTrue(all(p.read_bytes() == data for p, data in original.items()))

    def test_external_tampered_delivery_refuses_review_and_preserves_owner(self):
        m, source, _ = self.external_delivery()
        finish(self.engine, 'external', source / 'author-artifacts.json')
        (source / 'delivery/summary.md').write_text(document('summary', 'Changed after selection'))
        with patch('codinator.engine.reviewer') as reviewer:
            self.engine.run('external')
        reviewer.assert_not_called()
        self.assertEqual(self.store.get('external')['state'], 'blocked')
        self.assertIn('selection_mismatch', self.store.get('external')['reason'].lower())

    def new_fixture(self):
        case = HarnessTests('test_external_prepare_does_not_start_clock_or_dispatch_pi')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_external_start_budget_cannot_be_extended_by_author_file(self):
        m, source, _ = self.external_delivery()
        start = json.loads((source / 'external-start.json').read_text())
        changed = start | {'implementation_deadline': start['implementation_deadline'] + 60}
        (source / 'external-start.json').write_text(json.dumps(changed))
        with patch('codinator.external.time.time', return_value=start['implementation_deadline'] + 1):
            with self.assertRaisesRegex(Problem, 'controller budget record'):
                finish(self.engine, 'external', source / 'author-artifacts.json')
        self.assertEqual(self.store.get('external')['state'], 'external_implementing')
        self.assertTrue(external_marker(m).exists())

    def test_external_preparation_crash_cuts_recover_without_replaying_or_resetting_rounds(self):
        for cut in ('marker', 'before.json', 'external-start.json', 'contract', 'activate'):
            with self.subTest(cut=cut):
                case = self.new_fixture()
                m = case.publish('external', implementation='external',
                                 stage={'id': 'fault', 'max_seconds': 120, 'max_rounds': 2})
                marker = external_marker(m)
                case.addCleanup(lambda: marker.unlink(missing_ok=True))
                import codinator.external as ext
                original_once, original_write, original_update = ext._write_once, ext.write_json, case.store.update
                def once(path, data):
                    result = original_once(path, data)
                    if cut == 'marker' and path == marker:
                        raise SystemExit('crash after lease')
                    return result
                def write(path, data):
                    result = original_write(path, data)
                    if path.name == cut:
                        raise SystemExit('crash after artifact')
                    return result
                def update(task_id, **fields):
                    if cut == 'activate' and fields.get('state') == 'external_implementing':
                        raise SystemExit('crash before handing instructions to host')
                    return original_update(task_id, **fields)
                with patch.object(ext, '_write_once', side_effect=once), \
                     patch.object(ext, 'write_json', side_effect=write), \
                     patch.object(case.store, 'update', side_effect=update), \
                     patch.object(ext, 'prepare_contract', side_effect=SystemExit('crash contract')) if cut == 'contract' else contextlib.nullcontext():
                    with self.assertRaises(SystemExit):
                        begin(case.engine, 'external')
                self.assertEqual(case.store.get('external')['state'], 'external_preparing')
                deadline = case.store.get('external')['deadline']
                case.engine.recover()
                self.assertEqual(case.store.get('external')['state'], 'blocked')
                self.assertFalse(marker.exists())
                self.assertEqual(stage_status(case.store, m)['rounds_used'], 1)
                self.assertEqual(case.store.get('external')['deadline'], deadline)

    def test_external_recovery_keeps_unknown_active_host_but_releases_durable_stop(self):
        m = self.publish('external', implementation='external')
        begin(self.engine, 'external')
        marker = external_marker(m)
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        self.engine.recover()
        self.assertTrue(marker.exists())
        self.assertEqual(self.store.get('external')['state'], 'external_implementing')
        summary = self.base / 'stop.md'; summary.write_text(document('summary', 'All host tools stopped.'))
        unlink = Path.unlink
        def crash(path, *args, **kwargs):
            if path == marker:
                raise SystemExit('crash after durable release')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', crash):
            with self.assertRaises(SystemExit):
                stop(self.engine, 'external', summary)
        self.assertEqual(self.store.get('external')['state'], 'blocked')
        self.assertTrue(marker.exists())
        self.engine.recover()
        self.assertFalse(marker.exists())

    def test_external_raw_inventory_requires_every_file_and_refuses_links(self):
        m, source, _ = self.external_delivery()
        path = source / 'author-artifacts.json'
        original = json.loads(path.read_text())
        for label in original:
            path.write_text(json.dumps({k: v for k, v in original.items() if k != label}))
            with self.assertRaisesRegex(Problem, 'raw artifacts'):
                finish(self.engine, 'external', path)
        path.write_text(json.dumps(original))
        log = Path(original['commands']); data = log.read_bytes(); log.unlink()
        substitute = self.base / 'substitute.log'; substitute.write_bytes(data); log.symlink_to(substitute)
        with self.assertRaisesRegex(Problem, 'unlinked'):
            finish(self.engine, 'external', path)
        self.assertTrue(external_marker(m).exists())

    def test_external_all_raw_evidence_is_pinned_for_review_and_retry(self):
        for label in ('before_git', 'after_git', 'commands', 'check_unit_stdout', 'check_unit_stderr'):
            with self.subTest(label=label):
                case = self.new_fixture()
                _, source, _ = case.external_delivery()
                finish(case.engine, 'external', source / 'author-artifacts.json')
                raw = json.loads((source / 'external-completion.json').read_text())['artifacts']
                Path(raw[label]['path']).write_text('changed after frozen delivery')
                with patch('codinator.engine.reviewer') as reviewer:
                    case.engine.run('external')
                reviewer.assert_not_called()
                self.assertEqual(case.store.get('external')['state'], 'blocked')
                self.assertIn('raw artifact changed', case.store.get('external')['reason'])
                with self.assertRaisesRegex(Problem, 'raw artifact changed'):
                    case.engine.resume('external', review_only=True)

    def test_external_queue_contention_keeps_serve_alive_without_starting_budget(self):
        from codinator.cli import main
        m = self.publish('external', implementation='external')
        begin(self.engine, 'external')
        self.addCleanup(lambda: external_marker(m).unlink(missing_ok=True))
        other_workspace = self.base / 'unrelated-workspace'
        shutil.copytree(self.workspace, other_workspace)
        other = self.manifest | {'id': 'other', 'workspace': str(other_workspace)}
        self.engine.submit(other)
        with patch('codinator.cli.Store', return_value=self.store), \
             patch('codinator.cli.Engine', return_value=self.engine), \
             patch.object(self.engine, '_loop') as dispatch, \
             patch('codinator.cli.time.sleep', side_effect=KeyboardInterrupt):
            self.assertEqual(main(['--state-dir', str(self.store.root), 'serve']), 130)
        dispatch.assert_called_once_with('other')
        queued = self.store.get('test')
        self.assertIsNone(queued['started']); self.assertEqual(queued['attempt'], 0)
        other_store = Store(self.base / 'another-root'); self.addCleanup(other_store.db.close)
        # Insert competing queued task before taking the lease to model existing publication.
        with patch('codinator.stages.require_external_idle'):
            other_store.add(self.manifest, self.manifest['git']['base_commit'])
        competitor = test_engine.Engine(other_store, sandbox=self.engine.sandbox)
        with patch.object(competitor, '_loop') as dispatch:
            competitor.run('test')
        dispatch.assert_not_called()
        self.assertIsNone(other_store.get('test')['started'])
        summary = self.base / 'stop.md'; summary.write_text(document('summary', 'Tools stopped.'))
        stop(self.engine, 'external', summary)
        with patch.object(self.engine, '_loop') as dispatch:
            self.engine.run('test')
        dispatch.assert_called_once_with('test')

    def test_external_rework_passes_bound_feedback_and_preserves_stage_budget(self):
        m, source, _ = self.external_delivery()
        finish(self.engine, 'external', source / 'author-artifacts.json')
        frozen = {p: p.read_bytes() for p in (source / 'delivery').iterdir()}
        with patch.dict(os.environ, {'FAKE_MODE': 'no-progress'}):
            self.engine.run('external')
        task = self.store.get('external')
        self.assertEqual(task['state'], 'external_ready')
        feedback = json.loads(task['feedback'])
        status = report(self.store, task)
        self.assertEqual(status['pending_reply_to'], feedback['message']['message_id'])
        self.assertEqual(status['next_action'], 'begin-external')
        before_deadline = task['deadline']
        receipt = begin(self.engine, 'external')
        instructions = Path(receipt['instructions']).read_text()
        self.assertIn(task['feedback'], instructions)
        self.assertIn('manifest.json', instructions)
        self.assertIn('round 2', instructions)
        self.assertEqual(self.store.get('external')['deadline'], before_deadline)
        self.assertEqual(stage_status(self.store, m)['rounds_used'], 2)
        _, second_source, _ = self.deliver_external(m, self.engine.attempt_path(self.store.get('external')))
        finish(self.engine, 'external', second_source / 'author-artifacts.json')
        self.engine.run('external')
        self.assertEqual(self.store.get('external')['state'], 'accepted')
        self.assertEqual(stage_status(self.store, m)['rounds_used'], 2)
        self.assertTrue(all(p.read_bytes() == data for p, data in frozen.items()))

    def test_external_finish_crash_after_state_commit_releases_only_proven_stopped_lease(self):
        m, source, _ = self.external_delivery()
        marker = external_marker(m)
        unlink = Path.unlink
        def crash(path, *args, **kwargs):
            if path == marker:
                raise SystemExit('crash after review queue transaction')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', crash):
            with self.assertRaises(SystemExit):
                finish(self.engine, 'external', source / 'author-artifacts.json')
        self.assertEqual(self.store.get('external')['state'], 'review_ready')
        self.assertTrue(marker.exists())
        self.engine.recover()
        self.assertFalse(marker.exists())
        self.assertEqual(self.store.get('external')['state'], 'review_ready')
        self.assertEqual(stage_status(self.store, m)['rounds_used'], 1)

    def test_external_hardlinked_raw_log_is_refused_before_review_selection(self):
        m, source, _ = self.external_delivery()
        artifacts = json.loads((source / 'author-artifacts.json').read_text())
        log = Path(artifacts['commands'])
        alias = self.base / 'writable-alias.log'
        os.link(log, alias)
        with self.assertRaisesRegex(Problem, 'hardlink alias'):
            finish(self.engine, 'external', source / 'author-artifacts.json')
        self.assertEqual(self.store.get('external')['state'], 'external_implementing')
        self.assertTrue(external_marker(m).exists())
        self.assertFalse((source / 'delivery-selection.json').exists())

    def test_malformed_owner_is_a_protocol_error_not_silent_queue_wait(self):
        m = self.publish('external', implementation='external')
        begin(self.engine, 'external')
        marker = external_marker(m)
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        original = json.loads(marker.read_text())
        bad = ({}, [], original | {'extra': True}, original | {'task_id': 1},
               original | {'task_id': ''}, original | {'state_dir': 'relative'},
               original | {'state_dir': ''}, original | {'state_dir': 1})
        try:
            for owner in bad:
                with self.subTest(owner=owner):
                    marker.write_text(json.dumps(owner))
                    with self.assertRaisesRegex(Problem, 'Invalid external ownership'):
                        self.engine.run('test')
                    self.assertTrue(marker.exists())
                    self.assertIsNone(self.store.get('test')['started'])
                    self.assertEqual(self.store.get('external')['state'], 'external_implementing')
        finally:
            marker.write_text(json.dumps(original))
