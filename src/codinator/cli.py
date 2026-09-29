import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

from .config import load_manifest
from .engine import Engine
from .files import Problem
from .process import run_process
from .store import Store, lock
from .status import report


def notifications(store, codex_bin):
    """At-least-once delivery with a stable event ID; failure never rolls back acceptance."""
    with lock(store.root / 'notify.lock'):
        for row in store.db.execute('SELECT * FROM outbox WHERE delivered=0 AND thread IS NOT NULL').fetchall():
            number = row['attempts'] + 1
            with store.db:
                store.db.execute('UPDATE outbox SET attempts=? WHERE id=?', (number, row['id']))
            out = store.root / 'notifications' / row['task_id'] / row['id'].replace(':', '_') / str(number)
            try:
                run_process([codex_bin, 'queue', '--thread', row['thread'], '--message', row['message']],
                            cwd=store.root, env=os.environ.copy(), out=out, timeout=30)
            except (Problem, OSError) as exc:
                with store.db:
                    store.db.execute('UPDATE outbox SET last_error=? WHERE id=?', (str(exc), row['id']))
            else:
                with store.db:
                    store.db.execute('UPDATE outbox SET delivered=1,last_error=? WHERE id=?', ('', row['id']))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Codinator: Pi implementation and independent Codex acceptance')
    parser.add_argument('--state-dir', default=os.environ.get('CODINATOR_STATE_DIR', '~/.local/state/codinator'))
    parser.add_argument('--pi-bin', default='pi')
    parser.add_argument('--codex-bin', default='codex')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('submit', help='Publish an explicit immutable manifest')
    p.add_argument('manifest')
    p = sub.add_parser('pi', help='Native Pi interface with automatic independent Codex review/rework')
    p.add_argument('target', help='Manifest path for a new task, or existing interactive task ID')
    p.add_argument('--resume', action='store_true', help='Explicitly resume a paused interactive task')
    for name in ('run', 'status', 'pause', 'cancel', 'resume'):
        p = sub.add_parser(name)
        p.add_argument('task', nargs='?' if name == 'status' else None)
        if name == 'status':
            p.add_argument('--mode', choices=('background', 'pi'), default='background',
                           help='Read background tasks or the separate native Pi state; never starts a session')
        if name == 'resume':
            p.add_argument('--extra-seconds', type=int, default=0)
            p.add_argument('--attempt-seconds', type=int, help='Override each Pi/Codex process timeout for this task; original manifest remains unchanged')
            p.add_argument('--review-only', action='store_true', help='Retry only an unfinished review of the unchanged checked submission')
    sub.add_parser('serve', help='Process queued tasks serially; suitable for a user systemd service')
    sub.add_parser('recover', help='Stop and mark interrupted attempts; never replay prompts')
    sub.add_parser('notify', help='Retry pending explicitly configured Codex notifications')
    sub.add_parser('doctor', help='Check local dependencies and bubblewrap without calling models')
    sub.add_parser('service', help='Print a user service unit; does not install/enable it')
    args = parser.parse_args(argv)
    try:
        if args.command == 'pi':
            from .foreground import launch
            return launch(args.state_dir, args.target, resume=args.resume, pi_bin=args.pi_bin, codex_bin=args.codex_bin)
        if args.command == 'service':
            exe = shutil.which('codinator') or str(Path(sys.executable).parent / 'codinator')
            state = str(Path(args.state_dir).expanduser().resolve())
            print('[Unit]\nDescription=Codinator task controller\nAfter=network-online.target\n\n[Service]')
            # Explicit path captures the Node/Pi installation used during setup.
            print('Type=simple\nRestart=on-failure\nRestartSec=10\nKillMode=control-group\nTimeoutStopSec=15\nUMask=0077')
            def quote(value):
                return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
            print('Environment=' + quote('PATH=' + os.environ.get('PATH', '')))
            # User services do not inherit the launching terminal's proxy by default.
            for name in sorted(os.environ):
                if name.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
                    print('Environment=' + quote(name + '=' + os.environ[name]))
            print('ExecStart=' + ' '.join(quote(s) for s in [exe, '--state-dir', state, '--pi-bin', shutil.which(args.pi_bin) or args.pi_bin,
                                                           '--codex-bin', shutil.which(args.codex_bin) or args.codex_bin, 'serve']))
            print('\n[Install]\nWantedBy=default.target')
            return 0
        if args.command == 'status':
            state = Path(args.state_dir).expanduser()
            if args.mode == 'pi':
                state /= 'interactive'
            store = Store(state, read_only=True)
            try:
                with store.db:
                    store.db.execute('BEGIN')
                    tasks = [store.get(args.task)] if args.task else store.tasks()
                    print(json.dumps([report(store, t, args.mode) for t in tasks], ensure_ascii=False, indent=2))
            finally:
                store.db.close()
            return 0
        store = Store(args.state_dir)
        if args.command in ('pause', 'cancel'):
            store.request_control(args.task, args.command)
            return 0
        if args.command == 'notify':
            notifications(store, args.codex_bin)
            return 0
        engine = Engine(store, pi_bin=args.pi_bin, codex_bin=args.codex_bin)
        if args.command == 'doctor':
            missing = [p for p in (args.pi_bin, args.codex_bin, 'git', 'bwrap') if not shutil.which(p)]
            if missing:
                raise Problem('Missing executables: ' + ', '.join(missing))
            p = subprocess.run(['bwrap', '--ro-bind', '/', '/', '--unshare-pid', '--proc', '/proc', '--', '/usr/bin/true'], capture_output=True, timeout=10)
            if p.returncode:
                raise Problem('bubblewrap cannot run: ' + p.stderr.decode(errors='replace'))
            print('PASS: Python, git, Pi, Codex and bubblewrap available; model/auth connectivity not tested')
        elif args.command == 'submit':
            manifest = load_manifest(args.manifest)
            with lock(store.root / 'controller.lock'):
                engine.submit(manifest)
            print('ready:', manifest['id'])
        elif args.command == 'resume':
            if args.extra_seconds < 0:
                raise Problem('--extra-seconds must be non-negative')
            if args.attempt_seconds is not None and args.attempt_seconds <= 0:
                raise Problem('--attempt-seconds must be positive')
            engine.resume(args.task, args.extra_seconds, review_only=args.review_only, attempt_seconds=args.attempt_seconds)
            print('review_ready:' if args.review_only else 'ready:', args.task)
        elif args.command == 'recover':
            with lock(store.root / 'controller.lock'):
                engine.recover()
        elif args.command == 'run':
            engine.run(args.task)
            notifications(store, args.codex_bin)
            task = store.get(args.task)
            print(json.dumps({k: task[k] for k in ('id', 'state', 'reason')}, ensure_ascii=False, indent=2))
            return 0 if task['state'] == 'accepted' else 1
        elif args.command == 'serve':
            def interrupted(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, interrupted)
            with lock(store.root / 'service.lock'):
                with lock(store.root / 'controller.lock'):
                    engine.recover()
                while True:
                    for task in store.tasks():
                        if task['state'] in ('ready', 'review_ready', 'needs_changes'):
                            engine.run(task['id'])
                    notifications(store, args.codex_bin)
                    time.sleep(3)
        return 0
    except KeyboardInterrupt:
        return 130
    except (Problem, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f'codinator: {exc}', file=sys.stderr)
        return 2
