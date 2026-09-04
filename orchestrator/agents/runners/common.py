"""Helpers shared by the subprocess adapters."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from orchestrator.agents.base import Access, AgentRequest, Problem
from orchestrator.config.schema import RoleConfig

# Bash fragments that write to disk; used for best-effort read-only enforcement in hooks.
WRITE_PATTERNS = [
    r"(^|[^>])>(?!>)",
    r">>",
    r"\brm\b",
    r"\bmv\b",
    r"\bcp\b",
    r"\btouch\b",
    r"\bmkdir\b",
    r"\btee\b",
    r"\bsed\s+-i",
    r"\bgit\s+(commit|checkout|reset|rebase|merge|stash|clean|add|rm|mv)\b",
    r"\bpip\s+install\b",
    r"\bnpm\s+(install|i)\b",
]

# Commands no role may run; the orchestrator does pushing and PR creation itself.
DEFAULT_DENY_COMMANDS = ["git push", "gh ", "git remote set-url", "curl ", "wget "]


def which(binary: str) -> str | None:
    return shutil.which(binary)


def version_of(binary: str, args: list[str] | None = None) -> str | None:
    path = which(binary)
    if not path:
        return None
    try:
        out = subprocess.run([path] + (args or ["--version"]), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr).strip()
    m = re.search(r"\d+\.\d+(\.\d+)?", text)
    return m.group(0) if m else text[:40]


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


def binary_problems(binary: str, minimum: str, role: RoleConfig) -> list[Problem]:
    problems: list[Problem] = []
    path = which(binary)
    if not path:
        return [Problem("error", f"{role.runner}: '{binary}' is not on PATH")]
    ver = version_of(binary)
    if ver is None:
        problems.append(Problem("warning", f"{role.runner}: could not read '{binary}' version"))
    elif parse_version(ver) < parse_version(minimum):
        problems.append(
            Problem("error", f"{role.runner}: {binary} {ver} is older than the minimum {minimum}")
        )
    return problems


def soft_limit_warnings(role: RoleConfig, *, turn_cap: bool, budget_cap: bool) -> list[Problem]:
    out: list[Problem] = []
    if role.max_turns is not None and not turn_cap:
        out.append(
            Problem(
                "warning",
                f"{role.runner}: max_turns is not enforced natively; only the timeout bounds the run",
            )
        )
    if role.max_budget_usd is not None and not budget_cap:
        out.append(
            Problem(
                "warning",
                f"{role.runner}: max_budget_usd is checked from the usage report after the run, not enforced during it",
            )
        )
    return out


def read_only_paths(access: Access) -> list[str]:
    return [str(g.path) for g in access.grants if g.mode == "read"]


def write_roots(access: Access, cwd: Path) -> list[str]:
    roots = [str(cwd)]
    for g in access.grants:
        if g.mode == "read-write":
            roots.extend(str(p) for p in (g.write_under or (g.path,)))
    return roots


def config_home(request: AgentRequest, name: str) -> Path:
    home = request.run_dir / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def hook_rules(request: AgentRequest) -> dict[str, object]:
    """Rules consumed by the standalone hook scripts."""
    return {
        "deny_commands": request.deny_commands or DEFAULT_DENY_COMMANDS,
        "read_only_worktree": request.access.worktree == "read-only",
        "read_only_paths": read_only_paths(request.access),
        "write_roots": write_roots(request.access, request.cwd),
        "write_patterns": WRITE_PATTERNS,
    }
