"""Preflight checks for the machine, credentials, adapters, shares, and MCP sources."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from orchestrator.agents.base import Problem
from orchestrator.agents.registry import UnknownRunner, get_runner
from orchestrator.config.loader import SecretError, netrc_login, resolve_secret
from orchestrator.config.schema import Config
from orchestrator.docs.confluence import ConfluenceClient
from orchestrator.mcp.passthrough import check_mcp
from orchestrator.scm.github import GitHubHost
from orchestrator.scm.worktree import check_clone
from orchestrator.shares.grants import check_shares
from orchestrator.trackers.jira import JiraTracker


@dataclass
class Check:
    area: str
    status: str  # ok | warn | fail
    detail: str


def _p(area: str, problems: list[Problem]) -> list[Check]:
    return [Check(area, "fail" if p.level == "error" else "warn", p.message) for p in problems]


def check_interpreter(repo_root: Path) -> list[Check]:
    out = []
    v = sys.version_info
    if v < (3, 13):
        out.append(Check("python", "fail", f"Python {v.major}.{v.minor} is older than 3.13"))
    else:
        out.append(Check("python", "ok", f"Python {v.major}.{v.minor}.{v.micro} at {sys.executable}"))
    prefix = Path(sys.prefix)
    if prefix.name.startswith("python-env-") and prefix.parent == repo_root:
        out.append(Check("venv", "ok", f"running from the orchestrator's mkenv venv {prefix.name}"))
    elif sys.prefix != sys.base_prefix:
        out.append(
            Check("venv", "warn", f"running from {prefix}, not the repo's python-env-* created by mkenv")
        )
    else:
        out.append(
            Check(
                "venv",
                "warn",
                "not running in a virtual environment; run python mkenv.py and use bin/orchestrator",
            )
        )
    return out


def check_tools() -> list[Check]:
    out = []
    for tool in ("git", "gh"):
        path = shutil.which(tool)
        out.append(Check("tools", "ok" if path else "fail", f"{tool}: {path or 'not found on PATH'}"))
    return out


def check_secrets(cfg: Config) -> list[Check]:
    out = []
    refs = {
        "tracker": cfg.tracker.auth,
        "repo": cfg.repo.auth,
        "agents.worker": cfg.agents.worker.auth,
        "agents.reviewer": cfg.agents.reviewer.auth,
    }
    if cfg.confluence:
        refs["confluence"] = cfg.confluence.auth
    for name, ref in refs.items():
        try:
            resolve_secret(ref)
            where = f"env {ref.token_env}" if ref.token_env else f"netrc {ref.netrc_machine}"
            out.append(Check("secrets", "ok", f"{name}: resolved from {where}"))
        except SecretError as e:
            out.append(Check("secrets", "fail", f"{name}: {e}"))
    if cfg.tracker.auth.netrc_machine and not netrc_login(cfg.tracker.auth.netrc_machine):
        out.append(
            Check(
                "secrets",
                "warn",
                "tracker: netrc entry has no login; Jira Cloud API tokens need the account email",
            )
        )
    return out


def check_adapters(cfg: Config) -> list[Check]:
    out = []
    for role_name in ("worker", "reviewer"):
        role = cfg.agents.role(role_name)  # type: ignore[arg-type]
        try:
            runner = get_runner(role.runner)
        except UnknownRunner as e:
            out.append(Check(f"agents.{role_name}", "fail", str(e)))
            continue
        problems = runner.preflight(role)
        caps = runner.capabilities
        native = [
            n
            for n, on in (
                ("turns", caps.turn_cap),
                ("budget", caps.budget_cap),
                ("schema", caps.structured_output),
                ("resume", caps.session_resume),
            )
            if on
        ]
        out.append(
            Check(
                f"agents.{role_name}",
                "ok",
                f"{role.runner} ({role.model or 'default model'}), access {role.access}; native: {', '.join(native) or 'none'}",
            )
        )
        out.extend(_p(f"agents.{role_name}", problems))
    return out


def check_paths(cfg: Config) -> list[Check]:
    out = []
    for label, path in (
        ("repo.clone_path", cfg.repo.clone_path),
        ("repo.worktree_root", cfg.repo.worktree_root),
        ("state_dir", cfg.resolved_state_dir),
    ):
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent
        if label == "repo.clone_path" and not path.exists():
            out.append(Check("paths", "fail", f"{label} {path} does not exist"))
        elif os.access(target, os.W_OK) or label == "repo.clone_path":
            out.append(Check("paths", "ok", f"{label} {path}"))
        else:
            out.append(Check("paths", "fail", f"{label} {path} is not writable"))
    return out


def check_commands(cfg: Config) -> list[Check]:
    out = []
    seen: set[str] = set()
    all_cmds = (
        list(cfg.build.setup)
        + list(cfg.build.commands)
        + list(cfg.test.full_suite)
        + list(cfg.test.selection.fallback)
        + list(cfg.test.selection.commands)
    )
    for cmds in cfg.test.selection.map.values():
        all_cmds.extend(cmds)
    for argv in all_cmds:
        if not argv or argv[0] in seen:
            continue
        seen.add(argv[0])
        if argv[0] in ("python", "python3", "pytest") or shutil.which(argv[0]):
            out.append(Check("commands", "ok", f"{argv[0]} found"))
        else:
            out.append(Check("commands", "warn", f"{argv[0]} not on PATH (may come from the worktree venv)"))
    return out


async def check_remote(cfg: Config) -> list[Check]:
    out = []
    for msg in await check_clone(cfg.repo):
        out.append(Check("repo", "fail", msg))
    if not out:
        out.append(
            Check(
                "repo",
                "ok",
                f"{cfg.repo.github} clone at {cfg.repo.clone_path}, base {cfg.repo.base_branch} reachable",
            )
        )
    try:
        token = resolve_secret(cfg.repo.auth)
        host = GitHubHost(cfg.repo, token, cfg.repo.clone_path)
        problems = await host.check()
        out.extend(Check("github", "fail", p) for p in problems)
        if not problems:
            out.append(Check("github", "ok", "gh authenticated and repository visible"))
    except SecretError:
        pass
    try:
        jira_token = resolve_secret(cfg.tracker.auth)
        login = (
            netrc_login(cfg.tracker.auth.netrc_machine)
            if cfg.tracker.auth.netrc_machine
            else os.environ.get("JIRA_USER_EMAIL")
        )
        jira = JiraTracker(cfg.tracker, login=login, token=jira_token)
        try:
            problems = await jira.check()
        finally:
            await jira.aclose()
        out.extend(Check("jira", "fail", p) for p in problems)
        if not problems:
            out.append(Check("jira", "ok", f"{cfg.tracker.base_url} project {cfg.tracker.project}"))
    except SecretError:
        pass
    if cfg.confluence:
        try:
            c_token = resolve_secret(cfg.confluence.auth)
            login = (
                netrc_login(cfg.confluence.auth.netrc_machine)
                if cfg.confluence.auth.netrc_machine
                else os.environ.get("JIRA_USER_EMAIL")
            )
            client = ConfluenceClient(cfg.confluence, login=login, token=c_token)
            try:
                problems = await client.check()
            finally:
                await client.aclose()
            out.extend(Check("confluence", "fail", p) for p in problems)
            if not problems:
                out.append(
                    Check(
                        "confluence",
                        "ok",
                        f"{len(cfg.confluence.context_pages)} context page(s)"
                        + (", publishing enabled" if cfg.confluence.publish else ""),
                    )
                )
        except SecretError:
            pass
    return out


async def run_doctor(cfg: Config, repo_root: Path, *, online: bool = True) -> list[Check]:
    checks: list[Check] = []
    checks += check_interpreter(repo_root)
    checks += check_tools()
    checks += check_secrets(cfg)
    checks += check_adapters(cfg)
    checks += check_paths(cfg)
    checks += check_commands(cfg)
    checks += _p("shares", check_shares(cfg))
    checks += _p("mcp", check_mcp(cfg, cfg.repo.clone_path))
    if not any(c.status == "fail" and c.area in ("shares",) for c in checks) and cfg.shares:
        checks.append(
            Check("shares", "ok", ", ".join(f"{n} -> {s.path_for()}" for n, s in cfg.shares.items()))
        )
    if not any(c.area == "mcp" for c in checks):
        servers = sorted({s for r in ("worker", "reviewer") for s in cfg.agents.role(r).mcp_servers})  # type: ignore[arg-type]
        checks.append(Check("mcp", "ok", f"servers allowlisted: {', '.join(servers) or 'none'}"))
    if online:
        checks += await check_remote(cfg)
    return checks


def doctor_sync(cfg: Config, repo_root: Path, online: bool = True) -> list[Check]:
    return asyncio.run(run_doctor(cfg, repo_root, online=online))
