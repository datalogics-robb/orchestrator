"""The runtime-independent agent interface every adapter implements."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from orchestrator.config.schema import Role, RoleConfig, ShareMode, WorktreeAccess

Termination = Literal["completed", "max_turns", "max_budget", "timeout", "schema", "error", "killed"]


@dataclass(frozen=True)
class Capabilities:
    structured_output: bool
    session_resume: bool
    turn_cap: bool
    budget_cap: bool
    usage_report: bool
    read_only_mode: bool
    command_deny_hooks: bool
    config_dir_isolation: bool


@dataclass(frozen=True)
class PathGrant:
    name: str
    path: Path
    mode: ShareMode
    write_under: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Access:
    worktree: WorktreeAccess
    grants: tuple[PathGrant, ...] = ()

    @property
    def writable_paths(self) -> list[Path]:
        return [g.path for g in self.grants if g.mode == "read-write"]

    @property
    def readable_paths(self) -> list[Path]:
        return [g.path for g in self.grants]


@dataclass(frozen=True)
class Limits:
    timeout_seconds: int
    max_turns: int | None = None
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class Problem:
    level: Literal["error", "warning"]
    message: str


@dataclass
class AgentRequest:
    cwd: Path
    prompt: str
    role: Role
    schema: dict[str, Any]
    limits: Limits
    access: Access
    env: dict[str, str]
    run_dir: Path
    """Per-role, per-task directory for generated files, logs, and config homes."""
    model: str | None = None
    session: str | None = None
    system_prompt: str | None = None
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Normalized MCP definitions: name -> {"type": "stdio"|"http", ...}."""
    deny_tools: dict[str, list[str]] = field(default_factory=dict)
    deny_commands: list[str] = field(default_factory=list)
    """Shell command prefixes the role must not run, e.g. ["git push", "gh"]."""
    options: dict[str, Any] = field(default_factory=dict)
    prompt_and_parse: bool = False
    """Set by the pipeline when the adapter lacks native structured output."""
    cli_login: bool = False
    """Use the runtime's own stored login instead of an API key in env."""


@dataclass
class AgentResult:
    ok: bool
    termination: Termination
    raw_text: str = ""
    structured_output: dict[str, Any] | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    stderr_tail: str = ""
    error: str | None = None


@runtime_checkable
class AgentRunner(Protocol):
    name: str
    capabilities: Capabilities

    def preflight(self, role: RoleConfig) -> list[Problem]: ...

    async def run(self, request: AgentRequest) -> AgentResult: ...


# ---------------------------------------------------------------------------
# Shared machinery for subprocess-based adapters


@dataclass
class ProcessOutcome:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


async def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    stdin_text: str | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> ProcessOutcome:
    """Run a command in its own process group; kill the whole group on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(stdin_text.encode() if stdin_text is not None else None),
            timeout=timeout,
        )
    except TimeoutError:
        timed_out = True
        out_b, err_b = await _terminate(proc)
    except asyncio.CancelledError:
        # the caller is being cancelled: the child must not outlive the orchestrator
        await _terminate(proc)
        raise
    stdout = out_b.decode(errors="replace")
    stderr = err_b.decode(errors="replace")
    if stdout_path:
        stdout_path.write_text(stdout)
    if stderr_path:
        stderr_path.write_text(stderr)
    return ProcessOutcome(proc.returncode, stdout, stderr, timed_out)


async def _terminate(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """SIGTERM the process group, escalate to SIGKILL, and reap; returns whatever output remains."""
    _kill_group(proc.pid, signal.SIGTERM)
    try:
        return await asyncio.shield(asyncio.wait_for(proc.communicate(), timeout=10))
    except (TimeoutError, asyncio.CancelledError):
        pass
    _kill_group(proc.pid, signal.SIGKILL)
    try:
        await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=5))
    except (TimeoutError, asyncio.CancelledError):
        pass
    return b"", b""


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Return the last JSON object in text: a fenced block first, then a bare object."""
    candidates = _FENCE.findall(text)
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    # last resort: the outermost braces
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-lines:])


def schema_instructions(schema: dict[str, Any]) -> str:
    """Prompt text for adapters without native structured output."""
    return (
        "\n\n## Output format\n\n"
        "Your final message must be a single JSON object and nothing else, inside a ```json "
        "fence, matching this JSON Schema exactly:\n\n```json\n" + json.dumps(schema, indent=2) + "\n```\n"
    )
