"""Optional implementation progress requests; never a workflow completion protocol."""
import argparse
import json
import math
import re
from pathlib import Path
import shlex
import sys
import time

from .delivery import _directory, _encode, _json, _read, _write_once, validate_delivery, read_submission
from .files import Problem, file_info
from .handoff import enabled, validate_document


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
    fields = {'version', 'task_id', 'round', 'attempt', 'directory'}
    if (set(value) not in (fields, fields | {'handoff_protocol'}, fields | {'handoff_protocol', 'checkpoint_format'})
            or type(value['version']) is not int or value['version'] != 1
            or ('handoff_protocol' in value and
                (type(value['handoff_protocol']) is not int or value['handoff_protocol'] != 1))
            or type(value['directory']) is not str or not Path(value['directory']).is_absolute()):
        raise Problem('Invalid checkpoint contract')
    if 'checkpoint_format' in value and value['checkpoint_format'] != 'compact':
        raise Problem('Invalid checkpoint format')
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


def _progress(value, protocol=False, compact=False):
    fields = {'completed', 'checks', 'blockers', 'next_step', 'needs_guidance'}
    if compact:
        fields.add('candidate_commit')
    elif protocol:
        fields.add('summary')
    if type(value) is not dict or set(value) != fields:
        if protocol:
            raise Problem('Progress requires completed, checks, blockers, next_step, needs_guidance and summary')
        raise Problem('Progress requires only completed, checks, blockers, next_step and needs_guidance')
    if compact:
        sha = value['candidate_commit']
        if sha is not None and (type(sha) is not str or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha)):
            raise Problem('candidate_commit must be an exact SHA or null')
    elif protocol:
        validate_document(value['summary'], 'summary', 'progress.summary')
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


def _report(directory, identity, number, protocol=False, compact=False):
    value = _document(directory / 'reports' / f'{number:04d}.json')
    _bound(value, identity, number)
    if set(value) != {*identity, 'checkpoint', 'progress'}:
        raise Problem('Invalid checkpoint report fields')
    _progress(value['progress'], protocol, compact)
    return value


def _resolution(directory, identity, number, protocol=False, compact=False):
    value = _document(directory / 'resolutions' / f'{number:04d}.json')
    _bound(value, identity, number)
    source = value.get('source')
    fields = {*identity, 'checkpoint', 'source', 'elapsed_seconds', source}
    if protocol and source == 'report':
        fields.update(('report_file', 'stage_summary'))
    if (source not in ('report', 'final_delivery')
            or set(value) != fields
            or type(value['elapsed_seconds']) not in (int, float)
            or not math.isfinite(value['elapsed_seconds']) or value['elapsed_seconds'] < 0):
        raise Problem('Invalid checkpoint resolution')
    if source == 'report':
        report = value[source]
        if type(report) is not dict or set(report) != {*identity, 'checkpoint', 'progress'}:
            raise Problem('Invalid resolved checkpoint report')
        _bound(report, identity, number)
        _progress(report['progress'], protocol, compact)
        if protocol:
            report_path = directory / 'reports' / f'{number:04d}.json'
            stage_path = directory / f'stage-summary-{number:04d}.md'
            _read(report_path)
            data = _read(stage_path)
            if (value['report_file'] != file_info(report_path)
                    or value['stage_summary'] != {'path': str(stage_path), **file_info(stage_path)}
                    or data != _stage_summary(report['progress'], compact).encode('utf-8')):
                raise Problem('Checkpoint report or stage summary changed after resolution')
            if _report(directory, identity, number, True, compact) != report:
                raise Problem('Resolved checkpoint report differs from original evidence')
    else:
        delivery = value[source]
        if (type(delivery) is not dict or set(delivery) != {'directory', 'status', 'files'}
                or delivery['directory'] != str(directory.parent / 'delivery')
                or delivery['status'] not in (('awaiting_review', 'blocked', 'needs_guidance') if protocol
                                              else ('awaiting_review', 'blocked'))
                or type(delivery['files']) is not dict
                or set(delivery['files']) not in ({'summary.md', 'completion.json'},
                                                 {'summary.md', 'completion.json', 'evidence.json'})):
            raise Problem('Invalid final-delivery checkpoint resolution')
    return value


