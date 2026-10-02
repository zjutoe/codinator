"""Optional implementation progress requests; never a workflow completion protocol."""
import argparse
import json
import math
from pathlib import Path
import shlex
import sys
import time

from .delivery import _directory, _encode, _json, _read, _write_once, validate_delivery
from .files import Problem, file_info


RESPONSE_GRACE_SECONDS = 300


def _document(path):
    value, errors, _ = _json(_read(path), path)
    if errors or type(value) is not dict:
        raise Problem(f'Invalid checkpoint JSON object: {path}')
    return value


def _identity(value):
    if (type(value.get('task_id')) is not str or not value['task_id']
            or any(type(value.get(k)) is not int or value[k] < 1 for k in ('round', 'attempt'))):
        raise Problem('Invalid checkpoint task identity')
    return {k: value[k] for k in ('task_id', 'round', 'attempt')}


def _contract(path):
    value = _document(path)
    if (set(value) != {'version', 'task_id', 'round', 'attempt', 'directory'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['directory']) is not str or not Path(value['directory']).is_absolute()):
        raise Problem('Invalid checkpoint contract')
    _identity(value)
    return value


def _bound(value, identity, number):
    if _identity(value) != identity or type(value.get('checkpoint')) is not int or value['checkpoint'] != number:
        raise Problem('Checkpoint evidence belongs to a different task/round/attempt/request')


def _request(directory, identity, number):
    value = _document(directory / 'requests' / f'{number:04d}.json')
    _bound(value, identity, number)
    if (set(value) != {*identity, 'checkpoint', 'rpc_id', 'requested_at', 'elapsed_seconds'}
            or value['rpc_id'] != f'checkpoint-{number:04d}'
            or any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or value[k] < 0
                   for k in ('requested_at', 'elapsed_seconds'))):
        raise Problem('Invalid checkpoint request')
    return value


def _progress(value):
    if type(value) is not dict or set(value) != {'completed', 'checks', 'blockers', 'next_step', 'needs_guidance'}:
        raise Problem('Progress requires only completed, checks, blockers, next_step and needs_guidance')
    for key in ('completed', 'blockers'):
        if type(value[key]) is not list or any(type(s) is not str or not s.strip() for s in value[key]):
            raise Problem(f'Progress {key} must be an array of nonempty strings')
    if (type(value['next_step']) is not str or not value['next_step'].strip()
            or type(value['needs_guidance']) is not bool or type(value['checks']) is not list):
        raise Problem('Progress requires a next_step string, needs_guidance boolean and checks array')
    for check in value['checks']:
        if (type(check) is not dict or set(check) != {'argv', 'exit_code', 'result', 'evidence'}
                or type(check['argv']) is not list or not check['argv']
                or any(type(s) is not str or not s for s in check['argv'])
                or type(check['exit_code']) is not int
                or any(type(check[k]) is not str or not check[k].strip() for k in ('result', 'evidence'))):
            raise Problem('Each completed check needs argv, integer exit_code, result and evidence; use [] if none completed')
    return value


def _report(directory, identity, number):
    value = _document(directory / 'reports' / f'{number:04d}.json')
    _bound(value, identity, number)
    if set(value) != {*identity, 'checkpoint', 'progress'}:
        raise Problem('Invalid checkpoint report fields')
    _progress(value['progress'])
    return value


def _resolution(directory, identity, number):
    value = _document(directory / 'resolutions' / f'{number:04d}.json')
    _bound(value, identity, number)
    source = value.get('source')
    if (source not in ('report', 'final_delivery')
            or set(value) != {*identity, 'checkpoint', 'source', 'elapsed_seconds', source}
            or type(value['elapsed_seconds']) not in (int, float)
            or not math.isfinite(value['elapsed_seconds']) or value['elapsed_seconds'] < 0):
        raise Problem('Invalid checkpoint resolution')
    if source == 'report':
        report = value[source]
        if type(report) is not dict or set(report) != {*identity, 'checkpoint', 'progress'}:
            raise Problem('Invalid resolved checkpoint report')
        _bound(report, identity, number)
        _progress(report['progress'])
    else:
        delivery = value[source]
        if (type(delivery) is not dict or set(delivery) != {'directory', 'status', 'files'}
                or delivery['directory'] != str(directory.parent / 'delivery')
                or delivery['status'] not in ('awaiting_review', 'blocked')
                or type(delivery['files']) is not dict
                or set(delivery['files']) != {'summary.md', 'completion.json'}):
            raise Problem('Invalid final-delivery checkpoint resolution')
    return value


