"""Native Git and sandbox boundaries; the controller only relays agent claims."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_engine
from codinator.agents import worker as real_worker
from codinator.config import load_manifest
from codinator.engine import _repo_lock
from codinator.files import Problem
from codinator.sandbox import Sandbox
from codinator.status import report
from codinator.store import lock


class SimulatedCrash(BaseException):
    pass


class GitEngineTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp

    def test_new_contract_requires_v2_and_excludes_legacy_integration(self):
        with self.assertRaisesRegex(Problem, 'version 2'):
            self.engine.submit(self.manifest | {'version': 1, 'id': 'legacy'})
        self.assertFalse((self.store.root / 'tasks/legacy').exists())
        for change in ({'excludes': []}, {'integration': {}}, {'git': {}},
                       {'git': self.raw['git'] | {'base_commit': 'HEAD'}}):
            with self.subTest(change=change):
                self.path.write_text(json.dumps(self.raw | change))
                with self.assertRaises(Problem):
                    load_manifest(self.path)

    def test_linked_worktree_is_explicitly_unsupported(self):
        linked = self.base / 'linked'
        self.git('worktree', 'add', '-qb', 'linked-task', str(linked))
        raw = self.raw | {'id': 'linked', 'workspace': str(linked),
                          'git': self.raw['git'] | {'branch': 'linked-task'}}
        self.path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(Problem, 'ordinary|normal|linked|directory'):
            self.engine.submit(load_manifest(self.path))
        self.assertFalse((self.store.root / 'tasks/linked').exists())

    def test_non_task_ref_change_is_rejected_using_original_pi_tool_evidence(self):
        task, out = self.run_mode('move-other-ref')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('Non-task Git refs changed', task['reason'])
        initial = next(e for e in self.events('pi') if e['type'] == 'fixture_refs')
        self.assertIn(self.initial + ' refs/heads/main', initial['stdout'])
        self.assertTrue((out / 'codex/result.json').exists())

    def test_wrong_starting_sha_is_rejected_by_pi_without_controller_git(self):
        self.git('commit', '--allow-empty', '-qm', 'Unexplained change before dispatch')
        task, out = self.run_mode('accept')
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['expected_digest'], self.initial)
        self.assertFalse((out / 'codex').exists())
        self.assertFalse((self.workspace / 'product.py').exists())

    def test_ignored_cache_is_not_deleted_or_archived(self):
        def worker(*args):
            real_worker(*args)
            cache = self.workspace / '.pytest_cache'
            cache.mkdir()
            (cache / 'nodeids').write_text('["test_example"]\n')
        with patch('codinator.engine.worker', side_effect=worker):
            self.engine.run('test')
        self.assertEqual(self.task()['state'], 'accepted', self.task()['reason'])
        self.assertTrue((self.workspace / '.pytest_cache/nodeids').is_file())
        self.assertFalse(list((self.store.root / 'tasks/test').glob('cache-cleanup-*')))
        self.assertNotIn('.pytest_cache', self.git('ls-tree', '-r', '--name-only', 'HEAD'))

    def test_crash_after_pi_commit_does_not_adopt_or_replay_source_on_recover(self):
        update = self.store.update
        def crash(task_id, **fields):
            if 'expected_digest' in fields:
                raise SimulatedCrash('Pi claim persisted before DB update')
            return update(task_id, **fields)
        with patch.object(self.store, 'update', side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                self.engine.run('test')
        candidate = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(candidate, self.initial)
        before = self.task()
        with patch('subprocess.Popen', side_effect=AssertionError('recover is state-only')):
            self.engine.recover()
        self.assertEqual(self.task()['state'], 'blocked')
        self.assertEqual(self.task()['expected_digest'], self.initial)
        self.assertEqual(self.git('rev-parse', 'HEAD'), candidate)
        self.assertEqual((self.task()['attempt'], self.task()['round'], self.task()['deadline']),
                         (before['attempt'], before['round'], before['deadline']))

    def test_source_lock_covers_publish_run_resume_and_recover(self):
        with lock(_repo_lock(self.manifest)):
            with self.assertRaisesRegex(Problem, 'Another controller'):
                self.engine.submit(self.manifest | {'id': 'second'})
            with self.assertRaisesRegex(Problem, 'Another controller'):
                self.engine.run('test')
            self.store.update('test', state='blocked')
            with self.assertRaisesRegex(Problem, 'Another controller'):
                self.engine.resume('test')
            self.store.update('test', state='implementing', attempt=1)
            with self.assertRaisesRegex(Problem, 'Another controller'):
                self.engine.recover()
        self.assertEqual(self.task()['state'], 'implementing')

    def test_status_distinguishes_agent_claim_from_independently_accepted_sha(self):
        self.run_mode('codex-unavailable')
        candidate = self.task()['expected_digest']
        value = report(self.store, self.task())
        self.assertEqual(value['git']['checkpoint_commit'], candidate)
        self.assertEqual(value['git']['verification'], 'agent_claim_or_published_baseline')
        self.engine.resume('test', review_only=True)
        self.run_mode('accept')
        self.assertEqual(report(self.store, self.task())['git']['verification'], 'independent_codex')

    def test_no_checkpoint_while_worker_exit_is_unconfirmed(self):
        def worker(*args):
            (self.workspace / 'product.py').write_text('VALUE = 42\n')
            self.store.update('test', pid=123456, pid_start='still-alive')
            raise Problem('worker exit unknown')
        with patch('codinator.engine.worker', side_effect=worker), \
             patch('codinator.engine.process_start', return_value='still-alive'):
            self.engine.run('test')
        self.assertEqual(self.task()['state'], 'blocked')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.assertFalse(list(self.attempt().glob('git-*')))

    def test_full_relay_with_real_bwrap_commits_and_reviews_without_credentials(self):
        self.engine.sandbox = Sandbox()
        self.engine.run('test')
        self.assertEqual(self.task()['state'], 'accepted', self.task()['reason'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.task()['expected_digest'])
        self.assertFalse((self.attempt() / 'checks').exists())

    def test_real_bwrap_repair_and_review_cannot_write_source_or_git(self):
        sandbox = Sandbox()
        code = """import pathlib, subprocess
for name in ('.git/config', 'handoff.md', 'new-unallowed-file'):
    try:
        pathlib.Path(name).write_bytes(b'unexpected')
    except OSError:
        pass
    else:
        raise AssertionError('unexpected write: ' + name)
assert subprocess.run(['git','add','handoff.md'],capture_output=True).returncode != 0
assert subprocess.run(['git','commit','--allow-empty','-m','unallowed'],capture_output=True).returncode != 0
"""
        argv = sandbox.wrap([sys.executable, '-B', '-c', code], self.workspace)
        subprocess.run(argv, check=True, capture_output=True, timeout=15)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)

    def test_real_bwrap_delivery_repair_has_no_extra_git_commit_or_check(self):
        self.engine.sandbox = Sandbox()
        task, out = self.run_mode('malformed-then-repair')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '1')
        events = self.events('pi', out / 'delivery-repair')
        self.assertFalse(any(e['type'] in ('fixture_commit', 'fixture_check') for e in events))
