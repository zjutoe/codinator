"""Read-only task reports for a frontend; never resume, dispatch or accept work."""
import json

from .checkpoints import status as checkpoint_status
from .files import Problem
from .process import process_start


def report(store, task):
    directory = store.root / 'tasks' / task['id']
    attempt = directory / f"attempt-{task['attempt']:04d}"
    keys = ('id', 'state', 'phase', 'round', 'attempt', 'reason', 'control', 'deadline')
    result = {key: task[key] for key in keys}
    result.update(workspace=task['manifest']['workspace'], evidence=str(directory),
                  attempt_evidence=str(attempt) if task['attempt'] else None,
                  attempt_seconds=task['attempt_seconds_override'] or task['manifest']['attempt_seconds'],
                  process_alive=bool(task['pid'] and task['pid_start']
                                     and process_start(task['pid']) == task['pid_start']),
                  latest_review=None)
    if task['manifest']['version'] == 2:
        result['git'] = {'branch': task['manifest']['git']['branch'],
                         'base_commit': task['manifest']['git']['base_commit'],
                         'checkpoint_commit': task['expected_digest'],
                         'verification': 'independent_codex' if task['state'] == 'accepted' else 'agent_claim_or_published_baseline'}
    if 'checkpoint_seconds' in task['manifest']:
        result['checkpoint_seconds'] = task['manifest']['checkpoint_seconds']
        result['latest_checkpoint'] = None
        for number in range(task['attempt'], 0, -1):
            progress = checkpoint_status(directory / f'attempt-{number:04d}', task['id'], number)
            if progress is not None:
                result['latest_checkpoint'] = progress
                break
    if task.get('integration'):
        result['integration'] = task['integration']
        result['review_accepted'] = True
    # The latest completed review may belong to an earlier implementation round.
    # An outcome file is evidence, never a replacement for the persisted task state.
    for number in range(task['attempt'], 0, -1):
        source = directory / f'attempt-{number:04d}'
        outcome = source / 'outcome.json'
        if not outcome.exists() and not outcome.is_symlink():
            continue
        if outcome.resolve() != outcome.absolute() or not outcome.is_file() or outcome.stat().st_size > 1_000_000:
            raise Problem(f'Invalid review evidence: {outcome}')
        value = json.loads(outcome.read_text())
        if (type(value) is not dict or value.get('verdict') not in ('accepted', 'needs_changes', 'blocked')
                or not isinstance(value.get('summary'), str)):
            raise Problem(f'Invalid review outcome: {outcome}')
        result['latest_review'] = {'attempt': number, 'verdict': value['verdict'],
                                   'summary': value['summary'], 'outcome': str(outcome),
                                   'markdown': str(source / 'review.md')}
        break
    result['next_action'] = (
        'none' if task['state'] in ('accepted', 'cancelled') else
        'prepare_v2_successor_preserving_budget' if task['manifest']['version'] == 1 else
        'inspect_evidence_before_explicit_resume' if task['state'] in ('paused', 'blocked') else
        'ensure_service_running_then_monitor')
    return result