def main(argv=None, *, contract_path):
    parser = argparse.ArgumentParser(description='Submit progress for this attempt; never complete or accept it')
    parser.add_argument('--request', required=True, type=int)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.request < 1:
            raise Problem('Checkpoint request must be positive')
        contract = _contract(Path(contract_path))
        identity = _identity(contract)
        directory = Path(contract['directory'])
        _request(directory, identity, args.request)
        progress = _progress(_document(args.report))
        report = identity | {'checkpoint': args.request, 'progress': progress}
        target = directory / 'reports' / f'{args.request:04d}.json'
        try:
            _write_once(target, _encode(report))
        except FileExistsError:
            if _report(directory, identity, args.request) != report:
                raise Problem('Conflicting checkpoint report; original evidence preserved')
        print(json.dumps({'status': 'progress_recorded', **identity, 'checkpoint': args.request}))
        return 0
    except (Problem, OSError) as exc:
        print(json.dumps({'status': 'rejected', 'error': str(exc)}), file=sys.stderr)
        return 2


class Checkpoints:
    def __init__(self, task, context, interval):
        self.identity = _identity({'task_id': task['id'], 'round': task['round'], 'attempt': task['attempt']})
        self.interval = interval
        self.context = Path(context).absolute()
        self.directory = self.context / 'checkpoints'
        self.directory.mkdir()
        for name in ('requests', 'acks', 'reports', 'resolutions'):
            (self.directory / name).mkdir()
        _write_once(self.directory / 'policy.json', _encode({'response_grace_seconds': RESPONSE_GRACE_SECONDS}))
        contract = Path(context).absolute() / 'checkpoint-contract.json'
        launcher = Path(context).absolute() / 'submit-checkpoint.py'
        _write_once(contract, _encode({'version': 1, **self.identity, 'directory': str(self.directory)}))
        source = Path(__file__).resolve().parents[1]
        program = (f'import sys\nsys.path.insert(0, {str(source)!r})\n'
                   'from codinator.checkpoints import main\n'
                   f'raise SystemExit(main(contract_path={str(contract)!r}))\n')
        _write_once(launcher, program.encode())
        self.command = shlex.join([sys.executable, '-I', '-B', str(launcher)])
        self.number, self.started, self.next_due = 0, None, None
        self.pending = {}
        self.deadlines = {}
        self.resolutions = {}
        self.violation = None
        self.stopped = False

    def start(self, now):
        self.started, self.next_due = now, now + self.interval

    def tick(self, now, send):
        if now < self.next_due:
            return
        self.number += 1
        number = self.number
        request = self.identity | {'checkpoint': number, 'rpc_id': f'checkpoint-{number:04d}',
            'requested_at': time.time(), 'elapsed_seconds': now - self.started}
        _write_once(self.directory / 'requests' / f'{number:04d}.json', _encode(request))
        self.pending[request['rpc_id']] = number
        self.deadlines[number] = now + RESPONSE_GRACE_SECONDS
        message = f'''Codinator soft checkpoint {number}; this is a progress request, not a new task.
You MUST submit a progress report within five minutes of this request.
At the next safe tool/turn boundary, write /tmp/progress.json with exactly:
{{"completed": ["facts completed"], "checks": [{{"argv": ["actual", "command"],
"exit_code": 0, "result": "observed result", "evidence": "actual output/log reference"}}],
"blockers": [], "next_step": "one concrete next step", "needs_guidance": false}}.
completed and checks may both be empty; honestly record blockers and the next step.
Only include completed checks with their actual exit codes;
use checks=[] if none completed. Do not invent tests or evidence.
Submit using {self.command} --request {number} --report /tmp/progress.json.
This progress receipt cannot complete or accept work and does not extend any budget.
If no valid report arrives within five minutes, or needs_guidance is true, the controller will block
this attempt at a boundary with no active tool. An ongoing tool may finish, subject to the hard limit.
If repeated attempts fail, the contract is unclear, or needs_guidance is true, record the blocker,
then use the existing bound DELIVERY command with --status blocked and an honest summary, and stop.
The main Codex must guide the frozen blocked task. Otherwise continue the same authorized task.
Do not restart the session, expand scope, change acceptance, or switch model.'''
        send({'id': request['rpc_id'], 'type': 'steer', 'message': message})
        self.next_due = now + self.interval

    def _violate(self, number, reason, now, detail=''):
        if self.violation is None:
            self.violation = self.identity | {'checkpoint': number, 'reason': reason,
                'detected_elapsed_seconds': now - self.started, 'detail': detail}
            _write_once(self.directory / 'violation.json', _encode(self.violation))

    def _resolve(self, number, source, now, evidence):
        value = self.identity | {'checkpoint': number, 'source': source,
                                'elapsed_seconds': now - self.started, source: evidence}
        _write_once(self.directory / 'resolutions' / f'{number:04d}.json', _encode(value))
        self.resolutions[number] = value

    def observe(self, now):
        """Latch missed deadlines even while a tool is still running."""
        if self.violation is not None:
            return
        for number, deadline in self.deadlines.items():
            if number in self.resolutions:
                continue
            path = self.directory / 'reports' / f'{number:04d}.json'
            if now > deadline:
                self._violate(number, 'response_timeout', now,
                              'No valid report observed within the five-minute response grace')
                return
            try:
                if not path.exists() and not path.is_symlink():
                    continue
                value = _report(self.directory, self.identity, number)
            except (Problem, OSError, ValueError):
                # A malformed file does not satisfy the deadline. Give the agent
                # the remaining response grace without killing an active tool.
                continue
            self._resolve(number, 'report', now, value)
            if value['progress']['needs_guidance']:
                self._violate(number, 'needs_guidance', now)
                return

    def enforce(self, now, active_tools):
        self.observe(now)
        if self.violation is not None and not active_tools:
            if not self.stopped:
                _write_once(self.directory / 'stop.json', _encode(self.violation | {
                    'stopped_elapsed_seconds': now - self.started, 'active_tools': []}))
                self.stopped = True
            raise Problem(f'Checkpoint {self.violation["checkpoint"]}: {self.violation["reason"]}; '
                          'attempt blocked for main Codex guidance')

    def finish(self, now):
        self.enforce(now, set())
        outstanding = [n for n in self.deadlines if n not in self.resolutions]
        if not outstanding:
            return
        if outstanding != [self.number]:
            self._violate(outstanding[0], 'unfinished_checkpoints', now,
                          'Only the final checkpoint may be replaced by a completed delivery')
        else:
            delivery = self.context / 'delivery'
            try:
                disposition = validate_delivery(delivery, self.identity['task_id'],
                                                self.identity['round'], self.identity['attempt'])
            except (Problem, OSError, ValueError) as exc:
                self._violate(self.number, 'missing_final_delivery', now, str(exc))
            else:
                self._resolve(self.number, 'final_delivery', now, {
                    'directory': str(delivery), 'status': disposition,
                    'files': {name: file_info(delivery / name) for name in ('summary.md', 'completion.json')}})
        self.enforce(now, set())

    def response(self, event):
        rpc_id = event.get('id')
        if not isinstance(rpc_id, str) or not rpc_id.startswith('checkpoint-'):
            return False
        if rpc_id not in self.pending or type(event.get('success')) is not bool:
            raise Problem('Unexpected/duplicate checkpoint RPC acknowledgement')
        number = self.pending.pop(rpc_id)
        ack = self.identity | {'checkpoint': number, 'rpc_id': rpc_id, 'success': event['success'],
                              'acknowledged_at': time.time()}
        _write_once(self.directory / 'acks' / f'{number:04d}.json', _encode(ack))
        if not event['success']:
            raise Problem(f'Pi rejected checkpoint {number}: {event.get("error")}')
        return True


