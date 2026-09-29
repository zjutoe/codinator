"""Pi commits in a private clone; only a verified fast-forward reaches the target."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .agents import integrator, validate_verdict
from .files import Problem, changes, digest, file_info, snapshot, under, write_json
from .review_resume import checkpoint
from .process import Interrupted, run_process
from .store import lock


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return env | {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                  'GIT_ATTR_NOSYSTEM': '1', 'GIT_NO_REPLACE_OBJECTS': '1',
                  'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': 'file'}


def git_argv(root, *args):
    return ['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null',
            '-c', 'commit.gpgSign=false', '-c', 'core.fsmonitor=false',
            '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', *args]


def git(root, *args, data=None, index=None, sandbox=None):
    env = git_env()
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)
    argv = git_argv(root, *args)
    if sandbox is not None:
        argv = sandbox.wrap(argv, root, readonly=[root])
    p = subprocess.run(argv, input=data, capture_output=True, env=env, timeout=60)
    if p.returncode:
        raise Problem('Git integration failed: ' + p.stderr.decode(errors='replace').strip())
    return p.stdout


def text(root, *args, **kw):
    return git(root, *args, **kw).decode().strip()


def config_boundary(root, sandbox=None):
    # Reject executable conversion/filter configuration before asking Git to diff.
    argv = git_argv(root, 'config', '--local', '--name-only', '--get-regexp',
        r'^(filter\.|include\.|includeif\.|core\.attributesfile|core\.worktree|extensions\.)')
    if sandbox is not None:
        argv = sandbox.wrap(argv, root, readonly=[root])
    p = subprocess.run(argv,
        capture_output=True, env=git_env(), timeout=10)
    if p.returncode not in (0, 1) or p.stdout:
        raise Problem('Integration does not support local filters/includes/worktree overrides')


def clean_index(root):
    if git(root, 'diff', '--cached', '--name-only', '-z'):
        raise Problem('Integration requires an unstaged Git index')


def common_dir(root):
    return Path(text(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()


def planning_only(root, snap, spec, excludes):
    clean_index(root)
    tracked = set(git(root, 'ls-files', '-z').decode().split('\0')) - {''}
    dirty = set(git(root, 'diff', '--name-only', '-z').decode().split('\0')) - {''}
    dirty |= {p for p, info in snap['files'].items()
              if info['kind'] not in ('directory', 'excluded') and p not in tracked}
    if any(not under(p, spec['planning_paths']) and not under(p, excludes) for p in dirty):
        raise Problem('Uncommitted baseline changes outside declared planning paths')


def publication_preflight(manifest):
    spec = manifest.get('integration')
    if not spec:
        return
    source, target = Path(manifest['workspace']), Path(spec['target_workspace'])
    for root in (source, target):
        config_boundary(root)
        if Path(text(root, 'rev-parse', '--show-toplevel')).resolve() != root:
            raise Problem('Integration requires checkout roots')
        if text(root, 'rev-parse', 'HEAD') != spec['base_commit']:
            raise Problem('Integration source/target must match the pinned base commit')
        planning_only(root, snapshot(root, manifest['excludes']), spec, manifest['excludes'])
    if text(target, 'branch', '--show-current') != spec['target_branch']:
        raise Problem('Integration target branch mismatch')
    if common_dir(source) != common_dir(target):
        raise Problem('Integration source and target must be worktrees of the same repository')
    git(target, 'check-ref-format', '--branch', spec['target_branch'])


def accepted_checkpoint(store, task, sandbox):
    original, source = checkpoint(store, task, sandbox, accepted=True)
    review = store.root / 'tasks' / task['id'] / f"attempt-{task['attempt']:04d}"
    pinned = {str(source / p): info for p, info in original['files'].items()}
    def read(name):
        path = review / name
        if not path.is_file() or path.resolve() != path.absolute():
            raise Problem('Missing or linked accepted review evidence')
        pinned[str(path)] = file_info(path)
        return path
    intake = review.parent / 'intake.json'
    if intake.resolve() != intake.absolute() or not intake.is_file():
        raise Problem('Missing or linked published intake evidence')
    event = store.db.execute("SELECT payload FROM events WHERE task_id=? AND kind='submitted' ORDER BY seq LIMIT 1",
                             (task['id'],)).fetchone()
    if event is None or digest(json.loads(intake.read_text())) != json.loads(event[0])['digest']:
        raise Problem('Published intake evidence changed')
    pinned[str(intake)] = file_info(intake)
    outcome = validate_verdict(json.loads(read('outcome.json').read_text()), task['id'], task['expected_digest'])
    if outcome['verdict'] != 'accepted':
        raise Problem('Integration requires an independent accepted review')
    if json.loads(read('review-delivery/verdict.json').read_text()) != outcome:
        raise Problem('Accepted outcome differs from the reviewer artifact')
    result = json.loads(read('codex/result.json').read_text())
    streams = {s: file_info(read('codex/' + s)) for s in ('stdout.txt', 'stderr.txt')}
    if result.get('exit_code') != 0 or result.get('failure') is not None or result.get('streams') != streams:
        raise Problem('Accepted review process evidence is incomplete')
    events = [json.loads(line) for line in (review / 'codex/stdout.txt').read_bytes().splitlines() if line.strip()]
    if not any(e.get('type') == 'turn.completed' for e in events) or any(e.get('type') == 'turn.failed' for e in events):
        raise Problem('Accepted review has no successful terminal event')
    argv = json.loads(read('codex/launch.json').read_text())['argv']
    if '-m' not in argv or argv[argv.index('-m') + 1] != 'gpt-6-astra' or 'model_reasoning_effort="xhigh"' not in argv:
        raise Problem('Accepted review used an unexpected model')
    pin = {'review_attempt': task['attempt'], 'source_attempt': original['source_attempt'],
           'submission_digest': task['expected_digest'], 'evidence_digest': digest(pinned)}
    if task.get('integration') and task['integration']['acceptance'] != pin:
        raise Problem('Accepted integration checkpoint evidence changed')
    return pin, source


def reviewed_files(store, task, source):
    submitted = json.loads((source / 'submission.json').read_text())
    intake = json.loads((source.parent / 'intake.json').read_text())
    entries = {}
    for name in changes(intake, submitted):
        before, after = intake['files'].get(name), submitted['files'].get(name)
        if (before or after)['kind'] == 'directory':
            if before and before != after:
                raise Problem('Integration cannot change existing directory metadata')
            continue
        if not under(name, task['manifest']['allowed_paths']):
            raise Problem('Integration diff is outside the accepted allowlist')
        if any(v is not None and v['kind'] != 'file' for v in (before, after)):
            raise Problem('Integration only supports regular file changes')
        entries[name] = after
    if not entries:
        raise Problem('No accepted file changes to integrate')
    return entries


def target_preflight(task, entries, *, commit=None):
    spec = task['manifest']['integration']
    root = Path(spec['target_workspace'])
    config_boundary(root)
    if text(root, 'branch', '--show-current') != spec['target_branch']:
        raise Problem('Integration target branch changed')
    head = text(root, 'rev-parse', 'HEAD')
    if head not in (spec['base_commit'], commit):
        raise Problem('Integration target HEAD changed; new review/base is required')
    clean_index(root)
    tracked_dirty = set(git(root, 'diff', '--name-only', '-z').decode().split('\0')) - {''}
    untracked = set(git(root, 'ls-files', '--others', '-z').decode().split('\0')) - {''}
    for changed in entries:
        if any(p == changed or p.startswith(changed + '/') or changed.startswith(p + '/')
               for p in tracked_dirty | untracked):
            raise Problem('Integration overlaps uncommitted target changes: ' + changed)
        attr = git(root, 'check-attr', '-z', 'filter', 'text', 'eol', 'working-tree-encoding', '--', changed).decode().split('\0')
        if any(value not in ('unspecified', 'unset') for value in attr[2::3]):
            raise Problem('Integration does not support checkout conversions: ' + changed)
    return snapshot(root, task['manifest']['excludes'])


def prepare_clone(store, task, attempt, entries):
    spec = task['manifest']['integration']
    clone = attempt / 'repository'
    # Independent objects: no alternates or hardlinks into the original repository.
    git(spec['target_workspace'], 'clone', '--no-local', '--no-checkout', '--single-branch',
        '--branch', spec['target_branch'], spec['target_workspace'], str(clone))
    git(clone, 'checkout', '-b', 'codinator-integration', spec['base_commit'])
    git(clone, 'config', 'user.name', 'Pi + Bonsai')
    git(clone, 'config', 'user.email', 'pi-bonsai@codinator.local')
    git(clone, 'config', 'core.hooksPath', '/dev/null')
    git(clone, 'config', 'commit.gpgSign', 'false')
    expected_index = attempt / 'expected-index'
    git(clone, 'read-tree', spec['base_commit'], index=expected_index)
    for name, info in entries.items():
        dest = clone / name
        if info is None:
            dest.unlink()
            git(clone, 'update-index', '--force-remove', '--', name, index=expected_index)
        else:
            blob = store.root / 'blobs' / info['sha256']
            data = blob.read_bytes()
            if hashlib.sha256(data).hexdigest() != info['sha256']:
                raise Problem('Accepted content blob changed')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(info['mode'])
            oid = git(clone, 'hash-object', '-w', '--stdin', data=data).decode().strip()
            mode = '100755' if info['mode'] & 0o111 else '100644'
            git(clone, 'update-index', '--add', '--cacheinfo', mode, oid, name, index=expected_index)
    tree = git(clone, 'write-tree', index=expected_index).decode().strip()
    write_json(attempt / 'expected.json', {'base_commit': spec['base_commit'], 'tree': tree, 'entries': entries})
    return clone, tree


def verify_clone(task, attempt, sandbox):
    clone = attempt / 'repository'
    expected = json.loads((attempt / 'expected.json').read_text())
    spec = task['manifest']['integration']
    config_boundary(clone, sandbox)
    def read(*args):
        return text(clone, *args, sandbox=sandbox)
    if read('branch', '--show-current') != spec['target_branch']:
        raise Problem('Pi did not finish on the integration target branch')
    commit = read('rev-parse', 'HEAD')
    if read('rev-list', '--parents', '-n', '1', commit).split() != [commit, expected['base_commit']]:
        raise Problem('Pi must create exactly one commit on the pinned base')
    if read('rev-parse', commit + '^{tree}') != expected['tree']:
        raise Problem('Pi commit tree differs from the accepted file bytes/modes')
    if read('status', '--porcelain', '--untracked-files=all'):
        raise Problem('Pi integration clone is not clean')
    if not read('show', '-s', '--format=%b', commit):
        raise Problem('Pi integration commit requires a descriptive body')
    return commit, expected['tree']


def candidate_digest(attempt):
    names = ['expected.json', 'pi-runtime.json', 'delivery/summary.md', 'delivery/completion.json']
    paths = [attempt / name for name in names]
    paths += sorted((attempt / 'pi').iterdir())
    paths += sorted(p for p in (attempt / 'export').rglob('*') if not p.is_dir())
    values = {}
    for path in paths:
        if path.resolve() != path.absolute() or not path.is_file():
            raise Problem('Invalid integration evidence file')
        values[str(path.relative_to(attempt))] = file_info(path)
    return digest(values)


def verify_target(task, before, entries, commit):
    after = target_preflight(task, entries, commit=commit)
    if after['git']['head'] != commit:
        raise Problem('Integration target did not reach the verified commit')
    if after['root_mode'] != before['root_mode']:
        raise Problem('Integration changed target checkout permissions')
    for name in changes(before, after):
        old, new = before['files'].get(name), after['files'].get(name)
        if name in entries:
            continue
        if ((old is None or new is None) and (old or new)['kind'] == 'directory'
                and any(p.startswith(name + '/') for p in entries)):
            continue
        raise Problem('Integration changed an unrelated target path: ' + name)
    for name, expected in entries.items():
        actual = after['files'].get(name)
        if expected is None:
            if actual is not None:
                raise Problem('Integration did not apply accepted deletion')
        elif (actual is None or actual.get('kind') != 'file'
              or actual['sha256'] != expected['sha256']
              or bool(actual['mode'] & 0o111) != bool(expected['mode'] & 0o111)):
            raise Problem('Integrated target differs from accepted bytes/mode: ' + name)
    return after


def promote(engine, task, entries):
    store = engine.store
    if time.time() >= task['deadline']:
        raise Problem('Task wall-clock budget exhausted before integration publication')
    info = task['integration']
    candidate = info['candidate']
    parent = store.root / 'tasks' / task['id']
    attempt = parent / f"integration-{candidate['attempt']:04d}"
    bundle = attempt / 'export/candidate.bundle'
    if (candidate_digest(attempt) != candidate['evidence_digest']
            or file_info(bundle) != candidate['bundle']):
        raise Problem('Completed Pi integration evidence changed')
    commit, tree = verify_clone(task, attempt, engine.sandbox)
    if (commit, tree) != (candidate['commit'], candidate['tree']):
        raise Problem('Completed Pi integration clone changed')
    spec = task['manifest']['integration']
    target = Path(spec['target_workspace'])
    before = target_preflight(task, entries, commit=commit)
    intents = sorted(attempt.glob('promotion-*/intent.json'))
    # A crash after merge is reconciled from its durable intent, not replayed.
    if before['git']['head'] == commit:
        if not intents:
            raise Problem('Target already moved without a controller integration intent')
        intent = json.loads(intents[-1].read_text())
        verify_target(task, intent['target_before'], entries, commit)
        return commit
    transaction = attempt / f"promotion-{len(intents) + 1:04d}"
    write_json(transaction / 'intent.json', {'commit': commit, 'target_before': before})
    git(target, 'bundle', 'verify', str(bundle))
    heads = text(target, 'bundle', 'list-heads', str(bundle)).splitlines()
    if heads != [commit + ' refs/heads/' + spec['target_branch']]:
        raise Problem('Exported bundle advertises an unexpected commit')
    # Bundle transport reads a pack; it never executes the clone's upload-pack/config.
    git(target, '-c', 'fetch.fsckObjects=true', '-c', 'transfer.fsckObjects=true',
        'fetch', '--no-write-fetch-head', '--no-tags', '--no-recurse-submodules',
        str(bundle), 'refs/heads/' + spec['target_branch'])
    if text(target, 'rev-parse', commit + '^{tree}') != tree:
        raise Problem('Imported commit tree mismatch')
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        current = store.get(task['id'])
        if current['control'] or current['state'] != 'integrating':
            raise Interrupted('Pause/cancel requested before integration publication')
        if time.time() >= current['deadline']:
            raise Problem('Task wall-clock budget exhausted before integration publication')
        if snapshot(target, task['manifest']['excludes']) != before:
            raise Problem('Integration target changed before publication')
        output = git(target, 'merge', '--ff-only', '--no-edit', '--no-stat', commit)
        write_json(transaction / 'merge.json', {'commit': commit, 'stdout': output.decode()})
        verify_target(task, before, entries, commit)
        # Keep the publication transaction locked through the final state below.
        finished = info | {'status': 'complete', 'merged_commit': commit}
        store.finish(task['id'], f"{task['id']} accepted and integrated as {commit}. Evidence: {attempt}",
                     state='accepted', phase='done', reason='Accepted changes committed by Pi and fast-forwarded to target',
                     integration=finished, control=None)
    return commit


def run(engine, task_id, budget_timeout):
    store = engine.store
    task = store.get(task_id)
    root = Path(task['manifest']['workspace'])
    info = task['integration']
    try:
        if digest(snapshot(root, task['manifest']['excludes'])) != task['expected_digest']:
            raise Problem('Accepted source snapshot changed before integration')
        pin, source = accepted_checkpoint(store, task, engine.sandbox)
        entries = reviewed_files(store, task, source)
        spec = task['manifest']['integration']
        target = Path(spec['target_workspace'])
        target_lock = Path('/tmp') / f"codinator-integration-{os.getuid()}-{digest(str(common_dir(target)))}.lock"
        with lock(target_lock):
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                current = store.get(task_id)
                if current['state'] != 'integration_ready' or current['control']:
                    return
                store.update(task_id, state='integrating', phase='integration', reason='')
            task = store.get(task_id)
            limit = task['attempt_seconds_override'] or task['manifest']['attempt_seconds']
            budget_timeout(task['deadline'], limit)
            if not info.get('candidate'):
                target_preflight(task, entries)
                parent = store.root / 'tasks' / task_id
                number = max([0] + [int(p.name.split('-')[-1]) for p in parent.glob('integration-*') if p.is_dir()]) + 1
                attempt = parent / f'integration-{number:04d}'
                attempt.mkdir()
                private = store.root / 'private' / task_id / attempt.name
                private.mkdir(parents=True, mode=0o700)
                info = info | {'attempt': number, 'status': 'running'}
                store.update(task_id, integration=info)
                task = store.get(task_id)
                clone, _ = prepare_clone(store, task, attempt, entries)
                timeout = budget_timeout(task['deadline'], limit)
                integrator(task, attempt, private, engine.sandbox, engine.options(task_id, timeout),
                           number, entries, engine.pi_bin)
                commit, tree = verify_clone(task, attempt, engine.sandbox)
                export = attempt / 'export'
                export.mkdir()
                argv = git_argv(clone, 'bundle', 'create', str(export / 'candidate.bundle'),
                                'refs/heads/' + spec['target_branch'], '^' + spec['base_commit'])
                # Export under read-only isolation, then import the bundle, never a
                # potentially agent-modified Git repository's transport configuration.
                run_process(engine.sandbox.wrap(argv, clone, writable=[export]), cwd=clone, env=git_env(),
                            out=export / 'process', **engine.options(task_id, budget_timeout(task['deadline'], 60)))
                candidate = {'attempt': number, 'commit': commit, 'tree': tree,
                             'bundle': file_info(export / 'candidate.bundle')}
                candidate['evidence_digest'] = candidate_digest(attempt)
                info = info | {'candidate': candidate, 'status': 'prepared'}
                store.update(task_id, integration=info)
            task = store.get(task_id)
            accepted_checkpoint(store, task, engine.sandbox)
            if digest(snapshot(root, task['manifest']['excludes'])) != task['expected_digest']:
                raise Problem('Accepted source changed during integration')
            commit = promote(engine, task, entries)
            if store.get(task_id)['state'] != 'accepted':
                with store.db:
                    store.db.execute('BEGIN IMMEDIATE')
                    current = store.get(task_id)
                    if current['control']:
                        raise Interrupted('Pause/cancel requested before integration completion')
                    store.finish(task_id, f"{task_id} accepted and integrated as {commit}",
                                 state='accepted', phase='done', reason='Reconciled previously completed integration',
                                 integration=current['integration'] | {'status': 'complete', 'merged_commit': commit},
                                 control=None)
    except (Problem, OSError, ValueError, KeyboardInterrupt, subprocess.SubprocessError) as exc:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            current = store.get(task_id)
            control = current['control']
            state = ('cancelled' if control == 'cancel' or current['state'] == 'cancelled' else
                     'paused' if control == 'pause' or current['state'] == 'paused' or isinstance(exc, KeyboardInterrupt) else
                     'blocked')
            store.finish(task_id, f"{task_id} integration {state}: {exc}", state=state, phase='integration',
                         reason=str(exc), control=None, pid=None, pid_start=None)
        if isinstance(exc, KeyboardInterrupt):
            raise
