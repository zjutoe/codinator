"""Agent handoff orchestration; no project commands or Git operations."""
import json
import os
from pathlib import Path
import time

from .agents import repair_delivery, reviewer, worker
from .config import deadline_timestamp, positive
from .delivery import DeliveryError, read_submission, select_delivery, selected_delivery, validate_delivery
from .files import Problem, digest, write_json
from .process import Interrupted, process_start, stop_group
from .review_resume import checkpoint, require_unfinished_review
from .sandbox import Sandbox
from .store import lock


LEGACY = 'Version 1 tasks are read-only history; prepare an explicit version 2 successor with preserved budget accounting'


def _require_current(task):
    if task['manifest']['version'] != 2:
        raise Problem(LEGACY)


def _budget_timeout(deadline, limit):
    remaining = deadline - time.time()
    if remaining <= 0:
        raise Problem('Task wall-clock budget exhausted')
    return min(limit, remaining)


def _repo_lock(manifest):
    # This protocol deliberately supports normal checkouts only. Git discovery,
    # branch validation and all other Git operations belong to the agents.
    directory = Path(manifest['workspace']) / '.git'
    if directory.is_symlink() or not directory.is_dir():
        raise Problem('Agent Git tasks require a normal checkout with an ordinary .git directory')
    return Path('/tmp') / f"codinator-source-{os.getuid()}-{digest(str(directory.resolve()))}.lock"


def _source(manifest, commit):
    return {'kind': 'git', 'branch': manifest['git']['branch'], 'commit': commit}


