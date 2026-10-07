"""Read-only soft observation for checkpoint_mode='observe'.

The observer is passive: it does not request progress from Pi, sends no
checkpoint steer and does not stop an attempt for missing reports or silence.
It records bounded metadata about existing logs, tool events and declared
outputs once per interval, plus a start baseline. Every observation is
best-effort metadata only: it is an unverified claim, never progress, test
success or acceptance. Hard limits, cancellation and final-delivery
validation live in run_process and the delivery layer and are unchanged;
observation errors never interrupt the agent or extend the budget.

Persistence failures are recorded (persist_errors) and shown in records and
status; they are never silently swallowed as if the sample had been taken.
"""
import errno
import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from .checkpoints import _identity
from .delivery import _encode, _json, _read, _write_once
from .files import Problem

_MAX_READ = 32 * 1024
_MAX_FRAGMENT = 16 * 1024
_MAX_OUTPUTS = 16


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Observer:
    """Passive observation adapter bound to one attempt's logs and declared outputs."""

    BUILTIN = ("pi/stdout.jsonl", "pi/stderr.txt")
    NOTE = ("Observation of existing logs, tool events and declared artifacts; "
            "an unverified claim, not progress, test success or acceptance.")

    def __init__(self, task, context, interval, outputs=(), workspace=None, timeout=None):
        if type(interval) is not int or interval <= 0:
            raise Problem("checkpoint_seconds must be a positive integer")
        outputs = tuple(outputs or ())
        if len(outputs) > _MAX_OUTPUTS:
            raise Problem("checkpoint_outputs must contain at most 16 distinct file paths")
        self.task = task
        self.identity = _identity({
            "task_id": task["id"], "round": task["round"], "attempt": task["attempt"]})
        self.interval = interval
        self.context = Path(context).resolve()
        self.workspace = Path(workspace or context).resolve()
        self.timeout = timeout
        self.directory = self.context / "observations"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._fragment_dir = self.directory / "outputs"
        self._fragment_dir.mkdir(parents=True, exist_ok=True)
        self.sources = self.BUILTIN + outputs
        # Canonical absolute base directory per source; safe reads are only
        # opened under the matching base, never through any other route.
        self._source_base = {}
        for rel in self.BUILTIN:
            self._source_base[rel] = self.context
        for rel in outputs:
            self._source_base[rel] = self.workspace
        self.number = 0
        self.records = 0
        self.started = None
        self.next_due = None
        self.now = None
        self.tool_starts = {}
        self.tool_starts_total = 0
        self.tool_ends = 0
        self._file_state = {}
        self._persist_errors = []
        contract = self.identity | {
            "version": 1, "mode": "observe", "checkpoint_seconds": interval,
            "workspace": str(self.workspace), "sources": list(self.sources), "note": self.NOTE}
        self._write_once_safe(self.directory / "contract.json", _encode(contract))

    # -- entry points (must never interrupt the agent) -----------------------

    def start(self, now):
        if self.started is not None:
            raise Problem("Observer already started")
        self.started = self.now = now
        # Baseline is taken now; the first sampling may only happen after one
        # full interval has elapsed (baseline next_due == start + interval).
        self.next_due = self.started + self.interval
        self._collect(self.number, "baseline")

    def observe(self, now):
        try:
            if self.started is None or self.next_due is None:
                return
            self.now = now
            if now >= self.next_due:
                self.number += 1
                self._collect(self.number, "observation")
                self.next_due = self.now + self.interval
        except Exception:
            # Observation is metadata only: any failure is recorded in
            # persist_errors, never raised to the agent loop.
            pass

    def record_tool(self, event):
        try:
            kind = event.get("type")
            tool_id = event.get("toolCallId")
            if not kind or not isinstance(tool_id, str) or not tool_id:
                return
            if kind == "tool_execution_start":
                if tool_id in self.tool_starts:
                    return
                self.tool_starts[tool_id] = time.monotonic()
                self.tool_starts_total += 1
            elif kind == "tool_execution_end":
                if tool_id in self.tool_starts:
                    del self.tool_starts[tool_id]
                self.tool_ends += 1
        except Exception:
            pass

    def active_tools(self):
        return sorted(self.tool_starts)

    def finish(self):
        """Capture a final observation as metadata; it neither stops nor
        changes budgets."""
        try:
            if self.started is None:
                return
            now = time.monotonic()
            self.now = now
            # Always capture a final snapshot on normal completion; it is an
            # end-of-attempt record, not bounded by the sampling interval.
            self.number += 1
            self._collect(self.number, "final")
        except Exception:
            pass

    # -- persistence helpers -------------------------------------------------

    def _write_once_safe(self, path, data):
        """Publish once; a persistence failure is recorded, not raised, so the
        agent is never interrupted. Returns True on success."""
        try:
            _write_once(path, data)
            return True
        except Exception:
            name = Path(path).name
            if name not in self._persist_errors:
                self._persist_errors.append(name)
            return False

    def _collect(self, number, kind):
        record = self._record(number, kind)
        self.number = number
        # Every historical record is written once; record-NNNN is unique per
        # checkpoint number and never overwritten.
        self._write_once_safe(self.directory / ("record-%04d.json" % number),
                             _encode(record))
        self.records = number

    def _record(self, number, kind):
        sources = []
        for rel in self.sources:
            prev = self._file_state.get(rel)
            state = self._safe_read(self._source_base[rel], rel)
            raw_data = state.pop("data", None)
            state["path"] = rel
            state["base"] = "attempt" if rel in self.BUILTIN else "workspace"
            state["previously_read"] = prev is not None
            if (prev is not None and state.get("size") is not None
                    and prev.get("size") is not None
                    and state["size"] < prev["size"]):
                state["truncated"] = True
            if (prev is not None and state.get("sha256_sample") is not None
                    and prev.get("hash") is not None
                    and state["sha256_sample"] != prev.get("hash")):
                state["content_changed"] = True
            # Declared outputs get a bounded, versioned fragment copy; builtin
            # RPC logs never copy dialogue, only bounded metadata/offsets.
            if (rel not in self.BUILTIN and state["status"] == "read"
                    and raw_data is not None):
                fp, fb, frange = self._save_fragment(rel, number, raw_data,
                                                     state.get("range"))
                if fp is not None:
                    state["fragment_path"] = fp
                    state["fragment_bytes"] = fb
                    state["fragment_range"] = frange
            if state["status"] == "read":
                self._file_state[rel] = {
                    "size": state.get("size"),
                    "hash": state.get("sha256_sample"),
                    "inode": state.get("inode"),
                    "dev": state.get("dev"),
                }
            sources.append(state)
        return {
            "version": 1,
            "task_id": self.task["id"],
            "round": self.task["round"],
            "attempt": self.task["attempt"],
            "kind": kind,
            "mode": "observe",
            "checkpoint": number,
            "recorded_at_utc": _utc(),
            "elapsed_seconds": round(self.now - self.started, 3) if self.started is not None else 0.0,
            "interval_seconds": self.interval,
            "note": self.NOTE,
            "sha256_note": ("sha256_sample and sha256_fragment hash only the bounded "
                             "bytes read, not the full file."),
            "sources": sources,
            "tool_events": {
                "starts_total": self.tool_starts_total,
                "ends_total": self.tool_ends,
                "active_count": len(self.tool_starts),
                "active_tools": self.active_tools(),
            },
            "persist_errors": list(self._persist_errors),
        }

    # -- safe reads -----------------------------------------------------------

    def _classify_io(self, exc):
        code = getattr(exc, "errno", None)
        if code == errno.ENOENT:
            return "missing", "missing"
        if code == errno.ELOOP:
            return "unreadable", "symlink"
        if code == errno.EISDIR:
            return "unreadable", "directory"
        if code in (errno.EACCES, errno.EPERM):
            return "unreadable", "permission"
        return "unreadable", "io_error"

    def _open_under(self, base_abs, parts):
        """Open a relative path only through the base directory's file
        descriptor. Every directory component, including the base, is opened
        with O_DIRECTORY|O_NOFOLLOW and the process changes directory into it,
        so no component can be a symlink and no ancestor is rewalked after a
        previous check. The leaf is opened O_NOFOLLOW|O_NONBLOCK (not a
        directory) and returned; the caller fstats it. The process cwd is
        restored before returning, even on failure."""
        orig_cwd = os.getcwd()
        dir_fds = []
        leaf = None
        try:
            fd = os.open(base_abs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            dir_fds.append(fd)
            os.chdir(fd)
            for i, part in enumerate(parts):
                if not part or part == "." or part == "..":
                    raise OSError(errno.ELOOP, "invalid path component")
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if i == len(parts) - 1:
                    flags |= os.O_NONBLOCK
                    leaf = os.open(part, flags)
                else:
                    flags |= os.O_DIRECTORY
                    dir_fds.append(os.open(part, flags))
                    os.chdir(dir_fds[-1])
        finally:
            for d in dir_fds:
                try:
                    os.close(d)
                except OSError:
                    pass
            try:
                os.chdir(orig_cwd)
            except OSError:
                pass
        return leaf

    def _safe_read(self, base_abs, rel):
        state = {
            "status": "unreadable",
            "size": None,
            "range": None,
            "bytes_read": 0,
            "sha256_sample": None,
            "inode": None,
            "dev": None,
            "truncated": False,
            "previously_read": False,
            "content_changed": False,
            "utf8": True,
            "write_in_progress": False,
            "error": None,
            "jsonl": None,
            "data": None,
        }
        parts = rel.split(os.sep)
        if not parts or any(p == "" or p in (".", "..") for p in parts):
            state.update(status="unreadable", error="invalid_path")
            return state
        fd = None
        try:
            fd = self._open_under(str(base_abs), parts)
            st = os.fstat(fd)
        except OSError as exc:
            status_value, error = self._classify_io(exc)
            state.update(status=status_value, error=error)
            return state
        try:
            if not stat.S_ISREG(st.st_mode):
                error = "directory" if stat.S_ISDIR(st.st_mode) else "special_file"
                state.update(status="unreadable", error=error)
                return state
            size = st.st_size
            state.update(size=size, inode=st.st_ino, dev=st.st_dev)
            start = max(0, size - _MAX_READ)
            end = min(size, start + _MAX_READ)
            state["range"] = [start, end]
            if end > start:
                os.lseek(fd, start, os.SEEK_SET)
                data = os.read(fd, end - start)
            else:
                data = b""
        except OSError as exc:
            status_value, error = self._classify_io(exc)
            state.update(status=status_value, error=error)
            return state
        finally:
            if fd is not None:
                os.close(fd)
        state["data"] = data
        state["bytes_read"] = len(data)
        if data:
            state["sha256_sample"] = hashlib.sha256(data).hexdigest()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                state["utf8"] = False
            if state["utf8"]:
                state["write_in_progress"] = not data.endswith(b"\n")
        if rel == "pi/stdout.jsonl":
            state["jsonl"] = self._analyze_jsonl(data)
        state["status"] = "read"
        return state

    def _save_fragment(self, rel, number, data, sample_range):
        """Copy a bounded fragment of a declared output to a versioned file.
        The name carries the source identity and the checkpoint number, so
        each sampling cycle gets a distinct, non-overwritten file and the
        record's reference always points at this cycle's bytes."""
        if not data:
            return None, 0, None
        fragment = data[:_MAX_FRAGMENT]
        stem = re.sub(r"[^\w.\-]", "_", Path(rel).name)
        h = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]
        name = "%s_%04d_%s" % (h, number, stem)
        path = self._fragment_dir / name
        if not self._write_once_safe(path, fragment):
            return None, 0, None
        start = (sample_range or [0, 0])[0]
        return "outputs/%s" % name, len(fragment), [start, start + len(fragment)]

    def _analyze_jsonl(self, data):
        analysis = {"lines": 0, "parsed": 0, "last_line_complete": None, "incomplete": False}
        if not data:
            return analysis
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return analysis
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return analysis
        analysis["lines"] = len(lines)
        parsed = 0
        last_complete = True
        for ln in lines:
            try:
                json.loads(ln)
                parsed += 1
                last_complete = True
            except ValueError:
                last_complete = False
        analysis["parsed"] = parsed
        analysis["last_line_complete"] = last_complete
        analysis["incomplete"] = not last_complete
        return analysis


