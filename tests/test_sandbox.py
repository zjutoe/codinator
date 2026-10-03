import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codinator.files import Problem
from codinator.sandbox import Sandbox


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

    def test_delivery_repair_cannot_write_source_or_original_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            workspace = root / 'workspace'
            workspace.mkdir()
            source = workspace / 'product.py'
            source.write_text('VALUE = 42\n')
            attempt = root / 'attempt-0001'
            original = attempt / 'delivery'
            original.mkdir(parents=True)
            completion = original / 'completion.json'
            completion.write_text('{"status":"completed"}\n')
            contract = attempt / 'delivery-contract.json'
            contract.write_text('{"immutable":true}\n')
            repaired = attempt / 'delivery-repair/delivery'
            repaired.mkdir(parents=True)
            code = '''import errno, pathlib, sys
for value in sys.argv[1:-1]:
    try:
        pathlib.Path(value).write_text("unauthorized")
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES), exc
    else:
        raise AssertionError("Frozen path was writable: " + value)
pathlib.Path(sys.argv[-1]).write_text("repair is writable")
'''
            argv = Sandbox().wrap([sys.executable, '-I', '-B', '-c', code,
                                   str(source), str(completion), str(contract),
                                   str(repaired / 'summary.md')], workspace,
                                  writable=[repaired], readonly=[attempt])
            subprocess.run(argv, check=True, capture_output=True, timeout=15)
            self.assertEqual(source.read_text(), 'VALUE = 42\n')
            self.assertEqual(completion.read_text(), '{"status":"completed"}\n')
            self.assertEqual((repaired / 'summary.md').read_text(), 'repair is writable')
