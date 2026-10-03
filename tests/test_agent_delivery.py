"""Agent-owned Git/check claims: format validation only, no Git or check execution."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codinator import delivery as module
from codinator.delivery import (DeliveryError, MAX_BYTES, main, prepare_contract,
                                read_submission, select_delivery, selected_delivery)


class AgentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-delivery-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.attempt = self.root / 'attempt with spaces'
        self.delivery = self.attempt / 'delivery'
        self.delivery.mkdir(parents=True)
        self.task = {'id': 'T', 'round': 2, 'attempt': 3, 'expected_digest': 'b' * 40,
                     'manifest': {'version': 2, 'workspace': str(self.root / 'not-a-repository'),
                         'git': {'branch': 'task/source', 'base_commit': 'a' * 40},
                         'checks': [{'name': 'unit', 'argv': ['python', '-B', '-m', 'unittest'],
                                     'timeout_seconds': 20},
                                    {'name': 'lint', 'argv': ['python', 'lint.py'], 'timeout_seconds': 10}]}}
        self.command = prepare_contract(self.task, self.attempt, self.delivery)
        self.contract = self.attempt / 'delivery-contract.json'
        self.summary = self.root / 'summary.md'
        self.summary.write_text('Agent commit and check claims; independent review remains required.\n')
        self.input = self.root / 'agent evidence.json'
        self.packet = {'version': 1, 'task_id': 'T', 'round': 2, 'attempt': 3,
            'git': {'branch': 'task/source', 'base_commit': 'b' * 40, 'commit': 'c' * 40},
            'checks': [{'name': value['name'], 'argv': value['argv'], 'commit': 'c' * 40,
                        'status': 'passed', 'exit_code': 0,
                        'evidence': f'Pi raw tool events: {value["name"]} command and exit code'}
                       for value in self.task['manifest']['checks']]}
        self.input.write_text(json.dumps(self.packet))

    def submit(self, *, evidence=True, status='awaiting_review'):
        argv = ['--summary', str(self.summary), '--status', status]
        if evidence:
            argv += ['--evidence', str(self.input)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv, contract_path=self.contract)
        return code, stdout.getvalue(), stderr.getvalue()

    def write_delivery(self, packet, *, status='awaiting_review'):
        (self.delivery / 'summary.md').write_bytes(self.summary.read_bytes())
        (self.delivery / 'completion.json').write_text(json.dumps({
            'task_id': 'T', 'round': 2, 'attempt': 3, 'status': status}))
        if packet is not None:
            (self.delivery / 'evidence.json').write_text(json.dumps(packet))

    def rejected(self, packet, *, repairable=None):
        self.write_delivery(packet)
        with self.assertRaises(DeliveryError) as caught:
            read_submission(self.delivery, self.task)
        if repairable is not None:
            self.assertEqual(caught.exception.repairable, repairable)
        return caught.exception

    def files(self):
        return {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                for p in self.delivery.iterdir()}

    def test_contract_binds_current_attempt_base_and_original_checks(self):
        value = json.loads(self.contract.read_text())
        self.assertEqual(value['protocol'], 'agent_git_v1')
        self.assertEqual(value['git'], {'branch': 'task/source', 'base_commit': 'b' * 40})
        self.assertEqual(value['checks'], self.task['manifest']['checks'])
        self.assertEqual((value['task_id'], value['round'], value['attempt']), ('T', 2, 3))

    def test_receipt_copies_claims_without_running_git_or_checks(self):
        with patch('subprocess.run', side_effect=AssertionError('Receiver executed a process')), \
             patch('os.system', side_effect=AssertionError('Receiver executed a shell')):
            code, output, error = self.submit()
            self.assertEqual(code, 0, error)
            self.assertEqual(read_submission(self.delivery, self.task), self.packet)
        self.assertEqual(json.loads(output)['status'], 'submitted')
        self.assertEqual((self.delivery / 'evidence.json').read_bytes(), self.input.read_bytes())
        self.assertFalse(Path(self.task['manifest']['workspace']).exists())

    def test_bound_launcher_accepts_evidence_and_rejects_identity_override(self):
        argv = shlex.split(self.command)
        result = subprocess.run(argv + ['--summary', str(self.summary), '--status', 'awaiting_review',
            '--evidence', str(self.input)], capture_output=True, text=True, env={'PATH': os.defpath}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_submission(self.delivery, self.task), self.packet)
        result = subprocess.run(argv + ['--summary', str(self.summary), '--status', 'awaiting_review',
            '--evidence', str(self.input), '--commit', 'd' * 40], capture_output=True, text=True,
            env={'PATH': os.defpath}, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unrecognized arguments', result.stderr)

    def test_missing_evidence_cannot_finish_v2_awaiting_review(self):
        code, _, error = self.submit(evidence=False)
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(error)['repairable'])
        self.assertEqual(self.files(), {})
        self.write_delivery(None)
        with self.assertRaises(DeliveryError):
            read_submission(self.delivery, self.task)

    def test_blocked_without_evidence_keeps_no_invented_commit(self):
        code, output, error = self.submit(evidence=False, status='blocked')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['disposition'], 'blocked')
        self.assertIsNone(read_submission(self.delivery, self.task))
        self.assertFalse((self.delivery / 'evidence.json').exists())
        with self.assertRaises(DeliveryError):
            select_delivery(self.attempt, self.delivery)

    def test_blocked_with_evidence_is_validated_and_not_auto_repaired(self):
        self.input.write_text('{"version":')
        code, _, error = self.submit(status='blocked')
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(error)['repairable'])
        self.assertEqual(self.files(), {})

    def test_no_change_commit_may_equal_baseline(self):
        packet = copy.deepcopy(self.packet)
        packet['git']['commit'] = packet['git']['base_commit']
        for check in packet['checks']:
            check['commit'] = packet['git']['commit']
        self.write_delivery(packet)
        self.assertEqual(read_submission(self.delivery, self.task), packet)

    def test_sha256_commit_format_is_supported(self):
        packet = copy.deepcopy(self.packet)
        packet['git']['commit'] = 'd' * 64
        for check in packet['checks']:
            check['commit'] = packet['git']['commit']
        self.write_delivery(packet)
        self.assertEqual(read_submission(self.delivery, self.task), packet)

    def test_identity_baseline_branch_and_check_commit_mismatch_are_not_fact_repairs(self):
        for field, value in (('task_id', 'OTHER'), ('round', 99), ('attempt', 4)):
            with self.subTest(field=field):
                error = self.rejected(self.packet | {field: value}, repairable=False)
                self.assertTrue(any(item['code'] == 'binding_mismatch' for item in error.errors))
        for field, value in (('base_commit', 'a' * 40), ('branch', 'other')):
            with self.subTest(field=field):
                packet = copy.deepcopy(self.packet)
                packet['git'][field] = value
                self.rejected(packet, repairable=False)
        packet = copy.deepcopy(self.packet)
        packet['checks'][0]['commit'] = 'd' * 40
        self.rejected(packet, repairable=False)

    def test_exact_argv_binding_cannot_be_repaired_into_unperformed_command(self):
        packet = copy.deepcopy(self.packet)
        packet['checks'][0]['argv'] = ['true']
        error = self.rejected(packet, repairable=False)
        self.assertTrue(any(item['path'].endswith('.argv') for item in error.errors))

    def test_boolean_integer_fields_are_rejected(self):
        for field in ('version', 'round', 'attempt'):
            with self.subTest(field=field):
                self.rejected(self.packet | {field: True}, repairable=True)
        packet = copy.deepcopy(self.packet)
        packet['checks'][0]['exit_code'] = False
        self.rejected(packet, repairable=True)

    def test_short_uppercase_or_nonstring_sha_is_rejected(self):
        for value in ('abc123', 'C' * 40, 123, None, 'c' * 41):
            with self.subTest(value=value):
                packet = copy.deepcopy(self.packet)
                packet['git']['commit'] = value
                self.rejected(packet, repairable=True)

    def test_schema_extra_missing_and_duplicate_checks(self):
        packet = copy.deepcopy(self.packet)
        del packet['git']['commit']
        self.rejected(packet, repairable=True)
        self.rejected(self.packet | {'extra': 'not allowed'}, repairable=True)
        packet = copy.deepcopy(self.packet)
        packet['checks'].append(copy.deepcopy(packet['checks'][0]))
        error = self.rejected(packet, repairable=True)
        self.assertIn('duplicate_check', [item['code'] for item in error.errors])
        packet = copy.deepcopy(self.packet)
        packet['checks'].pop()
        error = self.rejected(packet, repairable=True)
        self.assertIn('missing_check', [item['code'] for item in error.errors])
        packet['checks'].append(copy.deepcopy(packet['checks'][0]) | {'name': 'invented'})
        self.rejected(packet, repairable=True)

    def test_malformed_nested_types_raise_structured_errors(self):
        cases = [self.packet | {'checks': {}}, self.packet | {'git': []},
                 self.packet | {'checks': [None]}, self.packet | {'checks': [{'name': []}]}]
        for packet in cases:
            with self.subTest(packet=packet):
                self.rejected(packet, repairable=True)

    def test_pass_fail_not_run_exit_codes_are_strict_without_certifying_success(self):
        packet = copy.deepcopy(self.packet)
        packet['checks'][0].update(status='failed', exit_code=7)
        packet['checks'][1].update(status='not_run', exit_code=None, evidence='Not run: unavailable dependency')
        self.write_delivery(packet)
        self.assertEqual(read_submission(self.delivery, self.task), packet)
        for status, code in (('passed', 1), ('passed', None), ('failed', 0), ('failed', None), ('not_run', 0)):
            with self.subTest(status=status, code=code):
                packet = copy.deepcopy(self.packet)
                packet['checks'][0].update(status=status, exit_code=code)
                self.rejected(packet, repairable=False)

    def test_empty_evidence_reference_is_rejected(self):
        packet = copy.deepcopy(self.packet)
        packet['checks'][0]['evidence'] = ' \n'
        self.rejected(packet, repairable=True)

    def test_duplicate_json_keys_are_rejected(self):
        self.write_delivery(self.packet)
        target = self.delivery / 'evidence.json'
        target.write_text(json.dumps(self.packet)[:-1] + ', "attempt": 3}')
        with self.assertRaises(DeliveryError) as caught:
            read_submission(self.delivery, self.task)
        self.assertIn('duplicate_key', [item['code'] for item in caught.exception.errors])

    def test_evidence_safety_boundaries_are_not_repairable(self):
        self.write_delivery(None)
        target = self.delivery / 'evidence.json'
        target.symlink_to(self.input)
        with self.assertRaises(DeliveryError) as caught:
            read_submission(self.delivery, self.task)
        self.assertFalse(caught.exception.repairable)
        target.unlink()
        target.write_bytes(b'x' * (MAX_BYTES + 1))
        with self.assertRaises(DeliveryError) as caught:
            read_submission(self.delivery, self.task)
        self.assertFalse(caught.exception.repairable)

    def test_idempotent_triplet_preserves_bytes_inodes_and_mtime(self):
        self.assertEqual(self.submit()[0], 0)
        original = self.files()
        self.assertEqual(self.submit()[0], 0)
        self.assertEqual(self.files(), original)

    def test_conflicting_evidence_cannot_replace_published_bytes(self):
        self.assertEqual(self.submit()[0], 0)
        original = self.files()
        packet = copy.deepcopy(self.packet)
        packet['checks'][0]['evidence'] = 'Different source claim'
        self.input.write_text(json.dumps(packet))
        self.assertEqual(self.submit()[0], 2)
        self.assertEqual(self.files(), original)

    def interrupted(self, filename):
        original = module._write_once
        def failure(path, data):
            if path.name == filename:
                raise OSError('injected publication interruption')
            return original(path, data)
        return patch.object(module, '_write_once', side_effect=failure)

    def test_summary_then_evidence_then_completion_interruption_resumes(self):
        with self.interrupted('evidence.json'):
            self.assertEqual(self.submit()[0], 2)
        self.assertEqual(set(self.files()), {'summary.md'})
        summary = self.files()['summary.md']
        with self.interrupted('completion.json'):
            self.assertEqual(self.submit()[0], 2)
        self.assertEqual(set(self.files()), {'summary.md', 'evidence.json'})
        evidence = self.files()['evidence.json']
        self.assertEqual(self.submit(evidence=False)[0], 0)
        self.assertEqual(self.files()['summary.md'], summary)
        self.assertEqual(self.files()['evidence.json'], evidence)

    def test_completed_but_missing_evidence_cannot_be_retroactively_filled(self):
        self.write_delivery(None)
        original = self.files()
        self.assertEqual(self.submit()[0], 2)
        self.assertEqual(self.files(), original)

    def test_existing_invalid_evidence_is_not_overwritten(self):
        (self.delivery / 'evidence.json').write_text('{invalid')
        original = self.files()
        self.assertEqual(self.submit()[0], 2)
        self.assertEqual(self.files(), original)

    def test_selection_binds_third_file_and_rejects_tampering(self):
        self.assertEqual(self.submit()[0], 0)
        select_delivery(self.attempt, self.delivery)
        selection = json.loads((self.attempt / 'delivery-selection.json').read_text())
        self.assertEqual(selection['version'], 2)
        self.assertEqual(set(selection['files']), {'summary.md', 'completion.json', 'evidence.json'})
        self.assertEqual(selected_delivery(self.attempt), self.delivery)
        (self.delivery / 'evidence.json').write_text(json.dumps(self.packet) + '\n')
        with self.assertRaises(DeliveryError) as caught:
            selected_delivery(self.attempt)
        self.assertEqual(caught.exception.errors[0]['code'], 'selection_mismatch')

    def test_selection_cannot_downgrade_to_missing_third_file(self):
        self.assertEqual(self.submit()[0], 0)
        select_delivery(self.attempt, self.delivery)
        path = self.attempt / 'delivery-selection.json'
        selection = json.loads(path.read_text())
        del selection['files']['evidence.json']
        (self.delivery / 'evidence.json').unlink()
        for version in (1, 2):
            with self.subTest(version=version):
                path.write_text(json.dumps(selection | {'version': version}))
                with self.assertRaises(DeliveryError):
                    selected_delivery(self.attempt)
        with self.assertRaises(DeliveryError):
            read_submission(self.delivery, self.task)

    def test_repair_selection_requires_same_agent_contract(self):
        repair = self.attempt / 'delivery-repair'
        repaired_delivery = repair / 'delivery'
        repaired_delivery.mkdir(parents=True)
        prepare_contract(self.task, repair, repaired_delivery)
        self.write_delivery(self.packet)
        for name in ('summary.md', 'completion.json', 'evidence.json'):
            (repaired_delivery / name).write_bytes((self.delivery / name).read_bytes())
        select_delivery(self.attempt, repaired_delivery)
        self.assertEqual(selected_delivery(self.attempt), repaired_delivery)
        contract = json.loads((repair / 'delivery-contract.json').read_text())
        contract['git']['base_commit'] = 'a' * 40
        (repair / 'delivery-contract.json').write_text(json.dumps(contract))
        with self.assertRaises(DeliveryError):
            selected_delivery(self.attempt)

    def test_legacy_two_file_reading_and_selection_remain_supported(self):
        legacy = self.root / 'legacy'
        target = legacy / 'delivery'
        target.mkdir(parents=True)
        task = {key: self.task[key] for key in ('id', 'round', 'attempt')}
        prepare_contract(task, legacy, target)
        self.write_delivery(None)
        for name in ('summary.md', 'completion.json'):
            (target / name).write_bytes((self.delivery / name).read_bytes())
        self.assertIsNone(read_submission(target, task))
        select_delivery(legacy, target)
        self.assertEqual(selected_delivery(legacy), target)
        selection = json.loads((legacy / 'delivery-selection.json').read_text())
        self.assertEqual(selection['version'], 1)
        self.assertEqual(set(selection['files']), {'summary.md', 'completion.json'})


if __name__ == '__main__':
    unittest.main()
