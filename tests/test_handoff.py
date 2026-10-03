import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codinator.files import Problem, digest, write_json
from codinator.handoff import (DocumentError, PROTOCOL_FILE, SECTIONS, enabled,
                              freeze_protocol, protocol_template, template,
                              validate_document, verify_protocol)


def document(kind, body='everything done'):
    return '\n\n'.join(f'## {name}\n{body}' for name in SECTIONS[kind]) + '\n'


class DocumentTests(unittest.TestCase):
    def test_shipped_templates_have_sections_and_role_instructions(self):
        roles = {'handoff': 'main Codex', 'summary': 'Pi', 'help': 'Pi',
                 'review': 'independent Codex', 'guidance': 'Codex guide'}
        for kind in SECTIONS:
            with self.subTest(kind=kind):
                text = template(kind)
                validate_document(text, kind)
                self.assertIn(roles[kind], text.splitlines()[0])
                self.assertGreater(len(text.split('## ', 1)[0].splitlines()), 3)
        with self.assertRaises(Problem):
            template('unknown')

    def test_no_semantic_content_requirement(self):
        for kind in SECTIONS:
            for body in ('everything done', 'please improve', '无', '未执行', 'None', 'x'):
                with self.subTest(kind=kind, body=body):
                    validate_document(document(kind, body), kind)
        validate_document(document('summary') + '\n## Extra\n自由内容\n', 'summary')

    def test_precise_structural_errors(self):
        text = document('summary').replace('## Completed\neverything done', '## Completed\n')
        text = text.replace('## Next step\neverything done', '## Additional\n未执行')
        text += '\n## Status\nNone\n'
        with self.assertRaises(DocumentError) as caught:
            validate_document(text, 'summary', 'packet.summary')
        self.assertEqual(caught.exception.errors, [
            {'code': 'duplicate_section', 'path': 'packet.summary.sections[Status]',
             'expected': 1, 'actual': 2},
            {'code': 'empty_section', 'path': 'packet.summary.sections[Completed]',
             'expected': 'nonempty body', 'actual': ''},
            {'code': 'missing_section', 'path': 'packet.summary.sections[Next step]',
             'expected': '## Next step', 'actual': None},
        ])
        with self.assertRaises(DocumentError) as caught:
            validate_document(None, 'summary')
        self.assertEqual(caught.exception.errors[0]['code'], 'invalid_type')

    def test_headings_in_fences_are_not_sections_or_duplicates(self):
        for fence in ('```python', '~~~~ markdown', '  ````'):
            close = fence.strip().split()[0].replace('python', '')
            text = f'{fence}\n{document("summary")}\n{close}\n'
            with self.subTest(fence=fence):
                with self.assertRaises(DocumentError) as caught:
                    validate_document(text, 'summary')
                self.assertTrue(all(item['code'] == 'missing_section' for item in caught.exception.errors))
                validate_document(document('summary') + text, 'summary')

    def test_shorter_and_different_fences_do_not_close_block(self):
        text = '````\n```\n~~~\n' + document('summary') + '\n````\n'
        with self.assertRaises(DocumentError):
            validate_document(text, 'summary')

    def test_additional_section_cannot_fill_empty_required_body(self):
        text = document('guidance').replace('## Advice\neverything done', '## Advice\n\n## Extra\nNone')
        with self.assertRaises(DocumentError) as caught:
            validate_document(text, 'guidance')
        self.assertEqual(caught.exception.errors[0]['code'], 'empty_section')

    def test_atx_closing_hashes_and_nested_headings(self):
        text = '\n'.join(f'  ## {name} ##\n### Detail\n无' for name in SECTIONS['summary'])
        validate_document(text, 'summary')

    def test_protocol_enable_is_exact_integer(self):
        self.assertTrue(enabled({'handoff_protocol': 1}))
        for value in (None, False, True, 1.0, '1', 2):
            self.assertFalse(enabled({'handoff_protocol': value}))
        self.assertFalse(enabled({}))


class FrozenProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.task_dir = Path(self.temp.name)
        self.manifest = {'version': 2, 'id': 'sample', 'handoff_protocol': 1}
        write_json(self.task_dir / 'manifest.json', self.manifest)
        (self.task_dir / 'handoff.md').write_text(document('handoff'), encoding='utf-8')

    def freeze(self):
        return freeze_protocol(self.task_dir, self.manifest)

    def test_frozen_contract_binds_manifest_and_handoff(self):
        record = self.freeze()
        self.assertEqual(record['manifest_sha256'], digest(self.manifest))
        self.assertEqual(record['contract_digest'], digest({
            'manifest': self.manifest, 'handoff_sha256': record['handoff_sha256']}))
        self.assertEqual(verify_protocol(self.task_dir, self.manifest), record)
        for kind in SECTIONS:
            self.assertEqual(protocol_template(self.task_dir, kind), template(kind))

    def test_installed_template_change_does_not_rewrite_history(self):
        record = self.freeze()
        before = {path: path.read_bytes() for path in self.task_dir.rglob('*') if path.is_file()}
        with patch('codinator.handoff.template', side_effect=AssertionError('installed template read')):
            self.assertEqual(self.freeze(), record)
            verify_protocol(self.task_dir, self.manifest)
            self.assertEqual(protocol_template(self.task_dir, 'summary'), template('summary'))
        self.assertEqual(before, {path: path.read_bytes() for path in self.task_dir.rglob('*') if path.is_file()})

    def test_manifest_handoff_and_template_tampering_fail(self):
        self.freeze()
        for name in ('manifest.json', 'handoff.md', 'templates/summary.md'):
            with self.subTest(name=name):
                path = self.task_dir / name
                original = path.read_bytes()
                path.write_bytes(original + b'\ncorruption')
                with self.assertRaises(Problem):
                    verify_protocol(self.task_dir, self.manifest)
                path.write_bytes(original)
        with self.assertRaises(Problem):
            verify_protocol(self.task_dir, self.manifest | {'id': 'different'})

    def test_missing_originals_and_templates_fail(self):
        self.freeze()
        for name in ('manifest.json', 'handoff.md', 'templates/help.md'):
            path = self.task_dir / name
            original = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaises(Problem):
                verify_protocol(self.task_dir, self.manifest)
            path.write_bytes(original)

    def test_template_path_escape_and_symlinks_fail(self):
        self.freeze()
        record_path = self.task_dir / PROTOCOL_FILE
        record = json.loads(record_path.read_text())
        record['templates']['help']['path'] = '../help.md'
        record_path.write_text(json.dumps(record))
        with self.assertRaises(Problem):
            protocol_template(self.task_dir, 'help')
        record['templates']['help']['path'] = 'templates/help.md'
        record_path.write_text(json.dumps(record))
        path = self.task_dir / 'templates/help.md'
        path.unlink()
        path.symlink_to(self.task_dir / 'templates/summary.md')
        with self.assertRaises(Problem):
            protocol_template(self.task_dir, 'help')

    def test_record_identity_tampering_fails(self):
        self.freeze()
        path = self.task_dir / PROTOCOL_FILE
        original = path.read_bytes()
        for field in ('manifest_sha256', 'handoff_sha256', 'contract_digest'):
            record = json.loads(original)
            record[field] = '0' * 64
            path.write_text(json.dumps(record))
            with self.subTest(field=field), self.assertRaises(Problem):
                verify_protocol(self.task_dir, self.manifest)
        path.write_bytes(original)

    def test_freeze_refuses_symlink_template_directory(self):
        target = self.task_dir / 'external'
        target.mkdir()
        (self.task_dir / 'templates').symlink_to(target, target_is_directory=True)
        with self.assertRaises(Problem):
            self.freeze()
        self.assertEqual(list(target.iterdir()), [])

    def test_partial_freeze_is_preserved_and_not_overwritten(self):
        original = template('handoff').encode()
        path = self.task_dir / 'templates/handoff.md'
        path.parent.mkdir()
        path.write_bytes(original)
        with self.assertRaises(FileExistsError):
            self.freeze()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.task_dir / PROTOCOL_FILE).exists())

    def test_freeze_failure_retains_scene_without_publishing_record(self):
        from codinator import handoff
        original = handoff._write_once
        count = 0

        def fail_second(path, data):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('injected publication failure')
            original(path, data)

        with patch('codinator.handoff._write_once', side_effect=fail_second), self.assertRaises(OSError):
            self.freeze()
        self.assertTrue((self.task_dir / 'templates/handoff.md').exists())
        self.assertFalse((self.task_dir / PROTOCOL_FILE).exists())
        with self.assertRaises(FileExistsError):
            self.freeze()


if __name__ == '__main__':
    unittest.main()
