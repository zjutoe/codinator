"""Relay lifecycle tests with agent-owned native Git/check subprocesses."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codinator.agents import reviewer as real_reviewer, worker as real_worker
from codinator.cli import notifications
from codinator.config import load_manifest
from codinator.engine import Engine, _budget_timeout
from codinator.files import Problem, file_info
from codinator.store import Store


class FakeSandbox:
    def wrap(self, argv, root, allowed=(), writable=(), readonly=()):
        return argv


class EngineTests(unittest.TestCase):
    def setUp(self):
        for name in ("git", "task", "attempt", "events", "run_mode", "review_failure"):
            if not hasattr(self, name):
                setattr(self, name, getattr(EngineTests, name).__get__(self))
        temporary = tempfile.TemporaryDirectory(prefix='relay-engine-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.workspace / 'handoff.md').write_text('Implement product.py with VALUE=42; commit before tests.\n')
        (self.workspace / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
        executable = self.workspace / 'fake-agent'
        shutil.copyfile(Path(__file__).parent / 'fake_agent.py', executable)
        executable.chmod(0o755)
        self.agent = str(executable)
        self.git('add', '.')
        self.git('commit', '-qm', 'Frozen fixture contract')
        self.initial = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'task')
        self.store = Store(self.base / 'state')
        self.addCleanup(self.store.db.close)
        self.engine = Engine(self.store, sandbox=FakeSandbox(), pi_bin=self.agent, codex_bin=self.agent)
        self.raw = {'version': 2, 'id': 'test', 'workspace': str(self.workspace), 'handoff': 'handoff.md',
                    'git': {'branch': 'task', 'base_commit': self.initial},
                    'allowed_paths': ['product.py'], 'checks': [{'name': 'unit', 'argv': [
                        sys.executable, '-B', '-c',
                        "import subprocess; from product import VALUE; assert VALUE == 42; "
                        "assert not subprocess.check_output(['git','status','--porcelain']); "
                        "print(subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip())"]}],
                    'max_rounds': 2, 'max_seconds': 120, 'attempt_seconds': 20}
        self.path = self.base / 'task.json'
        self.path.write_text(json.dumps(self.raw))
        self.manifest = load_manifest(self.path)
        environment = patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.base / 'empty-pi'),
                                             'CODEX_HOME': str(self.base / 'empty-codex'),
                                             'FAKE_MODE': 'accept', 'PYTHONDONTWRITEBYTECODE': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        self.engine.submit(self.manifest)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.workspace), *args],
                                       text=True, stderr=subprocess.PIPE).strip()

    def task(self):
        return self.store.get('test')

    def attempt(self):
        return self.engine.attempt_path(self.task())

    def events(self, role, attempt=None):
        path = (attempt or self.attempt()) / role / ('stdout.jsonl' if role == 'pi' else 'stdout.txt')
        return [json.loads(line) for line in path.read_text().split('\n') if line.strip()]

    def run_mode(self, mode):
        os.environ['FAKE_MODE'] = mode
        self.engine.run('test')
        return self.task(), self.attempt()

    def review_failure(self):
        task, source = self.run_mode('codex-unavailable')
        self.assertEqual((task['state'], task['phase']), ('blocked', 'stopped'), task['reason'])
        self.assertTrue((source / 'delivery/evidence.json').is_file())
        self.assertFalse((source / 'checks').exists())
        return source

    def test_pi_commits_before_tests_and_independent_codex_repeats_checks(self):
        task, out = self.run_mode('accept')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        head = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(head, self.initial)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), self.initial)
        self.assertEqual(self.git('rev-parse', 'main'), self.initial)
        self.assertEqual(task['expected_digest'], head)
        pi = self.events('pi')
        committed = next(i for i, event in enumerate(pi) if event['type'] == 'fixture_commit')
        checked = next(i for i, event in enumerate(pi) if event['type'] == 'fixture_check')
        self.assertLess(committed, checked)
        for role in ('pi', 'codex'):
            checks = [e for e in self.events(role) if e['type'] == 'fixture_check']
            self.assertEqual(len(checks), 1)
            self.assertEqual((checks[0]['role'], checks[0]['commit'], checks[0]['exit_code']), (role, head, 0))
        self.assertEqual(json.loads((out / 'before.json').read_text()),
                         {'kind': 'git', 'branch': 'task', 'commit': self.initial})
        self.assertEqual(json.loads((out / 'submission.json').read_text()),
                         {'kind': 'git', 'branch': 'task', 'commit': head})
        for name in ('checks', 'diff.json', 'git-submission-intent.json'):
            self.assertFalse((out / name).exists(), name)
        self.assertFalse((self.store.root / 'blobs').exists())

    def test_controller_process_only_launches_agents_never_git_or_check_argv(self):
        real_popen = subprocess.Popen
        calls = []
        def agent_only(argv, *args, **kwargs):
            calls.append(argv)
            self.assertEqual(argv[0], self.agent, f'Controller launched non-agent command: {argv}')
            return real_popen(argv, *args, **kwargs)
        with patch('subprocess.Popen', side_effect=agent_only):
            # Publication also must not execute native Git or scan source.
            self.engine.submit(self.manifest | {'id': 'second-publication'})
            self.engine.run('test')
        self.assertEqual(self.task()['state'], 'accepted', self.task()['reason'])
        self.assertEqual(len(calls), 2)
        self.assertIn('--mode', calls[0])
        self.assertIn('--output-last-message', calls[1])

    def test_rework_is_automatic_and_preserves_prior_attempt(self):
        task, out = self.run_mode('rework')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 2, 2), task['reason'])
        first = out.parent / 'attempt-0001'
        self.assertEqual(json.loads((first / 'outcome.json').read_text())['verdict'], 'needs_changes')
        first_commit = json.loads((first / 'submission.json').read_text())['commit']
        self.assertEqual(json.loads((out / 'before.json').read_text())['commit'], first_commit)
        self.assertEqual(json.loads((first / 'budget.json').read_text())['deadline'],
                         json.loads((out / 'budget.json').read_text())['deadline'])

    def test_repeated_issue_ids_use_published_round_budget(self):
        task, _ = self.run_mode('no-progress')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('blocked', 2, 2))
        self.assertIn('round budget exhausted', task['reason'])

    def test_scope_truth_is_checked_by_reviewer_not_controller_source_scan(self):
        task, out = self.run_mode('scope')
        self.assertEqual(task['state'], 'blocked')
        self.assertTrue((out / 'codex/result.json').exists())
        self.assertIn('outside allowed scope', task['reason'])
        self.assertTrue((self.workspace / 'forbidden').exists())

    def test_forged_sha_is_rejected_by_independent_git_verification(self):
        task, out = self.run_mode('forged-sha')
        self.assertEqual(task['state'], 'blocked')
        self.assertTrue((out / 'codex/result.json').is_file())
        self.assertIn('Candidate SHA', task['reason'])
        self.assertFalse((out / 'checks').exists())

    def test_claimed_pass_cannot_replace_independent_check(self):
        task, out = self.run_mode('lie-checks')
        self.assertEqual(task['state'], 'blocked')
        packet = json.loads((out / 'delivery/evidence.json').read_text())
        self.assertEqual(packet['checks'][0]['status'], 'passed')
        self.assertIn('Independent published check failed', task['reason'])

    def test_truncated_pi_never_reviews_or_creates_controller_commit(self):
        task, out = self.run_mode('truncated')
        self.assertEqual(task['state'], 'blocked')
        self.assertFalse((out / 'codex').exists())
        self.assertEqual(task['expected_digest'], self.initial)
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.assertFalse(list(out.glob('git-*')))

    def test_uncommitted_blocked_work_is_preserved_without_guessing_sha(self):
        task, out = self.run_mode('uncommitted')
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['expected_digest'], self.initial)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.assertIn('VALUE', (self.workspace / 'product.py').read_text())
        self.assertFalse((out / 'codex').exists())

    def test_bad_review_protocol_or_digest_never_accepts(self):
        for mode in ('stale-verdict', 'codex-exit-failure', 'codex-turn-failure', 'bad-review-shape'):
            with self.subTest(mode=mode):
                task, _ = self.run_mode(mode)
                self.assertEqual(task['state'], 'blocked')
                self.engine.resume('test')

    def test_pi_bypasses_proxy_and_codex_retains_it(self):
        with patch.dict(os.environ, {'https_proxy': 'http://codex-proxy.invalid:8888',
                                    'HTTP_PROXY': 'http://invalid:1', 'ALL_PROXY': 'socks5://invalid:1'}):
            task, _ = self.run_mode('proxy-routing')
        self.assertEqual(task['state'], 'accepted', task['reason'])

    def test_reconnection_diagnostic_does_not_override_terminal_success(self):
        task, _ = self.run_mode('codex-reconnect')
        self.assertEqual(task['state'], 'accepted', task['reason'])

    def test_pause_before_dispatch(self):
        self.store.update('test', control='pause')
        self.engine.run('test')
        self.assertEqual((self.task()['state'], self.task()['attempt']), ('paused', 0))

    def test_recover_does_not_replay_or_execute_git(self):
        self.store.update('test', state='implementing', attempt=1)
        with patch('subprocess.Popen', side_effect=AssertionError('recover must not launch a process')):
            self.engine.recover()
        self.assertEqual(self.task()['state'], 'blocked')
        self.assertIn('interrupted', self.task()['reason'])
        self.assertEqual(self.task()['expected_digest'], self.initial)
        self.engine.resume('test')
        task, _ = self.run_mode('accept')
        self.assertEqual((task['state'], task['attempt']), ('accepted', 2), task['reason'])

    def test_unconfirmed_old_process_blocks_resume_and_dispatch(self):
        self.store.update('test', state='implementing', pid=123456, pid_start='old')
        with patch('codinator.engine.stop_group'), patch('codinator.engine.process_start', return_value='old'):
            self.engine.recover()
            for review_only in (False, True):
                with self.assertRaises(Problem):
                    self.engine.resume('test', review_only=review_only)
            with self.assertRaises(Problem):
                self.engine.run('test')

    def test_notification_failure_does_not_rollback_acceptance(self):
        self.run_mode('accept')
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET thread='explicit-user-authorized-target'")
        os.environ['FAKE_MODE'] = 'notify-fail'
        notifications(self.store, self.agent)
        self.assertEqual(self.task()['state'], 'accepted')
        row = self.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual((row['delivered'], row['attempts']), (0, 1))
        os.environ['FAKE_MODE'] = 'accept'
        notifications(self.store, self.agent)
        row = self.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual((row['delivered'], row['attempts']), (1, 2))

    def test_terminal_state_and_notification_are_atomic(self):
        with patch.object(self.store, '_notify', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                self.store.finish('test', 'done', state='accepted')
        self.assertEqual(self.task()['state'], 'ready')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_review_only_preserves_evidence_and_skips_implementation(self):
        source = self.review_failure()
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        self.engine.resume('test', review_only=True)
        with patch('codinator.engine.worker', side_effect=AssertionError('Pi must not replay')):
            task, out = self.run_mode('accept')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 1, 2), task['reason'])
        self.assertFalse((out / 'pi').exists())
        self.assertFalse((out / 'checks').exists())
        self.assertEqual(json.loads((out / 'review-source.json').read_text())['source_attempt'], 1)
        self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()})
        self.assertEqual(len([e for e in self.events('codex') if e['type'] == 'fixture_check']), 1)

    def test_review_only_survives_restart_and_transport_failures(self):
        self.review_failure()
        self.engine.resume('test', review_only=True)
        reopened = Store(self.store.root)
        self.addCleanup(reopened.db.close)
        engine = Engine(reopened, sandbox=FakeSandbox(), pi_bin=self.agent, codex_bin=self.agent)
        with patch('codinator.engine.worker', side_effect=AssertionError('must not replay')):
            engine.run('test')
        self.assertEqual((self.task()['state'], self.task()['attempt']), ('blocked', 2))
        self.engine.resume('test', review_only=True)
        task, out = self.run_mode('accept')
        self.assertEqual((task['state'], task['round'], task['attempt']), ('accepted', 1, 3), task['reason'])
        self.assertEqual(json.loads((out / 'review-source.json').read_text())['source_attempt'], 1)

    def test_review_only_refuses_existing_verdict(self):
        self.review_failure()
        (self.attempt() / 'review-delivery/verdict.json').write_text('{}')
        with self.assertRaisesRegex(Problem, 'verdict/outcome'):
            self.engine.resume('test', review_only=True)

    def test_review_only_rejects_altered_packet_or_protocol(self):
        source = self.review_failure()
        for name, replacement in (('delivery/evidence.json', b'{}'),
                                  ('delivery/summary.md', b'changed'),
                                  ('submission.json', b'{}'), ('pi/result.json', b'{"exit_code":1}')):
            with self.subTest(name=name):
                path = source / name
                original = path.read_bytes()
                path.write_bytes(replacement)
                try:
                    with self.assertRaises(Problem):
                        self.engine.resume('test', review_only=True)
                finally:
                    path.write_bytes(original)

    def test_review_only_requires_protocol_completion_not_zero_exit_alone(self):
        source = self.review_failure()
        events = source / 'pi/stdout.jsonl'
        events.write_text(events.read_text().replace('"stopReason": "stop"', '"stopReason": "length"'))
        result = source / 'pi/result.json'
        data = json.loads(result.read_text())
        data['streams']['stdout.jsonl'] = file_info(events)
        result.write_text(json.dumps(data))
        with self.assertRaisesRegex(Problem, 'completion protocol'):
            self.engine.resume('test', review_only=True)

    def test_review_only_rechecks_pinned_evidence_before_dispatch(self):
        source = self.review_failure()
        self.engine.resume('test', review_only=True)
        (source / 'delivery/summary.md').write_text('changed after enqueue')
        with patch('codinator.engine.reviewer', side_effect=AssertionError('must not review')):
            self.engine.run('test')
        self.assertEqual(self.task()['state'], 'blocked')

    def test_review_only_git_drift_is_rejected_by_codex_not_resume_git_commands(self):
        self.review_failure()
        original = self.task()['expected_digest']
        self.git('commit', '--allow-empty', '-qm', 'Unreviewed SHA with identical tree')
        with patch('subprocess.Popen', side_effect=AssertionError('resume must not launch Git')):
            self.engine.resume('test', review_only=True)
        task, out = self.run_mode('accept')
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['expected_digest'], original)
        self.assertIn('Candidate SHA', task['reason'])
        self.assertTrue((out / 'codex/result.json').exists())

    def test_review_only_refuses_attempt_that_never_reached_review(self):
        self.run_mode('truncated')
        with self.assertRaises(Problem):
            self.engine.resume('test', review_only=True)

    def test_review_only_needs_changes_returns_to_implementation(self):
        self.review_failure()
        self.engine.resume('test', review_only=True)
        def review(*args, **kwargs):
            os.environ['FAKE_MODE'] = 'no-progress' if args[1].name == 'attempt-0002' else 'accept'
            return real_reviewer(*args, **kwargs)
        with patch('codinator.engine.worker', wraps=real_worker) as workers, \
             patch('codinator.engine.reviewer', side_effect=review):
            self.engine.run('test')
        self.assertEqual((self.task()['state'], self.task()['round'], self.task()['attempt']),
                         ('accepted', 2, 3), self.task()['reason'])
        self.assertEqual(workers.call_count, 1)

    def test_invalid_budget_does_not_recover_or_mutate(self):
        before = self.task()
        with patch.object(self.engine, 'recover', side_effect=AssertionError('validate first')):
            for value in (0, -1, True, 1.5, '7200'):
                with self.subTest(value=value), self.assertRaises(Problem):
                    self.engine.resume('test', attempt_seconds=value)
            for value in (-1, True, '60'):
                with self.subTest(extra=value), self.assertRaises(Problem):
                    self.engine.resume('test', extra_seconds=value)
        self.assertEqual(self.task(), before)

    def test_timeout_resume_preserves_old_evidence_and_manifest(self):
        self.store.update('test', state='paused')
        self.engine.resume('test', attempt_seconds=1)
        task, source = self.run_mode('sleep')
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('wall-clock', task['reason'])
        old = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
        manifest = (source.parent / 'manifest.json').read_bytes()
        self.engine.resume('test', attempt_seconds=30, extra_seconds=120)
        with patch('codinator.engine.worker', wraps=real_worker) as workers, \
             patch('codinator.engine.reviewer', wraps=real_reviewer) as reviews:
            task, out = self.run_mode('accept')
        self.assertEqual((task['state'], task['attempt']), ('accepted', 2), task['reason'])
        self.assertEqual(workers.call_args.args[4]['timeout'], 30)
        self.assertEqual(reviews.call_args.args[3]['timeout'], 30)
        self.assertEqual(task['manifest']['attempt_seconds'], 20)
        self.assertEqual(task['attempt_seconds_override'], 30)
        self.assertEqual((source.parent / 'manifest.json').read_bytes(), manifest)
        self.assertTrue(all(p.read_bytes() == data for p, data in old.items()))
        self.assertFalse((out / 'checks').exists())

    def test_override_caps_agents_by_shared_deadline(self):
        self.store.update('test', state='paused', deadline=time.time() + 10)
        self.engine.resume('test', attempt_seconds=100)
        with patch('codinator.engine.worker', wraps=real_worker) as workers, \
             patch('codinator.engine.reviewer', wraps=real_reviewer) as reviews:
            task, _ = self.run_mode('accept')
        self.assertEqual(task['state'], 'accepted', task['reason'])
        for timeout in (workers.call_args.args[4]['timeout'], reviews.call_args.args[3]['timeout']):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 10)

    def test_deadline_after_pi_never_launches_reviewer(self):
        calls = 0
        def after_pi(deadline, limit):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise Problem('Task wall-clock budget exhausted')
            return _budget_timeout(deadline, limit)
        with patch('codinator.engine._budget_timeout', side_effect=after_pi), \
             patch('codinator.engine.reviewer', side_effect=AssertionError('expired review launched')):
            task, out = self.run_mode('accept')
        self.assertEqual(task['state'], 'blocked')
        self.assertTrue((out / 'delivery/evidence.json').exists())
        self.assertFalse((out / 'codex').exists())
        self.assertFalse((out / 'checks').exists())
