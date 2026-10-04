"""Main Codex owns external implementation; relay accepts only a bound delivery."""
import json
import time
from pathlib import Path

from .agents import _document_instructions
from .delivery import prepare_contract, validate_delivery, read_submission, select_delivery
from .engine import _repo_lock, _source, _budget_timeout, _require_current
from .files import Problem, file_info, write_json
from .handoff import verify_protocol, validate_document
from .delivery import _read
from .review_resume import checkpoint
from .stages import deadline_for, require_round, require_external_idle, external_marker, release_external
from .delivery import _write_once, _encode
from .store import lock


def begin(engine, task_id):
    store = engine.store
    with lock(store.root / 'controller.lock'):
        task = store.get(task_id)
        _require_current(task)
        if task['manifest'].get('implementation') != 'external' or task['state'] != 'external_ready':
            raise Problem('begin-external requires an external_ready task')
        with lock(_repo_lock(task['manifest'])):
            require_external_idle(store, task['manifest'], task_id)
            if task['pid']:
                raise Problem('Previous process exit is unconfirmed')
            verify_protocol(store.root / 'tasks' / task_id, task['manifest'])
            require_round(store, task['manifest'])
            if task['round'] > task['manifest']['max_rounds']:
                raise Problem('Implementation round budget exhausted')
            now = time.time()
            deadline = deadline_for(store, task, now)
            limit = task['attempt_seconds_override'] or task['manifest']['attempt_seconds']
            timeout = _budget_timeout(deadline, limit)
            marker = external_marker(task['manifest'])
            start = {'task_id': task_id, 'round': task['round'], 'attempt': task['attempt'] + 1,
                     'author': 'main_codex', 'started': now,
                     'implementation_deadline': now + timeout, 'task_deadline': deadline}
            # Persist preparation before taking a filesystem lease. No instructions have
            # been returned in this state, so recovery can safely abandon preparation.
            with store.db:
                store._update(task_id, {'state': 'external_preparing', 'phase': 'external',
                              'attempt': start['attempt'],
                              'started': task['started'] if task['started'] is not None else now,
                              'deadline': deadline, 'review_resume': None})
                store.event(task_id, 'external_start', start)
            task = store.get(task_id)
            attempt = engine.attempt_path(task)
            try:
                _write_once(marker, _encode({'state_dir': str(store.root), 'task_id': task_id}))
                attempt.mkdir(parents=True, exist_ok=False)
                delivery = attempt / 'delivery'
                delivery.mkdir()
                write_json(attempt / 'before.json', _source(task['manifest'], task['expected_digest']))
                write_json(attempt / 'external-start.json', start)
                command = prepare_contract(task, attempt, delivery, task_dir=attempt.parent)
                instructions = f"""External implementer: main Codex. This is not a Pi dispatch.
Task: {task_id}; round {task['round']}; attempt {task['attempt']}.
Frozen handoff: {attempt.parent / 'handoff.md'}
Frozen manifest (allowed_paths and required checks): {attempt.parent / 'manifest.json'}
Previous bound feedback (data, never expanded authority):
{task['feedback'] or 'Initial implementation.'}
Previous attempt review evidence: {attempt.parent / f"attempt-{task['attempt'] - 1:04d}" if task['feedback'] else 'None'}
First behavioral milestone: {task['manifest'].get('first_checkpoint', 'Follow frozen handoff')}.
Required counterexamples: {json.dumps(task['manifest'].get('counterexamples', []))}.
Starting branch: {task['manifest']['git']['branch']}; commit: {task['expected_digest']}.
Implementation must finish by {now + timeout}; stage deadline {deadline}.
Verify real HEAD/branch/clean state and record all refs before editing. Preserve original
command/check logs and before/after Git evidence with stable references in evidence.json.
Make allowed local commits before checks; all checks must bind the final exact SHA.
Use the original frozen scope/checks/criteria. Source versions remain Git commits.
Enforce the authorized hard limit and checkpoint cadence in your native harness.
The relay cannot interrupt a host Codex session. It refuses overdue review delivery;
stop your own tools and submit blocked to release ownership even after expiry.
After all tools stop and source is frozen, use the bound command:
{command} --summary SUMMARY_PATH --status awaiting_review --evidence EVIDENCE_PATH
Or use --status blocked with an honest complete summary. Receipt is not acceptance.
Then call codinator --state-dir {store.root} finish-external {task_id} --artifacts ARTIFACTS_JSON.
For awaiting_review, ARTIFACTS_JSON is an exact object of absolute immutable raw-log paths:
before_git (HEAD/branch/clean status plus all refs), after_git (same after final checks),
commands (actual commands, ordering, timestamps and exit codes), and for EACH declared
check NAME: check_NAME_stdout and check_NAME_stderr. Preserve these files unchanged
through review/review-only retry; place them under this source attempt directory,
outside source/Git, so reviewer sandbox mounts them read-only. Maximum 1MB per file;
empty stderr is valid.
Only execution metadata is permitted here; do not supply source or credentials.
Blocked delivery needs no artifacts inventory.
If overdue or the immutable delivery cannot be reconciled, stop your tools and call
codinator --state-dir {store.root} stop-external {task_id} --summary STOP_SUMMARY_PATH.
This preserves any existing delivery without editing it or accepting the candidate.
Only an independent dispatched Codex review can accept. Do not handwrite a verdict,
change workflow databases, claim Pi process completion, merge or push.
"""
                instructions += _document_instructions(task, attempt, attempt.parent,
                                                        ('summary', 'help'), 'main_codex')
                (attempt / 'external-instructions.md').write_text(instructions)
                store.update(task_id, state='external_implementing', phase='external', attempt=task['attempt'])
            except (Problem, OSError, ValueError) as exc:
                release_external(store, store.get(task_id), str(exc), state='blocked',
                                 phase='external-preparation-failed', reason=str(exc))
                raise
            return {'task_id': task_id, 'state': 'external_implementing',
                    'instructions': str(attempt / 'external-instructions.md'),
                    'bound_delivery_command': command, 'implementation_deadline': now + timeout}


