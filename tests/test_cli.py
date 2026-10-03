import contextlib
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from codinator.cli import main


class CliTests(unittest.TestCase):
    def test_removed_entrypoints_fail_before_opening_state_or_starting_agents(self):
        for argv in (['pi', 'task.json'], ['run', 'task'], ['codex', 'task'],
                     ['status', 'task', '--mode', 'pi'], ['status', '--mode', 'background']):
            with self.subTest(argv=argv), patch('codinator.cli.Store') as store, \
                 patch('codinator.cli.Engine') as engine, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(argv)
                self.assertEqual(raised.exception.code, 2)
                store.assert_not_called()
                engine.assert_not_called()

    def test_state_directory_selection(self):
        cases = (
            ({}, ['status'], '~/.local/state/codinator'),
            ({'CODINATOR_STATE_DIR': '/tmp/controller-env'}, ['status'], '/tmp/controller-env'),
            ({'CODINATOR_STATE_DIR': '/tmp/controller-env'},
             ['--state-dir', '/tmp/controller-explicit', 'status'], '/tmp/controller-explicit'),
        )
        for environment, argv, expected in cases:
            with self.subTest(argv=argv, environment=environment), \
                 patch.dict(os.environ, environment, clear=True), \
                 patch('codinator.cli.Store') as store, contextlib.redirect_stdout(io.StringIO()):
                store.return_value.tasks.return_value = []
                self.assertEqual(main(argv), 0)
                store.assert_called_once_with(Path(expected).expanduser(), read_only=True)

    def test_service_uses_installed_command_and_explicit_state(self):
        output = io.StringIO()
        paths = {'codinator': '/opt/codinator/bin/codinator', 'pi': '/usr/bin/pi', 'codex': '/usr/bin/codex'}
        with patch('codinator.cli.shutil.which', side_effect=paths.get), \
             patch.dict(os.environ, {'PATH': '/usr/bin'}, clear=True), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(['--state-dir', '/tmp/controller-state', 'service']), 0)
        self.assertIn('ExecStart="/opt/codinator/bin/codinator" "--state-dir" "/tmp/controller-state" '
                      '"--pi-bin" "/usr/bin/pi" "--codex-bin" "/usr/bin/codex" "serve"', output.getvalue())

    def test_review_only_flag_is_explicit_and_preserves_extra_budget(self):
        output = io.StringIO()
        with patch('codinator.cli.Store') as store, patch('codinator.cli.Engine') as engine, contextlib.redirect_stdout(output):
            store.return_value.get.return_value = {'state': 'review_ready'}
            self.assertEqual(main(['resume', 'task', '--review-only', '--extra-seconds', '3600']), 0)
        engine.return_value.resume.assert_called_once_with('task', 3600, review_only=True, attempt_seconds=None)
        self.assertEqual(output.getvalue(), 'review_ready: task\n')

    def test_resume_reports_review_state_instead_of_new_implementation(self):
        output = io.StringIO()
        with patch('codinator.cli.Store') as store, patch('codinator.cli.Engine'), contextlib.redirect_stdout(output):
            store.return_value.get.return_value = {'state': 'review_ready'}
            self.assertEqual(main(['resume', 'task']), 0)
        self.assertEqual(output.getvalue(), 'review_ready: task\n')

    def test_resume_explicit_attempt_override(self):
        with patch('codinator.cli.Store'), patch('codinator.cli.Engine') as engine, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['resume', 'task', '--attempt-seconds', '7200', '--extra-seconds', '14400']), 0)
        engine.return_value.resume.assert_called_once_with('task', 14400, review_only=False, attempt_seconds=7200)

    def test_resume_rejects_nonpositive_override_without_dispatch(self):
        for value in ('0', '-1'):
            with self.subTest(value=value), patch('codinator.cli.Store'), patch('codinator.cli.Engine') as engine, contextlib.redirect_stderr(io.StringIO()):
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