def status(context, task_id, attempt):
    """Read-only latest observation for an attempt; None when observe mode was
    not used. The contract and the latest record are bound together and kept
    as separate fields; every counted record must carry the same contract
    identity."""
    directory = Path(context) / "observations"
    if not (directory.exists() or directory.is_symlink()):
        return None
    contract_path = directory / "contract.json"
    if not (contract_path.exists() or contract_path.is_symlink()):
        return None
    try:
        contract, errors, _ = _json(_read(contract_path), contract_path)
    except (Problem, OSError, ValueError):
        return None
    if errors or type(contract) is not dict or contract.get("mode") != "observe":
        return None
    try:
        identity = _identity({
            "task_id": contract.get("task_id"), "round": contract.get("round"),
            "attempt": contract.get("attempt")})
    except Problem:
        return None
    if identity["task_id"] != task_id or identity["attempt"] != attempt:
        return None
    latest_number = -1
    latest_value = None
    latest_path = None
    count = 0
    try:
        paths = [p for p in directory.glob("record-*.json")
                 if (p.is_file() or p.is_symlink())]
    except OSError:
        return None
    for p in paths:
        try:
            n = int(p.stem.split("-", 1)[1])
        except (IndexError, ValueError, AttributeError):
            continue
        try:
            value, errors, _ = _json(_read(p), p)
        except (Problem, OSError, ValueError):
            continue
        if errors or type(value) is not dict or value.get("checkpoint") != n:
            continue
        if value.get("mode") != "observe":
            continue
        # Each record is bound to the contract identity; unbound records are
        # not counted.
        if (value.get("task_id"), value.get("round"), value.get("attempt")) != \
                (identity["task_id"], identity["round"], identity["attempt"]):
            continue
        count += 1
        if n > latest_number:
            latest_number = n
            latest_value = value
            latest_path = p
    if latest_number < 0:
        return None
    return {
        "mode": "observe",
        "checkpoint_seconds": contract.get("checkpoint_seconds"),
        "contract": contract,
        "sources": latest_value.get("sources"),
        "count": count,
        "latest_checkpoint": latest_number,
        "latest_path": str(latest_path),
        "latest": latest_value,
        "note": "read-only observation; unverified claim, not progress, test success or acceptance.",
    }
