"""Exercise both real filesystem layers without a model or authentication call."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codinator.sandbox import Sandbox

codex = shutil.which('codex')
assert codex, 'Install Codex before running this explicit native validation'
fixture_parent = Path(sys.argv[1]).resolve()
assert fixture_parent.is_dir() and not fixture_parent.is_relative_to('/tmp')
with tempfile.TemporaryDirectory(prefix='codex-layers-', dir=fixture_parent) as name:
    root = Path(name)
    workspace = root / 'workspace'
    evidence = root / 'original-evidence'
    for path in (workspace, evidence):
        path.mkdir(mode=0o700)
    (workspace / '.git').mkdir()
    source = workspace / 'source.py'
    git_config = workspace / '.git/config'
    original = evidence / 'submission.json'
    for path in (source, git_config, original):
        path.write_text('frozen\n')
    code = '''
import errno, pathlib, sys, tempfile
mode = sys.argv[1]
try:
    with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
        p = pathlib.Path(tmp) / 'check-output'
        p.write_text('fixture works')
        assert p.read_text() == 'fixture works'
except OSError as exc:
    assert mode == 'read-only' and exc.errno in (errno.EROFS, errno.EACCES), exc
else:
    assert mode == 'read-only-tmp', 'Old read-only policy unexpectedly allowed scratch'
for value in sys.argv[2:]:
    try:
        pathlib.Path(value).write_text('unauthorized')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES), exc
    else:
        raise AssertionError('Frozen path was writable: ' + value)
print('PASS: ' + mode + '; source, Git and original evidence remain read-only')
'''
    for mode in ('read-only', 'read-only-tmp'):
        config = root / (mode + '-codex-home')
        config.mkdir(mode=0o700)
        command = [codex, 'sandbox', '-c', 'default_permissions=":read-only"']
        if mode == 'read-only-tmp':
            command = [codex, 'sandbox',
                       '-c', 'default_permissions="codinator_review"',
                       '-c', 'permissions.codinator_review.extends=":read-only"',
                       '-c', 'permissions.codinator_review.filesystem={"/tmp"="write"}',
                       '-c', 'permissions.codinator_review.network.enabled=false']
        command += ['--', sys.executable, '-B', '-c', code, mode,
                    str(source), str(git_config), str(original)]
        result = subprocess.run(Sandbox().wrap(command, workspace, writable=[config],
                                              readonly=[evidence]),
                                env=os.environ | {'CODEX_HOME': str(config)},
                                capture_output=True, text=True, timeout=20)
        print(result.stdout, end='')
        if result.returncode:
            print(result.stderr, end='', file=sys.stderr)
        assert result.returncode == 0, (mode, result.returncode)
    for path in (source, git_config, original):
        assert path.read_text() == 'frozen\n', path
