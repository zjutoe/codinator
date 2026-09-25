import os
from pathlib import Path
import tempfile
import unittest

from codidator.files import Problem
from codidator.sandbox import Sandbox


class SandboxTests(unittest.TestCase):
    def test_writable_hardlink_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'frozen').write_text('original')
            os.link(root / 'frozen', root / 'allowed')
            with self.assertRaises(Problem):
                Sandbox().wrap(['true'], root, ['allowed'])
            self.assertEqual((root / 'frozen').read_text(), 'original')

    def test_readonly_evidence_remounted_after_tmpfs(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            workspace = root / 'workspace'
            workspace.mkdir()
            evidence = root / 'evidence'
            evidence.mkdir()
            argv = Sandbox().wrap(['true'], workspace, readonly=[evidence])
            self.assertGreater(argv.index(str(evidence)), argv.index('--tmpfs'))
            self.assertEqual(argv[argv.index(str(evidence)) - 1], '--ro-bind')
            self.assertIn('--die-with-parent', argv)
            self.assertIn('--unshare-pid', argv)
