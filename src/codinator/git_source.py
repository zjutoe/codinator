"""Controller-owned Git source checkpoints, without a second content store."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .files import Problem, ScopeViolation, under


def git(root, *args, data=None, index=None, timeout=60, ok_returncodes=(0,)):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_ATTR_NOSYSTEM='1',
               GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1',
               GIT_OPTIONAL_LOCKS='0', GIT_ALLOW_PROTOCOL='')
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)
    argv = ['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', '-c', 'core.fileMode=true',
            '-c', 'core.ignoreStat=false',
            '-c', 'core.ignoreCase=false', '-c', 'core.attributesFile=/dev/null',
            '-c', 'core.excludesFile=/dev/null', '-c', 'commit.gpgSign=false',
            '-c', 'user.name=Codinator', '-c', 'user.email=controller@codinator.local', *args]
    try:
        result = subprocess.run(argv, input=data, capture_output=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Problem(f'Git source command failed: {exc}') from exc
    if result.returncode not in ok_returncodes:
        raise Problem('Git source command failed: ' + result.stderr.decode(errors='replace').strip())
    return result.stdout


def text(root, *args, **kwargs):
    return git(root, *args, **kwargs).decode().strip()


def common_dir(root):
    return Path(text(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()


def _branch(root, branch):
    if type(branch) is not str or not branch or branch.startswith('-'):
        raise Problem('Invalid Git source branch')
    git(root, 'check-ref-format', 'refs/heads/' + branch)


def _oid(value):
    if type(value) is not str or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value):
        raise Problem('Git source requires an exact object id')
    return value


def record(root, branch, commit=None):
    _branch(root, branch)
    commit = _oid(commit if commit is not None else text(root, 'rev-parse', '--verify', 'HEAD'))
    if text(root, 'cat-file', '-t', commit) != 'commit':
        raise Problem('Git source object is not a commit')
    return {'kind': 'git', 'branch': branch, 'commit': commit,
            'tree': _oid(text(root, 'rev-parse', commit + '^{tree}'))}


def _validate_record(root, branch, value):
    if (type(value) is not dict or set(value) != {'kind', 'branch', 'commit', 'tree'}
            or value['kind'] != 'git' or value['branch'] != branch):
        raise Problem('Invalid Git source record or branch identity')
    _oid(value['tree'])
    if record(root, branch, value['commit']) != value:
        raise Problem('Git source commit/tree identity changed')


def _head(root, branch):
    if text(root, 'symbolic-ref', '--no-recurse', 'HEAD') != 'refs/heads/' + branch:
        raise ScopeViolation('Git source branch changed')
    if text(root, 'for-each-ref', '--format=%(symref)', 'refs/heads/' + branch):
        raise Problem('Git source branch may not be a symbolic ref')
    return text(root, 'rev-parse', '--verify', 'HEAD')


def _boundary(root):
    if Path(text(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise Problem('Git source workspace must be the repository root')
    # Read local key names without following includes before any content operation.
    keys = text(root, 'config', '--local', '--no-includes', '--name-only', '--list').splitlines()
    forbidden = re.compile(r'^(filter\.|include\.|includeif\.|extensions\.|'
                           r'diff\..*\.(textconv|command)$|core\.(worktree|sparsecheckout|'
                           r'sparsecheckoutcone|splitindex)$)', re.I)
    if any(forbidden.search(key) for key in keys):
        raise Problem('Git source does not support filters, includes, textconv, sparse/split indexes or extensions')
    for name in ('info/exclude', 'info/attributes'):
        path = Path(text(root, 'rev-parse', '--path-format=absolute', '--git-path', name))
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise Problem('Unsupported Git source ' + name)
            if any(line.strip() and not line.lstrip().startswith(b'#') for line in path.read_bytes().splitlines()):
                raise Problem('Git source uses fixed .gitignore only; local excludes/attributes are unsupported')


def _tree_entries(root, commit):
    result = {}
    for entry in git(root, 'ls-tree', '-r', '-z', commit).split(b'\0'):
        if not entry:
            continue
        metadata, path = entry.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        if kind != b'blob' or mode not in (b'100644', b'100755', b'120000'):
            raise Problem('Git source does not support submodules or non-file tree entries')
        result[path] = mode + b' ' + oid + b' 0'
    return result


def _index_entries(root):
    result = {}
    for entry in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if entry:
            metadata, path = entry.split(b'\t', 1)
            if path in result or metadata.split()[-1] != b'0':
                raise ScopeViolation('Git source index has unmerged stages')
            result[path] = metadata
    debug = git(root, 'ls-files', '--debug', '-z')
    flags = re.findall(rb'\n  size: [0-9]+\tflags: ([0-9a-f]+)\n', debug)
    if len(flags) != len(result) or any(int(flag, 16) != 0 for flag in flags):
        raise ScopeViolation('Git source index flags changed (assume-unchanged/skip-worktree/extended flags)')
    return result


def _index_matches(root, *commits):
    current = _index_entries(root)
    if all(current != _tree_entries(root, commit) for commit in commits):
        raise ScopeViolation('Git source index was staged or otherwise changed')


def _paths(data):
    try:
        return [item.decode('utf-8') for item in data.split(b'\0') if item]
    except UnicodeError as exc:
        raise Problem('Git source requires UTF-8 paths') from exc


def _ignored_directory(root, path):
    # No trailing slash: that would let Git read this child's not-yet-validated
    # .gitignore and allow the child to hide itself from the parent traversal.
    return bool(git(root, 'check-ignore', '--no-index', '--stdin', '-z',
                    data=path.encode() + b'\0', ok_returncodes=(0, 1)))


def _fixed_ignores(root, commit, entries):
    tracked = set(entries)
    for path, metadata in entries.items():
        if path.rsplit(b'/', 1)[-1] != b'.gitignore':
            continue
        name = _paths(path + b'\0')[0]
        actual = root / name
        if (metadata.split()[0] not in (b'100644', b'100755') or actual.is_symlink()
                or not actual.is_file() or actual.read_bytes() != git(root, 'show', commit + ':' + name)):
            raise ScopeViolation('Fixed .gitignore changed: ' + name)
    # Validate parents before consulting Git for their child directories. A new
    # .gitignore cannot hide itself, but a cache already ignored by fixed parents
    # remains entirely outside this traversal (including its internal rules).
    for current, dirs, files in os.walk(root, followlinks=False):
        current = Path(current)
        relative = current.relative_to(root)
        if '.gitignore' in dirs + files:
            name = (relative / '.gitignore').as_posix()
            if name.encode() not in tracked:
                raise ScopeViolation('New effective .gitignore is forbidden: ' + name)
        if relative.parts and '.git' in dirs + files:
            raise ScopeViolation('Nested Git repository is forbidden: ' + relative.as_posix())
        for name in list(dirs):
            path = (relative / name).as_posix()
            if path == '.git' or (current / name).is_symlink() or _ignored_directory(root, path):
                dirs.remove(name)


def _conversions(root, paths):
    if not paths:
        return
    data = b'\0'.join(paths) + b'\0'
    attributes = git(root, 'check-attr', '-z', '--stdin', 'filter', 'text', 'eol',
                     'working-tree-encoding', 'ident', data=data).split(b'\0')[:-1]
    if len(attributes) % 3 or any(value not in (b'unspecified', b'unset') for value in attributes[2::3]):
        raise Problem('Git source does not support content conversion attributes')


@contextmanager
def _fresh_index(root, commit):
    with tempfile.TemporaryDirectory(prefix='codinator-git-index-', dir='/tmp') as temporary:
        index = Path(temporary) / 'index'
        git(root, 'read-tree', commit, index=index)
        yield index


def _worktree(root, commit, *, candidate=False):
    entries = _tree_entries(root, commit)
    _fixed_ignores(root, commit, entries)
    others = git(root, 'ls-files', '--others', '--exclude-standard', '-z')
    _conversions(root, list(entries) + [p for p in others.split(b'\0') if p])
    with _fresh_index(root, commit) as index:
        if candidate:
            git(root, 'add', '-A', '--', '.', index=index)
            tree = text(root, 'write-tree', index=index)
            _tree_entries(root, tree)
            return tree
        changed = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                      '--name-only', '-z', index=index)
        if changed or others:
            raise ScopeViolation('Git source workspace is not clean: ' + ', '.join(_paths(changed + others)[:20]))


def verify(manifest, expected=None):
    root = Path(manifest['workspace']).resolve()
    branch = manifest['git']['branch']
    _boundary(root)
    expected = record(root, branch) if expected is None else expected
    _validate_record(root, branch, expected)
    if _head(root, branch) != expected['commit']:
        raise ScopeViolation('Git source HEAD differs from the pinned checkpoint')
    _index_matches(root, expected['commit'])
    _worktree(root, expected['commit'])
    return expected


def preflight(manifest):
    return verify(manifest, record(manifest['workspace'], manifest['git']['branch'],
                                   manifest['git']['base_commit']))


def changed_paths(manifest, before, after):
    root, branch = Path(manifest['workspace']), manifest['git']['branch']
    for value in (before, after):
        _validate_record(root, branch, value)
    return _paths(git(root, 'diff-tree', '--no-commit-id', '--no-renames', '--no-ext-diff',
                      '--no-textconv', '-r', '--name-only', '-z', before['tree'], after['tree']))


def _scope(manifest, paths):
    outside = [p for p in paths if not under(p, manifest['allowed_paths'])
               or Path(p).name in ('.gitignore', '.gitmodules')]
    if outside:
        raise ScopeViolation('Git source changes outside allowed/fixed paths: ' + ', '.join(outside[:20]))


def validate_transition(manifest, before, after):
    paths = changed_paths(manifest, before, after)
    if before['commit'] != after['commit']:
        parents = text(manifest['workspace'], 'rev-list', '--parents', '-n', '1', after['commit']).split()[1:]
        if parents != [before['commit']] or before['tree'] == after['tree']:
            raise Problem('Git checkpoint must be one changed-tree child of its exact base, or reuse that base')
    elif before != after:
        raise Problem('Unchanged Git checkpoint identity mismatch')
    _tree_entries(manifest['workspace'], after['commit'])
    _scope(manifest, paths)
    return paths


def _document(path):
    path = Path(path)
    if path.resolve() != path.absolute() or not stat.S_ISREG(path.lstat().st_mode):
        raise Problem('Linked or non-regular Git checkpoint journal')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Problem('Duplicate Git checkpoint journal key')
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(), object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise Problem('Invalid Git checkpoint journal') from exc


def _write_once(path, value):
    path = Path(path)
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.git-journal-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _document(path) != value:
                raise Problem('Conflicting Git checkpoint journal: ' + str(path))
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


@contextmanager
def _locked_index(root, intent):
    index = Path(text(root, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    # The persistent inode proves ownership of index.lock after a crash; flock
    # prevents a concurrent replay of this same intent from taking over a live one.
    token = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
    owner = index.parent / ('codinator-index-' + token + '.json')
    _write_once(owner, intent)
    with owner.open('rb') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Problem('Git checkpoint reconciliation is already running') from exc
        lock = index.with_name(index.name + '.lock')
        try:
            os.link(owner, lock)
        except FileExistsError:
            if lock.is_symlink() or not os.path.samefile(owner, lock):
                raise Problem('Git index is locked by another operation')
        completed = False
        try:
            yield index
            completed = True
        finally:
            if lock.exists() and os.path.samefile(owner, lock):
                lock.unlink()
            if completed:
                owner.unlink()


def checkpoint(manifest, before, attempt_dir, task_id, attempt_number, purpose='submission'):
    if (type(task_id) is not str or not task_id or type(attempt_number) is not int or attempt_number < 1
            or type(purpose) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', purpose)):
        raise Problem('Invalid Git checkpoint task/attempt/purpose')
    root, branch = Path(manifest['workspace']).resolve(), manifest['git']['branch']
    attempt_dir = Path(attempt_dir)
    _boundary(root)
    _validate_record(root, branch, before)
    identity = {'version': 1, 'task_id': task_id, 'attempt': attempt_number, 'purpose': purpose,
                'workspace': str(root), 'before': before}
    intent_path = attempt_dir / f'git-{purpose}-intent.json'
    result_path = attempt_dir / f'git-{purpose}-result.json'
    if intent_path.exists() or intent_path.is_symlink():
        intent = _document(intent_path)
        if (type(intent) is not dict or set(intent) != {*identity, 'candidate'}
                or {key: intent[key] for key in identity} != identity):
            raise Problem('Git checkpoint intent identity changed')
        candidate = intent['candidate']
        validate_transition(manifest, before, candidate)
    else:
        if result_path.exists() or result_path.is_symlink():
            raise Problem('Git checkpoint result has no intent')
        if _head(root, branch) != before['commit']:
            raise ScopeViolation('Git source HEAD changed before checkpoint')
        _index_matches(root, before['commit'])
        tree = _worktree(root, before['commit'], candidate=True)
        paths = _paths(git(root, 'diff-tree', '--no-commit-id', '--no-renames', '-r', '--name-only',
                          '-z', before['tree'], tree))
        _scope(manifest, paths)
        commit = before['commit'] if tree == before['tree'] else text(root, 'commit-tree', tree,
            '-p', before['commit'], data=f'Codinator {task_id} attempt {attempt_number}: {purpose}\n'.encode())
        candidate = record(root, branch, commit)
        intent = identity | {'candidate': candidate}
        _write_once(intent_path, intent)
    complete = result_path.exists() or result_path.is_symlink()
    if complete:
        if _document(result_path) != candidate:
            raise Problem('Git checkpoint result differs from intent')
    with _locked_index(root, intent) as real_index:
        if complete:
            return verify(manifest, candidate)
        head = _head(root, branch)
        if head not in (before['commit'], candidate['commit']):
            raise ScopeViolation('Git source HEAD is neither the intent base nor its candidate')
        _index_matches(root, *([before['commit']] if head == before['commit'] else
                               [before['commit'], candidate['commit']]))
        if _worktree(root, before['commit'], candidate=True) != candidate['tree']:
            raise ScopeViolation('Git source changed after checkpoint intent')
        if head != candidate['commit']:
            git(root, 'update-ref', '--no-deref', '-m', 'Codinator source checkpoint',
                'refs/heads/' + branch, candidate['commit'], before['commit'])
        # Use a fresh cache-free index, never git reset/checkout or worktree writes.
        with _fresh_index(root, candidate['commit']) as fresh:
            fd, replacement = tempfile.mkstemp(prefix='codinator-index-', dir=real_index.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(fresh.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(replacement, real_index)
            finally:
                if os.path.exists(replacement):
                    os.unlink(replacement)
        directory = os.open(real_index.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        verify(manifest, candidate)
        _write_once(result_path, candidate)
    return candidate
