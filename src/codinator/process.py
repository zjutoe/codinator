"""Bounded processes with raw evidence, LF framing and process-group cleanup."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from .files import Problem, file_info, write_json


class Interrupted(Problem):
    pass


def process_start(pid):
    try:
        # comm can contain spaces or ')'; fields after the final ')' start at #3.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def stop_group(pid, start):
    if not start or process_start(pid) != start:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and process_start(pid) == start:
        time.sleep(0.05)
    if process_start(pid) == start:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def finish_owned_group(proc, identity):
    """The caller still owns its unreaped leader; kill remaining group members too."""
    if proc.returncode is None and process_start(proc.pid) == identity:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_process(argv, *, cwd, env, out, timeout, on_start=lambda p, s: None,
                on_exit=lambda: None, cancel=lambda: False, protocol=None):
    """protocol.start(send), protocol.event(json, send)->done, protocol.finish()."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    started = time.time()
    write_json(out / "launch.json", {"argv": argv, "cwd": str(cwd), "started": started, "timeout_seconds": timeout})
    code, failure, proc, identity = None, None, None, None
    try:
        with (out / "stdout.jsonl" if protocol else out / "stdout.txt").open("xb") as stdout, (out / "stderr.txt").open("xb") as stderr:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if protocol else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            identity = process_start(proc.pid)
            write_json(out / "process.json", {"pid": proc.pid, "start": identity})
            on_start(proc.pid, identity)
            def send(obj):
                proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
                proc.stdin.flush()
            if protocol:
                protocol.start(send)
            buffer = b""
            done = False
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as sel:
                sel.register(proc.stdout, selectors.EVENT_READ, stdout)
                sel.register(proc.stderr, selectors.EVENT_READ, stderr)
                while sel.get_map():
                    if cancel():
                        raise Interrupted("Pause/cancel requested")
                    if time.monotonic() >= deadline:
                        raise Problem("Process exceeded wall-clock budget")
                    for key, _ in sel.select(0.2):
                        data = os.read(key.fd, 65536)
                        if not data:
                            sel.unregister(key.fileobj)
                            continue
                        key.data.write(data)
                        key.data.flush()
                        if protocol and key.fileobj is proc.stdout:
                            buffer += data
                            if len(buffer) > 16 * 1024 * 1024:
                                raise Problem("RPC record exceeds 16 MiB")
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                if not line.rstrip(b"\r"):
                                    continue
                                try:
                                    event = json.loads(line)
                                except (ValueError, UnicodeError) as exc:
                                    raise Problem("Invalid JSONL from agent") from exc
                                if type(event) is not dict:
                                    raise Problem("Agent event must be a JSON object")
                                done = protocol.event(event, send) or done
                    if done:
                        protocol.finish()
                        # One prompt per process. No further continuation may own this workspace.
                        stop_group(proc.pid, identity)
                        proc.wait(timeout=5)
                        code = 0
                        break
                if buffer.strip():
                    raise Problem("Truncated JSONL record at process exit")
            if code is None:
                # All pipes closed. Terminate any detached group members before
                # wait() reaps the leader (which would remove its /proc identity).
                # waitid WNOWAIT observes exit without reaping it.
                remaining = max(0.1, deadline - time.monotonic())
                end = time.monotonic() + remaining
                while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                    if cancel():
                        raise Interrupted('Pause/cancel requested')
                    if time.monotonic() >= end:
                        raise Problem('Process did not exit after closing output')
                    time.sleep(0.05)
                finish_owned_group(proc, identity)
                code = proc.wait(timeout=5)
                if protocol:
                    protocol.finish()
            if code != 0:
                raise Problem(f"Process exited {code}; see {out}")
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if proc:
            stop_group(proc.pid, identity)
            finish_owned_group(proc, identity)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
        if proc is None or proc.returncode is not None:
            on_exit()
        streams = {}
        for path in out.iterdir():
            if path.name in ("stdout.jsonl", "stdout.txt", "stderr.txt"):
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
                streams[path.name] = file_info(path)
        write_json(out / "result.json", {"exit_code": code, "failure": failure,
                   "started": started, "finished": time.time(), "elapsed_seconds": time.time() - started,
                   "streams": streams})
    return code
