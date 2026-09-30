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
    if (errors or type(value) is not dict or set(value) != keys
            or type(value['version']) is not int or value['version'] != 1
            or type(value['task_id']) is not str or not value['task_id']
            or any(type(value[key]) is not int or value[key] < 1 for key in ('round', 'attempt'))
            or type(value['delivery_dir']) is not str or not Path(value['delivery_dir']).is_absolute()):
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
    parser = argparse.ArgumentParser(description='Submit this attempt for controller checks; never accept work')
    parser.add_argument('--summary', required=True, type=Path)
    parser.add_argument('--status', required=True, choices=STATUSES)
    args = parser.parse_args(argv)
    try:
        contract = _contract(Path(contract_path))
        directory = Path(contract['delivery_dir'])
        _directory(directory)
        summary = _read(args.summary)
        _summary(summary, args.summary)
        summary_path, completion_path = (directory / name for name in NAMES)
        old_summary, old_completion = _optional(summary_path), _optional(completion_path)
        identity = (contract['task_id'], contract['round'], contract['attempt'])
        if old_completion is not None:
            status = validate_delivery(directory, *identity)
            if status != args.status:
                raise _error('conflict', completion_path, status, args.status)
        if old_summary is not None and old_summary != summary:
            raise _error('conflict', summary_path, 'identical existing content', 'different content')
        completion = dict(zip(('task_id', 'round', 'attempt'), identity)) | {'status': args.status}
        if old_summary is None:
            _publish_same(summary_path, summary)
        if old_completion is None:
            _publish_same(completion_path, _encode(completion))
        validate_delivery(directory, *identity)
        print(json.dumps({'status': 'submitted', 'task_id': identity[0], 'round': identity[1],
                          'attempt': identity[2], 'delivery_dir': str(directory),
                          'disposition': args.status}))
        return 0
    except DeliveryError as exc:
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


def _infos(directory):
    result = {}
    for name in NAMES:
        _read(directory / name)
        result[name] = file_info(directory / name)
    return result


def select_delivery(attempt_dir, directory):
    attempt_dir = Path(attempt_dir).absolute()
    directory = _selection_directory(attempt_dir, directory)
    selection = {'version': 1, 'directory': directory.relative_to(attempt_dir).as_posix(),
                 'files': _infos(directory)}
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
            or type(value['version']) is not int or value['version'] != 1
            or value['directory'] not in ('delivery', 'delivery-repair/delivery')
            or type(value['files']) is not dict or set(value['files']) != set(NAMES)):
        raise _error('invalid_selection', path, 'version 1 selection with exactly two file identities', value)
    directory = _selection_directory(attempt_dir, attempt_dir / value['directory'])
    actual = _infos(directory)
    if json.dumps(value['files'], sort_keys=True) != json.dumps(actual, sort_keys=True):
        raise _error('selection_mismatch', path, value['files'], actual)
    return directory
