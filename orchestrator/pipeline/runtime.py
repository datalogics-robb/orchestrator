"""Everything a run needs, built once from the config and shared by all tasks."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from orchestrator import agents
from orchestrator.agents.base import AgentRunner, PathGrant
from orchestrator.agents.registry import get_runner
from orchestrator.build.runner import BuildSemaphore
from orchestrator.config.loader import netrc_login, resolve_secret
from orchestrator.config.schema import Config, Role
from orchestrator.docs.confluence import ConfluenceClient
from orchestrator.mcp import passthrough
from orchestrator.reporting.audit import AuditLog, Redactor
from orchestrator.scm.github import GitHubHost
from orchestrator.scm.worktree import WorktreeManager
from orchestrator.shares.grants import grants_for
from orchestrator.state.store import Store
from orchestrator.trackers.base import Tracker
from orchestrator.trackers.jira import JiraTracker

PROMPTS_DIR = Path(agents.__file__).parent / "prompts"


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


@dataclass
class RoleRuntime:
    runner: AgentRunner
    secrets: dict[str, str]
    grants: tuple[PathGrant, ...]
    mcp_servers: dict[str, dict[str, Any]]
    deny_tools: dict[str, list[str]]


@dataclass
class Runtime:
    cfg: Config
    config_path: Path
    run_id: str
    run_dir: Path
    audit: AuditLog
    redactor: Redactor
    store: Store
    tracker: Tracker
    confluence: ConfluenceClient | None
    worktrees: WorktreeManager
    github: GitHubHost | None
    github_token: str | None
    roles: dict[str, RoleRuntime]
    jinja: Environment
    dry_run: bool = False
    keep_worktrees: bool = False
    on_event: Callable[[str, str, str], None] = field(default=lambda key, state, note: None)
    """Progress callback: (key, state, note)."""

    def role(self, name: Role) -> RoleRuntime:
        return self.roles[name]

    def render(self, name: str, **ctx: Any) -> str:
        override = self.cfg.agents.prompt_overrides.get(name.removesuffix(".md"))
        if override:
            template = self.jinja.from_string(Path(override).read_text())
        else:
            template = self.jinja.get_template(name)
        return template.render(**ctx)

    def task_dir(self, key: str) -> Path:
        d = self.run_dir / key
        d.mkdir(parents=True, exist_ok=True)
        return d

    def event(self, key: str, state: str, note: str = "") -> None:
        self.on_event(key, state, note)

    async def aclose(self) -> None:
        close = getattr(self.tracker, "aclose", None)
        if close:
            await close()
        if self.confluence:
            await self.confluence.aclose()


def _role_runtime(cfg: Config, role: Role, redactor: Redactor, repo_root: Path) -> RoleRuntime:
    role_cfg = cfg.agents.role(role)
    runner = get_runner(role_cfg.runner)
    token = resolve_secret(role_cfg.auth)
    redactor.add(token)
    names = {role_cfg.auth.token_env or "API_KEY"}
    expected = getattr(runner, "api_key_var", None)
    if expected:
        names.add(expected)
    secrets = {n: token for n in names}
    servers = passthrough.servers_for_role(cfg, role, repo_root)
    for v in passthrough.secret_values(servers):
        redactor.add(v)
    return RoleRuntime(
        runner=runner,
        secrets=secrets,
        grants=grants_for(cfg, role),
        mcp_servers=servers,
        deny_tools=passthrough.deny_tools_for(cfg, servers),
    )


def build_runtime(
    cfg: Config,
    config_path: Path,
    *,
    run_id: str | None = None,
    dry_run: bool = False,
    keep_worktrees: bool = False,
    tracker: Tracker | None = None,
) -> Runtime:
    run_id = run_id or new_run_id()
    run_dir = cfg.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    redactor = Redactor()
    audit = AuditLog(run_dir / "audit.jsonl", run_id, redactor)
    store = Store(cfg.db_path)
    BuildSemaphore.configure(cfg.build.max_concurrent_builds)

    if tracker is None:
        jira_token = resolve_secret(cfg.tracker.auth)
        redactor.add(jira_token)
        login = (
            netrc_login(cfg.tracker.auth.netrc_machine)
            if cfg.tracker.auth.netrc_machine
            else os.environ.get("JIRA_USER_EMAIL")
        )
        tracker = JiraTracker(cfg.tracker, login=login, token=jira_token)

    confluence = None
    if cfg.confluence:
        c_token = resolve_secret(cfg.confluence.auth)
        redactor.add(c_token)
        c_login = (
            netrc_login(cfg.confluence.auth.netrc_machine)
            if cfg.confluence.auth.netrc_machine
            else os.environ.get("JIRA_USER_EMAIL")
        )
        confluence = ConfluenceClient(cfg.confluence, login=c_login, token=c_token)

    github_token: str | None = None
    github: GitHubHost | None = None
    try:
        github_token = resolve_secret(cfg.repo.auth)
        redactor.add(github_token)
        github = GitHubHost(cfg.repo, github_token, cfg.repo.clone_path)
    except Exception:
        if not dry_run:
            raise

    roles = {r: _role_runtime(cfg, r, redactor, cfg.repo.clone_path) for r in ("worker", "reviewer")}  # type: ignore[misc]

    jinja = Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    return Runtime(
        cfg=cfg,
        config_path=config_path,
        run_id=run_id,
        run_dir=run_dir,
        audit=audit,
        redactor=redactor,
        store=store,
        tracker=tracker,
        confluence=confluence,
        worktrees=WorktreeManager(cfg.repo),
        github=github,
        github_token=github_token,
        roles=roles,
        jinja=jinja,
        dry_run=dry_run,
        keep_worktrees=keep_worktrees,
    )


def platform_name() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"