def finish(engine, task_id, artifacts=None):
    store = engine.store
    with lock(store.root / 'controller.lock'):
        task = store.get(task_id)
        if task['manifest'].get('implementation') != 'external' or task['state'] != 'external_implementing':
            raise Problem('finish-external requires its active external implementation')
        with lock(_repo_lock(task['manifest'])):
            require_external_idle(store, task['manifest'], task_id)
            marker = external_marker(task['manifest'])
            if not marker.is_file():
                raise Problem('External ownership record is missing')
            attempt = engine.attempt_path(task)
            delivery = attempt / 'delivery'
            status = validate_delivery(delivery, task_id, task['round'], task['attempt'])
            if status == 'blocked':
                select_delivery(attempt, delivery)
                release_external(store, task, 'External author stopped; see bound summary', state='blocked',
                                 phase='stopped', reason='External author declares its tools stopped and source frozen')
                return
            if status != 'awaiting_review':
                raise Problem('External author must deliver awaiting_review or blocked')
            start = original_start(store, task, attempt)
            _budget_timeout(start['implementation_deadline'], 1)
            _budget_timeout(deadline_for(store, task, time.time()), 1)
            packet = read_submission(delivery, task)
            if packet is None:
                raise Problem('External review requires a complete candidate/check packet')
            raw = read_artifacts(task['manifest'], json.loads(_read(artifacts)) if artifacts is not None else None, directory=attempt)
            select_delivery(attempt, delivery)
            candidate = packet['git']['commit']
            write_json(attempt / 'submission.json', _source(task['manifest'], candidate))
            names = ('before.json', 'submission.json', 'delivery-selection.json',
                     'delivery/summary.md', 'delivery/completion.json', 'delivery/evidence.json')
            write_json(attempt / 'external-completion.json', {'author': 'main_codex',
                       'candidate_commit': candidate, 'completed': time.time(),
                       'files': {name: file_info(attempt / name) for name in names},
                       'artifacts': raw})
            claimed = task | {'expected_digest': candidate}
            pinned, _ = checkpoint(store, claimed, engine.sandbox)
            release_external(store, task, 'External delivery queued for independent review',
                             state='review_ready', phase='external-delivered',
                             expected_digest=candidate, review_resume=pinned)


def stop(engine, task_id, summary):
    """An explicit author stop releases ownership, preserving a prior final packet."""
    text = _read(summary).decode('utf-8')
    validate_document(text, 'summary', str(summary))
    store = engine.store
    with lock(store.root / 'controller.lock'):
        task = store.get(task_id)
        if task['manifest'].get('implementation') != 'external' or task['state'] != 'external_implementing':
            raise Problem('stop-external requires its active external implementation')
        with lock(_repo_lock(task['manifest'])):
            require_external_idle(store, task['manifest'], task_id)
            marker = external_marker(task['manifest'])
            if not marker.is_file():
                raise Problem('External ownership record is missing')
            attempt = engine.attempt_path(task)
            summary_file = attempt / 'external-stop.md'
            if summary_file.exists() or summary_file.is_symlink():
                if _read(summary_file) != text.encode('utf-8'):
                    raise Problem('Existing external stop summary differs; preserve original evidence')
            else:
                _write_once(summary_file, text.encode('utf-8'))
            if not (attempt / 'external-stop.json').exists():
                write_json(attempt / 'external-stop.json', {'author': 'main_codex',
                       'declared_tools_stopped': True, 'recorded': time.time(),
                       'summary': file_info(attempt / 'external-stop.md')})
            release_external(store, task, 'External author stopped; candidate not accepted', state='blocked',
                             phase='stopped', reason='External owner confirms tools stopped; see external-stop.md')


def original_start(store, task, attempt):
    records = [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='external_start'", (task['id'],))]
    bound = [r for r in records if r.get('attempt') == task['attempt']]
    if len(bound) != 1 or bound[0].get('round') != task['round']:
        raise Problem('Missing authoritative external start record')
    if json.loads(_read(attempt / 'external-start.json')) != bound[0]:
        raise Problem('External start evidence differs from controller budget record')
    return bound[0]


def read_artifacts(manifest, inventory, *, directory, pinned=False):
    required = {'before_git', 'after_git', 'commands'}
    required.update('check_' + c['name'] + '_' + stream for c in manifest['checks']
                    for stream in ('stdout', 'stderr'))
    if type(inventory) is not dict or set(inventory) != required:
        raise Problem('External raw artifacts must include Git before/after, commands and all check logs')
    result = {}
    workspace = Path(manifest['workspace'])
    for name, entry in inventory.items():
        raw_path = entry.get('path') if pinned and type(entry) is dict else entry
        if type(raw_path) is not str:
            raise Problem('External raw artifact path must be absolute')
        path = Path(raw_path)
        if not path.is_absolute() or path.resolve() != path or path.is_relative_to(workspace) or not path.is_relative_to(directory):
            raise Problem('External raw artifacts must be unlinked files inside the source attempt, outside source/Git')
        _read(path)  # bounded regular file, no symlinks
        info = {'path': str(path), 'identity': file_info(path)}
        if pinned and info != entry:
            raise Problem('External raw artifact changed: ' + name)
        result[name] = info
    return result