class Engine:
    def __init__(self, store, *, sandbox=None, pi_bin='pi', codex_bin='codex'):
        self.store = store
        self.sandbox = sandbox if sandbox is not None else Sandbox()
        self.pi_bin, self.codex_bin = pi_bin, codex_bin

    def submit(self, manifest):
        if manifest['version'] != 2:
            raise Problem('New tasks require manifest version 2; ' + LEGACY)
        root = Path(manifest['workspace'])
        if self.store.root == root or self.store.root.is_relative_to(root):
            raise Problem('Controller state must be outside the task workspace')
        task_dir = self.store.root / 'tasks' / manifest['id']
        with lock(_repo_lock(manifest)):
            if task_dir.exists():
                raise Problem('Task artifacts already exist; choose a new task id')
            task_dir.mkdir(parents=True)
            write_json(task_dir / 'manifest.json', manifest)
            write_json(task_dir / 'intake.json', _source(manifest, manifest['git']['base_commit']))
            (task_dir / 'handoff.md').write_bytes((root / manifest['handoff']).read_bytes())
            self.store.add(manifest, manifest['git']['base_commit'])

    def options(self, task_id, timeout):
        return dict(timeout=timeout,
                    on_start=lambda pid, start: self.store.update(task_id, pid=pid, pid_start=start),
                    on_exit=lambda: self.store.update(task_id, pid=None, pid_start=None),
                    cancel=lambda: self.store.get(task_id)['control'] is not None)

    def recover(self):
        """Called under controller.lock; never replay prompts or repair source."""
        for task in self.store.tasks():
            if task['state'] not in ('implementing', 'checking', 'reviewing', 'integrating') and not task['pid']:
                continue
            _require_current(task)
            with lock(_repo_lock(task['manifest'])):
                if task['pid']:
                    stop_group(task['pid'], task['pid_start'])
                    if process_start(task['pid']) == task['pid_start']:
                        self.store.update(task['id'], state='blocked', reason='Recovery could not confirm old process exit')
                        continue
                reason = ('Controller interrupted; inspect original agent evidence and Git state with the main Codex, '
                          'then explicitly resume. No prompt was replayed and no source checkpoint was created.')
                self.store.finish(task['id'], reason, state='blocked', phase='stopped', reason=reason,
                                  pid=None, pid_start=None, control=None)

    def attempt_path(self, task):
        return self.store.root / 'tasks' / task['id'] / f"attempt-{task['attempt']:04d}"

    def run(self, task_id):
        with lock(self.store.root / 'controller.lock'):
            _require_current(self.store.get(task_id))
            self.recover()
            task = self.store.get(task_id)
            if task['pid']:
                raise Problem('Previous process exit is unconfirmed; dispatch refused')
            if task['state'] not in ('ready', 'review_ready', 'needs_changes'):
                raise Problem(f"Task is {task['state']}; cannot dispatch")
            with lock(_repo_lock(task['manifest'])):
                self._loop(task_id)

    def _loop(self, task_id):
        while self.store.get(task_id)['state'] in ('ready', 'review_ready', 'needs_changes'):
            task = self.store.get(task_id)
            _require_current(task)
            m = task['manifest']
            if task['control']:
                self.store.update(task_id, state='paused' if task['control'] == 'pause' else 'cancelled', control=None)
                return
            if task['deadline'] is None:
                now = time.time()
                deadline = now + m['max_seconds']
                if 'deadline_utc' in m:
                    deadline = min(deadline, deadline_timestamp(m['deadline_utc']))
                self.store.update(task_id, started=now, deadline=deadline)
                task = self.store.get(task_id)
            try:
                _budget_timeout(task['deadline'], m['attempt_seconds'])
                before = _source(m, task['expected_digest'])
                review_only = task['state'] == 'review_ready'
                evidence_dir = None
                if review_only:
                    if task['review_resume'] is None:
                        raise Problem('Missing persisted review checkpoint')
                    require_unfinished_review(self.attempt_path(task))
                    pinned, evidence_dir = checkpoint(self.store, task, self.sandbox)
                with self.store.db:
                    self.store.db.execute('BEGIN IMMEDIATE')
                    current = self.store.get(task_id)
                    if current['state'] not in ('ready', 'review_ready', 'needs_changes') or current['control']:
                        return
                    self.store.update(task_id, attempt=task['attempt'] + 1,
                                      state='reviewing' if review_only else 'implementing',
                                      phase='codex' if review_only else 'pi', reason='')
                task = self.store.get(task_id)
                attempt = self.attempt_path(task)
                attempt.mkdir(parents=True, exist_ok=False)
                write_json(attempt / 'before.json', before)
                attempt_seconds = task['attempt_seconds_override'] or m['attempt_seconds']
                write_json(attempt / 'budget.json', {
                    'attempt_seconds': attempt_seconds,
                    'source': 'resume_override' if task['attempt_seconds_override'] is not None else 'manifest',
                    'manifest_attempt_seconds': m['attempt_seconds'],
                    'attempt_seconds_override': task['attempt_seconds_override'], 'deadline': task['deadline']})
                private = self.store.root / 'private' / task_id / attempt.name
                private.mkdir(parents=True, mode=0o700)
                if review_only:
                    write_json(attempt / 'review-source.json', pinned)
                    after = before
                else:
                    timeout = _budget_timeout(task['deadline'], attempt_seconds)
                    worker(task, attempt, private, self.sandbox, self.options(task_id, timeout), self.pi_bin)
                    if self.store.get(task_id)['pid']:
                        raise Problem('Previous process exit is unconfirmed; delivery refused')
                    delivery = attempt / 'delivery'
                    try:
                        pi_status = validate_delivery(delivery, task_id, task['round'], task['attempt'])
                        submission = read_submission(delivery, task)
                    except DeliveryError as exc:
                        write_json(attempt / 'delivery-error.json', {'errors': exc.errors, 'repairable': exc.repairable})
                        if not exc.repairable:
                            raise
                        if self.store.get(task_id)['control']:
                            raise Interrupted('Pause/cancel requested before delivery repair')
                        self.store.update(task_id, state='checking', phase='delivery-repair')
                        timeout = _budget_timeout(task['deadline'], min(attempt_seconds, 300))
                        repair_delivery(task, attempt, private, self.sandbox,
                                        self.options(task_id, timeout), exc, self.pi_bin)
                        delivery = attempt / 'delivery-repair/delivery'
                        pi_status = validate_delivery(delivery, task_id, task['round'], task['attempt'])
                        submission = read_submission(delivery, task)
                    if pi_status == 'blocked':
                        raise Problem(f'Pi reported blocked; read {delivery / "summary.md"}')
                    select_delivery(attempt, delivery)
                    after = _source(m, submission['git']['commit'])
                    # This records Pi's assertion, not controller verification of Git/tests.
                    write_json(attempt / 'implementation.json', after)
                    write_json(attempt / 'submission.json', after)
                    self.store.update(task_id, expected_digest=after['commit'])
                selected_delivery(evidence_dir or attempt)
                self.store.update(task_id, state='reviewing', phase='codex')
                timeout = _budget_timeout(task['deadline'], attempt_seconds)
                verdict = reviewer(task, attempt, after['commit'], self.options(task_id, timeout), self.codex_bin,
                                   sandbox=self.sandbox, private=private, evidence_dir=evidence_dir)
                selected_delivery(evidence_dir or attempt)
                if review_only:
                    checkpoint(self.store, self.store.get(task_id), self.sandbox)
                feedback = json.dumps(verdict, ensure_ascii=False, indent=2)
                with self.store.db:
                    self.store.db.execute('BEGIN IMMEDIATE')
                    if self.store.get(task_id)['control']:
                        raise Interrupted('Pause/cancel requested before verdict publication')
                    write_json(attempt / 'outcome.json', verdict)
                    (attempt / 'review.md').write_text(verdict['summary'] + '\n\n' + json.dumps(verdict['issues'], ensure_ascii=False, indent=2) + '\n')
                    if verdict['verdict'] == 'accepted':
                        self.store.finish(task_id, f'{task_id} accepted. Evidence: {attempt}', state='accepted',
                                          phase='done', feedback=feedback, reason=verdict['summary'])
                        return
                    if verdict['verdict'] == 'blocked':
                        raise Problem(verdict['summary'])
                    if task['round'] >= m['max_rounds']:
                        raise Problem('Automatic rework round budget exhausted; ' + feedback)
                    self.store.update(task_id, state='needs_changes', round=task['round'] + 1, phase='queued',
                                      feedback=feedback, last_issues=json.dumps(sorted(i['id'] for i in verdict['issues'])),
                                      review_resume=None)
            except (Problem, OSError, ValueError, KeyboardInterrupt) as exc:
                with self.store.db:
                    self.store.db.execute('BEGIN IMMEDIATE')
                    current = self.store.get(task_id)
                    control = current['control']
                    state = (current['state'] if current['state'] in ('paused', 'cancelled') else
                             'cancelled' if control == 'cancel' else
                             'paused' if control == 'pause' or isinstance(exc, KeyboardInterrupt) else 'blocked')
                    self.store.finish(task_id, f'{task_id} {state}: {exc}', state=state,
                                      phase='stopped', reason=str(exc), control=None)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                return

    def resume(self, task_id, extra_seconds=0, *, review_only=False, attempt_seconds=None):
        if type(extra_seconds) is not int or extra_seconds < 0:
            raise Problem('--extra-seconds must be a non-negative integer')
        if attempt_seconds is not None:
            positive(attempt_seconds, '--attempt-seconds')
        with lock(self.store.root / 'controller.lock'):
            _require_current(self.store.get(task_id))
            self.recover()
            task = self.store.get(task_id)
            with lock(_repo_lock(task['manifest'])):
                if task['pid']:
                    raise Problem('Previous process exit is unconfirmed; resume refused')
                if task['state'] not in ('blocked', 'paused'):
                    raise Problem('Only blocked/paused tasks can be resumed')
                pinned = None
                if review_only:
                    require_unfinished_review(self.attempt_path(task))
                    pinned, _ = checkpoint(self.store, task, self.sandbox)
                deadline = task['deadline']
                if extra_seconds:
                    deadline = max(time.time(), deadline or time.time()) + extra_seconds
                    if 'deadline_utc' in task['manifest']:
                        deadline = min(deadline, deadline_timestamp(task['manifest']['deadline_utc']))
                override = task['attempt_seconds_override'] if attempt_seconds is None else attempt_seconds
                effective_limit = override or task['manifest']['attempt_seconds']
                if task['manifest'].get('checkpoint_seconds', 0) >= effective_limit:
                    raise Problem('checkpoint_seconds must remain less than the resumed attempt limit')
                feedback = task['feedback'] + '\nPrevious interruption: ' + task['reason']
                feedback += ('\nMain Codex must reconcile any interrupted Git/workspace changes before resume. '
                             'Verify the recorded branch and exact starting commit before editing; never overwrite drift.')
                if attempt_seconds is not None or extra_seconds:
                    feedback += f'\nExplicit runtime budget update: process limit {effective_limit}s; shared deadline {deadline}. Original contract unchanged.'
                self.store.update(task_id, state='review_ready' if review_only else 'ready', review_resume=pinned,
                                  control=None, reason='', deadline=deadline, attempt_seconds_override=override, feedback=feedback)
