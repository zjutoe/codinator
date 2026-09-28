"""Linux filesystem boundary. No automatic unsandboxed fallback."""
import os
from pathlib import Path
import shutil
import stat

from .files import Problem, under


class Sandbox:
    def __init__(self, command="bwrap"):
        self.command = shutil.which(command)
        if not self.command:
            raise Problem("bubblewrap is required (bwrap not found)")

    def wrap(self, argv, workspace, allowed=(), writable=(), readonly=()):
        root = Path(workspace)
        command = [self.command, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
                   "--unshare-uts", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        for path in readonly:
            path = Path(path).resolve()
            command += ['--ro-bind', str(path), str(path)]
        # Read-only root also protects controller DB, old evidence and Git metadata.
        # Parent directories needed for atomic file replacement are writable; frozen
        # existing siblings are over-mounted read-only. New out-of-scope entries
        # are rejected by the post-execution snapshot gate before any review.
        if allowed:
            # A writable hardlink would also modify a read-only alias of its inode.
            for relative in allowed:
                target = root / relative.rstrip('/')
                entries = [target, *target.rglob('*')] if target.is_dir() and not target.is_symlink() else [target]
                for entry in entries:
                    if entry.exists():
                        info = entry.lstat()
                        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                            raise Problem(f'Writable hardlink forbidden: {entry}')
            if str(root).startswith("/tmp/"):
                command += ["--dir", str(root)]
            command += ["--bind", str(root), str(root)]
            def protect(directory):
                for entry in sorted(directory.iterdir()):
                    rel = entry.relative_to(root).as_posix()
                    if rel == ".git":
                        command.extend(["--ro-bind", str(entry), str(entry)])
                    elif under(rel, allowed):
                        if entry.is_symlink():
                            raise Problem(f"Writable symlink forbidden: {rel}")
                    elif entry.is_dir() and not entry.is_symlink() and any(p.startswith(rel + "/") for p in allowed):
                        protect(entry)
                    else:
                        command.extend(["--ro-bind", str(entry), str(entry)])
            protect(root)
        elif str(root).startswith("/tmp/"):
            command += ["--ro-bind", str(root), str(root)]
        for path in writable:
            path = Path(path).resolve()
            command += ["--bind", str(path), str(path)]
        command += ["--chdir", str(root), "--", *argv]
        return command


def pi_home(destination):
    """Private writable settings/session copy; never put credential contents in evidence."""
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    original = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
    for name in ("settings.json", "models.json", "auth.json"):
        source = original / name
        if source.is_file():
            shutil.copyfile(source, destination / name)
            (destination / name).chmod(0o600)
    return destination


def codex_home(destination):
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'
    if source.is_file():
        shutil.copyfile(source, destination / 'auth.json')
        (destination / 'auth.json').chmod(0o600)
    return destination
