"""Opt-in real Pi -> Codex -> Pi commit/merge in a new disposable repository.

Never pushes or changes existing workspaces. Raw runtime/configuration stays outside this repo.
"""
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codinator.config import load_manifest
from codinator.engine import Engine
from codinator.files import digest, snapshot, write_json
from codinator.integration import git, text
from codinator.store import Store

os.umask(0o077)
base = Path(tempfile.mkdtemp(prefix='codinator-integration-live-', dir='/home/mye/data'))
target, work = base / 'main', base / 'task'
target.mkdir()
git(target, 'init', '-b', 'master')
git(target, 'config', 'user.name', 'Codinator smoke setup')
git(target, 'config', 'user.email', 'fixture@codinator.local')
(target / 'README.md').write_text('Live integration fixture.\n')
(target / 'verify.py').write_text('from product import double\nassert double(0) == 0\nassert double(-3) == -6\nassert double(10**100) == 2*10**100\n')
git(target, 'add', 'README.md', 'verify.py')
git(target, 'commit', '-m', 'Prepare independent integration fixture', '-m', 'Fixed oracle; implementation and integration will use real Pi and Codex.')
head = text(target, 'rev-parse', 'HEAD')
git(target, 'worktree', 'add', '-b', 'task', str(work))
(target / 'README.md').write_text('Uncommitted planning must survive integration.\n')
(work / 'handoff.md').write_text('''# Integration smoke
Implement only product.py with a pure function double(value) returning value * 2
for integer inputs. Type validation is not required. No IO or mutable global state.
Read-only verification: python3 -B verify.py. Keep Git unchanged during implementation.
Write only the assigned delivery files. After independent acceptance the controller
will separately ask Pi to commit the accepted bytes and ff-only merge to master in
a private clone; then promote exactly that commit to the real fixture master.
No push. Preserve the original accepted workspace and unrelated main planning edits.
''')
manifest = {'version': 1, 'id': 'integration-live', 'workspace': str(work), 'handoff': 'handoff.md',
            'allowed_paths': ['product.py'], 'checks': [{'name': 'verify', 'argv': ['python3', '-B', 'verify.py'], 'timeout_seconds': 30}],
            'max_rounds': 2, 'max_seconds': 2400, 'attempt_seconds': 720,
            'integration': {'target_workspace': str(target), 'target_branch': 'master', 'base_commit': head,
                            'planning_paths': ['README.md', 'handoff.md']}}
path = base / 'task.json'
write_json(path, manifest)
print('LIVE_INTEGRATION_ROOT=' + str(base), flush=True)
code = Path(__file__).resolve().parents[1]
write_json(base / 'controller-code.json', {str(p.relative_to(code)): hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted((code / 'src/codinator').glob('*.py'))})
store = Store(base / 'state')
engine = Engine(store)
engine.submit(load_manifest(path))
engine.run('integration-live')
task = store.get('integration-live')
result = {'state': task['state'], 'reason': task['reason'], 'integration': task['integration'],
          'round': task['round'], 'attempt': task['attempt'], 'evidence_root': str(base),
          'source_snapshot_unchanged': digest(snapshot(work, task['manifest']['excludes'])) == task['expected_digest'],
          'target_head': text(target, 'rev-parse', 'HEAD'),
          'dirty_planning_preserved': (target / 'README.md').read_text() == 'Uncommitted planning must survive integration.\n'}
write_json(base / 'result.json', result)
print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
raise SystemExit(0 if task['state'] == 'accepted' and result['source_snapshot_unchanged'] and result['dirty_planning_preserved'] else 1)
