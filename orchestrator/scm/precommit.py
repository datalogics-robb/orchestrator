"""Run the target repository's pre-commit hooks: installed into each worktree, and gate the commit."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from orchestrator.agents.base import run_process, tail

CONFIG_NAME = ".pre-commit-config.yaml"
MODIFIED_MARKER = "files were modified by this hook"


@dataclass
class PreCommitResult:
    ok: bool
    output: str = ""
    autofixed: bool = False
    note: str = ""


def config_present(worktree: Path) -> bool:
    return (worktree / CONFIG_NAME).exists()


def find_executable(env: dict[str, str]) -> str | None:
    """pre-commit from the environment the build runs in: the worktree venv first, then the system."""
    return shutil.which("pre-commit", path=env.get("PATH"))


async def install_hooks(worktree: Path, env: dict[str, str], log_dir: Path) -> PreCommitResult:
    """`pre-commit install` so an agent's own `git commit` runs the hooks. Missing tool is a note, not an error."""
    exe = find_executable(env)
    if exe is None:
        return PreCommitResult(False, note="pre-commit is not on the build PATH; hooks not installed")
    log_dir.mkdir(parents=True, exist_ok=True)
    outcome = await run_process(
        [exe, "install", "--overwrite"],
        cwd=worktree,
        env=env,
        timeout=300,
        stdout_path=log_dir / "pre-commit-install.log",
    )
    ok = outcome.exit_code == 0 and not outcome.timed_out
    return PreCommitResult(
        ok, tail(outcome.stdout + outcome.stderr, 10), note="" if ok else "pre-commit install failed"
    )


async def run_on_files(
    worktree: Path, files: list[str], env: dict[str, str], log_path: Path
) -> PreCommitResult:
    """Run the hooks on the given files, once more after hooks that rewrite files in place."""
    exe = find_executable(env)
    if exe is None:
        return PreCommitResult(
            False,
            output=(
                f"{CONFIG_NAME} is present but no pre-commit executable is on the build PATH. Add pre-commit "
                "to the target's requirements (mkenv installs it into the worktree venv) or set commit.pre_commit: false."
            ),
        )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [exe, "run", "--files", *files]
    first = await run_process(argv, cwd=worktree, env=env, timeout=1800)
    output = first.stdout + first.stderr
    if first.timed_out:
        log_path.write_text(output)
        return PreCommitResult(False, output="pre-commit timed out after 30 minutes\n" + tail(output))
    if first.exit_code == 0:
        log_path.write_text(output)
        return PreCommitResult(True, tail(output))
    autofixed = MODIFIED_MARKER in output
    if not autofixed:
        log_path.write_text(output)
        return PreCommitResult(False, tail(output, 80))
    second = await run_process(argv, cwd=worktree, env=env, timeout=1800)
    output2 = second.stdout + second.stderr
    log_path.write_text(output + "\n--- second pass after auto-fixes ---\n" + output2)
    ok = second.exit_code == 0 and not second.timed_out
    return PreCommitResult(ok, tail(output2, 80), autofixed=True)
