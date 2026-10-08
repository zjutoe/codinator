"""Write-once protocol evidence and path helpers."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


class Problem(RuntimeError):
    pass



def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_bytes(path, data):
    """Publish complete bytes exclusively; the caller validates the destination directory."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def write_json(path, value):
    """Publish a new JSON artifact, refusing to replace an earlier one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


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
