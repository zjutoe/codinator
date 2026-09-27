import contextlib
import io
import os
import unittest
from unittest.mock import patch

from codidator.cli import main


class CliTests(unittest.TestCase):
    def test_review_only_flag_is_explicit_and_preserves_extra_budget(self):
        output = io.StringIO()
        with patch('codidator.cli.Store'), patch('codidator.cli.Engine') as engine, contextlib.redirect_stdout(output):
            self.assertEqual(main(['resume', 'task', '--review-only', '--extra-seconds', '3600']), 0)
        engine.return_value.resume.assert_called_once_with('task', 3600, review_only=True, attempt_seconds=None)
        self.assertEqual(output.getvalue(), 'review_ready: task\n')

    def test_resume_explicit_attempt_override(self):
        with patch('codidator.cli.Store'), patch('codidator.cli.Engine') as engine, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['resume', 'task', '--attempt-seconds', '7200', '--extra-seconds', '14400']), 0)
        engine.return_value.resume.assert_called_once_with('task', 14400, review_only=False, attempt_seconds=7200)

    def test_resume_rejects_nonpositive_override_without_dispatch(self):
        for value in ('0', '-1'):
            with self.subTest(value=value), patch('codidator.cli.Store'), patch('codidator.cli.Engine') as engine, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['resume', 'task', '--attempt-seconds', value]), 2)
                engine.return_value.resume.assert_not_called()

    def test_user_service_preserves_codex_proxy(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'https_proxy': 'http://localhost:8888', 'NO_PROXY': 'localhost'}), contextlib.redirect_stdout(output):
            self.assertEqual(main(['service']), 0)
        unit = output.getvalue()
        self.assertIn('Environment="https_proxy=http://localhost:8888"', unit)
        self.assertIn('Environment="NO_PROXY=localhost"', unit)
        self.assertIn('KillMode=control-group', unit)
