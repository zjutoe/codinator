import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codinator.config import load_manifest
from codinator.files import Problem, write_json
from codinator.store import lock


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'handoff.md').write_text('Contract')

    def test_json_artifacts_never_replaced(self):
        path = self.root / 'record.json'
        write_json(path, {'first': 1})
        with self.assertRaises(FileExistsError):
            write_json(path, {'second': 2})
        self.assertEqual(json.loads(path.read_text()), {'first': 1})

    def test_manifest_rejects_scope_escape_and_contract_write(self):
        base = {'version': 2, 'git': {'branch': 'task', 'base_commit': 'a' * 40}, 'id': 'task', 'workspace': str(self.root), 'handoff': 'handoff.md',
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

    def test_new_publication_rejects_legacy_exclusions(self):
        value = {'version': 2, 'git': {'branch': 'task', 'base_commit': 'a' * 40}, 'id': 'T', 'workspace': str(self.root), 'handoff': 'handoff.md',
                 'allowed_paths': ['src/'], 'excludes': ['src/cache/'],
                 'checks': [{'name': 'check', 'argv': ['true']}]}
        path = self.root / 'config.json'
        path.write_text(json.dumps(value))
        with self.assertRaises(Problem):
            load_manifest(path)
