"""Read-only task reports for a frontend; never resume, dispatch or accept work."""
import json

from .checkpoints import status as checkpoint_status
from .files import Problem, file_info
from .delivery import selected_delivery, validate_delivery, _read, _json
from .handoff import enabled, verify_protocol
from .process import process_start
from .stages import stage_status


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
    shared = stage_status(store, task['manifest'])
    if shared is not None:
        result['stage'] = shared
    if task['manifest'].get('implementation') == 'external':
        result['implementation_author'] = 'main_codex'
        result['external_instructions'] = str(attempt / 'external-instructions.md') if task['attempt'] else None
    terminal = task['state'] in ('accepted', 'cancelled')
    stopped = task['state'] in ('paused', 'blocked', 'cancelled')
    result['current_handler'] = (None if terminal else 'main_codex' if stopped or task['state'] in ('external_ready', 'external_implementing') else
                                 'guidance_codex' if task['state'] == 'guiding' else
                                 'review_codex' if task['state'] in ('reviewing', 'review_ready') else 'pi')
    result['pending_reply_to'] = None
    result['summaries'] = {'final': None, 'latest_stage': None,
                           'final_missing': bool(task['attempt'] and stopped)}
    if task['attempt'] and attempt.exists():
        source = (directory / f"attempt-{task['review_resume']['source_attempt']:04d}"
                  if task.get('review_resume') else attempt)
        selection = source / 'delivery-selection.json'
        delivery = selected_delivery(source) if selection.exists() or selection.is_symlink() else source / 'delivery'
        contract = source / 'delivery-contract.json'
        binding = None
        if contract.exists() or contract.is_symlink():
            binding, errors, _ = _json(_read(contract), contract)
            if (errors or type(binding) is not dict or binding.get('task_id') != task['id']
                    or type(binding.get('round')) is not int or binding['round'] < 1
                    or binding.get('attempt') != (task['review_resume']['source_attempt']
                                                if task.get('review_resume') else task['attempt'])):
                raise Problem('Invalid summary source binding')
        summary = delivery / 'summary.md'
        if summary.exists() or summary.is_symlink():
            try:
                source_round = binding['round'] if binding else task['round']
                source_attempt = binding['attempt'] if binding else task['attempt']
                status = validate_delivery(delivery, task['id'], source_round, source_attempt)
                result['summaries']['final'] = {'path': str(summary), 'status': status,
                                                'round': source_round, 'attempt': source_attempt,
                                                'identity': file_info(summary)}
                result['summaries']['final_missing'] = False
            except (Problem, OSError, ValueError) as exc:
                result['summaries']['final_error'] = str(exc)
        if enabled(task['manifest']) and binding is not None:
            if type(binding.get('handoff')) is not dict:
                raise Problem('Invalid pending protocol binding')
            result['pending_reply_to'] = binding['handoff']['reply_to']
        if enabled(task['manifest']) and task['state'] in ('guiding', 'reviewing', 'review_ready'):
            value, errors, _ = _json(_read(delivery / 'completion.json'), delivery / 'completion.json')
            if errors or type(value) is not dict or type(value.get('message')) is not dict:
                raise Problem('Invalid pending guidance request')
            result['pending_reply_to'] = value['message']['message_id']
    if enabled(task['manifest']) and task['state'] in ('ready', 'needs_changes') and task['feedback']:
        feedback = json.loads(task['feedback'])
        if type(feedback) is not dict or type(feedback.get('message')) is not dict:
            raise Problem('Invalid queued protocol reply')
        result['pending_reply_to'] = feedback['message']['message_id']
    if enabled(task['manifest']):
        protocol = verify_protocol(directory, task['manifest'])
        result['handoff_protocol'] = {'version': 1, 'contract_digest': protocol['contract_digest']}
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
                result['summaries']['latest_stage'] = progress.get('latest_stage_summary')
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
