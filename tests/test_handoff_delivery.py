"""Format-only receipts bind immutable documents, not semantic truth."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codinator.cli import main as cli
from codinator.delivery import (DeliveryError, main, prepare_contract, read_submission,
                                select_delivery, selected_delivery, validate_delivery)
from codinator.files import write_json
from codinator.handoff import SECTIONS, freeze_protocol, template


def document(kind, body='无'):
    return '\n\n'.join(f'## {heading}\n{body}' for heading in SECTIONS[kind]) + '\n'


class HandoffDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='handoff-receipts-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task_dir = self.root / 'T'
        self.task_dir.mkdir()
        self.task = {'id': 'T', 'round': 2, 'attempt': 3, 'expected_digest': 'b' * 40,
                     'feedback': '', 'manifest': {'version': 2, 'id': 'T', 'handoff_protocol': 1,
                         'workspace': str(self.root / 'no-repository'),
                         'git': {'branch': 'task', 'base_commit': 'a' * 40},
                         'checks': [{'name': 'unit', 'argv': ['python', '-B', '-m', 'unittest'],
                                     'timeout_seconds': 20}]}}
        write_json(self.task_dir / 'manifest.json', self.task['manifest'])
        (self.task_dir / 'handoff.md').write_text(template('handoff'))
        self.record = freeze_protocol(self.task_dir, self.task['manifest'])
        self.attempt = self.task_dir / 'attempt-0003'
        self.delivery = self.attempt / 'delivery'
        self.delivery.mkdir(parents=True)
        prepare_contract(self.task, self.attempt, self.delivery)
        self.summary = self.root / 'input.md'
        self.summary.write_text(document('summary', 'everything done; please improve'))
        self.packet = {'version': 1, 'task_id': 'T', 'round': 2, 'attempt': 3,
                       'git': {'branch': 'task', 'base_commit': 'b' * 40, 'commit': 'c' * 40},
                       'checks': [{'name': 'unit', 'argv': ['python', '-B', '-m', 'unittest'],
                                   'commit': 'c' * 40, 'status': 'not_run', 'exit_code': None,
                                   'evidence': '未执行'}]}
        self.evidence = self.root / 'input.json'
        self.evidence.write_text(json.dumps(self.packet))

    def submit(self, status='awaiting_review', *, packet=True):
        args = ['--summary', str(self.summary), '--status', status]
        if packet:
            args += ['--evidence', str(self.evidence)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(args, contract_path=self.attempt / 'delivery-contract.json')
        return result, out.getvalue(), err.getvalue()

    def completion(self):
        return json.loads((self.delivery / 'completion.json').read_text())

    def test_vague_text_and_unperformed_checks_pass_format_without_running_commands(self):
        with patch('subprocess.run', side_effect=AssertionError('controller ran project commands')):
            self.assertEqual(self.submit()[0], 0)
            self.assertEqual(read_submission(self.delivery, self.task), self.packet)
        message = self.completion()['message']
        self.assertEqual(message['message_id'], 'T:2:3:summary')
        self.assertEqual(message['reply_to'], 'T:handoff')
        self.assertEqual(message['contract_digest'], self.record['contract_digest'])
        self.assertEqual(message['submission_digest'], 'c' * 40)
        self.assertEqual((message['author'], message['recipient']), ('pi', 'controller'))
        self.assertFalse(Path(self.task['manifest']['workspace']).exists())

    def test_help_is_explicit_control_and_binds_partial_candidate(self):
        self.summary.write_text(document('help', 'None'))
        code, _, error = self.submit('needs_guidance')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.completion()['message']['kind'], 'help')
        self.assertEqual(validate_delivery(self.delivery, 'T', 2, 3), 'needs_guidance')
        self.assertEqual(read_submission(self.delivery, self.task), self.packet)

    def test_help_requires_its_sections_and_candidate_packet(self):
        self.assertEqual(self.submit('needs_guidance')[0], 2)
        self.assertEqual(list(self.delivery.iterdir()), [])
        self.summary.write_text(document('help'))
        self.assertEqual(self.submit('needs_guidance', packet=False)[0], 2)
        self.assertEqual(list(self.delivery.iterdir()), [])

    def test_nonempty_free_prose_cannot_replace_required_summary_sections(self):
        self.summary.write_text('Done. All checks passed.')
        code, _, error = self.submit()
        self.assertEqual(code, 2)
        details = json.loads(error)
        self.assertTrue(details['repairable'])
        self.assertTrue(all(item['code'] == 'missing_section' for item in details['errors']))
        self.assertEqual(list(self.delivery.iterdir()), [])

    def test_blocked_can_have_no_candidate_without_fabrication(self):
        self.assertEqual(self.submit('blocked', packet=False)[0], 0)
        self.assertIsNone(read_submission(self.delivery, self.task))
        self.assertIsNone(self.completion()['message']['submission_digest'])

    def test_submission_is_idempotent_and_conflicting_body_does_not_overwrite(self):
        self.assertEqual(self.submit()[0], 0)
        before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in self.delivery.iterdir()}
        self.assertEqual(self.submit()[0], 0)
        self.summary.write_text(document('summary', 'different claim'))
        self.assertEqual(self.submit()[0], 2)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_ino) for p in self.delivery.iterdir()}, before)

    def test_task_attempt_candidate_contract_and_reply_tampering_are_not_repairable(self):
        self.assertEqual(self.submit()[0], 0)
        original = self.completion()
        for key, value in (('attempt', 4), ('round', True), ('task_id', 'other'),
                           ('submission_digest', 'd' * 40), ('contract_digest', 'e' * 64),
                           ('reply_to', 'old-request'), ('author', 'review_codex')):
            with self.subTest(key=key):
                changed = copy.deepcopy(original)
                changed['message'][key] = value
                (self.delivery / 'completion.json').write_text(json.dumps(changed))
                with self.assertRaises(DeliveryError) as error:
                    read_submission(self.delivery, self.task)
                self.assertFalse(error.exception.repairable)

    def test_frozen_template_tampering_rejects_submission_before_publication(self):
        (self.task_dir / 'templates/help.md').write_text(document('help', 'changed'))
        code, _, error = self.submit()
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(error)['repairable'])
        self.assertEqual(list(self.delivery.iterdir()), [])

    def test_new_task_cannot_downgrade_its_bound_contract(self):
        self.assertEqual(self.submit()[0], 0)
        path = self.attempt / 'delivery-contract.json'
        contract = json.loads(path.read_text())
        del contract['handoff']
        path.write_text(json.dumps(contract))
        with self.assertRaises(DeliveryError):
            read_submission(self.delivery, self.task)

    def test_repair_keeps_exact_original_protocol_and_reply_binding(self):
        self.assertEqual(self.submit()[0], 0)
        repair = self.attempt / 'delivery-repair'
        target = repair / 'delivery'
        target.mkdir(parents=True)
        prepare_contract(self.task, repair, target, task_dir=self.task_dir)
        for p in self.delivery.iterdir():
            (target / p.name).write_bytes(p.read_bytes())
        select_delivery(self.attempt, target)
        self.assertEqual(selected_delivery(self.attempt), target)
        path = repair / 'delivery-contract.json'
        contract = json.loads(path.read_text())
        contract['handoff']['reply_to'] = 'unrelated'
        path.write_text(json.dumps(contract))
        with self.assertRaises(DeliveryError):
            selected_delivery(self.attempt)

    def test_template_cli_is_read_only_even_when_state_directory_does_not_exist(self):
        state = self.root / 'absent-state'
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('codinator.cli.Store', side_effect=AssertionError('state opened')):
            self.assertEqual(cli(['--state-dir', str(state), 'template', 'guidance']), 0)
        self.assertEqual(out.getvalue(), template('guidance'))
        self.assertFalse(state.exists())
