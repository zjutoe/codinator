"""Real Codex adapter check, separate from the Pi+Bonsai end-to-end smoke.

The controller authors this trivial fixture. Do not describe it as Pi output.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codidator.agents import reviewer
from codidator.files import digest, snapshot, write_json
from codidator.sandbox import Sandbox

base = Path(tempfile.mkdtemp(prefix='codidator-review-live-', dir='/home/mye/data'))
root = base / 'workspace'
root.mkdir()
subprocess.run(['git', 'init', '-q', str(root)], check=True)
(root / 'handoff.md').write_text('Implement answer.py containing answer() returning the exact int 42. No IO, dependencies or global state.\n')
before = snapshot(root, [])
(root / 'answer.py').write_text('def answer():\n    return 42\n')
after = snapshot(root, [])
attempt = base / 'attempt'
(attempt / 'delivery').mkdir(parents=True)
(attempt / 'delivery/summary.md').write_text('Controller-authored connectivity fixture, not Pi output. Added answer(). Reviewer must verify.\n')
write_json(attempt / 'before.json', before)
write_json(attempt / 'submission.json', after)
write_json(attempt / 'diff.json', {'paths': ['answer.py'], 'digest': digest(after)})
task = {'manifest': {'id': 'review-smoke', 'workspace': str(root), 'handoff': 'handoff.md'}, 'feedback': ''}
print('REVIEW_SMOKE_ROOT=' + str(base), flush=True)
try:
    verdict = reviewer(task, attempt, digest(after), {'timeout': 420}, sandbox=Sandbox(), private=base / 'private')
    result = {'verdict': verdict['verdict'], 'summary': verdict['summary'], 'evidence_root': str(base)}
except Exception as exc:
    result = {'verdict': 'integration_failure', 'reason': str(exc), 'evidence_root': str(base)}
write_json(base / 'result.json', result)
print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
raise SystemExit(0 if result['verdict'] == 'accepted' else 1)
