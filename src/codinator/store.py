"""Only the controller writes workflow state. Models submit proposals, not state."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from .files import Problem


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Problem(f"Another controller holds {path}") from None
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self, root, *, read_only=False):
        self.root = Path(root).expanduser().resolve()
        if read_only:
            database = self.root / 'state.sqlite'
            if not database.is_file():
                raise Problem(f'No task database at {database}; select the correct mode/state directory')
            self.db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=15)
            self.db.row_factory = sqlite3.Row
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, manifest TEXT NOT NULL, state TEXT NOT NULL,
                round INTEGER NOT NULL DEFAULT 1, attempt INTEGER NOT NULL DEFAULT 0,
                started REAL, deadline REAL, pid INTEGER, pid_start TEXT,
                reason TEXT NOT NULL DEFAULT '', control TEXT,
                feedback TEXT NOT NULL DEFAULT '', last_issues TEXT NOT NULL DEFAULT '',
                phase TEXT NOT NULL DEFAULT '', expected_digest TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                time REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, thread TEXT,
                message TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
        """)
        self.db.commit()
        # Serialise startup migrations across a CLI and a running service.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(tasks)')}
            if 'review_resume' not in columns:
                self.db.execute('ALTER TABLE tasks ADD COLUMN review_resume TEXT')
            if 'attempt_seconds_override' not in columns:
                self.db.execute('ALTER TABLE tasks ADD COLUMN attempt_seconds_override INTEGER')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise Problem(f"Unknown task {task_id}")
        result = dict(row)
        result["manifest"] = json.loads(result["manifest"])
        result['review_resume'] = json.loads(result['review_resume']) if result.get('review_resume') is not None else None
        result.setdefault('attempt_seconds_override', None)
        return result

    def tasks(self):
        return [self.get(r[0]) for r in self.db.execute("SELECT id FROM tasks ORDER BY created")]

    def add(self, manifest, fingerprint):
        now = time.time()
        try:
            with self.db:
                self.db.execute("INSERT INTO tasks(id,manifest,state,expected_digest,created,updated) VALUES(?,?,'ready',?,?,?)",
                                (manifest["id"], json.dumps(manifest), fingerprint, now, now))
                self.event(manifest["id"], "submitted", {"digest": fingerprint})
        except sqlite3.IntegrityError:
            raise Problem("Task id already exists; submission is never overwritten") from None

    def update(self, task_id, **fields):
        with self.db:
            self._update(task_id, fields)

    def request_control(self, task_id, action):
        if action not in ('pause', 'cancel'):
            raise ValueError('Invalid control action')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            task = self.get(task_id)
            if task['state'] in ('accepted', 'cancelled'):
                raise Problem('Task already terminal')
            if task['state'] in ('implementing', 'checking', 'reviewing'):
                self._update(task_id, {'control': action})
            else:
                self._update(task_id, {'state': 'paused' if action == 'pause' else 'cancelled',
                                      'control': None})

    def _update(self, task_id, fields):
        valid = {"state", "round", "attempt", "started", "deadline", "pid", "pid_start", "reason", "control",
                 "feedback", "last_issues", "phase", "expected_digest", "review_resume", "attempt_seconds_override"}
        if not fields or set(fields) - valid:
            raise ValueError("Invalid state update")
        fields["updated"] = time.time()
        if fields.get('review_resume') is not None:
            fields['review_resume'] = json.dumps(fields['review_resume'])
        self.db.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                        (*fields.values(), task_id))
        self.event(task_id, "state", fields)

    def event(self, task_id, kind, payload):
        self.db.execute("INSERT INTO events(task_id,time,kind,payload) VALUES(?,?,?,?)",
                        (task_id, time.time(), kind, json.dumps(payload, ensure_ascii=False)))

    def notify(self, task_id, message):
        with self.db:
            self._notify(task_id, message)

    def finish(self, task_id, message, **fields):
        """Terminal state and its notification are one durable transaction."""
        with self.db:
            self._update(task_id, fields)
            self._notify(task_id, message)

    def _notify(self, task_id, message):
        task = self.get(task_id)
        event_id = f"{task_id}:{task['attempt']}:{task['state']}"
        body = f"[codinator event {event_id}] {message}\n这是状态通知；同一事件ID只报告一次，不要据此重新执行任务。"
        self.db.execute("INSERT OR IGNORE INTO outbox(id,task_id,thread,message) VALUES(?,?,?,?)",
                        (event_id, task_id, task['manifest'].get('notify_thread'), body))
