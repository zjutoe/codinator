"""Attempt-bound delivery submission and immutable controller selection evidence."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile

from .files import Problem, file_info


MAX_BYTES = 1_000_000
STATUSES = ('awaiting_review', 'blocked')
NAMES = ('summary.md', 'completion.json')
AGENT_PROTOCOL = 'agent_git_v1'
AGENT_NAMES = (*NAMES, 'evidence.json')


class DeliveryError(Problem):
    def __init__(self, errors, *, repairable):
        self.errors = errors
        self.repairable = repairable
        super().__init__('; '.join(
            f"{e['path']}: expected {e['expected']!r}, got {e['actual']!r} [{e['code']}]"
            for e in errors))


def _error(code, path, expected, actual, *, repairable=False):
    return DeliveryError([{'code': code, 'path': str(path), 'expected': expected,
                           'actual': actual}], repairable=repairable)


def _ancestors(path):
    """Check lexical ancestors before opening anything; never resolve away links."""
    for parent in reversed(path.absolute().parents):
        try:
            mode = parent.lstat().st_mode
        except FileNotFoundError:
            raise _error('missing', parent, 'directory', 'missing', repairable=True)
        if stat.S_ISLNK(mode):
            raise _error('symlink', parent, 'ordinary directory', 'symlink')
        if not stat.S_ISDIR(mode):
            raise _error('not_directory', parent, 'directory', 'non-directory')


def _directory(path):
    _ancestors(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise _error('missing', path, 'directory', 'missing', repairable=True)
    if stat.S_ISLNK(mode):
        raise _error('symlink', path, 'ordinary directory', 'symlink')
    if not stat.S_ISDIR(mode):
        raise _error('not_directory', path, 'directory', 'non-directory')


def _read(path):
    path = Path(path)
    _ancestors(path)
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise _error('symlink', path, 'ordinary file', 'symlink')
        if not stat.S_ISREG(info.st_mode):
            raise _error('not_file', path, 'ordinary file', 'non-file')
        if info.st_size > MAX_BYTES:
            raise _error('too_large', path, f'<= {MAX_BYTES} bytes', info.st_size)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            current = os.fstat(stream.fileno())
            if not stat.S_ISREG(current.st_mode):
                raise _error('not_file', path, 'ordinary file', 'non-file')
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise _error('too_large', path, f'<= {MAX_BYTES} bytes', len(data))
        return data
    except FileNotFoundError:
        raise _error('missing', path, 'ordinary file', 'missing', repairable=True)
    except OSError as exc:
        raise _error('io_error', path, 'readable ordinary file', str(exc)) from exc


def _json(data, path):
    errors = []
    # Recognize explicit blockers even when later bytes are truncated or non-UTF-8.
    blocked = bool(re.search(rb'"status"\s*:\s*"blocked"', data))

    def pairs(items):
        nonlocal blocked
        result = {}
        for key, value in items:
            if key in result:
                errors.append({'code': 'duplicate_key', 'path': f'{path}.{key}',
                               'expected': 'unique JSON key', 'actual': key})
            if key == 'status' and value == 'blocked':
                blocked = True
            result[key] = value
        return result

    try:
        text = data.decode('utf-8')
        def invalid_constant(token):
            raise ValueError(f'Non-JSON constant: {token}')
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except RecursionError as exc:
        raise _error('json_too_deep', path, 'JSON within parser nesting limit',
                     'nesting limit exceeded') from exc
    except (ValueError, UnicodeError) as exc:
        raise _error('invalid_json', path, 'UTF-8 JSON object', str(exc),
                     repairable=not blocked) from exc
    return value, errors, blocked


def _completion_errors(value, path, task_id, round_number, attempt_number):
    if type(value) is not dict:
        return [{'code': 'invalid_type', 'path': str(path), 'expected': 'object',
                 'actual': type(value).__name__}]
    errors = []
    expected = {'task_id': task_id, 'round': round_number, 'attempt': attempt_number}
    for key in (*expected, 'status'):
        name = f'{path}.{key}'
        target = list(STATUSES) if key == 'status' else expected[key]
        if key not in value:
            errors.append({'code': 'missing_field', 'path': name,
                           'expected': target, 'actual': '<missing>'})
        elif ((key in ('round', 'attempt') and type(value[key]) is not int)
              or (key in ('task_id', 'status') and type(value[key]) is not str)):
            errors.append({'code': 'invalid_type', 'path': name,
                           'expected': 'integer' if key in ('round', 'attempt') else 'string',
                           'actual': type(value[key]).__name__})
        elif (value[key] not in STATUSES if key == 'status' else value[key] != target):
            errors.append({'code': 'invalid_status' if key == 'status' else 'identity_mismatch',
                           'path': name, 'expected': target, 'actual': value[key]})
    for key in sorted(set(value) - {*expected, 'status'}):
        errors.append({'code': 'unexpected_field', 'path': f'{path}.{key}',
                       'expected': '<absent>', 'actual': value[key]})
    return errors


def _summary(data, path):
    try:
        text = data.decode('utf-8')
    except UnicodeError as exc:
        raise _error('invalid_text', path, 'UTF-8 summary', str(exc), repairable=True) from exc
    if not text.strip():
        raise _error('empty_summary', path, 'nonempty summary', 'empty', repairable=True)


def validate_delivery(directory, task_id, round_number, attempt_number):
    directory = Path(directory)
    errors, repairable, status = [], True, None
    for name in NAMES:
        path = directory / name
        try:
            data = _read(path)
            if name == 'summary.md':
                _summary(data, path)
            else:
                value, duplicates, blocked = _json(data, path)
                errors.extend(duplicates)
                errors.extend(_completion_errors(value, path, task_id, round_number, attempt_number))
                repairable = repairable and not blocked
                if type(value) is dict:
                    status = value.get('status')
        except DeliveryError as exc:
            errors.extend(exc.errors)
            repairable = repairable and exc.repairable
    if errors:
        raise DeliveryError(errors, repairable=repairable)
    return status


def _sha(value):
    return type(value) is str and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value) is not None


def _agent_binding(task):
    manifest = task['manifest']
    value = {'protocol': AGENT_PROTOCOL, 'git': {'branch': manifest['git']['branch'],
             'base_commit': task['expected_digest']}, 'checks': manifest['checks']}
    if not _valid_binding(value):
        raise _error('invalid_contract', 'task', 'bound branch, full baseline SHA and unique required checks', value)
    return value


def _valid_binding(value):
    spec, checks = value.get('git'), value.get('checks')
    if (value.get('protocol') != AGENT_PROTOCOL or type(spec) is not dict
            or set(spec) != {'branch', 'base_commit'} or type(spec['branch']) is not str
            or not spec['branch'].strip() or not _sha(spec['base_commit']) or type(checks) is not list):
        return False
    names = set()
    for check in checks:
        if (type(check) is not dict or type(check.get('name')) is not str or not check['name'].strip()
                or check['name'] in names or type(check.get('argv')) is not list or not check['argv']
                or any(type(arg) is not str or not arg for arg in check['argv'])):
            return False
        names.add(check['name'])
    return True


def _evidence(data, path, contract):
    """Validate claims and their binding only; never run Git/checks or certify facts."""
    value, errors, blocked = _json(data, path)
    repairable = not blocked

    def error(code, name, expected, actual, *, format_only=True):
        nonlocal repairable
        errors.append({'code': code, 'path': str(name), 'expected': expected, 'actual': actual})
        repairable = repairable and format_only

    def fields(obj, keys, name):
        if type(obj) is not dict:
            error('invalid_type', name, 'object', type(obj).__name__)
            return False
        for key in sorted(keys - set(obj)):
            error('missing_field', f'{name}.{key}', 'required field', '<missing>')
        for key in sorted(set(obj) - keys):
            error('unexpected_field', f'{name}.{key}', '<absent>', obj[key])
        return True

    def equal(obj, key, expected, name, *, format_only=False):
        if key not in obj:
            return
        if type(obj[key]) is not type(expected):
            error('invalid_type', f'{name}.{key}', type(expected).__name__, type(obj[key]).__name__)
        elif obj[key] != expected:
            error('binding_mismatch', f'{name}.{key}', expected, obj[key], format_only=format_only)

    def sha(obj, key, name):
        if key in obj and not _sha(obj[key]):
            error('invalid_sha', f'{name}.{key}', 'full lowercase Git commit SHA', obj[key])
            return False
        return key in obj

    if fields(value, {'version', 'task_id', 'round', 'attempt', 'git', 'checks'}, path):
        equal(value, 'version', 1, path, format_only=True)
        for key in ('task_id', 'round', 'attempt'):
            equal(value, key, contract[key], path)
        spec = value.get('git')
        valid_commit = False
        if 'git' in value and fields(spec, {'branch', 'base_commit', 'commit'}, f'{path}.git'):
            equal(spec, 'branch', contract['git']['branch'], f'{path}.git')
            if sha(spec, 'base_commit', f'{path}.git'):
                equal(spec, 'base_commit', contract['git']['base_commit'], f'{path}.git')
            valid_commit = sha(spec, 'commit', f'{path}.git')
        checks = value.get('checks')
        expected = {check['name']: check['argv'] for check in contract['checks']}
        seen = set()
        if 'checks' in value and type(checks) is not list:
            error('invalid_type', f'{path}.checks', 'array', type(checks).__name__)
        elif type(checks) is list:
            for number, check in enumerate(checks):
                name = f'{path}.checks[{number}]'
                if not fields(check, {'name', 'argv', 'commit', 'status', 'exit_code', 'evidence'}, name):
                    continue
                check_name = check.get('name')
                if 'name' in check:
                    if type(check_name) is not str or not check_name.strip():
                        error('invalid_name', name + '.name', 'required check name', check_name)
                    elif check_name in seen:
                        error('duplicate_check', name + '.name', 'one entry per required check', check_name)
                    else:
                        seen.add(check_name)
                        if check_name not in expected:
                            error('unexpected_check', name + '.name', list(expected), check_name)
                if 'argv' in check:
                    argv = check['argv']
                    if type(argv) is not list or not argv or any(type(arg) is not str or not arg for arg in argv):
                        error('invalid_argv', name + '.argv', 'nonempty string array', argv)
                    elif type(check_name) is str and check_name in expected:
                        equal(check, 'argv', expected[check_name], name)
                if sha(check, 'commit', name) and valid_commit:
                    equal(check, 'commit', spec['commit'], name)
                status = check.get('status')
                if 'status' in check and (type(status) is not str or status not in ('passed', 'failed', 'not_run')):
                    error('invalid_status', name + '.status', ['passed', 'failed', 'not_run'], status)
                if 'exit_code' in check:
                    code = check['exit_code']
                    if code is not None and type(code) is not int:
                        error('invalid_type', name + '.exit_code', 'integer or null', type(code).__name__)
                    elif ((status == 'passed' and (code is None or code != 0))
                          or (status == 'failed' and (code is None or code == 0))
                          or (status == 'not_run' and code is not None)):
                        error('inconsistent_result', name + '.exit_code',
                              {'passed': 0, 'failed': 'nonzero integer', 'not_run': None}[status], code,
                              format_only=False)
                if 'evidence' in check and (type(check['evidence']) is not str or not check['evidence'].strip()):
                    error('empty_evidence', name + '.evidence', 'nonempty evidence text/reference', check['evidence'])
            for missing in sorted(set(expected) - seen):
                error('missing_check', f'{path}.checks', missing, '<missing>')
    if errors:
        raise DeliveryError(errors, repairable=repairable)
    return value


def _submission_evidence(directory, contract, status):
    path = Path(directory) / 'evidence.json'
    data = _optional(path)
    if data is None and status == 'blocked':
        return None
    try:
        return _evidence(_read(path) if data is None else data, path, contract)
    except DeliveryError as exc:
        if status == 'blocked':
            exc.repairable = False
        raise


def read_submission(directory, task):
    """Read bound agent claims. A valid claim is not independent acceptance."""
    identity = {'task_id': task['id'], 'round': task['round'], 'attempt': task['attempt']}
    status = validate_delivery(directory, *identity.values())
    if task.get('manifest', {}).get('version') != 2:
        return None
    return _submission_evidence(directory, identity | _agent_binding(task), status)


def _write_once(path, data):
    """Publish a complete file without ever replacing an earlier artifact."""
    _directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.delivery-pending-', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink()


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def prepare_contract(task, context_dir, delivery_dir):
    context_dir, delivery_dir = Path(context_dir).absolute(), Path(delivery_dir).absolute()
    _directory(context_dir)
    _directory(delivery_dir)
    contract = context_dir / 'delivery-contract.json'
    launcher = context_dir / 'submit-delivery.py'
    for path in (contract, launcher):
        if path.exists() or path.is_symlink():
            raise _error('already_exists', path, 'new artifact', 'existing artifact')
    value = {'version': 1, 'task_id': task['id'], 'round': task['round'],
             'attempt': task['attempt'], 'delivery_dir': str(delivery_dir)}
    if task.get('manifest', {}).get('version') == 2:
        value.update(_agent_binding(task))
    source = Path(__file__).resolve().parents[1]
    program = (f'import sys\nsys.path.insert(0, {str(source)!r})\n'
               'from codinator.delivery import main\n'
               f'raise SystemExit(main(contract_path={str(contract)!r}))\n')
    _write_once(contract, _encode(value))
    _write_once(launcher, program.encode('utf-8'))
    return shlex.join([sys.executable, '-I', '-B', str(launcher)])


def _contract(path):
    value, errors, _ = _json(_read(path), path)
    keys = {'version', 'task_id', 'round', 'attempt', 'delivery_dir'}
    if type(value) is dict and 'protocol' in value:
        keys |= {'protocol', 'git', 'checks'}
    if (errors or type(value) is not dict or set(value) != keys
            or type(value['version']) is not int or value['version'] != 1
            or type(value['task_id']) is not str or not value['task_id']
            or any(type(value[key]) is not int or value[key] < 1 for key in ('round', 'attempt'))
            or type(value['delivery_dir']) is not str or not Path(value['delivery_dir']).is_absolute()
            or ('protocol' in value and not _valid_binding(value))):
        raise _error('invalid_contract', path, 'version 1 attempt-bound contract', value)
    return value


def _optional(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return _read(path)


def _publish_same(path, data):
    try:
        _write_once(path, data)
    except FileExistsError:
        if _read(path) != data:
            raise _error('conflict', path, 'identical existing content', 'different content')


def main(argv=None, *, contract_path):
    parser = argparse.ArgumentParser(description='Submit immutable attempt evidence for independent review; never accept work')
    parser.add_argument('--summary', required=True, type=Path)
    parser.add_argument('--status', required=True, choices=STATUSES)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args(argv)
    try:
        contract = _contract(Path(contract_path))
        directory = Path(contract['delivery_dir'])
        _directory(directory)
        summary = _read(args.summary)
        _summary(summary, args.summary)
        summary_path, completion_path = (directory / name for name in NAMES)
        old_summary, old_completion = _optional(summary_path), _optional(completion_path)
        agent = contract.get('protocol') == AGENT_PROTOCOL
        if args.evidence is not None and not agent:
            raise _error('unsupported_evidence', args.evidence, 'agent_git_v1 contract', 'legacy contract')
        evidence_path = directory / 'evidence.json'
        old_evidence = _optional(evidence_path) if agent else None
        evidence = _read(args.evidence) if args.evidence is not None else old_evidence
        if agent:
            if old_evidence is not None:
                _evidence(old_evidence, evidence_path, contract)
            if evidence is not None:
                _evidence(evidence, args.evidence or evidence_path, contract)
            elif args.status == 'awaiting_review':
                raise _error('missing', evidence_path, 'bound Git/check evidence', 'missing', repairable=True)
            if old_evidence is not None and old_evidence != evidence:
                raise _error('conflict', evidence_path, 'identical existing content', 'different content')
        identity = (contract['task_id'], contract['round'], contract['attempt'])
        if old_completion is not None:
            status = validate_delivery(directory, *identity)
            if status != args.status:
                raise _error('conflict', completion_path, status, args.status)
            if agent:
                _submission_evidence(directory, contract, status)
                if old_evidence is None and evidence is not None:
                    raise _error('conflict', evidence_path, 'no new files after completion', 'new evidence')
        if old_summary is not None and old_summary != summary:
            raise _error('conflict', summary_path, 'identical existing content', 'different content')
        completion = dict(zip(('task_id', 'round', 'attempt'), identity)) | {'status': args.status}
        if old_summary is None:
            _publish_same(summary_path, summary)
        if agent and old_evidence is None and evidence is not None:
            _publish_same(evidence_path, evidence)
        if old_completion is None:
            _publish_same(completion_path, _encode(completion))
        validate_delivery(directory, *identity)
        if agent:
            _submission_evidence(directory, contract, args.status)
        print(json.dumps({'status': 'submitted', 'task_id': identity[0], 'round': identity[1],
                          'attempt': identity[2], 'delivery_dir': str(directory),
                          'disposition': args.status}))
        return 0
    except DeliveryError as exc:
        exc.repairable = exc.repairable and args.status != 'blocked'
        print(json.dumps({'status': 'rejected', 'errors': exc.errors,
                          'repairable': exc.repairable}), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({'status': 'rejected', 'error': str(exc)}), file=sys.stderr)
        return 2


def _selection_directory(attempt_dir, directory):
    attempt_dir = Path(attempt_dir).absolute()
    _directory(attempt_dir)
    directory = Path(directory).absolute()
    allowed = (attempt_dir / 'delivery', attempt_dir / 'delivery-repair' / 'delivery')
    if directory not in allowed:
        raise _error('invalid_selection', directory, 'current attempt delivery or delivery-repair/delivery',
                     str(directory))
    _directory(directory)
    return directory


def _selection_protocol(attempt_dir, directory):
    contracts = []
    for context in dict.fromkeys((attempt_dir, directory.parent)):
        path = context / 'delivery-contract.json'
        contracts.append(_contract(path) if _optional(path) is not None else None)
    if not any(value is not None and value.get('protocol') == AGENT_PROTOCOL for value in contracts):
        return 1, NAMES
    if any(value is None or value.get('protocol') != AGENT_PROTOCOL for value in contracts):
        raise _error('invalid_selection', directory, 'matching agent protocol contracts', contracts)
    keys = ('task_id', 'round', 'attempt', 'protocol', 'git', 'checks')
    first = {key: contracts[0][key] for key in keys}
    if any({key: value[key] for key in keys} != first for value in contracts[1:]):
        raise _error('invalid_selection', directory, 'same attempt and evidence binding', contracts)
    return 2, AGENT_NAMES


def _infos(directory, names=NAMES):
    result = {}
    for name in names:
        _read(directory / name)
        result[name] = file_info(directory / name)
    return result


def select_delivery(attempt_dir, directory):
    attempt_dir = Path(attempt_dir).absolute()
    directory = _selection_directory(attempt_dir, directory)
    version, names = _selection_protocol(attempt_dir, directory)
    selection = {'version': version, 'directory': directory.relative_to(attempt_dir).as_posix(),
                 'files': _infos(directory, names)}
    _write_once(attempt_dir / 'delivery-selection.json', _encode(selection))


def selected_delivery(attempt_dir):
    attempt_dir = Path(attempt_dir).absolute()
    path = attempt_dir / 'delivery-selection.json'
    _directory(attempt_dir)
    raw = _optional(path)
    if raw is None:
        contract = attempt_dir / 'delivery-contract.json'
        if contract.exists() or contract.is_symlink():
            raise _error('missing_selection', path, 'selection receipt for attempt-bound delivery', 'missing')
        return attempt_dir / 'delivery'
    value, errors, _ = _json(raw, path)
    if (errors or type(value) is not dict or set(value) != {'version', 'directory', 'files'}
            or type(value['version']) is not int or value['version'] not in (1, 2)
            or value['directory'] not in ('delivery', 'delivery-repair/delivery')
            or type(value['files']) is not dict):
        raise _error('invalid_selection', path, 'contract-bound selection with exact file identities', value)
    directory = _selection_directory(attempt_dir, attempt_dir / value['directory'])
    version, names = _selection_protocol(attempt_dir, directory)
    if value['version'] != version or set(value['files']) != set(names):
        raise _error('invalid_selection', path, f'version {version} selection with {list(names)}', value)
    actual = _infos(directory, names)
    if json.dumps(value['files'], sort_keys=True) != json.dumps(actual, sort_keys=True):
        raise _error('selection_mismatch', path, value['files'], actual)
    return directory
