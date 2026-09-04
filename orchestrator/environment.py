"""Construct the scrubbed environment agents and build commands run in."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

_KEEP = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SHELL", "USER", "LOGNAME")
_DROP_PREFIXES = ("PYTHON", "VIRTUAL_ENV", "PIP_", "CONDA")


def orchestrator_venv_bin() -> Path | None:
    """The bin directory of the venv the orchestrator itself runs from, if any."""
    if sys.prefix != sys.base_prefix:
        return Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
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
    for k in _KEEP:
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
        b = candidate / "bin"
        if (b / "python").exists():
            return b
    for name in (".venv", "venv"):
        b = worktree / name / "bin"
        if (b / "python").exists():
            return b
    return None
