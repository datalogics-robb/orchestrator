"""Construct the scrubbed environment agents and build commands run in."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

_KEEP = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SHELL", "USER", "LOGNAME")
# Windows equivalents of the above: the profile and temp directories, and the system variables
# without which Windows programs fail (SYSTEMROOT for sockets and crypto, COMSPEC and PATHEXT
# for finding programs). Claude Code needs CLAUDE_CODE_GIT_BASH_PATH when Git Bash is not on PATH.
_KEEP_WINDOWS = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "USERNAME",
    "USERDOMAIN",
    "COMPUTERNAME",
    "CLAUDE_CODE_GIT_BASH_PATH",
)
_DROP_PREFIXES = ("PYTHON", "VIRTUAL_ENV", "PIP_", "CONDA")


VENV_BIN = "Scripts" if sys.platform == "win32" else "bin"
"""The directory of a venv that holds its executables."""
VENV_PYTHON = "python.exe" if sys.platform == "win32" else "python"


def orchestrator_venv_bin() -> Path | None:
    """The bin directory of the venv the orchestrator itself runs from, if any."""
    if sys.prefix != sys.base_prefix:
        return Path(sys.prefix) / VENV_BIN
    return None


def scrubbed_path(base_path: str, *, prepend: list[Path] | None = None) -> str:
    """PATH without the orchestrator's own venv, with the given directories first."""
    own = orchestrator_venv_bin()
    prepend = prepend or []
    parts: list[str] = []
    for p in base_path.split(os.pathsep):
        if not p:
            continue
        if own and Path(p).resolve() == own.resolve():
            continue
        parts.append(p)
    return os.pathsep.join([str(p) for p in prepend] + parts)


def build_env(
    base: Mapping[str, str] | None = None,
    *,
    secrets: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
    prepend_path: list[Path] | None = None,
) -> dict[str, str]:
    """A minimal environment: PATH, HOME, locale, plus exactly what the caller adds.

    `secrets` are the API key variables a role is allowed (one per role in practice);
    `extra` is declared build.env or adapter config-dir variables. Nothing else leaks.
    """
    base = os.environ if base is None else base
    env: dict[str, str] = {}
    keep = _KEEP + _KEEP_WINDOWS if sys.platform == "win32" else _KEEP
    for k in keep:
        if k in base:
            env[k] = base[k]
    env["PATH"] = scrubbed_path(base.get("PATH", "/usr/bin:/bin"), prepend=prepend_path or [])
    for k, v in (extra or {}).items():
        if k.startswith(_DROP_PREFIXES) and k not in ("PIP_CACHE_DIR",):
            continue
        env[k] = v
    for k, v in (secrets or {}).items():
        env[k] = v
    return env


def worktree_venv_bin(worktree: Path) -> Path | None:
    """The mkenv-style venv inside a worktree, if one exists."""
    for candidate in sorted(worktree.glob("python-env-*")):
        b = candidate / VENV_BIN
        if (b / VENV_PYTHON).exists():
            return b
    for name in (".venv", "venv"):
        b = worktree / name / VENV_BIN
        if (b / VENV_PYTHON).exists():
            return b
    return None
