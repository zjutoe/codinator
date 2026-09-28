"""Native terminal launcher and a local, bounded review bridge."""
import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import sys
import tempfile
import termios
import threading

from .config import load_manifest
from .engine import Engine
from .files import Problem, digest, write_json
from .interactive import Interactive
from .process import process_start, stop_group
from .sandbox import Sandbox, pi_home
from .store import Store, lock


class Bridge(socketserver.UnixStreamServer):
    def __init__(self, path, controller):
        self.controller = controller
        super().__init__(str(path), Handler)
        os.chmod(path, 0o600)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        try:
            raw = self.rfile.readline(1_100_001)
            if len(raw) > 1_100_000 or not raw.endswith(b'\n'):
                raise Problem('Invalid bridge request length/framing')
            request = json.loads(raw)
            if type(request) is not dict:
                raise Problem('Bridge request must be an object')
            controller = self.server.controller
            op = request.get('op')
            if op == 'submit' and set(request) == {'op', 'request_id', 'markdown', 'runtime'}:
                value = controller.submit(request['request_id'], request['markdown'], request['runtime'])
            elif op in ('status', 'begin', 'pause', 'resume') and set(request) == {'op'}:
                value = getattr(controller, op)()
            else:
                raise Problem('Unknown bridge operation or fields')
            result = {'ok': True, 'value': value}
        except (Problem, OSError, ValueError, KeyError) as exc:
            result = {'ok': False, 'error': str(exc)}
        self.wfile.write((json.dumps(result, ensure_ascii=False) + '\n').encode())


def pi_environment(config, socket):
    env = {key: value for key, value in os.environ.items() if not key.lower().endswith('_proxy')}
    env.update({'PI_CODING_AGENT_DIR': str(config), 'PI_OFFLINE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'NO_PROXY': '*', 'no_proxy': '*', 'NODE_USE_ENV_PROXY': '0', 'CODINATOR_PI_SOCKET': str(socket)})
    return env


def terminal_process(argv, *, cwd, env):
    """Inherit the real TTY; restore its settings even after interruption."""
    saved = termios.tcgetattr(sys.stdin.fileno()) if sys.stdin.isatty() else None
    proc = None
    identity = None
    handlers = {}
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True)
        identity = process_start(proc.pid)
        def interrupt(signum, _frame):
            if proc.poll() is None:
                os.killpg(proc.pid, signum)
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, interrupt)
        return proc.wait()
    finally:
        if proc:
            stop_group(proc.pid, identity)
            proc.wait(timeout=5)
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)


def launch(state_dir, target, *, resume=False, pi_bin='pi', codex_bin='codex'):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Problem('codinator pi requires an interactive terminal')
    state = Path(state_dir).expanduser().resolve() / 'interactive'
    manifest = load_manifest(target) if Path(target).is_file() else None
    s = Store(state)
    try:
        task_id = manifest['id'] if manifest else target
        existing = next((t for t in s.tasks() if t['id'] == task_id), None)
        if existing:
            if manifest and manifest != existing['manifest']:
                raise Problem('Published manifest changed; use a new task ID')
            m = existing['manifest']
        elif manifest:
            m = manifest
        else:
            raise Problem('Unknown interactive task; launch with its manifest first')
        repo_lock = Path('/tmp') / f"codinator-{os.getuid()}-{digest(m['workspace'])}.lock"
        with lock(repo_lock):
            sandbox = Sandbox()
            if not existing:
                Engine(s, sandbox=sandbox).submit(m)
            controller = Interactive(state, task_id, sandbox, codex_bin)
            controller.recover()
            if resume:
                controller.resume()
            private = state / 'private' / task_id
            private.mkdir(parents=True, mode=0o700, exist_ok=True)
            # Short path keeps Unix socket names below Linux's 108-byte limit.
            with tempfile.TemporaryDirectory(prefix='codinator-ui-', dir=Path.home() / '.cache') as socket_dir:
                sock = Path(socket_dir) / 'bridge.sock'
                with Bridge(sock, controller) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    session = Path(tempfile.mkdtemp(prefix='session-', dir=private))
                    config = pi_home(session / 'pi')
                    controller.session_dir = config / 'sessions'
                    extension = Path(__file__).with_name('pi_extension.mjs')
                    argv = [pi_bin, '--provider', 'bonsai', '--model', 'bonsai2-27b', '--thinking', 'xhigh',
                            '--no-extensions', '--extension', str(extension), '--no-skills', '--no-prompt-templates',
                            '--no-themes', '--offline', '--session-dir', str(config / 'sessions')]
                    write_json(session / 'launch.json', {'argv': argv, 'workspace': m['workspace'],
                               'mode': 'native Pi UI', 'review_seconds': m['attempt_seconds'],
                               'interactive_total_wall_clock': None})
                    try:
                        terminal_process(sandbox.wrap(argv, m['workspace'], m['allowed_paths'], [config]),
                                         cwd=m['workspace'], env=pi_environment(config, sock))
                    finally:
                        controller.close()
                        server.shutdown()
                        thread.join(timeout=5)
            result = controller.status()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['state'] == 'accepted' else 1
    finally:
        s.db.close()
