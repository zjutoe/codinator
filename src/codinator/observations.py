"""Passive, bounded observations. Artifact failures are unknown, not verdicts.

Controller evidence failures propagate explicitly. The observer never sends RPC
requests, runs project commands, interprets model content or extends a deadline.
"""
import errno
import hashlib
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from .checkpoints import _identity
from .delivery import _encode, _json, _read, _write_once
from .files import Problem

_MAX_READ = 32 * 1024
_MAX_OUTPUTS = 16
_MAX_RECORD = 1_000_000


def _publish(path, data):
    if len(data) > _MAX_RECORD:
        raise Problem(f"Observation evidence exceeds size limit: {path}")
    try:
        _write_once(path, data)
    except OSError as exc:
        raise Problem(f"Cannot persist observation evidence: {path}: {exc}") from exc


def _open_under(base, rel):
    """Use directory descriptors, never process cwd or a checked-then-open path."""
    parts = rel.split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise OSError(errno.EINVAL, 'invalid relative path')
    directory = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
    finally:
        os.close(directory)


class Observer:
    BUILTIN = ('pi/stdout.jsonl', 'pi/stderr.txt')
    NOTE = ('Observation of existing logs, tool events and declared artifacts; '
            'an unverified claim, not progress, test success or acceptance.')

    def __init__(self, task, context, interval, outputs=(), workspace=None):
        if type(interval) is not int or interval <= 0:
            raise Problem('checkpoint_seconds must be a positive integer')
        if len(outputs) > _MAX_OUTPUTS:
            raise Problem('Too many checkpoint outputs')
        self.identity = _identity(dict(task_id=task['id'], round=task['round'], attempt=task['attempt']))
        self.interval = interval
        self.context = Path(context).absolute()
        self.workspace = Path(workspace or context).absolute()
        self.directory = self.context / 'observations'
        self.directory.mkdir(exist_ok=False)
        (self.directory / 'outputs').mkdir()
        self.sources = ([{'base': 'attempt', 'path': p} for p in self.BUILTIN]
                        + [{'base': 'workspace', 'path': p} for p in outputs])
        self.number = 0
        self.records = 0
        self.started = self.next_due = self.now = None
        self.finished = False
        self.tool_starts = {}
        self.tool_starts_total = self.tool_ends = 0
        self._file_state = {}
        contract = self.identity | dict(version=1, mode='observe', checkpoint_seconds=interval,
            workspace=str(self.workspace), sources=self.sources, note=self.NOTE)
        _publish(self.directory / 'contract.json', _encode(contract))

    def start(self, now):
        if self.started is not None:
            raise Problem('Observer already started')
        self.started = self.now = now
        self.next_due = now + self.interval
        self._collect('baseline')

    def observe(self, now):
        if self.started is not None and not self.finished and now >= self.next_due:
            self.now = now
            self.number += 1
            self._collect('observation')
            self.next_due = now + self.interval

    def record_tool(self, event):
        kind, tool_id = event.get('type'), event.get('toolCallId')
        if not isinstance(tool_id, str) or not tool_id:
            return
        if kind == 'tool_execution_start' and tool_id not in self.tool_starts:
            self.tool_starts[tool_id] = True
            self.tool_starts_total += 1
        elif kind == 'tool_execution_end':
            self.tool_starts.pop(tool_id, None)
            self.tool_ends += 1

    def active_tools(self):
        return sorted(self.tool_starts)

    def finish(self):
        if self.started is not None and not self.finished:
            self.now = time.monotonic()
            self.number += 1
            self._collect('final')
            self.finished = True

    def _collect(self, kind):
        sources = []
        for index, source in enumerate(self.sources):
            builtin = source['base'] == 'attempt'
            base = self.context if builtin else self.workspace
            state = self._safe_read(base, source['path'], sample=not builtin)
            data = state.pop('data', None)
            state.update(source)
            previous = self._file_state.get(index)
            state['previously_read'] = previous is not None
            state['replaced'] = previous is not None and state.get('inode') is not None and (
                state['dev'], state['inode']) != (previous['dev'], previous['inode'])
            state['truncated'] = previous is not None and state.get('size') is not None and state['size'] < previous['size']
            state['delta_bytes'] = None if previous is None or state.get('size') is None or state['replaced'] else state['size'] - previous['size']
            state['content_changed'] = previous is not None and data is not None and state['sha256_sample'] != previous['hash']
            if builtin and state['status'] == 'read':
                state['range'] = [previous['size'], state['size']] if previous and not state['replaced'] and not state['truncated'] else [0, state['size']]
                state['reference_only'] = True
            elif data is not None and state['text']:
                name = f'outputs/{self.number:04d}-{index:02d}.txt'
                _publish(self.directory / name, data)
                state.update(fragment_path=name, fragment_bytes=len(data),
                    fragment_range=state['range'], sha256_fragment=state['sha256_sample'])
            if state['status'] == 'read':
                self._file_state[index] = dict(size=state['size'], dev=state['dev'],
                    inode=state['inode'], hash=state['sha256_sample'])
            sources.append(state)
        active = self.active_tools()
        record = self.identity | dict(version=1, mode='observe', kind=kind, checkpoint=self.number,
            recorded_at_utc=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(self.now-self.started, 3), interval_seconds=self.interval,
            sources=sources, note=self.NOTE,
            sha256_note='Hashes identify only bounded sample/fragment bytes, not whole files.',
            tool_events=dict(starts_total=self.tool_starts_total, ends_total=self.tool_ends,
                active_count=len(active), active_tools=[p[:256] for p in active[:128]],
                active_tools_truncated=len(active)>128 or any(len(p)>256 for p in active[:128])))
        _publish(self.directory / f'record-{self.number:04d}.json', _encode(record))
        self.records += 1

    def _safe_read(self, base, rel, *, sample=True):
        result = dict(status='unreadable', error=None, size=None, range=None, bytes_read=0,
            inode=None, dev=None, mtime_ns=None, sha256_sample=None, utf8=None, text=None,
            write_in_progress=False, partial_line=None, jsonl=None, content_state='unknown', data=None)
        fd = None
        try:
            fd = _open_under(base, rel)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                result['error'] = 'directory' if stat.S_ISDIR(before.st_mode) else 'special_file'
                return result
            result.update(status='read', size=before.st_size, inode=before.st_ino,
                dev=before.st_dev, mtime_ns=before.st_mtime_ns)
            if not sample:
                return result
            offset = max(0, before.st_size-_MAX_READ)
            data = os.pread(fd, min(_MAX_READ, before.st_size-offset), offset)
            after = os.fstat(fd)
            changed = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            result.update(data=data, range=[offset, offset+len(data)], bytes_read=len(data),
                sha256_sample=hashlib.sha256(data).hexdigest(), write_in_progress=changed,
                partial_line=bool(data) and not data.endswith(b'\n'))
            try:
                text = data.decode('utf-8')
                is_text = not any((ord(c)<32 and c not in '\t\r\n\x1b') or ord(c)==127 for c in text)
                result.update(utf8=True, text=is_text, content_state='text' if is_text and not changed else 'unknown')
                if not is_text:
                    return result
            except UnicodeError:
                result.update(utf8=False, text=False, content_state='unknown')
                return result
            if Path(rel).suffix in ('.json', '.jsonl'):
                complete = False
                if not offset and not changed:
                    try:
                        if rel.endswith('.jsonl'):
                            for line in text.splitlines():
                                if line.strip(): json.loads(line)
                            complete = not result['partial_line']
                        else:
                            json.loads(text)
                            complete = True
                    except (ValueError, RecursionError):
                        pass
                result['jsonl'] = {'incomplete': not complete} if rel.endswith('.jsonl') else None
                result['content_state'] = 'json' if complete else 'unknown'
            return result
        except OSError as exc:
            result.update(status='missing' if exc.errno==errno.ENOENT else 'unreadable',
                error='symlink' if exc.errno==errno.ELOOP else 'io_error', content_state='unknown', data=None)
            return result
        finally:
            if fd is not None: os.close(fd)


