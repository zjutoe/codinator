"""Frontend reads must not change tasks, migrate schemas or start agents."""
import contextlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_engine
from codinator.cli import main
from codinator.files import write_json
from codinator.store import Store


class StatusTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def status(self, *args, root=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('codinator.cli.Engine', side_effect=AssertionError('must not start controller')):
            self.assertEqual(main(['--state-dir', str(root or self.store.root), 'status', 'test', *args]), 0)
        return json.loads(out.getvalue())[0]

    def test_read_only_status_keeps_paused_reason_and_all_records(self):
        self.store.update('test', state='paused', reason='transport result uncertain', phase='pi')
        before = list(self.store.db.iterdump())
        result = self.status()
        self.assertEqual(result['state'], 'paused')
        self.assertEqual(result['reason'], 'transport result uncertain')
        self.assertEqual(result['mode'], 'background')
        self.assertEqual(result['evidence'], str(self.store.root / 'tasks/test'))
        self.assertEqual(result['next_action'], 'inspect_evidence_before_explicit_resume')
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertIsNone(result['latest_review'])

    def test_old_review_is_labeled_by_attempt_not_current_task_state(self):
        source = self.store.root / 'tasks/test/attempt-0001'
        write_json(source / 'outcome.json', {'verdict': 'needs_changes', 'summary': 'Add a boundary case', 'issues': []})
        self.store.update('test', state='implementing', phase='pi', attempt=2, round=2)
        result = self.status()
        self.assertEqual((result['state'], result['round'], result['attempt']), ('implementing', 2, 2))
        self.assertEqual(result['latest_review']['attempt'], 1)
        self.assertEqual(result['latest_review']['verdict'], 'needs_changes')
        self.assertEqual(result['latest_review']['outcome'], str(source / 'outcome.json'))

    def test_outcome_on_disk_does_not_accept_interrupted_task(self):
        source = self.store.root / 'tasks/test/attempt-0001'
        write_json(source / 'outcome.json', {'verdict': 'accepted', 'summary': 'Written before crash', 'issues': []})
        self.store.update('test', state='blocked', phase='stopped', attempt=1)
        result = self.status()
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['latest_review']['verdict'], 'accepted')
        self.assertEqual(result['next_action'], 'inspect_evidence_before_explicit_resume')

    def test_native_pi_status_uses_separate_root_without_opening_session(self):
        state = self.base / 'other'
        with contextlib.closing(Store(state / 'interactive').db) as db:
            self.store.db.backup(db)
        with patch('codinator.foreground.launch', side_effect=AssertionError('must not start Pi')):
            result = self.status('--mode', 'pi', root=state)
        self.assertEqual(result['mode'], 'pi')
        self.assertEqual(result['evidence'], str(state / 'interactive/tasks/test'))
        self.assertFalse((state / 'state.sqlite').exists())

    def test_missing_database_does_not_create_state(self):
        root = self.base / 'missing'
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--state-dir', str(root), 'status']), 2)
        self.assertFalse(root.exists())

    def test_status_reads_old_schema_without_migration(self):
        self.store.db.execute('ALTER TABLE tasks DROP COLUMN review_resume')
        self.store.db.execute('ALTER TABLE tasks DROP COLUMN attempt_seconds_override')
        self.store.db.execute('ALTER TABLE tasks DROP COLUMN integration')
        self.store.db.commit()
        before = list(self.store.db.iterdump())
        self.assertEqual(self.status()['attempt_seconds'], self.manifest['attempt_seconds'])
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_database_connection_refuses_writes(self):
        with contextlib.closing(Store(self.store.root, read_only=True).db) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE tasks SET state='accepted'")

    def test_linked_or_malformed_outcomes_fail_without_changing_task(self):
        source = self.store.root / 'tasks/test/attempt-0001'
        source.mkdir()
        self.store.update('test', attempt=1)
        outside = self.base / 'outside.json'
        outside.write_text('{"verdict":"accepted","summary":"wrong file"}')
        target = source / 'outcome.json'
        target.symlink_to(outside)
        before = list(self.store.db.iterdump())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--state-dir', str(self.store.root), 'status', 'test']), 2)
        target.unlink()
        target.write_text('[]')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--state-dir', str(self.store.root), 'status', 'test']), 2)
        self.assertEqual(list(self.store.db.iterdump()), before)
