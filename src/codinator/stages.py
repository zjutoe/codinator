"""Shared allowance from persisted task/dispatch records; no project operations."""
import json
import re
import time
from .files import Problem
from .config import deadline_timestamp, positive


def validate_stage(spec):
    if (type(spec) is not dict or set(spec) != {'id', 'max_seconds', 'max_rounds'}
            or type(spec['id']) is not str
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', spec['id'])):
        raise Problem('stage requires id, max_seconds and max_rounds')
    for key in ('max_seconds', 'max_rounds'):
        positive(spec[key], 'stage.' + key)


def stage_status(store, manifest):
    spec = manifest.get('stage')
    if spec is None:
        return None
    members = [t for t in store.tasks() if t['manifest'].get('stage', {}).get('id') == spec['id']]
    for task in members:
        m = task['manifest']
        if m['stage'] != spec or m['workspace'] != manifest['workspace']:
            raise Problem('Shared stage identity/authorization/workspace conflicts with published tasks')
    starts = [t['started'] for t in members if t['started'] is not None]
    started = min(starts) if starts else None
    ceilings = [deadline_timestamp(t['manifest']['deadline_utc']) for t in members
                if 'deadline_utc' in t['manifest']]
    if 'deadline_utc' in manifest:
        ceilings.append(deadline_timestamp(manifest['deadline_utc']))
    if started is not None:
        ceilings.append(started + spec['max_seconds'])
    deadline = min(ceilings) if ceilings else None
    used = set()
    for task in members:
        for row in store.db.execute("SELECT payload FROM events WHERE task_id=? AND kind='state'", (task['id'],)):
            event = json.loads(row[0])
            if ((event.get('state'), event.get('phase')) in
                    (('implementing', 'pi'), ('external_preparing', 'external'),
                     ('external_implementing', 'external'))):
                used.add((task['id'], event.get('attempt')))
    return {**spec, 'started': started, 'deadline': deadline, 'rounds_used': len(used),
            'rounds_remaining': max(0, spec['max_rounds'] - len(used)),
            'seconds_remaining': max(0, deadline - time.time()) if deadline is not None else None,
            'members': [{'id': t['id'], 'state': t['state'], 'attempt': t['attempt']} for t in members]}


def require_round(store, manifest):
    stage = stage_status(store, manifest)
    if stage and stage['rounds_remaining'] == 0:
        raise Problem('Shared stage implementation round budget exhausted')


def deadline_for(store, task, now):
    m = task['manifest']
    deadline = task['deadline'] if task['deadline'] is not None else now + m['max_seconds']
    if 'deadline_utc' in m:
        deadline = min(deadline, deadline_timestamp(m['deadline_utc']))
    stage = stage_status(store, m)
    if stage:
        stage_deadline = stage['deadline'] if stage['started'] is not None else now + stage['max_seconds']
        if stage['deadline'] is not None:
            stage_deadline = min(stage_deadline, stage['deadline'])
        deadline = min(deadline, stage_deadline)
    return deadline


def external_marker(manifest):
    from .engine import _repo_lock
    return _repo_lock(manifest).with_suffix('.external.json')


class ExternalBusy(Problem):
    """Expected queue contention, distinct from corrupt ownership evidence."""


def release_external(store, task, message, **fields):
    # A durable release authorization precedes unlink. Recovery never infers a
    # host process stopped merely from expiry or a missing process ID.
    with store.db:
        store.event(task['id'], 'external_release', {'attempt': task['attempt']})
        store._update(task['id'], fields)
        store._notify(task['id'], message)
    marker = external_marker(task['manifest'])
    if marker.exists() or marker.is_symlink():
        from .delivery import _read
        if json.loads(_read(marker)) != {'state_dir': str(store.root), 'task_id': task['id']}:
            raise Problem('Cannot release another external owner')
        marker.unlink()


def recover_external(store, task):
    if task['manifest'].get('implementation') != 'external':
        return
    if task['state'] == 'external_preparing':
        release_external(store, task, 'Interrupted preparation; no external instructions delivered',
                         state='blocked', phase='external-preparation-failed',
                         reason='Inspect preparation evidence before explicit resume')
        return
    marker = external_marker(task['manifest'])
    if not marker.exists() and not marker.is_symlink():
        return
    from .delivery import _read
    owner = json.loads(_read(marker))
    if owner != {'state_dir': str(store.root), 'task_id': task['id']}:
        return
    if task['state'] == 'external_implementing':
        return  # Active or uncertain native author: only explicit stopped handoff releases.
    released = any(json.loads(r[0]) == {'attempt': task['attempt']} for r in store.db.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='external_release'", (task['id'],)))
    if not released:
        raise Problem('External ownership has no durable release authorization; inspect original evidence')
    marker.unlink()


def require_external_idle(store, manifest, task_id=None):
    if manifest['version'] != 2:
        return
    marker = external_marker(manifest)
    if marker.exists() or marker.is_symlink():
        from .delivery import _read
        owner = json.loads(_read(marker))
        if owner != {'state_dir': str(store.root), 'task_id': task_id}:
            raise ExternalBusy('External implementation in another task/state directory owns this checkout')
    for task in store.tasks():
        if (task['id'] != task_id and task['manifest']['workspace'] == manifest['workspace']
                and task['state'] in ('external_preparing', 'external_implementing')):
            raise ExternalBusy('External implementation owns this checkout; confirm its stop and complete its handoff first')
