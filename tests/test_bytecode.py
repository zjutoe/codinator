"""Real bytecode and filesystem fault injection, without model calls."""
import json
import os
from pathlib import Path
import py_compile
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codinator.bytecode import clean_bytecode
from codinator.files import Problem, ScopeViolation, file_info, snapshot


class BytecodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'source.py').write_text('VALUE = 42\n')
        self.before = snapshot(self.root, [])
        self.task = self.base / 'task'
        self.task.mkdir()
        self.blobs = self.base / 'blobs'

    def compile(self):
        return Path(py_compile.compile(str(self.root / 'source.py'), doraise=True))

    def clean(self):
        return clean_bytecode(self.root, self.before, snapshot(self.root, []), [], [], self.task, self.blobs)

    def test_archive_precedes_cleanup_and_original_bytes_remain(self):
        path = self.compile()
        original = path.read_bytes()
        info = file_info(path)
        self.assertEqual(self.clean(), self.before)
        self.assertFalse(path.exists())
        archive, = self.task.glob('cache-cleanup-*')
        self.assertEqual((archive / 'removed' / path.relative_to(self.root)).read_bytes(), original)
        self.assertEqual((self.blobs / info['sha256']).read_bytes(), original)
        self.assertIn(str(path.relative_to(self.root)), json.loads((archive / 'before.json').read_text())['files'])
        self.assertEqual(json.loads((archive / 'after.json').read_text()), self.before)

    def test_modified_baseline_cache_is_not_cleaned(self):
        path = self.compile()
        self.before = snapshot(self.root, [])
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaises(ScopeViolation):
            self.clean()
        self.assertTrue(path.exists())
        self.assertFalse(list(self.task.iterdir()))

    def test_other_violation_prevents_all_cleanup_even_when_gitignored(self):
        path = self.compile()
        (self.root / '.git/info/exclude').write_text('outside.txt\n')
        (self.root / 'outside.txt').write_text('must not disappear')
        with self.assertRaises(ScopeViolation):
            self.clean()
        self.assertTrue(path.exists())
        self.assertFalse(list(self.task.iterdir()))

    def test_fake_header_symlink_hardlink_or_unmatched_source_is_rejected(self):
        for kind in ('header', 'magic', 'tag', 'symlink', 'hardlink', 'source'):
            with self.subTest(kind=kind):
                path = self.compile()
                data = path.read_bytes()
                if kind == 'header':
                    path.write_bytes(b'important document, not bytecode')
                elif kind == 'magic':
                    path.write_bytes(b'\x00\x00\r\n' + data[4:])
                elif kind == 'tag':
                    unknown = path.with_name('source.cpython-999999.pyc')
                    path.rename(unknown)
                    path = unknown
                elif kind in ('symlink', 'hardlink'):
                    path.unlink()
                    target = self.base / kind
                    target.write_bytes(data)
                    if kind == 'symlink':
                        path.symlink_to(target)
                    else:
                        os.link(target, path)
                else:
                    unmatched = path.with_name('missing.cpython-313.pyc')
                    path.rename(unmatched)
                    path = unmatched
                try:
                    with self.assertRaises(ScopeViolation):
                        self.clean()
                    self.assertTrue(path.exists())
                    self.assertFalse(list(self.task.iterdir()))
                finally:
                    path.unlink()

    def test_archive_failure_does_not_remove_caches(self):
        path = self.compile()
        with patch('codinator.bytecode.preserve', side_effect=OSError('archive unavailable')):
            with self.assertRaisesRegex(OSError, 'archive unavailable'):
                self.clean()
        self.assertTrue(path.exists())

    def test_concurrent_change_is_refused_before_removing_anything(self):
        from codinator.bytecode import preserve
        path = self.compile()
        old = path.read_bytes()
        def change(*args):
            preserve(*args)
            path.write_bytes(old + b'changed')
        with patch('codinator.bytecode.preserve', side_effect=change):
            with self.assertRaisesRegex(Problem, 'changed before'):
                self.clean()
        self.assertEqual(path.read_bytes(), old + b'changed')

    def test_interrupted_move_retains_full_before_snapshot_and_all_bytes(self):
        path = self.compile()
        data = path.read_bytes()
        with patch.object(Path, 'rename', side_effect=OSError('move failed')):
            with self.assertRaisesRegex(OSError, 'move failed'):
                self.clean()
        self.assertEqual(path.read_bytes(), data)
        archive, = self.task.glob('cache-cleanup-*')
        self.assertTrue((archive / 'before.json').is_file())
        self.assertTrue((archive / 'cleanup.json').is_file())
        self.assertFalse((archive / 'after.json').exists())
        self.clean()
        self.assertEqual(len(list(self.task.glob('cache-cleanup-*'))), 2)

    def test_hardlink_added_after_archive_is_rejected_before_move(self):
        from codinator.bytecode import preserve
        path = self.compile()
        def link(*args):
            preserve(*args)
            os.link(path, self.base / 'external-alias')
        with patch('codinator.bytecode.preserve', side_effect=link):
            with self.assertRaisesRegex(Problem, 'changed during cleanup'):
                self.clean()
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_nlink, 2)

    def test_second_move_failure_keeps_first_archived_and_second_in_place(self):
        path = self.compile()
        (self.root / 'source2.py').write_text('VALUE = 43\n')
        # source2 is another legitimate baseline source for this fixture.
        self.before['files']['source2.py'] = file_info(self.root / 'source2.py')
        second = Path(py_compile.compile(str(self.root / 'source2.py'), doraise=True))
        originals = {p: p.read_bytes() for p in (path, second)}
        rename = Path.rename
        moves = []
        def fail_second(source, target):
            moves.append(source)
            if len(moves) == 2:
                raise OSError('second move failed')
            return rename(source, target)
        with patch.object(Path, 'rename', side_effect=fail_second, autospec=True):
            with self.assertRaisesRegex(OSError, 'second move failed'):
                self.clean()
        archive, = self.task.glob('cache-cleanup-*')
        for p, data in originals.items():
            retained = p if p.exists() else archive / 'removed' / p.relative_to(self.root)
            self.assertEqual(retained.read_bytes(), data)
        self.assertFalse((archive / 'after.json').exists())
