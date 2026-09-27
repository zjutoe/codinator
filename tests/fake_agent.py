#!/usr/bin/env python3
"""Deterministic subprocess fixture. No network or real model invocation."""
import json
import os
from pathlib import Path
import re
import sys
import time

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
            completion_path = Path(re.search(r'Write (.+/completion.json) as', prompt)[1])
            body = re.search(r'as (\{"task_id".*?\})\.', prompt, re.S)[1]
            task = json.loads(body)
            if MODE == 'sleep':
                time.sleep(60)
            Path('product.py').write_text('VALUE = 42\n')
            if MODE == 'scope':
                Path('forbidden').write_text('bad')
            if MODE != 'missing-delivery':
                completion_path.write_text(json.dumps([] if MODE == 'bad-completion-shape' else task))
                (completion_path.parent / 'summary.md').write_text('Implemented VALUE. Checks run by controller.\n')
            if MODE == 'proxy-routing':
                assert all(k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy') for k in os.environ)
                assert os.environ['NO_PROXY'] == os.environ['no_proxy'] == '*'
            # Test JSONL U+2028 framing. This is legal inside a string, not a new record.
            emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'length' if MODE == 'truncated' else 'stop', 'text': 'a\u2028b'}})
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
    if MODE == 'codex-unavailable':
        emit({'type': 'turn.failed', 'error': {'message': 'fixture: usage limit'}})
        sys.exit(9)
    first = result_path.parent.parent.name == 'attempt-0001'
    needs = MODE == 'no-progress' or (MODE == 'rework' and first)
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
