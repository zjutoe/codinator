"""Git lifecycle and crash tests use local repositories and fake model processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codinator.agents import worker as real_worker
from codinator.config import load_manifest
from codinator.engine import Engine, _repo_lock
from codinator.files import Problem
from codinator.sandbox import Sandbox
from codinator.status import report
from codinator.store import Store, lock
from test_engine import FakeSandbox


class SimulatedCrash(BaseException):
    pass


class GitEngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.workspace / 'handoff.md').write_text('Implement product.py with VALUE=42.\n')
        (self.workspace / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
        agent = self.workspace / 'fake-agent'
        shutil.copyfile(Path(__file__).parent / 'fake_agent.py', agent)
        agent.chmod(0o755)
        self.agent = str(agent)
        self.git('add', '.')
        self.git('commit', '-qm', 'Frozen fixture contract')
        self.initial = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'task')
        env = patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.base / 'empty-pi'),
                                     'CODEX_HOME': str(self.base / 'empty-codex'), 'FAKE_MODE': 'accept'})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(self.base / 'state')
        self.addCleanup(self.store.db.close)
        self.engine = Engine(self.store, sandbox=FakeSandbox(), pi_bin=self.agent, codex_bin=self.agent)
        self.path = self.base / 'task.json'
        self.raw = {'version': 2, 'id': 'git-test', 'workspace': str(self.workspace), 'handoff': 'handoff.md',
                    'git': {'branch': 'task', 'base_commit': self.initial}, 'allowed_paths': ['product.py'],
                    'checks': [{'name': 'unit', 'argv': [sys.executable, '-B', '-c',
                        "import subprocess; from product import VALUE; assert VALUE == 42; "
                        "assert not subprocess.check_output(['git','status','--porcelain']); "
                        "print(subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip())"]}],
                    'max_seconds': 120, 'attempt_seconds': 20, 'max_rounds': 2}
        self.path.write_text(json.dumps(self.raw))
        self.manifest = load_manifest(self.path)
        self.engine.submit(self.manifest)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.workspace), *args], stderr=subprocess.PIPE, text=True).strip()

    def task(self):
        return self.store.get('git-test')

    def attempt(self):
        return self.engine.attempt_path(self.task())

    def review_failure(self):
        os.environ['FAKE_MODE'] = 'codex-unavailable'
        self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'blocked', self.task()['reason'])
        self.assertTrue((self.attempt() / 'checks/unit/result.json').exists())
        return self.attempt()

    def test_new_publication_rejects_legacy_without_artifacts(self):
        with self.assertRaisesRegex(Problem, 'version 2'):
            self.engine.submit(self.manifest | {'version': 1, 'id': 'legacy'})
        self.assertFalse((self.store.root / 'tasks/legacy').exists())

    def test_new_contract_rejects_legacy_excludes_and_integration(self):
        for change in ({'excludes': []}, {'integration': {}}, {'git': {}}, {'git': self.raw['git'] | {'base_commit': 'HEAD'}}):
            with self.subTest(change=change):
                self.path.write_text(json.dumps(self.raw | change))
                with self.assertRaises(Problem):
                    load_manifest(self.path)

    def test_commit_precedes_checks_and_review_without_source_blobs(self):
        with (patch('codinator.engine.snapshot', side_effect=AssertionError('v2 must not snapshot')),
              patch('codinator.engine.preserve', side_effect=AssertionError('v2 must not preserve blobs'))):
            self.engine.run('git-test')
        task = self.task()
        self.assertEqual(task['state'], 'accepted', task['reason'])
        head = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(head, self.initial)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), self.initial)
        self.assertEqual(self.git('rev-parse', 'main'), self.initial)
        self.assertEqual(task['expected_digest'], head)
        self.assertEqual((self.attempt() / 'checks/unit/stdout.txt').read_text().strip(), head)
        self.assertEqual(json.loads((self.attempt() / 'review-delivery/verdict.json').read_text())['submission_digest'], head)
        self.assertEqual(set(json.loads((self.attempt() / 'submission.json').read_text())), {'kind', 'branch', 'commit', 'tree'})
        self.assertFalse((self.store.root / 'blobs').exists())
        self.assertEqual(report(self.store, task)['git']['checkpoint_commit'], head)

    def test_ignored_pytest_cache_does_not_block_or_get_deleted(self):
        def worker(*args):
            real_worker(*args)
            cache = self.workspace / '.pytest_cache'
            cache.mkdir()
            (cache / '.gitignore').write_text('*\n')
            (cache / 'nodeids').write_text('["test_example"]\n')
        with patch('codinator.engine.worker', side_effect=worker):
            self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'accepted', self.task()['reason'])
        self.assertTrue((self.workspace / '.pytest_cache/nodeids').is_file())
        self.assertNotIn('.pytest_cache', self.git('ls-tree', '-r', '--name-only', 'HEAD'))

    def test_out_of_scope_work_is_retained_without_a_commit_or_review(self):
        os.environ['FAKE_MODE'] = 'scope'
        self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'blocked')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.assertTrue((self.workspace / 'forbidden').is_file())
        self.assertFalse((self.attempt() / 'codex').exists())

    def test_interrupted_worker_gets_a_partial_checkpoint_but_no_acceptance(self):
        os.environ['FAKE_MODE'] = 'truncated'
        self.engine.run('git-test')
        task = self.task()
        self.assertEqual(task['state'], 'blocked')
        self.assertNotEqual(task['expected_digest'], self.initial)
        self.assertTrue((self.attempt() / 'git-interrupted-result.json').is_file())
        self.assertFalse((self.attempt() / 'checks').exists())
        deadline = task['deadline']
        self.engine.resume('git-test')
        self.assertEqual((self.task()['round'], self.task()['deadline']), (task['round'], deadline))
        os.environ['FAKE_MODE'] = 'accept'
        self.engine.run('git-test')
        self.assertEqual((self.task()['state'], self.task()['attempt']), ('accepted', 2), self.task()['reason'])

    def test_crash_after_commit_before_database_update_does_not_replay_pi(self):
        update = self.store.update
        def crash(task_id, **fields):
            if 'expected_digest' in fields:
                raise SimulatedCrash('after Git checkpoint before DB')
            return update(task_id, **fields)
        with patch.object(self.store, 'update', side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                self.engine.run('git-test')
        candidate = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(candidate, self.initial)
        self.assertEqual(self.task()['expected_digest'], self.initial)
        previous = self.task()
        with patch('codinator.engine.worker', side_effect=AssertionError('must not replay')):
            self.engine.recover()
        task = self.task()
        self.assertEqual(task['state'], 'blocked', task['reason'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), candidate)
        self.assertEqual(task['expected_digest'], candidate)
        self.assertEqual((task['attempt'], task['round'], task['deadline']), (previous['attempt'], previous['round'], previous['deadline']))

    def test_review_only_reuses_exact_commit_and_checked_evidence(self):
        source = self.review_failure()
        head = self.git('rev-parse', 'HEAD')
        before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        self.engine.resume('git-test', review_only=True)
        os.environ['FAKE_MODE'] = 'accept'
        with (patch('codinator.engine.worker', side_effect=AssertionError('must not implement')),
              patch('codinator.engine.run_process', side_effect=AssertionError('must not repeat checks'))):
            self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'accepted', self.task()['reason'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(before, {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()})
        self.assertEqual((self.task()['round'], self.task()['attempt']), (1, 2))

    def test_review_only_refuses_different_commit_with_identical_tree(self):
        self.review_failure()
        tree = self.git('rev-parse', 'HEAD^{tree}')
        self.git('commit', '--allow-empty', '-qm', 'Unreviewed identity')
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}'), tree)
        with self.assertRaises(Problem):
            self.engine.resume('git-test', review_only=True)

    def test_review_mutation_is_not_adopted_as_an_interrupted_checkpoint(self):
        os.environ['FAKE_MODE'] = 'mutate-review'
        self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'blocked')
        self.assertNotEqual((self.workspace / 'product.py').read_text(), self.git('show', 'HEAD:product.py') + '\n')
        self.assertFalse((self.attempt() / 'git-interrupted-result.json').exists())
        with self.assertRaises(Problem):
            self.engine.resume('git-test')

    def test_source_lock_covers_publish_run_resume_and_recover(self):
        with lock(_repo_lock(self.manifest)):
            with self.assertRaisesRegex(Problem, 'Another controller'):
                self.engine.submit(self.manifest | {'id': 'second'})
            with self.assertRaisesRegex(Problem, 'Another controller'):
                self.engine.run('git-test')
            self.store.update('git-test', state='blocked')
            with self.assertRaisesRegex(Problem, 'Another controller'):
                self.engine.resume('git-test')
            self.store.update('git-test', state='implementing', attempt=1)
            with self.assertRaisesRegex(Problem, 'Another controller'):
                self.engine.recover()
        self.assertEqual(self.task()['state'], 'implementing')

    def test_rework_keeps_branch_and_does_not_reset_budget(self):
        os.environ['FAKE_MODE'] = 'rework'
        self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'accepted', self.task()['reason'])
        self.assertEqual(self.git('branch', '--show-current'), 'task')
        self.assertEqual((self.task()['round'], self.task()['attempt']), (2, 2))
        self.assertTrue((self.store.root / 'tasks/git-test/attempt-0001/outcome.json').exists())
        first = json.loads((self.store.root / 'tasks/git-test/attempt-0001/budget.json').read_text())
        last = json.loads((self.attempt() / 'budget.json').read_text())
        self.assertEqual(first['deadline'], last['deadline'])

    def test_delivery_repair_is_still_frozen_on_the_git_candidate(self):
        os.environ['FAKE_MODE'] = 'missing-then-repair'
        self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'accepted', self.task()['reason'])
        self.assertTrue((self.attempt() / 'delivery-repair/pi/result.json').exists())
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '1')

    def test_no_checkpoint_while_worker_exit_is_unconfirmed(self):
        def worker(*args):
            (self.workspace / 'product.py').write_text('VALUE = 42\n')
            self.store.update('git-test', pid=123456, pid_start='still-alive')
            raise Problem('worker exit unknown')
        with patch('codinator.engine.worker', side_effect=worker), patch('codinator.engine.process_start', return_value='still-alive'):
            self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'blocked')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.assertFalse((self.attempt() / 'git-interrupted-intent.json').exists())

    def test_full_git_lifecycle_with_real_bwrap_and_fake_agents(self):
        self.engine.sandbox = Sandbox()
        self.engine.run('git-test')
        self.assertEqual(self.task()['state'], 'accepted', self.task()['reason'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.task()['expected_digest'])

    def test_real_bwrap_keeps_git_metadata_and_frozen_source_readonly(self):
        code = '''import pathlib, subprocess, sys
for name in sys.argv[1:]:
    path = pathlib.Path(name)
    try:
        path.write_bytes(b"unexpected")
    except OSError:
        pass
    else:
        raise AssertionError("unexpected write: " + name)
assert subprocess.run(["git", "add", "handoff.md"], capture_output=True).returncode != 0
'''
        sandbox = Sandbox()
        for allowed in (['product.py'], []):
            with self.subTest(stage='worker' if allowed else 'checks/review'):
                argv = sandbox.wrap([sys.executable, '-B', '-c', code, '.git/config', 'handoff.md'],
                                    self.workspace, allowed=allowed)
                subprocess.run(argv, check=True, capture_output=True, timeout=15)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
