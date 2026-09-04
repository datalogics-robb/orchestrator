"""Thin async wrapper over the git CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    pass


@dataclass
class GitResult:
    code: int
    out: str
    err: str


async def git(
    *args: str, cwd: Path, env: dict[str, str] | None = None, check: bool = True, timeout: float = 600
) -> GitResult:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        raise GitError(f"git {' '.join(args)} timed out") from e
    res = GitResult(proc.returncode or 0, out_b.decode(errors="replace"), err_b.decode(errors="replace"))
    if check and res.code != 0:
        raise GitError(f"git {' '.join(args)} failed ({res.code}): {res.err.strip() or res.out.strip()}")
    return res
