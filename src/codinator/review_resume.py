"""Read-only validation of the original implementation evidence for review retries."""
import json
from pathlib import Path

from .files import Problem, file_info
from .delivery import selected_delivery, validate_delivery, read_submission


def require_unfinished_review(attempt):
    # A verdict may have been published just before the controller crashed.
    # Never use a retry to bypass that decision, even without outcome.json.
    for name in ('outcome.json', 'review-delivery/verdict.json'):
        path = attempt / name
        if path.exists() or path.is_symlink():
            raise Problem('Existing verdict/outcome requires inspection; review-only resume refused')


def checkpoint(store, task, sandbox, *, accepted=False):
    """Pin original handoff evidence; the reviewer must independently recheck Git/tests."""
    if task['manifest']['version'] != 2:
        raise Problem('Version 1 tasks are read-only history')
    previous = task['review_resume']
    number = previous['source_attempt'] if previous else task['attempt']
    if type(number) is not int or not 0 < number <= task['attempt']:
        raise Problem('No implementation attempt eligible for review-only resume')
    source = store.root / 'tasks' / task['id'] / f'attempt-{number:04d}'
    files = {}

    def evidence(name):
        path = source.parent / name[3:] if name.startswith('../') else source / name
        if path.resolve() != path.absolute() or not path.is_file():
            raise Problem(f'Missing or linked review evidence: {name}')
        info = file_info(path)
        if info['kind'] != 'file':
            raise Problem(f'Invalid review evidence: {name}')
        files[name] = info
        return path

    def document(name):
        value = json.loads(evidence(name).read_text())
        if type(value) is not dict:
            raise Problem(f'Review evidence must be a JSON object: {name}')
        return value

    def successful_process(name, streams):
        result = document(name + '/result.json')
        if (type(result.get('exit_code')) is not int or result['exit_code'] != 0
                or 'failure' not in result or result['failure'] is not None):
            raise Problem(f'Review requires successful process evidence: {name}')
        recorded = result.get('streams')
        actual = {s: file_info(evidence(name + '/' + s)) for s in streams}
        if recorded != actual:
            raise Problem(f'Review process evidence hashes do not match: {name}')
        return document(name + '/launch.json')

    # State proves dispatch reached review, not that project checks or Git were verified.
    attempt, entered_review, entered_repair = 0, False, False
    for row in store.db.execute("SELECT payload FROM events WHERE task_id=? AND kind='state' ORDER BY seq", (task['id'],)):
        fields = json.loads(row[0])
        attempt = fields.get('attempt', attempt)
        if attempt == number and fields.get('state') == 'reviewing' and fields.get('phase') == 'codex':
            entered_review = True
        if attempt == number and fields.get('state') == 'checking' and fields.get('phase') == 'delivery-repair':
            entered_repair = True
    if not entered_review:
        raise Problem('No controller checkpoint proving this attempt reached review')
    if not accepted:
        require_unfinished_review(source)
    before, submitted = document('before.json'), document('submission.json')
    manifest = task['manifest']
    for name in ('delivery-selection.json', 'delivery-contract.json', 'submit-delivery.py'):
        evidence(name)
    delivery = selected_delivery(source)
    submission_task = task | {'attempt': number, 'expected_digest': before.get('commit')}
    packet = read_submission(delivery, submission_task)
    if packet is None:
        raise Problem('Review requires a complete agent submission packet')
    fingerprint = packet['git']['commit']
    expected = {'kind': 'git', 'branch': manifest['git']['branch'], 'commit': fingerprint}
    if submitted != expected or fingerprint != task['expected_digest']:
        raise Problem('Review submission no longer matches the recorded agent claim')
    if before != expected | {'commit': packet['git']['base_commit']}:
        raise Problem('Review submission base does not match the original handoff')

    def successful_pi(prefix=''):
        successful_process(prefix + 'pi', ('stdout.jsonl', 'stderr.txt'))
        identity = document(prefix + 'pi-runtime.json')
        if (identity.get('provider'), identity.get('model'), identity.get('thinking')) != ('bonsai', 'bonsai2-27b', 'xhigh'):
            raise Problem('Unexpected Pi runtime identity in review evidence')
        ack, settled, stop = False, False, None
        with (source / (prefix + 'pi/stdout.jsonl')).open() as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                if type(event) is not dict:
                    raise Problem('Invalid Pi protocol evidence')
                if event.get('type') == 'response' and event.get('id') == 'prompt':
                    ack = event.get('success') is True
                if event.get('type') == 'agent_settled':
                    settled = True
                message = event.get('message')
                if event.get('type') == 'message_end' and type(message) is dict and message.get('role') == 'assistant':
                    stop = message.get('stopReason')
        if not (ack and settled and stop == 'stop'):
            raise Problem('Pi completion protocol evidence is missing or unsuccessful')

    successful_pi()
    delivery = selected_delivery(source)
    if delivery != source / 'delivery':
        if not entered_repair:
            raise Problem('No controller checkpoint proving delivery repair')
        error = document('delivery-error.json')
        if error.get('repairable') is not True or not error.get('errors'):
            raise Problem('Missing recoverable delivery error evidence')
        successful_pi('delivery-repair/')
        for name in ('delivery-contract.json', 'submit-delivery.py', 'worker-prompt.txt'):
            evidence('delivery-repair/' + name)
    prefix = delivery.relative_to(source).as_posix() + '/'
    document(prefix + 'completion.json')
    if validate_delivery(delivery, task['id'], task['round'], number) != 'awaiting_review':
        raise Problem('Pi completion identity/status does not match the review source')
    evidence(prefix + 'summary.md')
    evidence(prefix + 'evidence.json')
    # Freeze the actual published requirements and original prompt along with the
    # protocol evidence. Project checks are rerun by Codex, never by this module.
    evidence('../handoff.md')
    evidence('../manifest.json')
    evidence('worker-prompt.txt')
    result = {'source_attempt': number, 'source_round': task['round'],
              'submission_digest': fingerprint, 'files': files}
    if previous is not None and result != previous:
        raise Problem('Pinned review evidence changed; refusing to reuse checks')
    return result, source
