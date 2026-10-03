"""Historical integration records remain readable, never executable."""
import json
import unittest
from unittest.mock import patch

import test_engine
from legacy_fixture import submit_legacy
from codinator.files import Problem, write_json
from codinator.status import report


class LegacyHistoryTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def legacy(self, **changes):
        manifest = {k: v for k, v in self.manifest.items() if k != 'git'}
        manifest.update(version=1, id='legacy', excludes=[])
        submit_legacy(self.engine, manifest)
        if changes:
            self.store.update('legacy', **changes)
        return self.store.get('legacy')

    def test_legacy_run_and_resume_refuse_before_recovery_or_agent_dispatch(self):
        self.legacy(state='implementing', pid=321, pid_start='old')
        before = self.store.get('legacy')
        records = list(self.store.db.iterdump())
        with patch.object(self.engine, 'recover', side_effect=AssertionError('legacy must fail before recovery')), \
             patch('subprocess.Popen', side_effect=AssertionError('historical task must not execute')):
            for action in (lambda: self.engine.run('legacy'),
                           lambda: self.engine.resume('legacy'),
                           lambda: self.engine.resume('legacy', review_only=True)):
                with self.subTest(action=action), self.assertRaisesRegex(Problem, 'version 2|v1|legacy'):
                    action()
        self.assertEqual(self.store.get('legacy'), before)
        self.assertEqual(list(self.store.db.iterdump()), records)

    def test_legacy_outcome_and_integration_status_remain_readable(self):
        record = {'attempt': 1, 'state': 'accepted', 'commit': 'a' * 40,
                  'evidence': '/historical/integration-0001'}
        task = self.legacy(state='accepted', attempt=1, phase='integration', integration=record)
        out = self.engine.attempt_path(task)
        out.mkdir()
        outcome = {'verdict': 'accepted', 'summary': 'Historical acceptance', 'issues': []}
        write_json(out / 'outcome.json', outcome)
        before = (out / 'outcome.json').read_bytes()
        with patch('subprocess.Popen', side_effect=AssertionError('read-only history')):
            value = report(self.store, task)
        self.assertEqual(value['integration'], record)
        self.assertTrue(value['review_accepted'])
        self.assertEqual(value['latest_review']['summary'], 'Historical acceptance')
        self.assertEqual((out / 'outcome.json').read_bytes(), before)
        self.assertNotIn('git', value)

    def test_legacy_blocked_status_requires_explicit_v2_successor(self):
        task = self.legacy(state='blocked', reason='Historical interruption')
        value = report(self.store, task)
        self.assertEqual(value['next_action'], 'prepare_v2_successor_preserving_budget')
        self.assertEqual(value['reason'], 'Historical interruption')
