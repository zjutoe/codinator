#!/usr/bin/env python3
"""Local agent fixture: agents own Git/checks, never invoke a model."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

MODE = os.environ.get('FAKE_MODE', 'accept')

DOCUMENT_SECTIONS = {
    'summary': ('Status', 'Completed', 'Incomplete', 'Attempts and results',
                'Artifacts and evidence', 'Deviations and unknowns', 'Next step'),
    'review': ('Subject', 'Verdict', 'Basis and verification', 'Rework', 'Stop reason'),
    'guidance': ('Question', 'Advice', 'Basis', 'Unknowns', 'Next boundary'),
}
DOCUMENT_SECTIONS['help'] = (*DOCUMENT_SECTIONS['summary'], 'Blocker', 'Question for Codex')

def document(kind, detail):
    return '\n\n'.join(f'## {heading}\n{detail}' for heading in DOCUMENT_SECTIONS[kind]) + '\n'

def bound_message(schema):
    return {key: field['enum'][0] for key, field in schema['properties']['message']['properties'].items()}

def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)

def git(*args):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'commit.gpgsign=false', *args], text=True, stderr=subprocess.PIPE).strip()

def check_records(contract, commit, role):
    records = []
    for check in contract['checks']:
        run = subprocess.run(check['argv'], capture_output=True, text=True,
                             timeout=check.get('timeout_seconds', 120))
        evidence = f'{role}: commit={commit}; exit={run.returncode}; stdout={run.stdout!r}; stderr={run.stderr!r}'
        emit({'type': 'fixture_check', 'role': role, 'commit': commit,
              'name': check['name'], 'exit_code': run.returncode, 'evidence': evidence})
        records.append({'name': check['name'], 'argv': check['argv'], 'commit': commit,
                        'status': 'passed' if run.returncode == 0 else 'failed',
                        'exit_code': run.returncode, 'evidence': evidence})
    return records

def implement(contract):
    assert contract['protocol'] == 'agent_git_v1'
    assert git('branch', '--show-current') == contract['git']['branch']
    assert git('rev-parse', 'HEAD') == contract['git']['base_commit']
    assert not git('status', '--porcelain'), 'Pi baseline must be clean'
    emit({'type': 'fixture_refs', 'argv': ['git', 'show-ref'], 'stdout': git('show-ref')})
    value = -1 if MODE in ('bad-product', 'lie-checks') else 42
    Path('product.py').write_text(f'VALUE = {value}\n# attempt {contract["attempt"]}\n')
    if MODE == 'scope':
        Path('forbidden').write_text('out-of-scope agent change\n')
    if MODE == 'uncommitted':
        return None
    git('add', '-A')
    git('commit', '-qm', f'Implement fixture attempt {contract["attempt"]}')
    head = git('rev-parse', 'HEAD')
    emit({'type': 'fixture_commit', 'role': 'pi', 'commit': head})
    if MODE == 'move-other-ref':
        git('update-ref', 'refs/heads/main', head)
    checks = check_records(contract, head, 'pi')
    if MODE == 'lie-checks':
        for check in checks:
            check.update(status='passed', exit_code=0, evidence='fabricated success claim')
    claim = 'f' * 40 if MODE == 'forged-sha' else head
    for check in checks:
        check['commit'] = claim
    return {'version': 1, **{k: contract[k] for k in ('task_id', 'round', 'attempt')},
            'git': {**contract['git'], 'commit': claim}, 'checks': checks}

def request_help(contract):
    assert git('branch', '--show-current') == contract['git']['branch']
    assert git('rev-parse', 'HEAD') == contract['git']['base_commit']
    assert not git('status', '--porcelain'), 'Partial candidate must start clean'
    emit({'type': 'fixture_refs', 'argv': ['git', 'show-ref'], 'stdout': git('show-ref')})
    head = git('rev-parse', 'HEAD')
    claim = 'f' * 40 if MODE == 'guidance-candidate-mismatch' else head
    if MODE == 'guidance-dirty':
        Path('product.py').write_text('VALUE = 0\n')
    checks = [{'name': check['name'], 'argv': check['argv'], 'commit': claim,
               'status': 'not_run', 'exit_code': None, 'evidence': '未执行: help before implementation'}
              for check in contract['checks']]
    return {'version': 1, **{k: contract[k] for k in ('task_id', 'round', 'attempt')},
            'git': {**contract['git'], 'commit': claim}, 'checks': checks}

def pi(prompt):
    context = Path(re.search(r'^Delivery contract: (.+)$', prompt, re.M)[1]).parent
    contract = json.loads((context / 'delivery-contract.json').read_text())
    delivery = Path(contract['delivery_dir'])
    completion = delivery / 'completion.json'
    repairing = 'repairing ONLY the delivery protocol' in prompt
    help_requested = ('handoff' in contract and MODE.startswith('guidance')
                      and (contract['round'] == 1 or MODE in ('guidance-repeat', 'guidance-replayed-reply')))
    if MODE == 'sleep':
        time.sleep(60)
    if MODE == 'gated-rework':
        while not Path(os.environ['FAKE_GATE']).exists():
            time.sleep(.05)
    if repairing:
        # Reformat preserved agent evidence only; never Git/check again.
        packet = json.loads((context.parent / 'delivery/agent-evidence.json').read_text())
    else:
        handoff = Path(re.search(r'Read the frozen published handoff at (.+?) and applicable AGENTS.md', prompt)[1])
        assert 'Implement product.py' in handoff.read_text()
        packet = request_help(contract) if help_requested else implement(contract)
        if packet is not None:
            (delivery / 'agent-evidence.json').write_text(json.dumps(packet))
    if repairing and MODE == 'repair-mutation':
        Path('product.py').write_text('VALUE = -1\n')
    missing = MODE == 'missing-delivery' or (MODE == 'missing-then-repair' and not repairing)
    malformed = MODE == 'bad-completion-shape' or (
        MODE in ('malformed-then-repair', 'repair-mutation', 'repair-truncated', 'repair-unavailable') and not repairing)
    task = {k: contract[k] for k in ('task_id', 'round', 'attempt')}
    task['status'] = 'awaiting_review'
    if MODE in ('misplaced-delivery', 'misplaced-then-repair') and not repairing:
        Path('delivery').mkdir(exist_ok=True)
        completion = Path('delivery/completion.json')
    if not missing:
        if MODE == 'deep-completion':
            completion.write_text('[' * 10000 + '0' + ']' * 10000)
            (completion.parent / 'summary.md').write_text('Fixture delivery.\n')
        elif malformed:
            bad = [] if MODE == 'bad-completion-shape' else {
                'task_id': 'research-id', 'round': 2, 'attempt': 1, 'status': 'completed', 'run_id': 'r'}
            completion.write_text(json.dumps(bad))
            (completion.parent / 'summary.md').write_text('Pi committed and ran checks; evidence retained.\n')
        elif MODE in ('symlink-delivery', 'malformed-blocked', 'misplaced-delivery', 'misplaced-then-repair') and not repairing:
            if MODE == 'malformed-blocked':
                task.update(status='blocked', extra='must not repair')
            completion.write_text(json.dumps(task))
            summary = completion.parent / 'summary.md'
            if MODE == 'symlink-delivery':
                summary.symlink_to(Path.cwd() / 'handoff.md')
            else:
                summary.write_text('Fixture delivery.\n')
        else:
            command = shlex.split(re.search(r'^Bound submission command: (.+)$', prompt, re.M)[1])
            with tempfile.TemporaryDirectory() as scratch:
                summary = Path(scratch) / 'summary.md'
                evidence = Path(scratch) / 'packet.json'
                detail = ('No implementation yet; checks 未执行. Please explain how to implement VALUE=42.'
                          if help_requested else 'Pi committed before tests. Codex must independently verify all claims.')
                summary.write_text(document('help' if help_requested else 'summary', detail)
                                   if 'handoff' in contract else detail + '\n')
                args = [*command, '--summary', str(summary), '--status',
                        'blocked' if MODE in ('blocked', 'uncommitted') else
                        'needs_guidance' if help_requested else 'awaiting_review']
                if packet is not None:
                    evidence.write_text(json.dumps(packet))
                    args += ['--evidence', str(evidence)]
                result = subprocess.run(args, capture_output=True, text=True)
                if result.returncode:
                    sys.stderr.write(result.stdout + result.stderr)
                    raise SystemExit(result.returncode)
    if MODE == 'proxy-routing':
        assert all(k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy') for k in os.environ)
        assert os.environ['NO_PROXY'] == os.environ['no_proxy'] == '*'
    truncated = MODE == 'truncated' or (MODE == 'repair-truncated' and repairing)
    emit({'type': 'message_end', 'message': {'role': 'assistant',
          'stopReason': 'length' if truncated else 'stop', 'text': 'a\u2028b'}})
    emit({'type': 'agent_end'})
    emit({'type': 'agent_settled'})

def guide(schema):
    prompt = sys.argv[-1]
    result_path = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
    task_id = re.search(r'guide for task (.+?)\.', prompt)[1]
    fingerprint = re.search(r'Exact declared candidate: (\w+)', prompt)[1]
    attempt = result_path.parent.parent
    contract = json.loads((attempt / 'delivery-contract.json').read_text())
    manifest = json.loads((attempt.parent / 'manifest.json').read_text())
    before_refs = git('show-ref')
    before_head = git('rev-parse', 'HEAD')
    problems = []
    if before_head != fingerprint:
        problems.append('Candidate SHA does not match actual Git HEAD')
    if git('branch', '--show-current') != contract['git']['branch']:
        problems.append('Wrong Git branch')
    if git('status', '--porcelain'):
        problems.append('Workspace is not clean')
    if not problems:
        try:
            for base in (manifest['git']['base_commit'], contract['git']['base_commit']):
                git('merge-base', '--is-ancestor', base, fingerprint)
            for commit in git('rev-list', contract['git']['base_commit'] + '..' + fingerprint).splitlines():
                for path in git('diff-tree', '--no-commit-id', '--name-only', '-r', commit).splitlines():
                    if not any(path == allowed.rstrip('/') or (allowed.endswith('/') and path.startswith(allowed))
                               for allowed in manifest['allowed_paths']):
                        problems.append('Changed path outside allowed scope: ' + path)
            frozen = (attempt.parent / 'handoff.md').read_bytes()
            tracked = subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null',
                '-c', 'core.fsmonitor=false', 'show', manifest['git']['base_commit'] + ':' + manifest['handoff']])
            if frozen != tracked:
                problems.append('Frozen handoff differs from tracked original')
            if git('diff', '--name-only', manifest['git']['base_commit'], fingerprint, '--',
                   manifest['handoff'], '.gitignore'):
                problems.append('Frozen handoff or ignore rules changed')
        except subprocess.SubprocessError as exc:
            problems.append('Independent candidate verification failed: ' + str(exc))
    if MODE == 'guidance-blocked':
        problems.append('External fixture input required')
    if git('rev-parse', 'HEAD') != before_head or git('show-ref') != before_refs:
        problems.append('Source changed during guidance')
    detail = '; '.join(problems) or 'Verified clean candidate; implement product.py VALUE=42 and commit before checks.'
    emit({'type': 'fixture_guide_verification', 'commit': before_head,
          'clean': not git('status', '--porcelain'), 'result': 'blocked' if problems else 'continue'})
    result = {'task_id': task_id, 'submission_digest': fingerprint,
              'result': 'blocked' if problems else 'continue',
              'summary': document('guidance', detail), 'message': bound_message(schema)}
    if MODE == 'guidance-replayed-reply' and contract['round'] > 1:
        result = json.loads((attempt.parent / 'attempt-0001/guidance-delivery/guidance.json').read_text())
    if MODE == 'guidance-stale-sha':
        result['submission_digest'] = '0' * 40
    if MODE == 'guidance-wrong-task':
        result['task_id'] = 'different-task'
    if MODE == 'guidance-wrong-result':
        result['result'] = 'accepted'
    if MODE == 'guidance-missing-message':
        del result['message']
    if MODE == 'guidance-invalid-message':
        result['message'] = None
    if MODE == 'guidance-missing-section':
        result['summary'] = result['summary'].replace('## Unknowns', '## Other')
    for mode, field, value in (
        ('guidance-stale-attempt', 'attempt', 0), ('guidance-stale-round', 'round', 0),
        ('guidance-wrong-contract', 'contract_digest', '0' * 64),
        ('guidance-wrong-reply', 'reply_to', 'unrelated-request'),
        ('guidance-wrong-author', 'author', 'pi'),
        ('guidance-wrong-kind', 'kind', 'review')):
        if MODE == mode:
            result['message'][field] = value
    if MODE != 'guidance-absent-output':
        result_path.write_text(json.dumps(result))
    emit({'type': 'thread.started', 'thread_id': 'fake-guide'})
    if MODE != 'guidance-no-completion':
        if MODE in ('guidance-duplicate-event', 'guidance-identical-duplicate-event'):
            first = 'turn.failed' if MODE == 'guidance-duplicate-event' else 'turn.completed'
            print('{"type":"' + first + '","type":"turn.completed"}', flush=True)
        else:
            emit({'type': 'turn.failed' if MODE == 'guidance-turn-failed' else 'turn.completed'})
    if MODE == 'guidance-nonzero':
        raise SystemExit(9)

def review():
    prompt = sys.argv[-1]
    schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
    if 'result' in schema['properties']:
        return guide(schema)
    task_id = re.search(r'reviewer for task (.+?)\.', prompt)[1]
    fingerprint = re.search(r'Exact submission (?:digest|commit): (\w+)', prompt)[1]
    result_path = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
    if MODE in ('codex-unavailable', 'repair-unavailable'):
        emit({'type': 'turn.failed', 'error': {'message': 'fixture: unavailable'}})
        raise SystemExit(9)
    current = result_path.parent.parent
    source = current
    if (current / 'review-source.json').exists():
        pin = json.loads((current / 'review-source.json').read_text())
        source = current.parent / f'attempt-{pin["source_attempt"]:04d}'
    contract = json.loads((source / 'delivery-contract.json').read_text())
    manifest = json.loads((source.parent / 'manifest.json').read_text())
    submitted = json.loads((source / 'submission.json').read_text())
    selection = json.loads((source / 'delivery-selection.json').read_text())
    packet = json.loads((source / selection['directory'] / 'evidence.json').read_text())
    issues = []
    def reject(detail):
        issues.append({'id': f'relay-identity-{len(issues) + 1}', 'priority': 'P1', 'path': 'product.py',
                       'description': detail, 'required_change': 'Repair the agent-owned Git/check evidence',
                       'validation': 'Verify SHA, clean branch, scope and rerun published checks'})
    try:
        if MODE == 'mutate-review':
            Path('product.py').write_text('VALUE = -1\n')
        if (git('rev-parse', 'HEAD') != fingerprint or submitted['commit'] != fingerprint
                or packet['git']['commit'] != fingerprint):
            reject('Candidate SHA does not match actual Git HEAD')
        if manifest.get('implementation') == 'external':
            refs_event = {'stdout': Path(json.loads((source / 'external-completion.json').read_text())['artifacts']['before_git']['path']).read_text()}
        else:
            pi_events = [json.loads(line) for line in (source / 'pi/stdout.jsonl').read_text().split('\n') if line]
            refs_event = next((e for e in pi_events if e.get('type') == 'fixture_refs'), None)
        def non_task_refs(output):
            refs = dict(line.split(' ', 1)[::-1] for line in output.splitlines())
            refs.pop('refs/heads/' + contract['git']['branch'], None)
            return refs
        if refs_event is None or non_task_refs(refs_event['stdout']) != non_task_refs(git('show-ref')):
            reject('Non-task Git refs changed or initial git show-ref evidence missing')
        if git('branch', '--show-current') != contract['git']['branch']:
            reject('Wrong Git branch')
        if git('status', '--porcelain'):
            reject('Workspace is not clean')
        git('merge-base', '--is-ancestor', contract['git']['base_commit'], fingerprint)
        paths = git('diff', '--name-only', contract['git']['base_commit'], fingerprint).splitlines()
        for path in paths:
            if not any(path == p.rstrip('/') or (p.endswith('/') and path.startswith(p))
                       for p in manifest['allowed_paths']):
                reject('Changed path outside allowed scope: ' + path)
        checked = check_records(contract, fingerprint, 'codex')
        if any(c['status'] != 'passed' for c in checked):
            reject('Independent published check failed')
        if any(c['status'] != 'passed' for c in packet['checks']):
            reject('Pi recorded failed or unrun checks')
        if git('rev-parse', 'HEAD') != fingerprint or git('status', '--porcelain'):
            reject('Review changed the frozen candidate')
    except (AssertionError, OSError, subprocess.SubprocessError) as exc:
        reject(f'Independent Git/check verification failed: {exc}')
    first = current.name == 'attempt-0001'
    needs = MODE == 'no-progress' or (MODE in ('rework', 'gated-rework') and first)
    if needs:
        issues.append({'id': 'R1', 'priority': 'P2', 'path': 'product.py',
                       'description': 'Need a boundary case', 'required_change': 'Add the boundary case',
                       'validation': 'Run the published check'})
    verdict = 'blocked' if any(i['id'].startswith('relay-identity-') for i in issues) else 'needs_changes' if needs else 'accepted'
    result = {'task_id': task_id, 'submission_digest': fingerprint, 'verdict': verdict,
              'summary': 'Independent fixture review: ' + ('; '.join(i['description'] for i in issues) or 'Git and checks verified'),
              'issues': issues}
    if 'message' in schema['properties']:
        result['message'] = bound_message(schema)
        result['summary'] = document('review', result['summary'])
    if MODE == 'stale-verdict':
        result['submission_digest'] = '0' * 40
    if MODE == 'proxy-routing':
        assert os.environ['https_proxy'] == 'http://codex-proxy.invalid:8888'
    result_path.write_text(json.dumps(result))
    emit({'type': 'thread.started', 'thread_id': 'fake-review'})
    if MODE == 'codex-reconnect':
        emit({'type': 'error', 'message': 'Reconnecting... 2/5 (request timed out)'})
    emit([] if MODE == 'bad-review-shape' else {
        'type': 'turn.failed' if MODE == 'codex-turn-failure' else 'turn.completed',
        'usage': {'input_tokens': 1, 'output_tokens': 1}})
    if MODE == 'codex-exit-failure':
        raise SystemExit(9)

if '--mode' in sys.argv:
    for line in sys.stdin.buffer:
        command = json.loads(line)
        if command['type'] == 'get_state':
            emit({'type': 'response', 'id': 'state', 'success': True, 'data': {
                'model': {'provider': 'bonsai', 'id': 'bonsai2-27b'}, 'thinkingLevel': 'xhigh',
                'isStreaming': False, 'pendingMessageCount': 0, 'sessionId': 'fake'}})
        elif command['type'] == 'prompt':
            emit({'type': 'response', 'id': 'prompt', 'success': True})
            emit({'type': 'agent_start'})
            # Real Pi receives both the appended system instructions and RPC task message.
            instructions = sys.argv[sys.argv.index('--append-system-prompt') + 1]
            pi(instructions + '\n' + command['message'])
elif 'queue' in sys.argv:
    if MODE == 'notify-fail':
        raise SystemExit(7)
    print('queued')
else:
    review()
