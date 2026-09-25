import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .config import load_manifest
from .engine import Engine
from .files import Problem
from .process import run_process
from .store import Store, lock


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
    parser = argparse.ArgumentParser(description='Codidator: Pi implementation and independent Codex acceptance')
    parser.add_argument('--state-dir', default=os.environ.get('CODIDATOR_STATE_DIR', '~/.local/state/codidator'))
    parser.add_argument('--pi-bin', default='pi')
    parser.add_argument('--codex-bin', default='codex')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('submit', help='Publish an explicit immutable manifest')
    p.add_argument('manifest')
    for name in ('run', 'status', 'pause', 'cancel', 'resume'):
        p = sub.add_parser(name)
        p.add_argument('task', nargs='?' if name == 'status' else None)
        if name == 'resume':
            p.add_argument('--extra-seconds', type=int, default=0)
    sub.add_parser('serve', help='Process queued tasks serially; suitable for a user systemd service')
    sub.add_parser('recover', help='Stop and mark interrupted attempts; never replay prompts')
    sub.add_parser('notify', help='Retry pending explicitly configured Codex notifications')
    sub.add_parser('doctor', help='Check local dependencies and bubblewrap without calling models')
    sub.add_parser('service', help='Print a user service unit; does not install/enable it')
    args = parser.parse_args(argv)
    try:
        if args.command == 'service':
            exe = shutil.which('codidator') or str(Path(sys.executable).parent / 'codidator')
            state = str(Path(args.state_dir).expanduser().resolve())
            print('[Unit]\nDescription=Codidator task controller\nAfter=network-online.target\n\n[Service]')
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
        store = Store(args.state_dir)
        if args.command == 'status':
            tasks = [store.get(args.task)] if args.task else store.tasks()
            keys = ('id', 'state', 'phase', 'round', 'attempt', 'reason', 'deadline')
            print(json.dumps([{k: t[k] for k in keys} for t in tasks], ensure_ascii=False, indent=2))
            return 0
        if args.command in ('pause', 'cancel'):
            task = store.get(args.task)
            if task['state'] in ('accepted', 'cancelled'):
                raise Problem('Task already terminal')
            if task['state'] in ('implementing', 'checking', 'reviewing'):
                store.update(args.task, control=args.command)
            else:
                store.update(args.task, state='paused' if args.command == 'pause' else 'cancelled')
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
            engine.resume(args.task, args.extra_seconds)
            print('ready:', args.task)
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
                        if task['state'] in ('ready', 'needs_changes'):
                            engine.run(task['id'])
                    notifications(store, args.codex_bin)
                    time.sleep(3)
        return 0
    except KeyboardInterrupt:
        return 130
    except (Problem, OSError, ValueError, KeyError) as exc:
        print(f'codidator: {exc}', file=sys.stderr)
        return 2
