"""Main Codex owns external implementation; relay accepts only a bound delivery."""
import json
import time

from .agents import _document_instructions
from .delivery import prepare_contract, validate_delivery, read_submission, select_delivery
from .engine import _repo_lock, _source, _budget_timeout, _require_current
from .files import Problem, file_info, write_json
from .handoff import verify_protocol, validate_document
from .delivery import _read
from .review_resume import checkpoint
from .stages import deadline_for, require_round, require_external_idle, external_marker
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
            _write_once(marker, _encode({'state_dir': str(store.root), 'task_id': task_id}))
            store.update(task_id, state='external_implementing', phase='external',
                         attempt=task['attempt'] + 1, started=task['started'] if task['started'] is not None else now,
                         deadline=deadline, review_resume=None)
            task = store.get(task_id)
            attempt = engine.attempt_path(task)
            try:
                attempt.mkdir(parents=True, exist_ok=False)
                delivery = attempt / 'delivery'
                delivery.mkdir()
                write_json(attempt / 'before.json', _source(task['manifest'], task['expected_digest']))
                write_json(attempt / 'external-start.json', {'author': 'main_codex',
                           'started': now, 'implementation_deadline': now + timeout,
                           'task_deadline': deadline})
                command = prepare_contract(task, attempt, delivery, task_dir=attempt.parent)
                instructions = f"""External implementer: main Codex. This is not a Pi dispatch.
Frozen handoff: {attempt.parent / 'handoff.md'}
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
Then call codinator --state-dir {store.root} finish-external {task_id}.
If overdue or the immutable delivery cannot be reconciled, stop your tools and call
codinator --state-dir {store.root} stop-external {task_id} --summary STOP_SUMMARY_PATH.
This preserves any existing delivery without editing it or accepting the candidate.
Only an independent dispatched Codex review can accept. Do not handwrite a verdict,
change workflow databases, claim Pi process completion, merge or push.
"""
                instructions += _document_instructions(task, attempt, attempt.parent,
                                                        ('summary', 'help'), 'main_codex')
                (attempt / 'external-instructions.md').write_text(instructions)
            except (Problem, OSError, ValueError) as exc:
                store.finish(task_id, str(exc), state='blocked', phase='stopped', reason=str(exc))
                marker.unlink()  # No external instructions were delivered, so no author has started.
                raise
            return {'task_id': task_id, 'state': 'external_implementing',
                    'instructions': str(attempt / 'external-instructions.md'),
                    'bound_delivery_command': command, 'implementation_deadline': now + timeout}


def finish(engine, task_id):
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
                store.finish(task_id, 'External author stopped; see bound summary', state='blocked',
                             phase='stopped', reason='External author declares its tools stopped and source frozen')
                marker.unlink()
                return
            if status != 'awaiting_review':
                raise Problem('External author must deliver awaiting_review or blocked')
            start = json.loads((attempt / 'external-start.json').read_text())
            _budget_timeout(start['implementation_deadline'], 1)
            _budget_timeout(deadline_for(store, task, time.time()), 1)
            packet = read_submission(delivery, task)
            if packet is None:
                raise Problem('External review requires a complete candidate/check packet')
            select_delivery(attempt, delivery)
            candidate = packet['git']['commit']
            write_json(attempt / 'submission.json', _source(task['manifest'], candidate))
            names = ('before.json', 'submission.json', 'delivery-selection.json',
                     'delivery/summary.md', 'delivery/completion.json', 'delivery/evidence.json')
            write_json(attempt / 'external-completion.json', {'author': 'main_codex',
                       'candidate_commit': candidate, 'completed': time.time(),
                       'files': {name: file_info(attempt / name) for name in names}})
            claimed = task | {'expected_digest': candidate}
            pinned, _ = checkpoint(store, claimed, engine.sandbox)
            store.update(task_id, state='review_ready', phase='external-delivered',
                         expected_digest=candidate, review_resume=pinned)
            marker.unlink()


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
            _write_once(attempt / 'external-stop.md', text.encode('utf-8'))
            write_json(attempt / 'external-stop.json', {'author': 'main_codex',
                       'declared_tools_stopped': True, 'recorded': time.time(),
                       'summary': file_info(attempt / 'external-stop.md')})
            store.finish(task_id, 'External author stopped; candidate not accepted', state='blocked',
                         phase='stopped', reason='External owner confirms tools stopped; see external-stop.md')
            marker.unlink()
