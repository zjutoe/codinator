"""Archive newly generated Python caches before applying the unchanged scope gate."""
import re
import sys
import tempfile
from importlib.util import MAGIC_NUMBER
from pathlib import Path

from .files import Problem, assert_scope, file_info, preserve, snapshot, under, write_json


def clean_bytecode(root, before, current, allowed, excludes, task_dir, blobs):
    files = []
    for name, info in current['files'].items():
        path = Path(name)
        match = re.fullmatch(r'(.+)\.' + re.escape(sys.implementation.cache_tag)
                             + r'(?:\.opt-[12])?\.pyc', path.name)
        if (name in before['files'] or under(name, allowed) or info['kind'] != 'file'
                or path.parent.name != '__pycache__' or not match):
            continue
        source = (path.parent.parent / (match[1] + '.py')).as_posix()
        if current['files'].get(source, {}).get('kind') != 'file':
            continue
        actual = root / name
        # Only the controller interpreter's known tag/magic pair is automatic.
        # Unknown versions remain explicit violations, never guessed or executed.
        with actual.open('rb') as stream:
            header = stream.read(16)
        if (len(header) == 16 and header[:4] == MAGIC_NUMBER
                and int.from_bytes(header[4:8], 'little') in (0, 1, 3)
                and actual.lstat().st_nlink == 1):
            files.append(name)
    removed = set(files)
    directories = sorted({str(Path(name).parent) for name in files})
    directories = [name for name in directories if name not in before['files']
                   and all(p in removed for p in current['files'] if p.startswith(name + '/'))]
    removed.update(directories)
    projected = current | {'files': {p: v for p, v in current['files'].items() if p not in removed}}
    # Never clean around another violation, including modified baseline caches,
    # symlinks, tracked files, Git changes or arbitrary ignored output.
    assert_scope(before, projected, allowed)
    if not removed:
        return current

    archive = Path(tempfile.mkdtemp(prefix='cache-cleanup-', dir=task_dir))
    preserve(root, current, blobs)
    write_json(archive / 'before.json', current)
    write_json(archive / 'cleanup.json', {'files': files, 'directories': directories,
                                         'policy': 'new-python-bytecode-v1'})
    if snapshot(root, excludes) != current:
        raise Problem('Workspace changed before bytecode cleanup; no cleanup performed')
    for name in files:
        source = root / name
        if (source.resolve() != source.absolute() or source.lstat().st_nlink != 1
                or file_info(source) != current['files'][name]):
            raise Problem('Bytecode changed during cleanup; inspect archived evidence')
        target = archive / 'removed' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Keep the actual entry as well as its pre-cleanup blob. A partial failure
        # never discards bytes; cross-filesystem moves fail without a delete fallback.
        source.rename(target)
        if target.lstat().st_nlink != 1 or file_info(target) != current['files'][name]:
            raise Problem('Bytecode changed during cleanup; retained in ' + str(target))
    for name in directories:
        (root / name).rmdir()  # Only new, now-empty cache directories.
    after = snapshot(root, excludes)
    write_json(archive / 'after.json', after)
    if after != projected:
        raise Problem('Workspace changed during bytecode cleanup; inspect archived evidence')
    return after