def _stage_summary(progress, compact):
    if not compact:
        return progress['summary']
    # Render only the author's supplied claims, without inventing completion facts.
    return 'Checkpoint claims (not acceptance):\n' + json.dumps(progress, ensure_ascii=False, indent=2) + '\n'


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
        protocol = enabled(contract)
        compact = contract.get('checkpoint_format') == 'compact'
        progress = _progress(_document(args.report), protocol, contract.get('checkpoint_format') == 'compact')
        report = identity | {'checkpoint': args.request, 'progress': progress}
        target = directory / 'reports' / f'{args.request:04d}.json'
        try:
            _write_once(target, _encode(report))
        except FileExistsError:
            if _report(directory, identity, args.request, protocol) != report:
                raise Problem('Conflicting checkpoint report; original evidence preserved')
        print(json.dumps({'status': 'progress_recorded', **identity, 'checkpoint': args.request}))
        return 0
    except (Problem, OSError) as exc:
        print(json.dumps({'status': 'rejected', 'error': str(exc)}), file=sys.stderr)
        return 2


class Checkpoints:
    def __init__(self, task, context, interval, timeout=None):
        self.task = task
        self.identity = _identity({'task_id': task['id'], 'round': task['round'], 'attempt': task['attempt']})
        self.interval = interval
        self.protocol = enabled(task.get('manifest', {}))
        self.compact = task.get('manifest', {}).get('checkpoint_format') == 'compact'
        self.timeout = timeout
        self.context = Path(context).absolute()
        self.directory = self.context / 'checkpoints'
        self.directory.mkdir()
        for name in ('requests', 'acks', 'reports', 'resolutions'):
            (self.directory / name).mkdir()
        if self.compact:
            (self.directory / 'observed').mkdir()
        if self.protocol:
            for name in ('help-requests', 'help-acks'):
                (self.directory / name).mkdir()
        _write_once(self.directory / 'policy.json', _encode({'response_grace_seconds': RESPONSE_GRACE_SECONDS}))
        contract = Path(context).absolute() / 'checkpoint-contract.json'
        launcher = Path(context).absolute() / 'submit-checkpoint.py'
        binding = {'version': 1, **self.identity, 'directory': str(self.directory)}
        if self.protocol:
            binding['handoff_protocol'] = 1
        if self.compact:
            binding['checkpoint_format'] = 'compact'
        _write_once(contract, _encode(binding))
        source = Path(__file__).resolve().parents[1]
        program = (f'import sys\nsys.path.insert(0, {str(source)!r})\n'
                   'from codinator.checkpoints import main\n'
                   f'raise SystemExit(main(contract_path={str(contract)!r}))\n')
        _write_once(launcher, program.encode())
        self.command = shlex.join([sys.executable, '-I', '-B', str(launcher)])
        self.number, self.started, self.next_due, self.now = 0, None, None, None
        self.pending = {}
        self.deadlines = {}
        self.resolutions = {}
        self.violation = None
        self.stopped = False
        self.guidance = None
        self.guidance_delivery = None
        self.help_sent = False

    def start(self, now):
        self.started, self.next_due, self.now = now, now + self.interval, now

    def tick(self, now, send):
        self.now = now
        if self.guidance is not None:
            if not self.help_sent and self.violation is None:
                number = self.guidance['checkpoint']
                request = self.identity | {'checkpoint': number, 'rpc_id': f'checkpoint-help-{number:04d}',
                    'requested_at': time.time(), 'elapsed_seconds': now - self.started,
                    'deadline_elapsed_seconds': self.guidance['deadline_elapsed_seconds']}
                _write_once(self.directory / 'help-requests' / f'{number:04d}.json', _encode(request))
                self.pending[request['rpc_id']] = number
                self.help_sent = True
                send({'id': request['rpc_id'], 'type': 'steer', 'message':
                    'Your checkpoint explicitly requests Codex guidance. At the next safe tool boundary, '
                    'stop implementation, submit the existing bound DELIVERY command with '
                    '--status needs_guidance, a complete summary with the help-template headings, and then stop. '
                    'Record the specific question, attempts and observed results honestly. '
                    'This is a final help delivery, not another progress report. You have at most '
                    'five minutes from the guidance report within the original hard timeout; '
                    'no budget, permission or acceptance changes are authorized.'})
            return
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
        if self.protocol:
            message = f'''Codinator soft checkpoint {number}; progress for the same authorized attempt.
At the next safe tool boundary, submit a report within five minutes using:
{self.command} --request {number} --report /tmp/progress.json
The JSON report requires exactly completed (string array), checks (array), blockers
(string array), next_step (nonempty string), needs_guidance (boolean) and summary
(Markdown string with the required headings from the supplied summary template).
Each completed check requires argv (string array), exit_code (integer), result
and evidence (nonempty strings). Use checks=[] if none actually completed.
Record actual completion, attempts, unfinished work, observed results and unknowns
honestly. Use None, 无 or 未执行 when appropriate; never fabricate evidence.
A format-valid stage summary is an implementation claim, not independent acceptance.
If needs_guidance=true, stop implementation and submit the bound DELIVERY command
with --status needs_guidance and summary.md using the help-template headings, then
stop. A separate steer will request that final help delivery within five minutes of
the report, capped by the original hard timeout. A progress report is not final help.
If no valid report arrives within five minutes, this attempt blocks at a boundary
with no active tool. An ongoing tool may finish subject to the original hard limit.
Otherwise continue this task. Do not extend budgets, permissions or scope, change
acceptance, restart the session, replay uncertain execution or switch model.'''
        if self.compact:
            goal = self.task['manifest']['first_checkpoint']
            message = f'''Codinator soft checkpoint {number}; progress for the same attempt.
At the next safe tool boundary, report BEFORE beginning more implementation work.
Within five minutes use {self.command} --request {number} --report /tmp/progress.json.
Exactly six JSON fields: completed (facts/criterion IDs), checks (actual argv,
integer exit_code, result, evidence), blockers, next_step, needs_guidance,
candidate_commit (exact SHA or null). Arrays may be empty. No Markdown summary.
First verifiable outcome: {goal}
Report whether that outcome has actual evidence. Reading/design alone is not
behavioral verification. If the goal lacks evidence, state why and request guidance
when no concrete progress or an unresolved contract conflict prevents continuation.
Codex evaluates content; this tool checks only structure. Final/help/blocked DELIVERY
still requires its full summary. Report receipt is not acceptance. The five-minute
grace and original hard limit remain unchanged. Never interrupt an active tool,
replay uncertain work or extend scope/budget.'''
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
        if self.protocol and source == 'report':
            stage_path = self.directory / f'stage-summary-{number:04d}.md'
            _write_once(stage_path, _stage_summary(evidence['progress'], self.compact).encode('utf-8'))
            value['stage_summary'] = {'path': str(stage_path), **file_info(stage_path)}
            value['report_file'] = file_info(self.directory / 'reports' / f'{number:04d}.json')
        _write_once(self.directory / 'resolutions' / f'{number:04d}.json', _encode(value))
        self.resolutions[number] = value

    def observe(self, now):
        """Latch missed deadlines even while a tool is still running."""
        self.now = now
        if self.violation is not None:
            return
        if self.protocol:
            for number in self.resolutions:
                try:
                    _resolution(self.directory, self.identity, number, True, self.compact)
                except (Problem, OSError, ValueError) as exc:
                    self._violate(number, 'checkpoint_evidence_changed', now, str(exc))
                    return
        if self.guidance is not None:
            self._observe_guidance(now)
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
                value = _report(self.directory, self.identity, number, self.protocol, self.compact)
            except (Problem, OSError, ValueError):
                # A malformed file does not satisfy the deadline. Give the agent
                # the remaining response grace without killing an active tool.
                continue
            self._resolve(number, 'report', now, value)
            if value['progress']['needs_guidance']:
                if self.protocol:
                    deadline = now + RESPONSE_GRACE_SECONDS
                    if self.timeout is not None:
                        deadline = min(deadline, self.started + self.timeout)
                    self.guidance = self.identity | {'checkpoint': number,
                        'detected_elapsed_seconds': now - self.started,
                        'deadline_elapsed_seconds': deadline - self.started}
                    _write_once(self.directory / 'guidance.json', _encode(self.guidance))
                    self._observe_guidance(now)
                    return
                self._violate(number, 'needs_guidance', now)
                return

    def _observe_guidance(self, now):
        if self.guidance_delivery is not None:
            return
        if now > self.started + self.guidance['deadline_elapsed_seconds']:
            self._violate(self.guidance['checkpoint'], 'guidance_delivery_timeout', now,
                          'No valid final needs_guidance delivery within the bounded grace')
            return
        try:
            delivery = self.context / 'delivery'
            disposition = validate_delivery(delivery, self.identity['task_id'],
                                            self.identity['round'], self.identity['attempt'])
            if disposition != 'needs_guidance':
                return
            packet = read_submission(delivery, self.task)
            if packet is None:
                return
            names = ['summary.md', 'completion.json', 'evidence.json']
            value = self.identity | {'checkpoint': self.guidance['checkpoint'],
                'elapsed_seconds': now - self.started,
                'files': {name: file_info(delivery / name) for name in names}}
        except (Problem, OSError, ValueError):
            # Give the existing session its remaining bounded time to correct format.
            return
        _write_once(self.directory / 'guidance-delivery.json', _encode(value))
        self.guidance_delivery = value

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
        if self.guidance is not None:
            if self.guidance_delivery is None:
                self._violate(self.guidance['checkpoint'], 'missing_final_help_delivery', now,
                              'The process finished without a valid final needs_guidance delivery')
            else:
                for name, binding in self.guidance_delivery['files'].items():
                    path = self.context / 'delivery' / name
                    _read(path)
                    if file_info(path) != binding:
                        self._violate(self.guidance['checkpoint'], 'final_help_evidence_changed', now, name)
                        break
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
                names = ['summary.md', 'completion.json']
                if self.task.get('manifest', {}).get('version') == 2:
                    packet = read_submission(delivery, self.task)
                    if packet is not None:
                        names.append('evidence.json')
            except (Problem, OSError, ValueError) as exc:
                self._violate(self.number, 'missing_final_delivery', now, str(exc))
            else:
                self._resolve(self.number, 'final_delivery', now, {
                    'directory': str(delivery), 'status': disposition,
                    'files': {name: file_info(delivery / name) for name in names}})
        self.enforce(now, set())

    def message_observed(self, event):
        if not self.compact or event.get('type') != 'message_end':
            return
        message = event.get('message', {})
        if type(message) is not dict or message.get('role') != 'user':
            return
        for part in message.get('content', []):
            if type(part) is not dict or part.get('type') != 'text':
                continue
            match = re.match(r'Codinator soft checkpoint (\d+);', part.get('text', ''))
            if match and int(match[1]) in self.deadlines:
                number = int(match[1])
                path = self.directory / 'observed' / f'{number:04d}.json'
                if not path.exists():
                    _write_once(path, _encode(self.identity | {'checkpoint': number,
                        'observed_elapsed_seconds': time.monotonic() - self.started}))

    def response(self, event):
        rpc_id = event.get('id')
        if not isinstance(rpc_id, str) or not rpc_id.startswith('checkpoint-'):
            return False
        if rpc_id not in self.pending or type(event.get('success')) is not bool:
            raise Problem('Unexpected/duplicate checkpoint RPC acknowledgement')
        number = self.pending.pop(rpc_id)
        help_ack = rpc_id.startswith('checkpoint-help-')
        ack = self.identity | {'checkpoint': number, 'rpc_id': rpc_id, 'success': event['success'],
                              'acknowledged_at': time.time()}
        if help_ack:
            ack['elapsed_seconds'] = self.now - self.started
            ack['within_deadline'] = ack['elapsed_seconds'] < self.guidance['deadline_elapsed_seconds']
        _write_once(self.directory / ('help-acks' if help_ack else 'acks') / f'{number:04d}.json', _encode(ack))
        if help_ack and not ack['within_deadline']:
            raise Problem('Late checkpoint help RPC acknowledgement')
        if not event['success']:
            raise Problem(f'Pi rejected checkpoint {number}: {event.get("error")}')
        return True


