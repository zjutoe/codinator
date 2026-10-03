"""Legacy history cannot trigger recovery, integration or hidden processes."""
import unittest
from unittest.mock import patch

import test_integration
from codinator.cli import notifications
from codinator.files import Problem


class LegacyRecoveryTests(unittest.TestCase):
    setUp = test_integration.LegacyHistoryTests.setUp
    legacy = test_integration.LegacyHistoryTests.legacy

    def test_active_legacy_refuses_recovery_without_stopping_or_changing_history(self):
        self.legacy()
        for state, pid in (('implementing', None), ('checking', None), ('reviewing', None),
                           ('integrating', None), ('blocked', 123456), ('ready', 123456)):
            with self.subTest(state=state, pid=pid):
                self.store.update('legacy', state=state, pid=pid, pid_start='historical' if pid else None)
                before = self.store.get('legacy')
                with patch('codinator.engine.stop_group', side_effect=AssertionError('do not touch historical pid')), \
                     patch('subprocess.Popen', side_effect=AssertionError('do not execute historical task')):
                    with self.assertRaises(Problem):
                        self.engine.recover()
                self.assertEqual(self.store.get('legacy'), before)

    def test_active_legacy_blocks_current_dispatch_until_old_writer_is_reconciled(self):
        self.legacy(state='integrating', pid=123456, pid_start='historical')
        before = self.store.get('legacy')
        with patch('codinator.engine.stop_group', side_effect=AssertionError('do not stop old protocol process')), \
             patch('codinator.engine.worker', side_effect=AssertionError('old writer is still uncertain')):
            with self.assertRaises(Problem):
                self.engine.run('test')
        self.assertEqual(self.store.get('test')['attempt'], 0)
        self.assertEqual(self.store.get('legacy'), before)

    def test_idle_legacy_is_untouched_and_does_not_prevent_current_dispatch(self):
        self.legacy(state='paused')
        before = self.store.get('legacy')
        self.engine.run('test')
        self.assertEqual(self.store.get('test')['state'], 'accepted', self.store.get('test')['reason'])
        self.assertEqual(self.store.get('legacy'), before)

    def test_legacy_control_and_notification_are_readonly(self):
        self.legacy(state='blocked')
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO outbox(id,task_id,thread,message,delivered,attempts) VALUES(?,?,?,?,?,?)",
                ('legacy:event', 'legacy', 'historical-target', 'old event', 0, 0))
        before = self.store.get('legacy')
        records = list(self.store.db.iterdump())
        for action in ('pause', 'cancel'):
            with self.subTest(action=action), self.assertRaises(Problem):
                self.store.request_control('legacy', action)
        with patch('subprocess.Popen', side_effect=AssertionError('do not replay historical notification')):
            notifications(self.store, self.agent)
        self.assertEqual(self.store.get('legacy'), before)
        self.assertEqual(list(self.store.db.iterdump()), records)
        event = self.store.db.execute('SELECT * FROM outbox WHERE task_id=?', ('legacy',)).fetchone()
        self.assertEqual((event['delivered'], event['attempts']), (0, 0))