def _document(path):
    value, errors, _ = _json(_read(path), path)
    if errors or type(value) is not dict:
        raise Problem(f'Invalid observation evidence: {path}')
    return value


def status(context, task_id, attempt):
    directory = Path(context) / 'observations'
    if not (directory.exists() or directory.is_symlink()):
        return None
    contract = _document(directory / 'contract.json')
    identity = _identity(contract)
    if (identity['task_id'], identity['attempt']) != (task_id, attempt) or contract.get('mode') != 'observe' or contract.get('version') != 1:
        raise Problem(f'Observation contract binding mismatch: {directory}')
    interval = contract.get('checkpoint_seconds')
    if type(interval) is not int or interval <= 0 or type(contract.get('sources')) is not list:
        raise Problem(f'Invalid observation contract: {directory}')
    latest = latest_path = None
    try:
        with os.scandir(directory) as entries:
            paths = [directory / e.name for e in entries
                     if e.name.startswith('record-') and e.name.endswith('.json')]
        paths.sort(key=lambda p: int(p.stem.split('-', 1)[1]))
    except OSError as exc:
        raise Problem(f'Cannot enumerate observation evidence: {directory}: {exc}') from exc
    except ValueError as exc:
        raise Problem(f'Invalid observation record name: {directory}') from exc
    for number, path in enumerate(paths):
        if path.name != f'record-{number:04d}.json':
            raise Problem(f'Observation sequence mismatch: {path}')
        value = _document(path)
        if (_identity(value) != identity or type(value.get('checkpoint')) is not int or value.get('checkpoint') != number
                or value.get('mode') != 'observe' or value.get('version') != 1
                or value.get('interval_seconds') != interval
                or type(value.get('sources')) is not list
                or any(type(s) is not dict for s in value['sources'])
                or [{'base':s.get('base'), 'path':s.get('path')} for s in value['sources']] != contract['sources']):
            raise Problem(f'Observation record binding mismatch: {path}')
        for source in value['sources']:
            fragment = source.get('fragment_path')
            if fragment is not None:
                index = value['sources'].index(source)
                if fragment != f'outputs/{number:04d}-{index:02d}.txt':
                    raise Problem(f'Invalid observation fragment reference: {path}')
                data = _read(directory / fragment)
                if len(data) != source.get('fragment_bytes') or hashlib.sha256(data).hexdigest() != source.get('sha256_fragment'):
                    raise Problem(f'Observation fragment integrity mismatch: {path}')
        latest, latest_path = value, str(path)
    return dict(mode='observe', checkpoint_seconds=interval, contract=contract,
        count=len(paths), latest_checkpoint=None if latest is None else latest['checkpoint'],
        latest_path=latest_path, latest=latest, sources=None if latest is None else latest['sources'],
        note=Observer.NOTE)
