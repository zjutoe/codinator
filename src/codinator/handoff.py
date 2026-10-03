"""Format-only documents and immutable task-local template contracts."""
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import re
import tempfile

from .files import Problem, digest, write_json


SECTIONS = {
    'handoff': ('Goal', 'Scope and constraints', 'Required inputs', 'Deliverables',
                'Acceptance criteria', 'Handoff rules'),
    'summary': ('Status', 'Completed', 'Incomplete', 'Attempts and results',
                'Artifacts and evidence', 'Deviations and unknowns', 'Next step'),
    'help': ('Status', 'Completed', 'Incomplete', 'Attempts and results',
             'Artifacts and evidence', 'Deviations and unknowns', 'Next step',
             'Blocker', 'Question for Codex'),
    'review': ('Subject', 'Verdict', 'Basis and verification', 'Rework', 'Stop reason'),
    'guidance': ('Question', 'Advice', 'Basis', 'Unknowns', 'Next boundary'),
}
PROTOCOL_FILE = 'handoff-protocol.json'


class DocumentError(Problem):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('Document format errors: ' + json.dumps(errors, ensure_ascii=False))


def enabled(manifest):
    return type(manifest.get('handoff_protocol')) is int and manifest['handoff_protocol'] == 1


def _kind(kind):
    if not isinstance(kind, str) or kind not in SECTIONS:
        raise Problem(f'Unknown document kind: {kind}')


def template(kind):
    """Read a shipped template without touching workflow state or invoking agents."""
    _kind(kind)
    return resources.files('codinator').joinpath('templates', kind + '.md').read_text(encoding='utf-8')


def validate_document(text, kind, location='document'):
    """Check required h2 sections and nonempty bodies, without interpreting prose."""
    _kind(kind)
    if not isinstance(text, str):
        raise DocumentError([{'code': 'invalid_type', 'path': location,
                              'expected': 'string', 'actual': type(text).__name__}])
    bodies = {name: [] for name in SECTIONS[kind]}
    current = None
    fence = None
    for line in text.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence is not None:
            if current is not None:
                current.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
            continue
        if marker and (marker[1][0] == '~' or '`' not in marker[2]):
            fence = (marker[1][0], len(marker[1]))
            if current is not None:
                current.append(line)
            continue
        heading = re.match(r'^ {0,3}##(?:[ \t]+(.*)|[ \t]*)$', line)
        if heading:
            name = re.sub(r'[ \t]+#+[ \t]*$', '', heading[1] or '').strip()
            current = None
            if name in bodies:
                current = []
                bodies[name].append(current)
            continue
        if current is not None:
            current.append(line)
    errors = []
    for name, occurrences in bodies.items():
        path = f'{location}.sections[{name}]'
        if not occurrences:
            errors.append({'code': 'missing_section', 'path': path,
                           'expected': f'## {name}', 'actual': None})
        elif len(occurrences) > 1:
            errors.append({'code': 'duplicate_section', 'path': path,
                           'expected': 1, 'actual': len(occurrences)})
        elif not '\n'.join(occurrences[0]).strip():
            errors.append({'code': 'empty_section', 'path': path,
                           'expected': 'nonempty body', 'actual': ''})
    if errors:
        raise DocumentError(errors)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Problem(f'Missing or non-regular frozen protocol artifact: {path}')
    try:
        return path.read_bytes()
    except OSError as exc:
        raise Problem(f'Cannot read frozen protocol artifact: {path}') from exc


