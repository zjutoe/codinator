"""Prepare an isolated v2 fixture; --run explicitly spends real Pi/Codex model budget."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codinator.config import load_manifest
from codinator.engine import Engine
from codinator.store import Store


def prepare(base):
    """Host-owned Git preparation; the controller never runs Git or project checks."""
    workspace = base / 'workspace'
    workspace.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.fsmonitor=false', '-c', 'commit.gpgsign=false',
            '-c', 'user.name=Smoke fixture', '-c', 'user.email=smoke@example.invalid',
            *args], cwd=workspace, text=True).strip()
    git('init', '-q', '-b', 'main')
    (workspace / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
    (workspace / 'tests').mkdir()
    (workspace / 'tests/test_arithmetic.py').write_text("""import unittest
from arithmetic import add
class ArithmeticTests(unittest.TestCase):
    def test_values(self):
        for a,b in ((0,0),(2,3),(-5,2),(10**100,10**100)):
            self.assertEqual(add(a,b),a+b)
    def test_types(self):
        for a,b in ((True,2),(2,False),(1.5,2),(2,'3'),(None,1)):
            with self.assertRaises(TypeError): add(a,b)
""")
    (workspace / 'handoff.md').write_text("""# Isolated real-model smoke

## Goal
Pi implements arithmetic.add(a,b). Independent Codex verifies the committed result.

## Scope and constraints
Edit only arithmetic.py. Accept arguments with type(x) is int. Reject bool and
all other types with TypeError. Return the exact integer sum for valid inputs.
Use only Python stdlib. Do not use IO or mutable global state. The tests and
handoff are read-only. Do not merge, push, change branch, or start background work.

## Required inputs
Read tests/test_arithmetic.py as the fixed oracle. The manifest binds the branch,
baseline SHA and verification argv. First verify the clean declared starting state.

## Deliverables
Commit arithmetic.py locally before checks. Submit a final summary and Git/check
evidence through this attempt's bound delivery command. Codinator does not run
Git or project checks. A submitted receipt is not acceptance.

## Acceptance criteria
The fixed unittest oracle passes on the exact clean candidate SHA. Independent
Codex verifies the real Git identity, allowed commit scope and raw evidence,
and reruns the declared check. Negative and arbitrarily large integer sums work.

## Handoff rules
Follow the frozen summary/help templates. At the first behavioral checkpoint,
record the actual check command, exit code and raw output in tool events. Passive
observations need no response. Repeated failure or unclear requirements need
bound help and committed partial evidence; an external blocker needs blocked.
Stop after a valid delivery. No invented Git or test claims are allowed.
""")
    git('add', '.')
    git('commit', '-qm', 'Freeze isolated smoke contract and oracle')
    baseline = git('rev-parse', 'HEAD')
    git('switch', '-qc', 'live-smoke')
    manifest = {'version': 2, 'id': 'live-smoke', 'workspace': str(workspace),
        'handoff': 'handoff.md', 'handoff_protocol': 1,
        'git': {'branch': 'live-smoke', 'base_commit': baseline},
        'allowed_paths': ['arithmetic.py'],
        'checks': [{'name': 'unit', 'argv': ['python3', '-B', '-m', 'unittest',
                    'discover', '-s', 'tests', '-v'], 'timeout_seconds': 30}],
        'checkpoint_mode': 'observe', 'checkpoint_seconds': 600,
        'max_rounds': 2, 'max_seconds': 2400, 'attempt_seconds': 1200}
    path = base / 'task.json'
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    return load_manifest(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Call real Pi/Bonsai and Codex; 40 minutes total, two dispatch rounds')
    parser.add_argument('--output-parent', type=Path, default=Path(tempfile.gettempdir()),
                        help='Existing writable parent for a fresh fixture and private runtime')
    args = parser.parse_args(argv)
    base = Path(tempfile.mkdtemp(prefix='codinator-live-', dir=args.output_parent)).resolve()
    base.chmod(0o700)
    manifest = prepare(base)
    print('LIVE_SMOKE_ROOT=' + str(base), flush=True)
    if not args.run:
        print('Fixture prepared only; no agents dispatched or connectivity verified.', flush=True)
        return 0
    store = Store(base / 'state')
    try:
        engine = Engine(store)
        engine.submit(manifest)
        engine.run(manifest['id'])
        task = store.get(manifest['id'])
        result = {key: task[key] for key in ('state', 'reason', 'round', 'attempt')}
        result['evidence_root'] = str(base)
        (base / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if task['state'] == 'accepted' else 1
    finally:
        store.db.close()


if __name__ == '__main__':
    raise SystemExit(main())
