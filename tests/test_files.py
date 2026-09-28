import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codinator.config import load_manifest
from codinator.files import Problem, assert_scope, changes, digest, preserve, snapshot, write_json
from codinator.store import lock


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'handoff.md').write_text('Contract')

    def test_snapshot_untracked_binary_mode_delete_symlink(self):
        (self.root / 'binary').write_bytes(b'\x00\xff')
        (self.root / 'link').symlink_to('/unreadable/elsewhere')
        first = snapshot(self.root, [])
        self.assertEqual(first['files']['link']['target'], '/unreadable/elsewhere')
        (self.root / 'binary').chmod(0o755)
        (self.root / 'new').write_text('untracked')
        (self.root / 'handoff.md').unlink()
        second = snapshot(self.root, [])
        self.assertEqual(changes(first, second), ['binary', 'handoff.md', 'new'])
        self.assertNotEqual(digest(first), digest(second))
        with self.assertRaises(Problem):
            assert_scope(first, second, ['new'])

    def test_git_index_changes_are_rejected(self):
        a = snapshot(self.root, [])
        subprocess.run(['git', '-C', str(self.root), 'add', 'handoff.md'], check=True)
        b = snapshot(self.root, [])
        with self.assertRaises(Problem):
            assert_scope(a, b, ['handoff.md'])

    def test_tracked_file_cannot_be_excluded(self):
        subprocess.run(['git', '-C', str(self.root), 'add', 'handoff.md'], check=True)
        with self.assertRaises(Problem):
            snapshot(self.root, ['handoff.md'])

    def test_json_artifacts_never_replaced(self):
        path = self.root / 'record.json'
        write_json(path, {'first': 1})
        with self.assertRaises(FileExistsError):
            write_json(path, {'second': 2})
        self.assertEqual(json.loads(path.read_text()), {'first': 1})

    def test_manifest_rejects_scope_escape_and_contract_write(self):
        base = {'version': 1, 'id': 'task', 'workspace': str(self.root), 'handoff': 'handoff.md',
                'allowed_paths': ['product.py'], 'checks': [{'name': 'unit', 'argv': ['true']}]}
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(base))
        self.assertEqual(load_manifest(path)['max_rounds'], 4)
        for p in ('../escape', '.git/config', 'handoff.md', '**', '.', '/tmp/escape'):
            with self.subTest(path=p):
                path.write_text(json.dumps(base | {'allowed_paths': [p]}))
                with self.assertRaises(Problem):
                    load_manifest(path)

    def test_lock_prevents_second_controller(self):
        with lock(self.root / 'lock'):
            with self.assertRaises(Problem):
                with lock(self.root / 'lock'):
                    self.fail('second owner acquired lock')

    def test_empty_directory_scope_and_implicit_parent(self):
        before = snapshot(self.root, [])
        (self.root / 'src').mkdir()
        (self.root / 'src/product.py').write_text('ok')
        after = snapshot(self.root, [])
        assert_scope(before, after, ['src/product.py'])
        (self.root / 'forbidden').mkdir()
        with self.assertRaises(Problem):
            assert_scope(before, snapshot(self.root, []), ['src/product.py'])

    def test_allowed_directory_cannot_contain_exclusion(self):
        value = {'version': 1, 'id': 'T', 'workspace': str(self.root), 'handoff': 'handoff.md',
                 'allowed_paths': ['src/'], 'excludes': ['src/cache/'],
                 'checks': [{'name': 'check', 'argv': ['true']}]}
        path = self.root / 'config.json'
        path.write_text(json.dumps(value))
        with self.assertRaises(Problem):
            load_manifest(path)

    def test_existing_corrupted_blob_is_not_trusted(self):
        snap = snapshot(self.root, [])
        blobs = self.root / 'blobs'
        blobs.mkdir()
        target = blobs / snap['files']['handoff.md']['sha256']
        target.write_text('partial write')
        with self.assertRaises(Problem):
            preserve(self.root, snap, blobs)
