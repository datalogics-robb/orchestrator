"""Run declared build and test commands with timeouts, logs, and a global build semaphore."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.agents.base import run_process


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    log_path: Path

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class StepResult:
    name: str
    results: list[CommandResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def summary(self) -> str:
        lines = []
        for r in self.results:
            status = "timed out" if r.timed_out else f"exit {r.exit_code}"
            lines.append(f"- `{' '.join(r.argv)}`: {status} (log: {r.log_path.name})")
        return "\n".join(lines) or "- nothing to run"

    def failure_excerpt(self, lines: int = 60) -> str:
        parts = []
        for r in self.results:
            if not r.ok:
                text = r.log_path.read_text(errors="replace") if r.log_path.exists() else ""
                parts.append(f"$ {' '.join(r.argv)}\n" + "\n".join(text.splitlines()[-lines:]))
        return "\n\n".join(parts)


class BuildSemaphore:
    """One per process; bounds concurrent builds across all tasks."""

    _instance: asyncio.Semaphore | None = None
    _size: int = 1

    @classmethod
    def configure(cls, size: int) -> None:
        cls._size = max(1, size)
        cls._instance = None

    @classmethod
    def get(cls) -> asyncio.Semaphore:
        if cls._instance is None:
            cls._instance = asyncio.Semaphore(cls._size)
        return cls._instance


async def run_step(
    name: str,
    commands: list[list[str]],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    log_dir: Path,
    serialize: bool = False,
) -> StepResult:
    """Run commands in order, stopping at the first failure."""
    step = StepResult(name)
    log_dir.mkdir(parents=True, exist_ok=True)
    sem = BuildSemaphore.get() if serialize else None
    for i, argv in enumerate(commands):
        log_path = log_dir / f"{name}-{i:02d}.log"
        if sem:
            async with sem:
                outcome = await run_process(argv, cwd=cwd, env=env, timeout=timeout_seconds)
        else:
            outcome = await run_process(argv, cwd=cwd, env=env, timeout=timeout_seconds)
        log_path.write_text(
            outcome.stdout + ("\n--- stderr ---\n" + outcome.stderr if outcome.stderr else "")
        )
        step.results.append(CommandResult(argv, outcome.exit_code, outcome.timed_out, log_path))
        if not step.results[-1].ok:
            break
    return step
