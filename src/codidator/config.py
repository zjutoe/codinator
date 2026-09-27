import json
from pathlib import Path, PurePosixPath
import re

from .files import Problem, under

DEFAULT_EXCLUDES = [".venv/", ".pytest_cache/"]


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise Problem(f"{name} must be a positive integer")
    return value


def relative(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise Problem("Paths must be nonempty strings")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in (".", "..") for x in value.rstrip("/").split("/")):
        raise Problem(f"Path must be relative without dot components: {value}")
    if p.parts[0] in (".git", ".codex", ".agents") or any(c in value for c in "*?[]{}"):
        raise Problem(f"Protected path or unsupported wildcard: {value}")
    return value


def load_manifest(path):
    raw = json.loads(Path(path).read_text())
    allowed_keys = {"version", "id", "workspace", "handoff", "allowed_paths", "checks", "excludes",
                    "max_rounds", "max_seconds", "attempt_seconds", "notify_thread"}
    if type(raw) is not dict or set(raw) - allowed_keys:
        raise Problem("Unknown manifest field(s)")
    if raw.get("version") != 1 or type(raw.get("version")) is not int:
        raise Problem("Manifest version must be 1")
    if not isinstance(raw.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", raw["id"]):
        raise Problem("Invalid task id")
    root = Path(raw["workspace"]).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise Problem("workspace must be a directory")
    raw["workspace"] = str(root)
    raw["handoff"] = relative(raw["handoff"])
    handoff = root / raw["handoff"]
    if not handoff.is_file() or handoff.resolve() != handoff.absolute():
        raise Problem("handoff must be an existing file without symlink components")
    for key in ("allowed_paths", "checks"):
        if not isinstance(raw.get(key), list) or not raw[key]:
            raise Problem(f"{key} must be a nonempty array")
    raw["allowed_paths"] = [relative(p) for p in raw["allowed_paths"]]
    if under(raw["handoff"], raw["allowed_paths"]):
        raise Problem("The published handoff is immutable; use delivery/summary.md for execution records")
    for p in raw["allowed_paths"]:
        target = root / p.rstrip("/")
        if target.resolve() != target.absolute():
            raise Problem(f"Writable path may not traverse a symlink: {p}")
        if target.is_dir() and not p.endswith("/"):
            raise Problem(f"Directory allowlists need trailing '/': {p}")
    raw.setdefault("excludes", DEFAULT_EXCLUDES.copy())
    if not isinstance(raw["excludes"], list):
        raise Problem("excludes must be an array")
    raw["excludes"] = [relative(p) for p in raw["excludes"]]
    if under(raw["handoff"], raw["excludes"]):
        raise Problem("Cannot exclude the handoff")
    for p in raw["allowed_paths"]:
        if under(p.rstrip("/"), raw["excludes"]) or any(under(e.rstrip('/'), [p]) for e in raw['excludes']):
            raise Problem("Allowed paths cannot be excluded from snapshots")
    names = set()
    for check in raw["checks"]:
        if type(check) is not dict or set(check) - {"name", "argv", "timeout_seconds"}:
            raise Problem("Unknown check field")
        name = check.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or name in names:
            raise Problem("Check names must be unique simple identifiers")
        names.add(name)
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(s, str) or not s or "\0" in s for s in argv):
            raise Problem("Check argv must be a nonempty array of nonempty strings, never a shell expression")
        check["timeout_seconds"] = positive(check.get("timeout_seconds", 120), "check timeout")
    for key, default in (("max_rounds", 4), ("max_seconds", 14400), ("attempt_seconds", 7200)):
        raw[key] = positive(raw.get(key, default), key)
    if raw.get("notify_thread") is not None and (not isinstance(raw["notify_thread"], str) or not raw["notify_thread"].strip()):
        raise Problem("notify_thread must be a nonempty string")
    return raw
