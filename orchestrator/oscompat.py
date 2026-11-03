"""Where POSIX and Windows differ: finding programs, process groups, killing a tree, and file locks.

The rest of the package is written once and calls here where the platforms disagree. Branches test
`sys.platform` directly so mypy checks each one under its own `--platform`.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

_CMD_META = re.compile(r'[\r\n"%!^&|<>()]')
"""Characters cmd.exe interprets inside a batch file's arguments, quoted or not."""

_NPM_SHIM_TARGET = re.compile(r'"%(?:~dp0|dp0%)\\([^"]+)"\s+%\*', re.IGNORECASE)
"""The target an npm cmd-shim runs: `"%dp0%\\node_modules\\pkg\\cli.js" %*` (`%~dp0` in old shims)."""

_LOCK_OFFSET = 1 << 30
"""Windows locks a byte range, and a locked range cannot be read by anyone else; the lock is
placed far past the data so other processes can still read a locked pidfile."""


class BatchArgumentError(OSError):
    """A batch file would be run with an argument cmd.exe would rewrite or execute."""


def process_group_kwargs() -> dict[str, Any]:
    """Popen arguments that start the child as the leader of its own process group.

    The terminal's Ctrl+C then reaches only the orchestrator, which stops its children itself,
    and `kill_tree` can reach the child's descendants.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached_kwargs() -> dict[str, Any]:
    """Popen arguments for a process that outlives the terminal that started it.

    On Windows the child gets its own hidden console rather than none, so the console programs it
    starts in turn inherit that console instead of each opening a window.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(pid: int, *, force: bool) -> None:
    """Ask a process group started with `process_group_kwargs` to stop, or force it.

    POSIX sends SIGTERM, or SIGKILL when forced, to the group. Windows always forces, with
    `taskkill /T /F`: it finds descendants through their parent, so it has to run while the
    child is still alive, and a console program cannot be asked to exit by anything that also
    reaches its descendants.
    """
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/T", "/F", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return


def resolve_argv(argv: list[str], *, path: str | None, cwd: Path) -> list[str]:
    """The argv to start, with the program found the way POSIX exec would find it.

    POSIX: unchanged; exec already searches the child's PATH and resolves a relative program
    against the child's cwd.

    Windows: CreateProcess searches the parent's PATH, adds only `.exe`, and resolves a relative
    program against the parent's cwd. So the program is looked up here on the child's PATH with
    PATHEXT. An npm `.cmd` shim is replaced by the command it wraps, because cmd.exe re-parses
    every argument of a batch file; any other batch file runs only if no argument holds a
    character cmd.exe interprets (`BatchArgumentError` otherwise).
    """
    if sys.platform != "win32" or not argv:
        return argv
    return _resolve_windows(argv, path=path, cwd=cwd)


def _resolve_windows(argv: list[str], *, path: str | None, cwd: Path) -> list[str]:
    program = argv[0]
    if ("/" in program or "\\" in program) and not Path(program).is_absolute():
        program = str(cwd / program)
    found = shutil.which(program, path=path)
    if found is None:
        return argv  # CreateProcess reports the missing program
    if Path(found).suffix.lower() not in (".cmd", ".bat"):
        return [found, *argv[1:]]
    wrapped = unwrap_npm_shim(Path(found), path=path)
    if wrapped:
        return [*wrapped, *argv[1:]]
    unsafe = [a for a in argv[1:] if _CMD_META.search(a)]
    if unsafe:
        raise BatchArgumentError(
            f"{found} is a batch file, and cmd.exe would reinterpret the argument {unsafe[0][:60]!r}; "
            "install the program as an .exe or npm package, or put an .exe wrapper first on PATH"
        )
    return [found, *argv[1:]]


def unwrap_npm_shim(shim: Path, *, path: str | None) -> list[str] | None:
    """The command an npm cmd-shim runs, without cmd.exe: `[node, script]` or `[program.exe]`.

    None when the file is not an npm shim, its target is missing, or node cannot be found.
    """
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _NPM_SHIM_TARGET.search(text)
    if not m:
        return None
    target = shim.parent.joinpath(*re.split(r"[\\/]", m.group(1)))
    if not target.is_file():
        return None
    suffix = target.suffix.lower()
    if suffix == ".exe":
        return [str(target)]
    if suffix not in (".js", ".cjs", ".mjs"):
        return None
    bundled = shim.parent / "node.exe"
    node = str(bundled) if bundled.is_file() else shutil.which("node", path=path)
    return [node, str(target)] if node else None


def try_lock(fd: int) -> bool:
    """Take an exclusive, non-blocking lock on an open file; False when another process holds it.

    The lock lasts until `unlock` or until the process exits.
    """
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def replace_contents(fd: int, data: bytes) -> None:
    """Overwrite an open file with `data` (Windows has no pwrite)."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)
