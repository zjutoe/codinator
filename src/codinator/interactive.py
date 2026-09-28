"""Controller for a visible Pi session. The frontend cannot issue acceptance."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import tempfile

from .agents import reviewer, validate_verdict
from .bytecode import clean_bytecode
from .files import Problem, ScopeViolation, changes, digest, preserve, snapshot, write_json
from .process import Interrupted, run_process, stop_group, process_start
from .store import Store


class SubmissionRejected(Problem):
    def __init__(self, reason, status):
        super().__init__(reason)
        self.status = status


class Interactive:
    def __init__(self, state, task_id, sandbox, codex_bin="codex"):
        self.state = Path(state)
        self.task_id = task_id
        self.sandbox = sandbox
        self.codex_bin = codex_bin
        self.mutex = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.session_dir = None

    @contextmanager
    def store(self):
        with self.mutex:
            store = Store(self.state)
            try:
                yield store
            finally:
                store.db.close()

    def task_dir(self):
        return self.state / 'tasks' / self.task_id

    def attempt(self, task):
        return self.task_dir() / f"attempt-{task['attempt']:04d}"

    def next_attempt(self, task):
        # A crash may leave a directory without a completed transaction. Its
        # number and bytes remain reserved permanently.
        return max([task['attempt']] + [int(p.name.removeprefix('attempt-'))
                   for p in self.task_dir().glob('attempt-*')]) + 1

    def current(self, task):
        m = task['manifest']
        return snapshot(Path(m['workspace']), m['excludes'])

    def prepare_workspace(self, task, before, current):
        m = task['manifest']
        return clean_bytecode(Path(m['workspace']), before, current, m['allowed_paths'],
                              m['excludes'], self.task_dir(), self.state / 'blobs')

    def recover(self):
        """Called under the workspace lock after a previous frontend exited."""
        with self.store() as s:
            t = s.get(self.task_id)
            if t['pid']:
                stop_group(t['pid'], t['pid_start'])
                if process_start(t['pid']) == t['pid_start']:
                    raise Problem('Old reviewer/check process exit is unconfirmed')
            if t['state'] in ('implementing', 'checking', 'reviewing'):
                s.update(self.task_id, state='paused', pid=None, pid_start=None,
                         reason='Previous Pi session interrupted. Explicit resume required; no prompt replayed.')

    def status(self):
        with self.store() as s:
            t = s.get(self.task_id)
            result = {k: t[k] for k in ('id', 'state', 'round', 'attempt', 'phase', 'reason')}
            result['handoff'] = str(Path(t['manifest']['workspace']) / t['manifest']['handoff'])
            result['evidence'] = str(self.attempt(t)) if t['attempt'] else str(self.task_dir())
            result['feedback'] = t['feedback']
            result['allowed_paths'] = t['manifest']['allowed_paths']
            result['checks'] = t['manifest']['checks']
            return result

    def begin(self):
        with self.store() as s:
            t = s.get(self.task_id)
            if t['state'] not in ('ready', 'needs_changes'):
                raise Problem('Cannot start implementation from ' + t['state'])
            if digest(self.current(t)) != t['expected_digest']:
                raise Problem('Workspace changed outside the recorded attempt')
            s.update(self.task_id, state='implementing', phase='pi', reason='')
        return self.status()

    def submit(self, request_id, markdown, runtime):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise Problem('Invalid submission request ID')
        if not isinstance(markdown, str) or not markdown.strip() or len(markdown.encode()) > 1_000_000:
            raise Problem('Submission must be nonempty Markdown, at most 1 MB')
        if runtime != {'provider': 'bonsai', 'model': 'bonsai2-27b', 'thinking': 'xhigh'}:
            raise Problem('Pi runtime must be bonsai/bonsai2-27b/xhigh')
        with self.store() as s:
            # Only the durable transaction is a receipt; an orphan request.json
            # never means dispatch succeeded.
            for row in s.db.execute("SELECT payload FROM events WHERE task_id=? AND kind='interactive_submitted'", (self.task_id,)):
                old = json.loads(row['payload'])
                if old['id'] == request_id:
                    if old['summary_sha256'] != hashlib.sha256(markdown.encode()).hexdigest():
                        raise Problem('Submission ID reused with different content')
                    return self.status()
            t = s.get(self.task_id)
            if t['state'] != 'implementing':
                raise Problem('Submission requires an active implementation')
            before = json.loads((self.task_dir() / 'intake.json').read_text())
            after = self.current(t)
            try:
                after = self.prepare_workspace(t, before, after)
            except ScopeViolation as exc:
                # Only this pre-acceptance gate can declare a definite rejection.
                # Storage/transport failures and later crashes remain uncertain.
                rejection = Path(tempfile.mkdtemp(prefix='rejected-', dir=self.task_dir()))
                preserve(Path(t['manifest']['workspace']), after, self.state / 'blobs')
                write_json(rejection / 'snapshot.json', after)
                write_json(rejection / 'request.json', {'id': request_id, 'markdown': markdown,
                                                       'runtime': runtime, 'reason': str(exc)})
                with s.db:
                    s._update(self.task_id, {'reason': 'Submission rejected: ' + str(exc)})
                    s.event(self.task_id, 'interactive_submission_rejected',
                            {'id': request_id, 'evidence': str(rejection), 'reason': str(exc)})
                raise SubmissionRejected(str(exc), self.status()) from exc
            preserve(Path(t['manifest']['workspace']), after, self.state / 'blobs')
            n = self.next_attempt(t)
            s.update(self.task_id, attempt=n, state='checking', phase='submission',
                     expected_digest=digest(after), reason='', review_resume=None)
            a = self.task_dir() / f'attempt-{n:04d}'
            a.mkdir(exist_ok=False)
            write_json(a / 'before.json', before)
            write_json(a / 'submission.json', after)
            write_json(a / 'diff.json', {'paths': changes(before, after), 'digest': digest(after)})
            summary = (f"---\ntask: {self.task_id}\nround: {t['round']}\n"
                       f"submission_digest: {digest(after)}\nauthor: Pi\n---\n\n{markdown.rstrip()}\n")
            (a / 'submission.md').write_text(summary)
            (a / 'delivery').mkdir()
            (a / 'delivery/summary.md').write_text(summary)
            if self.session_dir is not None:
                sessions = list(Path(self.session_dir).glob('*.jsonl'))
                if len(sessions) != 1 or sessions[0].is_symlink():
                    raise Problem('Expected exactly one native Pi session evidence file')
                (a / 'pi-session.jsonl').write_bytes(sessions[0].read_bytes())
            write_json(a / 'request.json', {'id': request_id, 'summary_sha256': hashlib.sha256(markdown.encode()).hexdigest(),
                                           'runtime': runtime, 'source': 'Pi extension runtime; not server weight attestation'})
            with s.db:
                s._update(self.task_id, {'phase': 'checks'})
                s.event(self.task_id, 'interactive_submitted', {'id': request_id, 'attempt': n,
                        'summary_sha256': hashlib.sha256(markdown.encode()).hexdigest(), 'digest': digest(after)})
            self._spawn()
        return self.status()

    def _spawn(self):
        if self.thread and self.thread.is_alive():
            raise Problem('Previous review is still stopping')
        self.cancel.clear()
        self.thread = threading.Thread(target=self._run, name='codinator-review', daemon=True)
        self.thread.start()

    def _pid(self, pid=None, start=None):
        with self.store() as s:
            s.update(self.task_id, pid=pid, pid_start=start)

    def _options(self, timeout):
        return dict(timeout=timeout, cancel=self.cancel.is_set,
                    on_start=self._pid, on_exit=self._pid)

    def _run(self):
        try:
            with self.store() as s:
                t = s.get(self.task_id)
                if t['state'] not in ('checking', 'reviewing') or self.cancel.is_set():
                    raise Interrupted('Workflow paused before review thread started')
            a, m = self.attempt(t), t['manifest']
            root = Path(m['workspace'])
            source = a
            resumed = (a / 'review-source.json').exists()
            if resumed:
                pinned = json.loads((a / 'review-source.json').read_text())
                if pinned != t['review_resume']:
                    raise Problem('Review checkpoint metadata changed')
                source = self.task_dir() / pinned['attempt']
                self._check_evidence(source, pinned['digest'])
                checked_digest = pinned['digest']
            if digest(self.current(t)) != t['expected_digest']:
                raise Problem('Frozen submission changed before verification')
            failures = []
            if not resumed:
                for check in m['checks']:
                    try:
                        run_process(self.sandbox.wrap(check['argv'], root), cwd=root,
                                    env=os.environ | {'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
                                    out=a / 'checks' / check['name'], **self._options(check['timeout_seconds']))
                    except Interrupted:
                        raise
                    except Problem as exc:
                        failures.append({'id': 'checks:' + check['name'], 'priority': 'P1', 'path': check['name'],
                                         'description': str(exc), 'required_change': 'Fix the required check within the published scope',
                                         'validation': 'Run the original check; read its raw evidence'})
                checked_digest = self._evidence_digest(a)
                pin = {'attempt': a.name, 'digest': checked_digest, 'passed': not failures}
                write_json(a / 'check-evidence.json', pin)
                with self.store() as s:
                    if s.get(self.task_id)['state'] != 'checking' or self.cancel.is_set():
                        raise Interrupted('Workflow paused during checks')
                    s.update(self.task_id, review_resume=pin)
            if digest(self.current(t)) != t['expected_digest']:
                raise Problem('Workspace changed during checks; rejecting stale submission')
            if self.cancel.is_set():
                raise Interrupted('User paused the workflow')
            if failures:
                verdict = {'verdict': 'needs_changes', 'summary': 'Required controller checks failed.', 'issues': failures}
                author = 'Controller checks'
            else:
                with self.store() as s:
                    if s.get(self.task_id)['state'] not in ('checking', 'reviewing') or self.cancel.is_set():
                        raise Interrupted('Workflow paused before Codex dispatch')
                    s.update(self.task_id, state='reviewing', phase='codex')
                private = self.state / 'private' / self.task_id / a.name
                private.mkdir(parents=True, mode=0o700)
                verdict = reviewer(t, a, t['expected_digest'], self._options(m['attempt_seconds']), self.codex_bin,
                                   sandbox=self.sandbox, private=private, evidence_dir=source)
                author = 'Codex'
            with self.store() as s:
                if self.cancel.is_set() or s.get(self.task_id)['state'] not in ('checking', 'reviewing'):
                    raise Interrupted('User paused before verdict publication')
                if digest(self.current(t)) != t['expected_digest']:
                    raise Problem('Workspace changed during review; rejecting stale verdict')
                self._check_evidence(source, checked_digest)
                write_json(a / 'outcome.json', verdict)
                report = self._review_markdown(t, verdict, author)
                (a / 'review.md').write_text(report)
                state, reason = verdict['verdict'], verdict['summary']
                issues = json.dumps(sorted(i['id'] for i in verdict['issues']))
                next_round = t['round']
                if state == 'needs_changes':
                    if t['round'] >= m['max_rounds']:
                        state, reason = 'blocked', 'Automatic rework round budget exhausted'
                    else:
                        # Stable issue IDs can describe partially repaired work.
                        # The published round limit bounds automatic rework.
                        next_round += 1
                s.update(self.task_id, state=state, phase='done' if state == 'accepted' else 'feedback',
                         round=next_round, feedback=report, last_issues=issues, reason=reason)
        except Exception as exc:
            # This thread is the external execution boundary: unexpected failures
            # must persist as blocked, never leave a phantom active review.
            with self.store() as s:
                current = s.get(self.task_id)
                reason = current['reason'] if current['state'] == 'paused' else str(exc)
                s.update(self.task_id, state='paused' if self.cancel.is_set() else 'blocked', reason=reason)

    @staticmethod
    def _review_markdown(task, verdict, author):
        rows = [f"---\ntask: {task['id']}\nround: {task['round']}\nsubmission_digest: {task['expected_digest']}\n"
                f"verdict: {verdict['verdict']}\nauthor: {author}\n---\n", verdict['summary']]
        for i in verdict['issues']:
            rows.append(f"\n## {i['id']} ({i['priority']}) — {i['path']}\n\n{i['description']}\n\n"
                        f"Required change: {i['required_change']}\n\nValidation: {i['validation']}")
        return '\n'.join(rows) + '\n'

    @staticmethod
    def _evidence_digest(a):
        paths = [a / p for p in ('before.json', 'submission.json', 'diff.json', 'submission.md', 'delivery/summary.md', 'request.json')]
        paths += sorted((a / 'checks').rglob('*'))
        if (a / 'pi-session.jsonl').exists():
            paths.append(a / 'pi-session.jsonl')
        content = {}
        for p in paths:
            if p.is_symlink():
                raise Problem('Evidence may not contain symlinks')
            if p.is_file():
                content[str(p.relative_to(a))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return digest(content)

    def _check_evidence(self, a, expected):
        if self._evidence_digest(a) != expected:
            raise Problem('Immutable submission/check evidence changed')

    def pause(self, reason='Paused by user or Pi session exit'):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 10000:
            raise Problem('Pause reason must be nonempty text, at most 10000 characters')
        with self.store() as s:
            self.cancel.set()
            t = s.get(self.task_id)
            if t['state'] in ('ready', 'implementing', 'checking', 'reviewing', 'needs_changes'):
                s.update(self.task_id, state='paused', reason=reason)
        return self.status()

    def resume(self):
        with self.store() as s:
            t = s.get(self.task_id)
            if t['state'] not in ('paused', 'blocked'):
                raise Problem('Only paused/blocked tasks can resume')
            if self.thread and self.thread.is_alive():
                raise Problem('Previous check/reviewer is still stopping; retry resume after it exits')
            if t['phase'] in ('', 'pi', 'submission'):
                current = self.current(t)
                intake = json.loads((self.task_dir() / 'intake.json').read_text())
                current = self.prepare_workspace(t, intake, current)
                preserve(Path(t['manifest']['workspace']), current, self.state / 'blobs')
                s.update(self.task_id, state='ready', phase='pi', reason='', expected_digest=digest(current))
            elif t['phase'] == 'feedback':
                legacy_block = t['state'] == 'blocked'
                if legacy_block:
                    if t['reason'] != 'Two consecutive reviews retain the same issue set':
                        raise Problem('Task requires a new contract or explicit investigation; automatic resume is unavailable')
                    if t['round'] >= t['manifest']['max_rounds']:
                        raise Problem('Automatic rework round budget exhausted')
                if digest(self.current(t)) != t['expected_digest']:
                    raise Problem('Workspace changed after review')
                a = self.attempt(t)
                verdict = json.loads((a / 'outcome.json').read_text())
                if verdict['verdict'] != 'needs_changes':
                    raise Problem('No pending implementation rework')
                if legacy_block:
                    checked = t['review_resume']
                    if checked is None:
                        raise Problem('Missing submission/check evidence checkpoint')
                    self._check_evidence(self.task_dir() / checked['attempt'], checked['digest'])
                    if checked['passed']:
                        validate_verdict(verdict, self.task_id, t['expected_digest'])
                    author = 'Codex' if checked['passed'] else 'Controller checks'
                    if (self._review_markdown(t, verdict, author) != t['feedback']
                            or (a / 'review.md').read_text() != t['feedback']):
                        raise Problem('Recorded review changed; reconciliation required')
                # A paused feedback state already scheduled its next round;
                # the old duplicate-issue blocker stopped before that increment.
                s.update(self.task_id, state='needs_changes', reason='',
                         round=t['round'] + int(legacy_block))
            elif t['phase'] in ('checks', 'codex'):
                if digest(self.current(t)) != t['expected_digest']:
                    raise Problem('Frozen submission changed; cannot resume its review')
                old = self.attempt(t)
                if (old / 'outcome.json').exists() or (old / 'review-delivery/verdict.json').exists():
                    raise Problem('Existing verdict requires reconciliation; refusing an uncertain replay')
                checked = t['review_resume']
                if checked is not None and checked['passed'] is not True:
                    raise Problem('Failed checks require implementation rework')
                source = self.task_dir() / checked['attempt'] if checked else old
                if checked:
                    self._check_evidence(source, checked['digest'])
                n = self.next_attempt(t)
                a = self.task_dir() / f"attempt-{n:04d}"
                a.mkdir(exist_ok=False)
                if checked:
                    write_json(a / 'review-source.json', checked)
                else:
                    # Interrupted checks: a fresh run is explicit, old artifacts stay.
                    for name in ('before.json', 'submission.json', 'diff.json', 'submission.md', 'delivery/summary.md', 'request.json', 'pi-session.jsonl'):
                        src = source / name
                        if not src.exists() and name == 'pi-session.jsonl':
                            continue
                        dst = a / name
                        dst.parent.mkdir(exist_ok=True, parents=True)
                        dst.write_bytes(src.read_bytes())
                s.update(self.task_id, attempt=n, state='reviewing' if checked else 'checking',
                         phase='codex' if checked else 'checks', reason='')
                self._spawn()
            else:
                raise Problem('Task requires a new contract or explicit investigation; automatic resume is unavailable')
        return self.status()

    def close(self):
        self.pause('Pi session exit')
        if self.thread:
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                raise Problem('Review process did not stop; retain the workspace lock')
