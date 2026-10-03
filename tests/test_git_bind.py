"""Writable Git metadata must not alias read-only host evidence."""
import os
from pathlib import Path
import tempfile
import unittest

from codinator.files import Problem
from codinator.sandbox import Sandbox


class GitBindTests(unittest.TestCase):
    def test_unreadable_git_directory_cannot_hide_an_alias(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            hidden = root / '.git' / 'objects' / 'hidden'
            hidden.mkdir(parents=True)
            evidence = root / 'evidence'
            evidence.write_text('immutable')
            os.link(evidence, hidden / 'alias')
            hidden.chmod(0)
            try:
                with self.assertRaises(Problem):
                    Sandbox().wrap(['/usr/bin/true'], root, writable=[root / '.git'])
            finally:
                hidden.chmod(0o700)
            self.assertEqual(evidence.read_text(), 'immutable')

    def test_git_hardlink_to_readonly_evidence_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'workspace'
            git = root / '.git'
            git.mkdir(parents=True)
            evidence = Path(name) / 'original-evidence'
            evidence.write_text('immutable')
            os.link(evidence, git / 'aliased-object')
            with self.assertRaisesRegex(Problem, 'Writable hardlink'):
                Sandbox().wrap(['/usr/bin/true'], root, writable=[git])
            self.assertEqual(evidence.read_text(), 'immutable')

    def test_git_symlink_bind_or_descendant_refused(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            real = root / 'real'
            real.mkdir()
            alias = root / '.git'
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(Problem, 'symlink'):
                Sandbox().wrap(['/usr/bin/true'], root, writable=[alias])
            alias.unlink()
            alias.mkdir()
            (alias / 'objects').symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(Problem, 'symlink'):
                Sandbox().wrap(['/usr/bin/true'], root, writable=[alias])
