import json
from datetime import datetime
from pathlib import Path, PurePosixPath
import re

from .files import Problem, under

DEFAULT_EXCLUDES = [".venv/", ".pytest_cache/"]


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise Problem(f"{name} must be a positive integer")
    return value


def deadline_timestamp(value):
    if (type(value) is not str or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value)):
        raise Problem('deadline_utc must be an ISO8601 timestamp with explicit UTC (Z or +00:00)')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError as exc:
        raise Problem('Invalid deadline_utc calendar timestamp') from exc


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
                    "max_rounds", "max_seconds", "attempt_seconds", "checkpoint_seconds", "deadline_utc",
                    "notify_thread", "integration"}
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
    if 'checkpoint_seconds' in raw:
        if positive(raw['checkpoint_seconds'], 'checkpoint_seconds') >= raw['attempt_seconds']:
            raise Problem('checkpoint_seconds must be less than attempt_seconds')
    if 'deadline_utc' in raw:
        deadline_timestamp(raw['deadline_utc'])
    if raw.get("notify_thread") is not None and (not isinstance(raw["notify_thread"], str) or not raw["notify_thread"].strip()):
        raise Problem("notify_thread must be a nonempty string")
    if "integration" in raw:
        spec = raw["integration"]
        if type(spec) is not dict or set(spec) != {"target_workspace", "target_branch", "base_commit", "planning_paths"}:
            raise Problem("integration requires target_workspace, target_branch, base_commit and planning_paths")
        target = Path(spec["target_workspace"]).expanduser().resolve(strict=True)
        if not target.is_dir() or target == root or target.is_relative_to(root) or root.is_relative_to(target):
            raise Problem("Integration target must be a separate checkout")
        if not isinstance(spec["target_branch"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]*", spec["target_branch"]):
            raise Problem("Invalid integration target branch")
        if not isinstance(spec["base_commit"], str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", spec["base_commit"]):
            raise Problem("Integration requires an exact base commit")
        if not isinstance(spec["planning_paths"], list):
            raise Problem("integration planning_paths must be an array")
        spec["planning_paths"] = [relative(p) for p in spec["planning_paths"]]
        if any(under(p.rstrip("/"), spec["planning_paths"]) or
               any(under(q.rstrip("/"), [p]) for q in spec["planning_paths"]) for p in raw["allowed_paths"]):
            raise Problem("Integration planning paths may not overlap implementation paths")
        spec["target_workspace"] = str(target)
    return raw
