import contextlib
import io
import os
import unittest
from unittest.mock import patch

from codidator.cli import main


class CliTests(unittest.TestCase):
    def test_user_service_preserves_codex_proxy(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'https_proxy': 'http://localhost:8888', 'NO_PROXY': 'localhost'}), contextlib.redirect_stdout(output):
            self.assertEqual(main(['service']), 0)
        unit = output.getvalue()
        self.assertIn('Environment="https_proxy=http://localhost:8888"', unit)
        self.assertIn('Environment="NO_PROXY=localhost"', unit)
        self.assertIn('KillMode=control-group', unit)
