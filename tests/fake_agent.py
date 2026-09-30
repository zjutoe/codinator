#!/usr/bin/env python3
"""Deterministic subprocess fixture. No network or real model invocation."""
import json
import os
from pathlib import Path
import re
import sys
import time
import subprocess
import shlex
import tempfile

MODE = os.environ.get('FAKE_MODE', 'accept')


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


if '--mode' in sys.argv:
    for line in sys.stdin.buffer:
        cmd = json.loads(line)
        if cmd['type'] == 'get_state':
            emit({'type': 'response', 'id': 'state', 'success': True, 'data': {
                'model': {'provider': 'bonsai', 'id': 'bonsai2-27b'}, 'thinkingLevel': 'xhigh',
                'isStreaming': False, 'pendingMessageCount': 0, 'sessionId': 'fake'}})
        elif cmd['type'] == 'prompt':
            emit({'type': 'response', 'id': 'prompt', 'success': True})
            prompt = cmd['message']
            integrating = 'You are the INTEGRATOR' in prompt
            repairing = 'repairing ONLY the delivery protocol' in prompt
            if integrating:
                completion_path = Path(re.search(r'Write (.+/completion.json) as', prompt)[1])
                body = re.search(r'as (\{"task_id".*?\})\.', prompt, re.S)[1]
                task = json.loads(body)
            else:
                contract = json.loads(Path(re.search(r'^Delivery contract: (.+)$', prompt, re.M)[1]).read_text())
                completion_path = Path(contract['delivery_dir']) / 'completion.json'
                task = {k: contract[k] for k in ('task_id', 'round', 'attempt')}
                task['status'] = 'awaiting_review'
            if MODE == 'sleep':
                time.sleep(60)
            if MODE == 'gated-rework':
                gate = Path(os.environ['FAKE_GATE'])
                while not gate.exists():
                    time.sleep(.05)
            if integrating:
                def git(*args):
                    subprocess.run(['git', *args], check=True, stdout=sys.stderr, stderr=sys.stderr)
                integration_mode = os.environ.get('FAKE_INTEGRATION', '')
                if integration_mode == 'wrong-tree':
                    Path('unreviewed.py').write_text('not accepted\n')
                git('add', '-A')
                message = ['-m', 'Implement accepted fixture']
                if integration_mode != 'no-body':
                    message += ['-m', 'Reviewed product.py VALUE=42. Controller check passed.']
                git('commit', *message)
                git('switch', 'master')
                git('merge', '--ff-only', 'codinator-integration')
                if integration_mode == 'crash':
                    sys.exit(9)
            elif not repairing:
                Path('product.py').write_text('VALUE = 42\n')
            if MODE == 'scope':
                Path('forbidden').write_text('bad')
            if repairing and MODE == 'repair-mutation':
                Path('product.py').write_text('VALUE = -1\n')
            missing = MODE == 'missing-delivery' or (MODE == 'missing-then-repair' and not repairing)
            malformed = MODE == 'bad-completion-shape' or (
                MODE in ('malformed-then-repair', 'repair-mutation', 'repair-truncated', 'repair-unavailable') and not repairing)
            if MODE == 'misplaced-delivery':
                Path('delivery').mkdir(exist_ok=True)
                completion_path = Path('delivery/completion.json')
            if not missing:
                if MODE == 'deep-completion':
                    completion_path.write_text('[' * 10000 + '0' + ']' * 10000)
                    (completion_path.parent / 'summary.md').write_text('Implemented VALUE.\n')
                elif malformed:
                    bad = [] if MODE == 'bad-completion-shape' else {
                        'task_id': 'research-id', 'round': 2, 'attempt': 1, 'status': 'completed', 'run_id': 'r'}
                    completion_path.write_text(json.dumps(bad))
                    (completion_path.parent / 'summary.md').write_text('Implemented VALUE; request independent checks.\n')
                elif MODE in ('symlink-delivery', 'malformed-blocked', 'misplaced-delivery') or integrating:
                    if MODE == 'malformed-blocked':
                        task.update(status='blocked', extra='must not repair')
                    completion_path.write_text(json.dumps(task))
                    summary = completion_path.parent / 'summary.md'
                    if MODE == 'symlink-delivery':
                        summary.symlink_to(Path.cwd() / 'handoff.md')
                    else:
                        summary.write_text('Implemented VALUE. Checks run by controller.\n')
                else:
                    bound_command = shlex.split(re.search(r'^Bound submission command: (.+)$', prompt, re.M)[1])
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.md') as summary:
                        summary.write('Implemented VALUE. Controller must run checks and independent review.\n')
                        summary.flush()
                        subprocess.run([*bound_command, '--summary', summary.name, '--status',
                                        'blocked' if MODE == 'blocked' else 'awaiting_review'],
                                       check=True, capture_output=True)
            if MODE == 'proxy-routing':
                assert all(k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy') for k in os.environ)
                assert os.environ['NO_PROXY'] == os.environ['no_proxy'] == '*'
            # Test JSONL U+2028 framing. This is legal inside a string, not a new record.
            truncated = MODE == 'truncated' or (MODE == 'repair-truncated' and repairing)
            emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'length' if truncated else 'stop', 'text': 'a\u2028b'}})
            emit({'type': 'agent_end'})
            emit({'type': 'agent_settled'})
elif 'queue' in sys.argv:
    if MODE == 'notify-fail':
        sys.exit(7)
    print('queued')
else:
    if MODE == 'proxy-routing':
        assert os.environ['https_proxy'] == 'http://codex-proxy.invalid:8888'
    prompt = sys.argv[-1]
    task_id = re.search(r'reviewer for task (.+?)\.', prompt)[1]
    fingerprint = re.search(r'Exact submission digest: (\w+)', prompt)[1]
    result_path = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
    if MODE in ('codex-unavailable', 'repair-unavailable'):
        emit({'type': 'turn.failed', 'error': {'message': 'fixture: usage limit'}})
        sys.exit(9)
    first = result_path.parent.parent.name == 'attempt-0001'
    needs = MODE == 'no-progress' or (MODE in ('rework', 'gated-rework') and first)
    result = {'task_id': task_id, 'submission_digest': fingerprint, 'verdict': 'needs_changes' if needs else 'accepted',
              'summary': 'Independent fixture review', 'issues': []}
    if needs:
        result['issues'] = [{'id': 'R1', 'priority': 'P2', 'path': 'product.py', 'description': 'Need a boundary case',
                             'required_change': 'Add the boundary case', 'validation': 'Run unit'}]
    if MODE == 'stale-verdict':
        result['submission_digest'] = 'wrong'
    if MODE == 'mutate-review':
        Path('product.py').write_text('VALUE = -1\n')
    result_path.write_text(json.dumps(result))
    emit({'type': 'thread.started', 'thread_id': 'fake-review'})
    if MODE == 'codex-reconnect':
        emit({'type': 'error', 'message': 'Reconnecting... 2/5 (request timed out)'})
    emit([] if MODE == 'bad-review-shape' else {'type': 'turn.failed' if MODE == 'codex-turn-failure' else 'turn.completed',
                                              'usage': {'input_tokens': 1, 'output_tokens': 1}})
    if MODE == 'codex-exit-failure':
        sys.exit(9)