def status(context, task_id, attempt):
    """Read progress claims separately from RPC acknowledgement and task state."""
    context = Path(context)
    contract_path = context / 'checkpoint-contract.json'
    if not contract_path.exists() and not contract_path.is_symlink():
        return None
    contract = _contract(contract_path)
    identity = _identity(contract)
    if identity['task_id'] != task_id or identity['attempt'] != attempt:
        raise Problem('Checkpoint contract does not belong to the reported task/attempt')
    directory = context.absolute() / 'checkpoints'
    if contract['directory'] != str(directory):
        raise Problem('Checkpoint contract points outside this attempt')
    for name in ('requests', 'acks', 'reports'):
        _directory(directory / name)
    policy_path = directory / 'policy.json'
    policy = None
    if policy_path.exists() or policy_path.is_symlink():
        policy = _document(policy_path)
        if policy != {'response_grace_seconds': RESPONSE_GRACE_SECONDS}:
            raise Problem('Invalid checkpoint response policy')
        _directory(directory / 'resolutions')
    latest_request = latest_report = latest_resolution = None
    unanswered, unresolved, count = [], [], 0
    paths = list((directory / 'requests').glob('*.json'))
    for path in paths:
        if not path.stem.isdigit() or int(path.stem) < 1 or path.name != f'{int(path.stem):04d}.json':
            raise Problem('Invalid checkpoint request filename')
    for path in sorted(paths, key=lambda p: int(p.stem)):
        number = int(path.stem)
        request = _request(directory, identity, number)
        ack_path = directory / 'acks' / path.name
        ack = None
        if ack_path.exists() or ack_path.is_symlink():
            ack = _document(ack_path)
            _bound(ack, identity, number)
            if (set(ack) != {*identity, 'checkpoint', 'rpc_id', 'success', 'acknowledged_at'}
                    or ack['rpc_id'] != request['rpc_id'] or type(ack['success']) is not bool
                    or type(ack['acknowledged_at']) not in (int, float)
                    or not math.isfinite(ack['acknowledged_at']) or ack['acknowledged_at'] < 0):
                raise Problem('Invalid checkpoint acknowledgement')
        report_path = directory / 'reports' / path.name
        resolution_path = directory / 'resolutions' / path.name
        resolution = None
        if resolution_path.exists() or resolution_path.is_symlink():
            resolution = _resolution(directory, identity, number)
            latest_resolution = resolution | {'path': str(resolution_path)}
        responded = report_path.exists() or report_path.is_symlink()
        if resolution is not None and resolution['source'] == 'report':
            latest_report = resolution['report'] | {'path': str(report_path)}
            responded = True
        elif responded:
            latest_report = _report(directory, identity, number)
            latest_report['path'] = str(report_path)
        else:
            unanswered.append(number)
        if policy is not None and resolution is None:
            unresolved.append(number)
        latest_request = request | {'rpc_accepted': ack['success'] if ack else None,
                                    'report_received': responded, 'path': str(path),
                                    'resolved_by': resolution['source'] if resolution else None}
        count += 1
    enforcement = {}
    for name in ('violation', 'stop'):
        path = directory / f'{name}.json'
        if path.exists() or path.is_symlink():
            value = _document(path)
            number = value.get('checkpoint')
            if type(number) is not int or not 0 < number <= count:
                raise Problem('Invalid checkpoint enforcement request identity')
            _bound(value, identity, number)
            enforcement[name] = value | {'path': str(path)}
    return {**identity, 'request_count': count, 'latest_request': latest_request,
            'latest_report': latest_report, 'unanswered_requests': unanswered,
            'response_policy': policy, 'latest_resolution': latest_resolution,
            'unresolved_requests': unresolved, **enforcement}
