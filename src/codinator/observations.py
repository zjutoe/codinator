"""Read-only soft observation for checkpoint_mode='observe'.

The observer is passive: it does not request progress from Pi, sends no checkpoint
steer and does not stop an attempt for missing reports or silence. It records bounded
metadata about existing logs, tool events and declared artifacts once per interval,
plus a start baseline. Every observation is best-effort metadata only: it is an
unverified claim, never progress, test success or acceptance. Hard limits, cancellation
and final-delivery validation live in run_process and the delivery layer and are
unchanged; no observation error may interrupt the agent or extend the budget.
"""
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
        self.context = Path(context).absolute()
        self.workspace = Path(workspace or context).absolute()
        self.timeout = timeout
        self.directory = self.context / "observations"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._fragment_dir = self.directory / "outputs"
        self._fragment_dir.mkdir(parents=True, exist_ok=True)
        self.sources = self.BUILTIN + outputs
        # Canonical absolute base per source; builtin sources live in the attempt dir,
        # declared outputs in the workspace. The observer must never read outside its base.
        self._source_base = {rel: str(Path(base).resolve()) for base, rels in
                             [(self.context, self.BUILTIN), (self.workspace, outputs)]
                             for rel in rels}
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
        self.started = self.next_due = self.now = now
        self._collect(self.number, "baseline")

    def observe(self, now):
        self.now = now
        try:
            if self.started is None or self.next_due is None:
                return
            if now >= self.next_due:
                self.number += 1
                self._collect(self.number, "observation")
                self.next_due = now + self.interval
        except Exception:
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
        """Capture a final observation as metadata; it neither stops nor changes budgets."""
        if self.started is None:
            return
        try:
            self.now = time.monotonic()
            if self.next_due is None or self.now >= self.next_due:
                self.number += 1
                self._collect(self.number, "final")
        except Exception:
            pass

    # -- observation internals ------------------------------------------------

    def _write_once_safe(self, path, data):
        """Publish once; a persistence failure must never interrupt the agent."""
        try:
            _write_once(path, data)
        except Exception:
            self._persist_errors.append(path.name)

    def _collect(self, number, kind):
        record = self._record(number, kind)
        self.number = number
        self._write_once_safe(self.directory / f"record-{number:04d}.json", _encode(record))
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
            if prev is not None and state.get("size") is not None and state["size"] < prev["size"]:
                state["truncated"] = True
            if prev is not None and state.get("sha256_sample") is not None:
                state["content_changed"] = state["sha256_sample"] != prev.get("hash")
            if rel not in self.BUILTIN and state["status"] == "read":
                fp, fb = self._save_fragment(rel, raw_data)
                if fp is not None:
                    state["fragment_path"] = fp
                    state["fragment_bytes"] = fb
            if state["status"] == "read":
                self._file_state[rel] = {"size": state["size"], "hash": state.get("sha256_sample")}
            sources.append(state)
        return {
            "version": 1,
            "kind": kind,
            "mode": "observe",
            "checkpoint": number,
            "recorded_at_utc": _utc(),
            "elapsed_seconds": round(self.now - self.started, 3) if self.started is not None else 0.0,
            "interval_seconds": self.interval,
            "note": self.NOTE,
            "sha256_note": ("sha256_sample and sha256_fragment hash only the bounded bytes "
                             "read, not the full file."),
            "sources": sources,
            "tool_events": {
                "starts_total": self.tool_starts_total,
                "ends_total": self.tool_ends,
                "active_count": len(self.tool_starts),
                "active_tools": self.active_tools(),
            },
            "persist_errors": list(self._persist_errors),
        }

    def _safe_read(self, base_abs, rel):
        state = {"status": "read", "size": None, "range": None, "bytes_read": 0,
                 "sha256_sample": None, "truncated": False, "previously_read": False,
                 "content_changed": False, "utf8": True, "error": None,
                 "jsonl": None, "data": None}
        path = Path(base_abs) / rel
        # 1) Reject anything that is not a regular file, without following it.
        try:
            st = path.lstat()
        except OSError:
            state["status"] = "missing"
            return state
        if stat.S_ISLNK(st.st_mode):
            state.update(status="unreadable", error="symlink")
            return state
        if stat.S_ISDIR(st.st_mode):
            state.update(status="unreadable", error="directory")
            return state
        if not stat.S_ISREG(st.st_mode):
            state.update(status="unreadable", error="special_file")
            return state
        # 2) No symlink escape in any ancestor of the canonical path.
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            state.update(status="unreadable", error="resolve")
            return state
        if not (str(resolved) == base_abs or str(resolved).startswith(base_abs + os.sep)):
            state.update(status="unreadable", error="symlink_escape")
            return state
        size = st.st_size
        state["size"] = size
        # 3) Bounded, non-following, non-blocking read of the tail.
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            if size == 0:
                start = end = 0
            else:
                start = max(0, size - _MAX_READ)
                end = min(size, start + _MAX_READ)
            os.lseek(fd, start, os.SEEK_SET)
            data = os.read(fd, end - start)
        except OSError:
            state.update(status="unreadable", error="io_error")
            return state
        finally:
            if fd is not None:
                os.close(fd)
        state["data"] = data
        state["range"] = [start, end]
        state["bytes_read"] = len(data)
        if data:
            state["sha256_sample"] = hashlib.sha256(data).hexdigest()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            state["utf8"] = False
        if rel == "pi/stdout.jsonl":
            state["jsonl"] = self._analyze_jsonl(data)
        return state

    def _save_fragment(self, rel, data):
        """Save a bounded fragment of a declared output; reference it by relative path."""
        if not data:
            return None, 0
        fragment = data[:_MAX_FRAGMENT]
        stem = re.sub(r"[^\w.\-]", "_", Path(rel).name)
        frag_name = "%s_%s" % (hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12], stem)
        path = self._fragment_dir / frag_name
        self._write_once_safe(path, fragment)
        return "outputs/" + frag_name, len(fragment)

    def _analyze_jsonl(self, data):
        analysis = {"lines": 0, "parsed": 0, "last_line_complete": None, "incomplete": False}
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
    """Read-only latest observation for an attempt; None when observe mode was not used."""
    context = Path(context)
    directory = context / "observations"
    if not (directory.exists() or directory.is_symlink()):
        return None
    contract_path = directory / "contract.json"
    if not (contract_path.exists() or contract_path.is_symlink()):
        return None
    try:
        value, errors, _ = _json(_read(contract_path), contract_path)
    except (Problem, OSError, ValueError):
        return None
    if errors or type(value) is not dict or value.get("mode") != "observe":
        return None
    try:
        identity = _identity({
            "task_id": value.get("task_id"), "round": value.get("round"),
            "attempt": value.get("attempt")})
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
        count += 1
        if n > latest_number:
            latest_number = n
            latest_value = value
            latest_path = str(p)
    if latest_number < 0:
        return None
    return {
        "mode": "observe",
        "checkpoint_seconds": value.get("checkpoint_seconds"),
        "sources": value.get("sources"),
        "count": count,
        "latest_checkpoint": latest_number,
        "latest_path": latest_path,
        "latest": latest_value,
        "note": "read-only observation; unverified claim, not progress, test success or acceptance.",
    }
