"""Real bubblewrap smoke, no model calls. Writes only fresh temporary fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codinator.files import Problem
from codinator.sandbox import Sandbox

with tempfile.TemporaryDirectory(prefix='codinator-native-') as name:
    root = Path(name)
    workspace = root / 'workspace'
    evidence = root / 'evidence'
    workspace.mkdir()
    evidence.mkdir()
    (workspace / 'frozen').write_text('original')
    (workspace / 'allowed').write_text('before')
    (workspace / '.git').mkdir()
    (workspace / '.git/config').write_text('frozen git')
    (evidence / 'schema.json').write_text('{}')
    code = '''
from pathlib import Path
import sys
root = Path(sys.argv[1]); evidence = Path(sys.argv[2])
assert (evidence/'schema.json').read_text() == '{}'
(root/'allowed').write_text('after')
for p in (root/'frozen',root/'.git/config',evidence/'schema.json'):
    try:
        p.write_text('illegal')
    except OSError:
        pass
    else:
        raise AssertionError('write protection failed: '+str(p))
print('PASS: writable allowlist, frozen files/Git/evidence, /tmp evidence remount')
'''
    command = Sandbox().wrap([sys.executable, '-c', code, str(workspace), str(evidence)], workspace,
                             ['allowed'], readonly=[evidence])
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    print(result.stdout, end='')
    print(result.stderr, end='', file=sys.stderr)
    assert result.returncode == 0
    assert (workspace / 'frozen').read_text() == 'original'
    assert (workspace / 'allowed').read_text() == 'after'
    os.link(workspace / 'frozen', workspace / 'alias')
    try:
        Sandbox().wrap(['true'], workspace, ['alias'])
    except Problem:
        print('PASS: writable hardlink rejected before launch')
    else:
        raise AssertionError('hardlink accepted')
