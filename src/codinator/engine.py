import json
import os
from pathlib import Path
import time

from .agents import reviewer, worker
from .config import positive
from .files import Problem, assert_scope, changes, digest, preserve, snapshot, write_json
from .process import Interrupted, process_start, run_process, stop_group
from .review_resume import checkpoint, require_unfinished_review
from .sandbox import Sandbox
from .store import lock


def _budget_timeout(deadline, limit):
    remaining = deadline - time.time()
    if remaining <= 0:
        raise Problem('Task wall-clock budget exhausted')
    return min(limit, remaining)


class Engine:
    def __init__(self, store, *, sandbox=None, pi_bin='pi', codex_bin='codex'):
        self.store = store
        self.sandbox = sandbox if sandbox is not None else Sandbox()
        self.pi_bin = pi_bin
        self.codex_bin = codex_bin

    def submit(self, manifest):
        root = Path(manifest['workspace'])
        if self.store.root == root or self.store.root.is_relative_to(root):
            raise Problem("Controller state must be outside the task workspace")
        task_dir = self.store.root / 'tasks' / manifest['id']
        if task_dir.exists():
            raise Problem("Task artifacts already exist; choose a new task id")
        before = snapshot(root, manifest['excludes'])
        task_dir.mkdir(parents=True)
        write_json(task_dir / 'manifest.json', manifest)
        write_json(task_dir / 'intake.json', before)
        (task_dir / 'handoff.md').write_bytes((root / manifest['handoff']).read_bytes())
        preserve(root, before, self.store.root / 'blobs')
        self.store.add(manifest, digest(before))

    def options(self, task_id, timeout):
        return dict(timeout=timeout,
                    on_start=lambda pid, start: self.store.update(task_id, pid=pid, pid_start=start),
                    on_exit=lambda: self.store.update(task_id, pid=None, pid_start=None),
                    cancel=lambda: self.store.get(task_id)['control'] is not None)

    def recover(self):
        """Only call while holding controller.lock. Never replay an uncertain prompt."""
        for task in self.store.tasks():
            if task['state'] not in ('implementing', 'checking', 'reviewing') and not task['pid']:
                continue
            if task['pid']:
                stop_group(task['pid'], task['pid_start'])
                if process_start(task['pid']) == task['pid_start']:
                    # PID may be an unreaped zombie: still do not run another worker.
                    self.store.update(task['id'], state='blocked', reason='Recovery could not confirm old process exit')
                    continue
            reason = 'Controller interrupted; previous attempt is uncertain. Inspect evidence, then explicitly resume.'
            # Preserve partial in-scope work for an explicit new attempt, never overwrite old logs.
            try:
                manifest = task['manifest']
                current = snapshot(Path(manifest['workspace']), manifest['excludes'])
                attempt = self.attempt_path(task)
                before = json.loads((attempt / 'before.json').read_text())
                if task['state'] == 'implementing':
                    assert_scope(before, current, manifest['allowed_paths'])
                    preserve(Path(manifest['workspace']), current, self.store.root / 'blobs')
                    self.store.update(task['id'], expected_digest=digest(current))
                elif digest(current) != task['expected_digest']:
                    raise Problem('Frozen submission changed during checks/review')
            except (Problem, OSError, ValueError) as exc:
                reason += ' Snapshot recovery: ' + str(exc)
            self.store.finish(task['id'], reason, state='blocked', reason=reason, pid=None, pid_start=None, control=None)

    def attempt_path(self, task):
        return self.store.root / 'tasks' / task['id'] / f"attempt-{task['attempt']:04d}"

    def run(self, task_id):
        with lock(self.store.root / 'controller.lock'):
            self.recover()
            task = self.store.get(task_id)
            if task['pid']:
                raise Problem('Previous process exit is unconfirmed; dispatch refused')
            if task['state'] not in ('ready', 'review_ready', 'needs_changes'):
                raise Problem(f"Task is {task['state']}; cannot dispatch")
            repo_lock = Path('/tmp') / f"codinator-{os.getuid()}-{digest(task['manifest']['workspace'])}.lock"
            with lock(repo_lock):
                self._loop(task_id)

    def _loop(self, task_id):
        while self.store.get(task_id)['state'] in ('ready', 'review_ready', 'needs_changes'):
            task = self.store.get(task_id)
            m = task['manifest']
            if task['control']:
                self.store.update(task_id, state='paused' if task['control'] == 'pause' else 'cancelled', control=None)
                return
            if task['deadline'] is None:
                now = time.time()
                self.store.update(task_id, started=now, deadline=now + m['max_seconds'])
                task = self.store.get(task_id)
            root = Path(m['workspace'])
            try:
                if time.time() >= task['deadline']:
                    raise Problem('Task wall-clock budget exhausted')
                before = snapshot(root, m['excludes'])
                if digest(before) != task['expected_digest']:
                    raise Problem('Workspace changed outside the recorded attempt; inspect before resuming')
                review_only = task['state'] == 'review_ready'
                evidence_dir = None
                if review_only:
                    if task['review_resume'] is None:
                        raise Problem('Missing persisted review checkpoint')
                    require_unfinished_review(self.attempt_path(task))
                    pinned, evidence_dir = checkpoint(self.store, task, self.sandbox)
                self.store.update(task_id, attempt=task['attempt'] + 1,
                                  state='reviewing' if review_only else 'implementing',
                                  phase='codex' if review_only else 'pi', reason='')
                task = self.store.get(task_id)
                attempt = self.attempt_path(task)
                attempt.mkdir(parents=True, exist_ok=False)
                write_json(attempt / 'before.json', before)
                attempt_seconds = task['attempt_seconds_override']
                if attempt_seconds is None:
                    attempt_seconds = m['attempt_seconds']
                write_json(attempt / 'budget.json', {
                    'attempt_seconds': attempt_seconds,
                    'source': 'resume_override' if task['attempt_seconds_override'] is not None else 'manifest',
                    'manifest_attempt_seconds': m['attempt_seconds'],
                    'attempt_seconds_override': task['attempt_seconds_override'],
                    'deadline': task['deadline'],
                })
                private = self.store.root / 'private' / task_id / attempt.name
                private.mkdir(parents=True, mode=0o700)
                failures = []
                if review_only:
                    write_json(attempt / 'review-source.json', pinned)
                    after = before
                else:
                    timeout = _budget_timeout(task['deadline'], attempt_seconds)
                    pi_status = worker(task, attempt, private, self.sandbox, self.options(task_id, timeout), self.pi_bin)
                    after = snapshot(root, m['excludes'])
                    write_json(attempt / 'submission.json', after)
                    write_json(attempt / 'diff.json', {'paths': changes(before, after), 'digest': digest(after)})
                    preserve(root, after, self.store.root / 'blobs')
                    assert_scope(before, after, m['allowed_paths'])
                    self.store.update(task_id, expected_digest=digest(after))
                    if pi_status == 'blocked':
                        raise Problem('Pi reported blocked; read delivery/summary.md')
                    self.store.update(task_id, state='checking', phase='checks')
                    for check in m['checks']:
                        check_timeout = _budget_timeout(task['deadline'], check['timeout_seconds'])
                        try:
                            argv = self.sandbox.wrap(check['argv'], root)
                            run_process(argv, cwd=root, env=os.environ | {'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
                                        out=attempt / 'checks' / check['name'], **self.options(task_id, check_timeout))
                        except Interrupted:
                            raise
                        except Problem as exc:
                            failures.append(f"{check['name']}: {exc}")
                if digest(snapshot(root, m['excludes'])) != digest(after):
                    raise Problem('Workspace changed during verification; submission is invalid')
                if failures:
                    feedback = 'Required controller checks failed:\n' + '\n'.join(failures)
                    verdict = {'verdict': 'needs_changes', 'summary': feedback,
                               'issues': [{'id': 'checks:' + c['name']} for c in m['checks'] if any(f.startswith(c['name'] + ':') for f in failures)]}
                else:
                    self.store.update(task_id, state='reviewing', phase='codex')
                    timeout = _budget_timeout(task['deadline'], attempt_seconds)
                    verdict = reviewer(task, attempt, digest(after), self.options(task_id, timeout), self.codex_bin,
                                       sandbox=self.sandbox, private=private, evidence_dir=evidence_dir)
                    if digest(snapshot(root, m['excludes'])) != digest(after):
                        raise Problem('Workspace changed during review; rejecting stale verdict')
                    if review_only:
                        checkpoint(self.store, task, self.sandbox)
                    feedback = json.dumps(verdict, ensure_ascii=False, indent=2)
                write_json(attempt / 'outcome.json', verdict)
                (attempt / 'review.md').write_text(verdict['summary'] + '\n\n' + json.dumps(verdict['issues'], ensure_ascii=False, indent=2) + '\n')
                if verdict['verdict'] == 'accepted':
                    self.store.finish(task_id, f"{task_id} accepted. Evidence: {attempt}",
                                      state='accepted', phase='done', feedback=feedback, reason=verdict['summary'])
                    return
                if verdict['verdict'] == 'blocked':
                    raise Problem(verdict['summary'])
                ids = json.dumps(sorted(i['id'] for i in verdict['issues']))
                if task['round'] >= m['max_rounds']:
                    raise Problem('Automatic rework round budget exhausted; ' + feedback)
                if ids == task['last_issues']:
                    raise Problem('Two consecutive reviews retain the same issue set; ' + feedback)
                self.store.update(task_id, state='needs_changes', round=task['round'] + 1, phase='queued',
                                  feedback=feedback, last_issues=ids, review_resume=None)
            except (Problem, OSError, ValueError, KeyboardInterrupt) as exc:
                current_task = self.store.get(task_id)
                control = current_task['control']
                state = 'paused' if control == 'pause' or isinstance(exc, KeyboardInterrupt) else 'cancelled' if control == 'cancel' else 'blocked'
                # Keep successful in-scope edits from an interrupted Pi run, but do
                # not legitimize concurrent edits after the submission was frozen.
                if current_task['state'] == 'implementing':
                    try:
                        current = snapshot(root, m['excludes'])
                        assert_scope(before, current, m['allowed_paths'])
                        preserve(root, current, self.store.root / 'blobs')
                        self.store.update(task_id, expected_digest=digest(current))
                    except (Problem, OSError):
                        pass
                self.store.finish(task_id, f"{task_id} {state}: {exc}",
                                  state=state, phase='stopped', reason=str(exc), control=None)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                return

    def resume(self, task_id, extra_seconds=0, *, review_only=False, attempt_seconds=None):
        if type(extra_seconds) is not int or extra_seconds < 0:
            raise Problem('--extra-seconds must be a non-negative integer')
        if attempt_seconds is not None:
            positive(attempt_seconds, '--attempt-seconds')
        with lock(self.store.root / 'controller.lock'):
            self.recover()
            task = self.store.get(task_id)
            if task['pid']:
                raise Problem('Previous process exit is unconfirmed; resume refused')
            if task['state'] not in ('blocked', 'paused'):
                raise Problem('Only blocked/paused tasks can be resumed')
            current = snapshot(Path(task['manifest']['workspace']), task['manifest']['excludes'])
            if digest(current) != task['expected_digest']:
                raise Problem('Workspace no longer matches the recorded checkpoint; restore it or publish a new task')
            pinned = None
            if review_only:
                require_unfinished_review(self.attempt_path(task))
                pinned, _ = checkpoint(self.store, task, self.sandbox)
            deadline = task['deadline']
            if extra_seconds:
                deadline = max(time.time(), deadline or time.time()) + extra_seconds
            override = task['attempt_seconds_override'] if attempt_seconds is None else attempt_seconds
            feedback = task['feedback'] + '\nPrevious interruption: ' + task['reason']
            if attempt_seconds is not None or extra_seconds:
                effective_limit = override if override is not None else task['manifest']['attempt_seconds']
                total = f'Unix UTC {deadline}' if deadline is not None else 'set from manifest total budget at first dispatch'
                feedback += (f'\nRuntime budget update (explicit resume): each Pi/Codex process <= {effective_limit} seconds; '
                             f'shared task deadline: {total}. Original handoff/manifest remain unchanged; '
                             'required-check timeouts and scope remain unchanged.')
            self.store.update(task_id, state='review_ready' if review_only else 'ready',
                              review_resume=pinned, control=None, reason='', deadline=deadline,
                              attempt_seconds_override=override, feedback=feedback)