class Closeout:
    """One optional steer near the existing hard deadline; never a new execution.

    PiProtocol should start this adapter with its own monotonic start, call observe
    on every process observation, tick only while the acknowledged agent is active,
    route response events here and call finish only on protocol completion. The
    caller still owns cancellation, hard timeout and final-delivery validation.
    A transport acknowledgement records receipt only, never a final summary.
    """
    def __init__(self, task, context, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise Problem('Closeout timeout must be a positive finite number')
        self.identity = _identity({'task_id': task['id'], 'round': task['round'], 'attempt': task['attempt']})
        self.directory = Path(context).absolute() / 'closeout'
        self.directory.mkdir()
        self.timeout = timeout
        self.started = self.now = self.request = self.ack = None
        self.finished = False

    def start(self, now):
        if self.started is not None:
            raise Problem('Closeout adapter already started')
        self.started = self.now = now
        self.due = now + self.timeout - min(RESPONSE_GRACE_SECONDS, self.timeout / 2)
        self.deadline = now + self.timeout

    def observe(self, now):
        self.now = now

    def tick(self, now, send):
        self.observe(now)
        if self.finished or self.request is not None or now < self.due or now >= self.deadline:
            return
        request = self.identity | {'rpc_id': 'closeout-0001', 'requested_at': time.time(),
            'elapsed_seconds': now - self.started,
            'response_deadline_elapsed_seconds': min(now + RESPONSE_GRACE_SECONDS, self.deadline) - self.started}
        _write_once(self.directory / 'request.json', _encode(request))
        self.request = request
        remaining = max(0, self.deadline - now)
        send({'id': request['rpc_id'], 'type': 'steer', 'message':
            f'The existing process hard limit has {remaining:.1f} seconds remaining. '
            'At the next safe tool boundary, close out the same authorized attempt using '
            'the bound DELIVERY command with an honest final summary and its actual status. '
            'Record completed, unfinished, attempted and unknown items with real evidence. '
            'If guidance is needed, submit needs_guidance with the required help document. '
            'Do not start new work, replay uncertain execution, fabricate results, extend '
            'the deadline or interrupt an active tool merely to acknowledge this request.'})

    def response(self, event):
        rpc_id = event.get('id')
        if not isinstance(rpc_id, str) or not rpc_id.startswith('closeout-'):
            return False
        if (self.finished or self.request is None or rpc_id != self.request['rpc_id'] or self.ack is not None
                or type(event.get('success')) is not bool):
            raise Problem('Unexpected/duplicate closeout RPC acknowledgement')
        ack = self.identity | {'rpc_id': rpc_id, 'success': event['success'],
            'acknowledged_at': time.time(), 'elapsed_seconds': self.now - self.started,
            'within_deadline': self.now - self.started < self.request['response_deadline_elapsed_seconds']}
        _write_once(self.directory / 'ack.json', _encode(ack))
        self.ack = ack
        if not ack['within_deadline']:
            raise Problem('Late closeout RPC acknowledgement')
        if not ack['success']:
            raise Problem(f'Pi rejected closeout: {event.get("error")}')
        return True

    def finish(self, now):
        self.observe(now)
        self.finished = True


def status(context, task_id, attempt):
    """Read progress claims separately from RPC acknowledgement and task state."""
    context = Path(context)
    contract_path = context / 'checkpoint-contract.json'
    if not contract_path.exists() and not contract_path.is_symlink():
        return None
    contract = _contract(contract_path)
    protocol = enabled(contract)
    compact = contract.get('checkpoint_format') == 'compact'
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
    latest_request = latest_report = latest_resolution = latest_stage = None
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
            resolution = _resolution(directory, identity, number, protocol, compact)
            latest_resolution = resolution | {'path': str(resolution_path)}
        responded = report_path.exists() or report_path.is_symlink()
        if resolution is not None and resolution['source'] == 'report':
            latest_report = resolution['report'] | {'path': str(report_path)}
            if protocol:
                latest_stage = resolution['stage_summary']
            responded = True
        elif responded:
            latest_report = _report(directory, identity, number, protocol, compact)
            latest_report['path'] = str(report_path)
        else:
            unanswered.append(number)
        if policy is not None and resolution is None:
            unresolved.append(number)
        latest_request = request | {'rpc_accepted': ack['success'] if ack else None,
                                    'report_received': responded, 'path': str(path),
                                    'resolved_by': resolution['source'] if resolution else None}
        if compact:
            observed_path = directory / 'observed' / path.name
            seen = _document(observed_path) if observed_path.exists() else None
            if seen is not None:
                _bound(seen, identity, number)
            latest_request['agent_message_observed'] = seen is not None
            latest_request['agent_message_latency_seconds'] = (seen['observed_elapsed_seconds'] - request['elapsed_seconds']) if seen else None
            latest_request['report_latency_seconds'] = (resolution['elapsed_seconds'] - request['elapsed_seconds']) if resolution else None
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
    result = {**identity, 'request_count': count, 'latest_request': latest_request,
            'latest_report': latest_report, 'unanswered_requests': unanswered,
            'response_policy': policy, 'latest_resolution': latest_resolution,
            'unresolved_requests': unresolved, **enforcement}
    if protocol:
        result['latest_stage_summary'] = latest_stage
        for name in ('guidance', 'guidance-delivery'):
            path = directory / f'{name}.json'
            if path.exists() or path.is_symlink():
                value = _document(path)
                number = value.get('checkpoint')
                if type(number) is not int or not 0 < number <= count:
                    raise Problem('Invalid checkpoint guidance request identity')
                _bound(value, identity, number)
                result[name.replace('-', '_')] = value | {'path': str(path)}
    return result
