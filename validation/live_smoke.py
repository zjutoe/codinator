"""Opt-in real Pi/Bonsai + Codex smoke in a newly created, isolated repository.

No notifications, commits, pushes, or modifications to KMesh. Runtime state may
contain private credentials; print only its location and outcome.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codidator.config import load_manifest
from codidator.engine import Engine
from codidator.store import Store

base = Path(tempfile.mkdtemp(prefix='codidator-live-', dir='/home/mye/data'))
workspace = base / 'workspace'
workspace.mkdir()
subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
(workspace / 'tests').mkdir()
(workspace / 'tests/test_arithmetic.py').write_text('''import unittest
from arithmetic import add
class ArithmeticTests(unittest.TestCase):
    def test_values(self):
        for a,b in ((0,0),(2,3),(-5,2),(10**100,10**100)):
            self.assertEqual(add(a,b),a+b)
    def test_types(self):
        for a,b in ((True,2),(2,False),(1.5,2),(2,"3"),(None,1)):
            with self.assertRaises(TypeError): add(a,b)
''')
(workspace / 'handoff.md').write_text('''# Live smoke contract
Implement only arithmetic.py, exposing add(a,b). Both arguments must have
type(x) is int (bool is rejected). Invalid types raise TypeError; valid inputs
return their exact integer sum, including negatives and arbitrarily large values.
Use only Python stdlib, no IO, no global mutable state. No new test files are
required: tests/test_arithmetic.py is the fixed external oracle and is read-only.
Validation: python3 -m unittest discover -s tests -v.
Do not stage/commit/push or edit this handoff. Write the assigned delivery summary
and completion JSON, then stop. This is an isolated integration fixture.
''')
manifest = {'version': 1, 'id': 'live-smoke', 'workspace': str(workspace), 'handoff': 'handoff.md',
            'allowed_paths': ['arithmetic.py'],
            'checks': [{'name': 'unit', 'argv': ['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-v'], 'timeout_seconds': 30}],
            'max_rounds': 2, 'max_seconds': 2400, 'attempt_seconds': 1200}
path = base / 'task.json'
path.write_text(json.dumps(manifest, indent=2) + '\n')
print('LIVE_SMOKE_ROOT=' + str(base), flush=True)
store = Store(base / 'state')
engine = Engine(store)
engine.submit(load_manifest(path))
engine.run('live-smoke')
task = store.get('live-smoke')
result = {'state': task['state'], 'reason': task['reason'], 'round': task['round'], 'attempt': task['attempt'],
          'evidence_root': str(base)}
(base / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
raise SystemExit(0 if task['state'] == 'accepted' else 1)
