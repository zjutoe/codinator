import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codinator.delivery import (DeliveryError, MAX_BYTES, main, prepare_contract,
                                select_delivery, selected_delivery, validate_delivery)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codinator-delivery-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.attempt = self.base / 'attempt with spaces'
        self.attempt.mkdir()
        self.delivery = self.attempt / 'delivery'
        self.delivery.mkdir()
        self.task = {'id': 'MF-001-R2', 'round': 1, 'attempt': 2}
        self.completion = {'task_id': 'MF-001-R2', 'round': 1, 'attempt': 2,
                           'status': 'awaiting_review'}
        self.summary = self.base / 'summary input.md'
        self.summary.write_text('Fixed implementation; focused checks passed.\n')

    def write_delivery(self, *, completion=None, directory=None):
        directory = self.delivery if directory is None else directory
        (directory / 'summary.md').write_bytes(self.summary.read_bytes())
        (directory / 'completion.json').write_text(json.dumps(
            self.completion if completion is None else completion))

    def validate(self, directory=None):
        return validate_delivery(self.delivery if directory is None else directory, 'MF-001-R2', 1, 2)

    def contract(self):
        return prepare_contract(self.task, self.attempt, self.delivery)

    def submit(self, status='awaiting_review'):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--summary', str(self.summary), '--status', status],
                        contract_path=self.attempt / 'delivery-contract.json')
        return code, stdout.getvalue(), stderr.getvalue()

    def test_r1_wrong_directory_is_not_searched_or_adopted(self):
        wrong = self.base / 'workspace' / 'delivery'
        wrong.mkdir(parents=True)
        self.write_delivery(directory=wrong)
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        error = caught.exception
        self.assertTrue(error.repairable)
        self.assertEqual([item['code'] for item in error.errors], ['missing', 'missing'])
        self.assertTrue(all(str(self.delivery) in item['path'] for item in error.errors))
        self.assertEqual(list(self.delivery.iterdir()), [])

    def test_r2_lists_every_incorrect_identity_status_and_extra_field(self):
        self.write_delivery(completion=self.completion | {
            'task_id': 'MF-001', 'round': 2, 'status': 'completed', 'role': 'implementer'})
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        error = caught.exception
        self.assertTrue(error.repairable)
        by_field = {item['path'].rsplit('.', 1)[-1]: item for item in error.errors}
        self.assertEqual(set(by_field), {'task_id', 'round', 'status', 'role'})
        self.assertEqual(by_field['task_id']['expected'], 'MF-001-R2')
        self.assertEqual(by_field['task_id']['actual'], 'MF-001')
        self.assertEqual(by_field['round']['expected'], 1)
        self.assertEqual(by_field['round']['actual'], 2)
        self.assertEqual(by_field['role']['code'], 'unexpected_field')
        self.assertIn('awaiting_review', str(error))
        self.assertIn('completed', str(error))

    def test_valid_dispositions(self):
        for status in ('awaiting_review', 'blocked'):
            with self.subTest(status=status):
                self.write_delivery(completion=self.completion | {'status': status})
                self.assertEqual(self.validate(), status)

    def test_boolean_integer_and_missing_field_are_rejected(self):
        for field in ('round', 'attempt'):
            with self.subTest(field=field):
                self.write_delivery(completion=self.completion | {field: True})
                with self.assertRaises(DeliveryError) as caught:
                    self.validate()
                self.assertEqual(caught.exception.errors[0]['code'], 'invalid_type')
        invalid = self.completion.copy()
        del invalid['attempt']
        self.write_delivery(completion=invalid)
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        self.assertEqual(caught.exception.errors[0]['code'], 'missing_field')

    def test_invalid_shapes_types_and_truncated_json_are_repairable(self):
        self.write_delivery()
        for payload in ('[]', 'null', '{"task_id":', json.dumps(self.completion | {'status': []})):
            with self.subTest(payload=payload):
                (self.delivery / 'completion.json').write_text(payload)
                with self.assertRaises(DeliveryError) as caught:
                    self.validate()
                self.assertTrue(caught.exception.repairable)

    def test_duplicate_keys_rejected_even_when_identity_matches(self):
        self.write_delivery()
        data = json.dumps(self.completion)[:-1] + ', "round": 1}'
        (self.delivery / 'completion.json').write_text(data)
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        self.assertEqual(caught.exception.errors[0]['code'], 'duplicate_key')
        self.assertTrue(caught.exception.repairable)

    def test_deep_json_is_rejected_without_unhandled_parser_failure(self):
        self.write_delivery()
        (self.delivery / 'completion.json').write_text('[' * 10000 + '0' + ']' * 10000)
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        self.assertEqual(caught.exception.errors[0]['code'], 'json_too_deep')
        self.assertFalse(caught.exception.repairable)

    def test_recognizable_blocked_is_not_auto_repairable(self):
        self.write_delivery()
        for payload in (json.dumps(self.completion | {'status': 'blocked', 'round': 99}),
                        '{"status": "blocked",',
                        json.dumps(self.completion)[:-1] + ', "status": "blocked"}',
                        '{"status":"blocked",' + json.dumps(self.completion)[1:]):
            with self.subTest(payload=payload):
                (self.delivery / 'completion.json').write_text(payload)
                with self.assertRaises(DeliveryError) as caught:
                    self.validate()
                self.assertFalse(caught.exception.repairable)

    def test_empty_and_non_utf8_summary_are_rejected(self):
        self.write_delivery()
        for data in (b' \n', b'\xff'):
            with self.subTest(data=data):
                (self.delivery / 'summary.md').write_bytes(data)
                with self.assertRaises(DeliveryError) as caught:
                    self.validate()
                self.assertTrue(caught.exception.repairable)

    def test_blocked_with_invalid_utf8_tail_is_not_repaired(self):
        self.write_delivery()
        (self.delivery / 'completion.json').write_bytes(b'{"status":"blocked",\xff')
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        self.assertEqual(caught.exception.errors[0]['code'], 'invalid_json')
        self.assertFalse(caught.exception.repairable)

    def test_symlink_file_and_symlink_ancestor_are_not_repairable(self):
        self.write_delivery()
        path = self.delivery / 'summary.md'
        path.unlink()
        path.symlink_to(self.summary)
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        self.assertFalse(caught.exception.repairable)
        self.assertEqual(caught.exception.errors[0]['code'], 'symlink')
        path.unlink()
        path.write_bytes(self.summary.read_bytes())
        alias = self.base / 'alias'
        alias.symlink_to(self.attempt, target_is_directory=True)
        with self.assertRaises(DeliveryError) as caught:
            self.validate(alias / 'delivery')
        self.assertFalse(caught.exception.repairable)

    def test_non_regular_and_large_files_are_not_repairable(self):
        self.write_delivery()
        path = self.delivery / 'summary.md'
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        self.assertFalse(caught.exception.repairable)
        self.assertEqual(caught.exception.errors[0]['code'], 'not_file')
        path.unlink()
        path.write_bytes(b'x' * (MAX_BYTES + 1))
        with self.assertRaises(DeliveryError) as caught:
            self.validate()
        self.assertEqual(caught.exception.errors[0]['code'], 'too_large')
        self.assertFalse(caught.exception.repairable)

    def test_helper_is_bound_and_has_no_identity_or_destination_flags(self):
        command = self.contract()
        argv = shlex.split(command)
        self.assertEqual(argv[1:3], ['-I', '-B'])
        contract = json.loads((self.attempt / 'delivery-contract.json').read_text())
        self.assertEqual(contract, {'version': 1, 'task_id': 'MF-001-R2', 'round': 1,
                                    'attempt': 2, 'delivery_dir': str(self.delivery)})
        for flag in ('--task-id', '--round', '--attempt', '--delivery-dir', '--contract'):
            with self.subTest(flag=flag):
                result = subprocess.run(argv + ['--summary', str(self.summary), '--status', 'awaiting_review',
                                                flag, 'unexpected'], capture_output=True, text=True,
                                        env={'PATH': os.defpath}, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('unrecognized arguments', result.stderr)
        self.assertEqual(list(self.delivery.iterdir()), [])
        result = subprocess.run(argv + ['--summary', str(self.summary), '--status', 'awaiting_review'],
                                capture_output=True, text=True, env={'PATH': os.defpath}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'submitted')
        self.assertEqual(self.validate(), 'awaiting_review')

    def test_contract_and_launcher_are_write_once(self):
        self.contract()
        paths = [self.attempt / name for name in ('delivery-contract.json', 'submit-delivery.py')]
        before = [path.read_bytes() for path in paths]
        with self.assertRaises(DeliveryError):
            prepare_contract(self.task | {'attempt': 3}, self.attempt, self.delivery)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_idempotent_submission_does_not_rewrite_existing_bytes(self):
        self.contract()
        self.assertEqual(self.submit()[0], 0)
        before = [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                  for p in (self.delivery / 'summary.md', self.delivery / 'completion.json')]
        self.assertEqual(self.submit()[0], 0)
        after = [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                 for p in (self.delivery / 'summary.md', self.delivery / 'completion.json')]
        self.assertEqual(before, after)

    def test_conflicting_summary_or_status_cannot_replace_evidence(self):
        self.contract()
        self.assertEqual(self.submit()[0], 0)
        before = {p.name: p.read_bytes() for p in self.delivery.iterdir()}
        self.assertEqual(self.submit('blocked')[0], 2)
        self.summary.write_text('Changed claim')
        self.assertEqual(self.submit()[0], 2)
        self.assertEqual({p.name: p.read_bytes() for p in self.delivery.iterdir()}, before)

    def test_existing_invalid_completion_is_not_overwritten(self):
        self.contract()
        self.write_delivery(completion=self.completion | {'round': 2})
        before = {p.name: p.read_bytes() for p in self.delivery.iterdir()}
        code, _, error = self.submit()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)['errors'][0]['code'], 'identity_mismatch')
        self.assertEqual({p.name: p.read_bytes() for p in self.delivery.iterdir()}, before)

    def test_completion_without_summary_cannot_be_adopted(self):
        self.contract()
        path = self.delivery / 'completion.json'
        path.write_text(json.dumps(self.completion))
        before = path.read_bytes()
        self.assertEqual(self.submit()[0], 2)
        self.assertFalse((self.delivery / 'summary.md').exists())
        self.assertEqual(path.read_bytes(), before)

    def test_interrupted_summary_write_can_resume_without_rewrite(self):
        self.contract()
        from codinator.delivery import _write_once
        def interrupted(path, data):
            if path.name == 'completion.json':
                raise OSError('injected interruption before completion publication')
            return _write_once(path, data)
        with patch('codinator.delivery._write_once', side_effect=interrupted):
            self.assertEqual(self.submit()[0], 2)
        path = self.delivery / 'summary.md'
        original = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        self.assertFalse((self.delivery / 'completion.json').exists())
        self.assertEqual(self.submit()[0], 0)
        self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), original)
        self.assertEqual(self.validate(), 'awaiting_review')

    def test_submit_blocked_preserves_disposition(self):
        self.contract()
        code, receipt, _ = self.submit('blocked')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(receipt)['disposition'], 'blocked')
        self.assertEqual(self.validate(), 'blocked')

    def test_selection_binds_original_files_and_cannot_be_overwritten(self):
        self.write_delivery()
        select_delivery(self.attempt, self.delivery)
        self.assertEqual(selected_delivery(self.attempt), self.delivery)
        receipt = self.attempt / 'delivery-selection.json'
        before = receipt.read_bytes()
        with self.assertRaises(FileExistsError):
            select_delivery(self.attempt, self.delivery)
        self.assertEqual(receipt.read_bytes(), before)
        (self.delivery / 'summary.md').write_text('mutated after selection')
        with self.assertRaises(DeliveryError) as caught:
            selected_delivery(self.attempt)
        self.assertEqual(caught.exception.errors[0]['code'], 'selection_mismatch')

    def test_select_repair_keeps_original_evidence(self):
        repair = self.attempt / 'delivery-repair' / 'delivery'
        repair.mkdir(parents=True)
        (self.delivery / 'completion.json').write_text('{invalid original')
        self.write_delivery(directory=repair)
        select_delivery(self.attempt, repair)
        self.assertEqual(selected_delivery(self.attempt), repair)
        self.assertEqual((self.delivery / 'completion.json').read_text(), '{invalid original')

    def test_selection_receipt_hash_tampering_and_escape_are_rejected(self):
        self.write_delivery()
        select_delivery(self.attempt, self.delivery)
        path = self.attempt / 'delivery-selection.json'
        original = json.loads(path.read_text())
        changed = json.loads(path.read_text())
        changed['files']['summary.md']['sha256'] = '0' * 64
        path.write_text(json.dumps(changed))
        with self.assertRaises(DeliveryError):
            selected_delivery(self.attempt)
        for directory in ('../delivery', '/tmp', 'delivery/../delivery', 'other'):
            with self.subTest(directory=directory):
                path.write_text(json.dumps(original | {'directory': directory}))
                with self.assertRaises(DeliveryError):
                    selected_delivery(self.attempt)
        with self.assertRaises(DeliveryError):
            select_delivery(self.attempt, self.base)

    def test_selection_receipt_and_selected_directory_symlinks_are_rejected(self):
        self.write_delivery()
        select_delivery(self.attempt, self.delivery)
        path = self.attempt / 'delivery-selection.json'
        other = self.base / 'selection.json'
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(DeliveryError):
            selected_delivery(self.attempt)
        path.unlink()
        other.rename(path)
        renamed = self.attempt / 'renamed-delivery'
        self.delivery.rename(renamed)
        self.delivery.symlink_to(renamed, target_is_directory=True)
        with self.assertRaises(DeliveryError):
            selected_delivery(self.attempt)

    def test_legacy_fallback_only_without_new_contract(self):
        self.assertEqual(selected_delivery(self.attempt), self.delivery)
        self.contract()
        with self.assertRaises(DeliveryError) as caught:
            selected_delivery(self.attempt)
        self.assertEqual(caught.exception.errors[0]['code'], 'missing_selection')


if __name__ == '__main__':
    unittest.main()
