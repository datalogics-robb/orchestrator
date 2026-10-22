"""Resolve a role's share grants for the current platform and stage the copy helper."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from orchestrator.agents.base import PathGrant, Problem
from orchestrator.config.schema import Config, Role
from orchestrator.shares import cp


class ShareError(Exception):
    pass


def grants_for(cfg: Config, role: Role, platform: str = sys.platform) -> tuple[PathGrant, ...]:
    out: list[PathGrant] = []
    for name, mode in cfg.agents.role(role).shares.items():
        share = cfg.shares[name]
        path = share.path_for(platform)
        if path is None:
            raise ShareError(f"share '{name}' has no path for platform {platform}")
        write_under = tuple(path / sub for sub in share.write_under) if mode == "read-write" else ()
        out.append(PathGrant(name=name, path=path, mode=mode, write_under=write_under))
    return tuple(out)


def grants_manifest(grants: tuple[PathGrant, ...]) -> list[dict[str, object]]:
    return [
        {
            "name": g.name,
            "path": str(g.path),
            "mode": g.mode,
            "write_under": [str(p) for p in g.write_under],
        }
        for g in grants
    ]


def stage_helper(
    run_dir: Path, grants: tuple[PathGrant, ...], audit_path: Path
) -> tuple[Path, dict[str, str]]:
    """Write the grants manifest and a PATH shim for orchestrator-cp.

    The shim is a shell script, which Git Bash also runs on Windows; Windows gets an
    `orchestrator-cp.cmd` beside it for agents whose commands run in cmd or PowerShell.
    Returns (bin_dir to prepend to PATH, env vars the helper needs).
    """
    bin_dir = run_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "grants.json"
    manifest.write_text(json.dumps(grants_manifest(grants), indent=2), encoding="utf-8")
    python = Path(sys.executable)
    script = Path(cp.__file__).resolve()
    shim = bin_dir / "orchestrator-cp"
    shim.write_text(f'#!/bin/sh\nexec "{python.as_posix()}" "{script.as_posix()}" "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if sys.platform == "win32":
        (bin_dir / "orchestrator-cp.cmd").write_text(f'@"{python}" "{script}" %*\r\n', encoding="utf-8")
    return bin_dir, {
        cp.GRANTS_ENV: str(manifest),
        cp.AUDIT_ENV: str(audit_path),
    }


def check_shares(cfg: Config, platform: str = sys.platform) -> list[Problem]:
    """doctor: mounts exist, source readable, destination write roots writable."""
    problems: list[Problem] = []
    for name, share in cfg.shares.items():
        path = share.path_for(platform)
        if path is None:
            problems.append(Problem("error", f"share {name}: no path configured for {platform}"))
            continue
        if not path.is_dir():
            problems.append(Problem("error", f"share {name}: {path} is not mounted or not a directory"))
            continue
        if not os.access(path, os.R_OK):
            problems.append(Problem("error", f"share {name}: {path} is not readable"))
        writable = os.access(path, os.W_OK)
        if share.expect_read_only_mount and writable:
            problems.append(
                Problem(
                    "warning", f"share {name}: {path} is mounted writable; a read-only mount is recommended"
                )
            )
        for sub in share.write_under:
            root = path / sub
            if not root.is_dir():
                problems.append(Problem("error", f"share {name}: write root {root} does not exist"))
                continue
            probe = root / ".orchestrator-probe"
            try:
                probe.write_text("probe", encoding="utf-8")
                probe.unlink()
            except OSError as e:
                problems.append(Problem("error", f"share {name}: cannot write under {root}: {e}"))
    return problems