def _write_once(path, data):
    """Publish complete bytes atomically without replacing earlier evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise Problem(f'Symlink frozen template directory: {path.parent}')
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(name)


def _record(task_dir):
    try:
        record = json.loads(_read(task_dir / PROTOCOL_FILE))
    except (ValueError, UnicodeError) as exc:
        raise Problem('Invalid frozen handoff protocol record') from exc
    if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1:
        raise Problem('Invalid frozen handoff protocol version')
    for key in ('contract_digest', 'manifest_sha256', 'handoff_sha256'):
        if not isinstance(record.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', record[key]):
            raise Problem(f'Invalid frozen handoff protocol {key}')
    templates = record.get('templates')
    if not isinstance(templates, dict) or set(templates) != set(SECTIONS):
        raise Problem('Invalid frozen template inventory')
    for kind, entry in templates.items():
        if (not isinstance(entry, dict) or entry.get('path') != f'templates/{kind}.md'
                or not isinstance(entry.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
            raise Problem(f'Invalid frozen template identity: {kind}')
    return record


def _frozen_template(task_dir, record, kind):
    entry = record['templates'][kind]
    path = task_dir / entry['path']
    if path.parent.is_symlink():
        raise Problem(f'Symlink frozen template directory: {path.parent}')
    data = _read(path)
    if _sha(data) != entry['sha256']:
        raise Problem(f'Frozen template identity mismatch: {kind}')
    try:
        text = data.decode('utf-8')
    except UnicodeError as exc:
        raise Problem(f'Invalid frozen template encoding: {kind}') from exc
    validate_document(text, kind, f'templates.{kind}')
    return text


def freeze_protocol(task_dir, manifest):
    """Freeze originals once; an existing completed record is verified, never updated."""
    if not enabled(manifest):
        raise Problem('Freezing templates requires handoff_protocol: 1')
    task_dir = Path(task_dir)
    if (task_dir / PROTOCOL_FILE).exists():
        return verify_protocol(task_dir, manifest)
    if digest(_manifest(task_dir)) != digest(manifest):
        raise Problem('Frozen manifest artifact identity mismatch')
    handoff_data = _read(task_dir / 'handoff.md')
    try:
        handoff_text = handoff_data.decode('utf-8')
    except UnicodeError as exc:
        raise Problem('Invalid frozen handoff encoding') from exc
    validate_document(handoff_text, 'handoff', 'handoff')
    handoff_sha256 = _sha(handoff_data)
    record = {'version': 1, 'manifest_sha256': digest(manifest),
              'handoff_sha256': handoff_sha256,
              'contract_digest': digest({'manifest': manifest, 'handoff_sha256': handoff_sha256}),
              'templates': {}}
    for kind in SECTIONS:
        content = template(kind)
        validate_document(content, kind, f'templates.{kind}')
        data = content.encode('utf-8')
        name = f'templates/{kind}.md'
        _write_once(task_dir / name, data)
        record['templates'][kind] = {'path': name, 'sha256': _sha(data)}
    write_json(task_dir / PROTOCOL_FILE, record)
    return verify_protocol(task_dir, manifest)


def _manifest(task_dir):
    try:
        value = json.loads(_read(task_dir / 'manifest.json'))
    except (ValueError, UnicodeError) as exc:
        raise Problem('Invalid frozen manifest') from exc
    if not isinstance(value, dict):
        raise Problem('Invalid frozen manifest type')
    return value


def verify_protocol(task_dir, manifest):
    """Verify task-local originals; installed template updates do not affect history."""
    if not enabled(manifest):
        raise Problem('Verification requires handoff_protocol: 1')
    task_dir = Path(task_dir)
    record = _record(task_dir)
    handoff_data = _read(task_dir / 'handoff.md')
    handoff_sha256 = _sha(handoff_data)
    if digest(manifest) != record['manifest_sha256']:
        raise Problem('Frozen manifest identity mismatch')
    if handoff_sha256 != record['handoff_sha256']:
        raise Problem('Frozen handoff identity mismatch')
    if digest({'manifest': manifest, 'handoff_sha256': handoff_sha256}) != record['contract_digest']:
        raise Problem('Frozen handoff contract digest mismatch')
    if digest(_manifest(task_dir)) != record['manifest_sha256']:
        raise Problem('Frozen manifest artifact identity mismatch')
    for kind in SECTIONS:
        _frozen_template(task_dir, record, kind)
    return record


def protocol_template(task_dir, kind):
    """Read the exact template frozen for this task, with identity validation."""
    _kind(kind)
    task_dir = Path(task_dir)
    return _frozen_template(task_dir, _record(task_dir), kind)
