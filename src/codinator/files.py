"""Content snapshots and a write-once evidence boundary."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import shutil
import tempfile


class Problem(RuntimeError):
    pass


class ScopeViolation(Problem):
    """A definite refusal before any submission is accepted."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    """Publish a new artifact atomically, refusing to replace an earlier one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".pending")
    with temp.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temp, path)
    finally:
        temp.unlink()
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_info(path):
    s = path.lstat()
    mode = stat.S_IMODE(s.st_mode)
    if path.is_symlink():
        return {"kind": "symlink", "mode": mode, "target": os.readlink(path)}
    if not stat.S_ISREG(s.st_mode):
        raise Problem(f"Unsupported workspace entry: {path}")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return {"kind": "file", "mode": mode, "sha256": h.hexdigest(), "size": s.st_size}


def under(name, patterns):
    return any(name == p.rstrip("/") or (p.endswith("/") and name.startswith(p)) for p in patterns)


def git_info(root):
    def git(*args, required=True):
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=10)
        if required and p.returncode:
            raise Problem(p.stderr.decode(errors="replace").strip())
        return p.stdout.decode().strip() if p.returncode == 0 else None
    top = git("rev-parse", "--show-toplevel")
    if Path(top).resolve() != root:
        raise Problem("workspace must be the Git repository root")
    return {"head": git("rev-parse", "--verify", "HEAD", required=False),
            "branch": git("symbolic-ref", "--short", "HEAD", required=False),
            "index": git("ls-files", "--stage", "-z")}


def snapshot(root, excludes):
    """Includes untracked/ignored files except explicit exclusions, no symlink traversal."""
    root = Path(root)
    git = git_info(root)
    tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], timeout=10).decode().split("\0")
    for path in tracked:
        if path and under(path, excludes):
            raise Problem(f"Exclusions may not hide tracked files: {path}")
    entries = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        rel = Path(current).relative_to(root)
        for name in list(dirs):
            p = Path(current) / name
            key = (rel / name).as_posix()
            if key == ".git":
                dirs.remove(name)
            elif under(key, excludes):
                entries[key] = {'kind': 'excluded', 'mode': stat.S_IMODE(p.lstat().st_mode)}
                dirs.remove(name)
            elif p.is_symlink():
                entries[key] = file_info(p)
                dirs.remove(name)
            else:
                entries[key] = {'kind': 'directory', 'mode': stat.S_IMODE(p.stat().st_mode)}
        for name in files:
            key = (rel / name).as_posix()
            if key == ".git":
                continue
            if under(key, excludes):
                entries[key] = {'kind': 'excluded', 'mode': stat.S_IMODE((Path(current) / name).lstat().st_mode)}
                continue
            entries[key] = file_info(Path(current) / name)
    return {"git": git, "root_mode": stat.S_IMODE(root.stat().st_mode), "files": dict(sorted(entries.items()))}


def changes(before, after):
    return sorted(p for p in before["files"].keys() | after["files"].keys()
                  if before["files"].get(p) != after["files"].get(p))


def assert_scope(before, after, allowed):
    if before["git"] != after["git"]:
        raise ScopeViolation("Git HEAD/branch/index changed; no commit, branch switch or staging is authorized")
    if before['root_mode'] != after['root_mode']:
        raise ScopeViolation('Workspace root permissions changed')
    def permitted(p):
        if under(p, allowed):
            return True
        return (p not in before['files'] and after['files'].get(p, {}).get('kind') == 'directory'
                and any(a.startswith(p + '/') for a in allowed))
    outside = [p for p in changes(before, after) if not permitted(p)]
    if outside:
        raise ScopeViolation("Changes outside allowed paths: " + ", ".join(outside[:20]))


def preserve(root, snap, blobs):
    """Deduplicate immutable file contents; snapshot JSON retains paths/modes/links."""
    blobs = Path(blobs)
    blobs.mkdir(parents=True, exist_ok=True)
    for name, entry in snap['files'].items():
        if entry['kind'] != 'file':
            continue
        target = blobs / entry['sha256']
        if target.exists():
            if target.is_symlink() or file_info(target).get('sha256') != entry['sha256']:
                raise Problem(f'Corrupted existing snapshot blob: {entry["sha256"]}')
            continue
        source = Path(root) / name
        fd, tmpname = tempfile.mkstemp(prefix='.pending-', dir=blobs)
        temp = Path(tmpname)
        try:
            with source.open('rb') as src, os.fdopen(fd, 'wb') as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            if file_info(temp)['sha256'] != entry['sha256']:
                raise Problem(f"File changed while snapshotting: {name}")
            os.link(temp, target)
        finally:
            temp.unlink()
        directory = os.open(blobs, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
