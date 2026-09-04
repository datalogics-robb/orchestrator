"""Pydantic models for the orchestrator YAML configuration.

Unknown keys are errors everywhere so typos surface immediately. Commands are stored as
argv lists; a YAML string is split with shlex, a YAML list is taken as-is.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Platform = Literal["darwin", "linux"]
Role = Literal["worker", "reviewer"]
WorktreeAccess = Literal["read-only", "workspace-write"]
ShareMode = Literal["read", "read-write"]
Outcome = Literal["started", "pr_opened", "blocked", "failed", "completed"]


def _to_argv(value: Any) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ValueError("a command must be a string or a list of strings")


Command = Annotated[list[str], BeforeValidator(_to_argv)]


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return Path(value).expanduser()
    return value


UserPath = Annotated[Path, BeforeValidator(_expand)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthRef(StrictModel):
    """Names where a secret lives. The secret itself never appears in YAML."""

    netrc_machine: str | None = None
    token_env: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> AuthRef:
        if bool(self.netrc_machine) == bool(self.token_env):
            raise ValueError("auth needs exactly one of netrc_machine or token_env")
        return self


class Statuses(StrictModel):
    in_progress: str = "In Progress"
    in_review: str = "In Review"
    blocked: str = "Blocked"


class TrackerConfig(StrictModel):
    kind: Literal["jira"] = "jira"
    base_url: str
    project: str
    auth: AuthRef
    statuses: Statuses = Statuses()
    comment_on: list[Outcome] = ["started", "pr_opened", "blocked", "failed"]
    acceptance_field: str | None = None
    epic_children_jql: str = 'parent = "{key}" ORDER BY rank'
    attachment_max_bytes: int = 10 * 1024 * 1024

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class ConfluencePublish(StrictModel):
    space: str
    parent_page_id: str
    when: list[Literal["blocked", "completed", "failed"]] = ["blocked", "completed"]


class ConfluenceConfig(StrictModel):
    base_url: str
    auth: AuthRef
    context_pages: list[str] = []
    publish: ConfluencePublish | None = None

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class PRConfig(StrictModel):
    draft: bool = True
    labels: list[str] = ["agent-generated"]
    reviewers: list[str] = []
    title_template: str = "{key}: {summary}"


class RepoConfig(StrictModel):
    github: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = "main"
    clone_path: UserPath
    worktree_root: UserPath
    branch_template: str = "agent/{key}-{slug}"
    pr: PRConfig = PRConfig()
    auth: AuthRef


class ShareConfig(StrictModel):
    paths: dict[Platform, UserPath]
    expect_read_only_mount: bool = False
    write_under: list[str] = []

    def path_for(self, platform: str = sys.platform) -> Path | None:
        key: Platform | None
        if platform == "darwin":
            key = "darwin"
        elif platform.startswith("linux"):
            key = "linux"
        else:
            key = None
        return self.paths.get(key) if key else None


def _normalize_deny(value: Any) -> dict[str, list[str]]:
    """Accept {server: [tools]} or a list of single-key mappings and return {server: [tools]}."""
    if isinstance(value, dict):
        return {str(k): list(v) for k, v in value.items()}
    if isinstance(value, list):
        out: dict[str, list[str]] = {}
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("deny_tools entries must be mappings of server to tool list")
            for k, v in item.items():
                out.setdefault(str(k), []).extend(v)
        return out
    raise ValueError("deny_tools must be a mapping or a list of mappings")


class MCPConfig(StrictModel):
    sources: dict[str, list[UserPath]] = {}
    deny_tools: Annotated[dict[str, list[str]], BeforeValidator(_normalize_deny)] = {}


class BuildConfig(StrictModel):
    setup: list[Command] = []
    commands: list[Command] = []
    timeout_minutes: int = 60
    env: dict[str, str] = {}
    max_concurrent_builds: int = 1


class TestSelection(StrictModel):
    strategy: Literal["changed-paths", "named-suite", "agent-chosen"] = "named-suite"
    map: dict[str, list[Command]] = {}
    fallback: list[Command] = []
    commands: list[Command] = []
    allowed_prefixes: list[str] = []
    max_commands: int = 3


class TestConfig(StrictModel):
    timeout_minutes: int = 30
    selection: TestSelection = TestSelection()
    full_suite: list[Command] = []


class RoleConfig(StrictModel):
    runner: str
    model: str | None = None
    access: WorktreeAccess = "workspace-write"
    timeout_minutes: int = 60
    max_turns: int | None = None
    max_budget_usd: float | None = None
    auth: AuthRef
    shares: dict[str, ShareMode] = {}
    mcp_servers: list[str] = []
    options: dict[str, Any] = {}


class AgentsConfig(StrictModel):
    review_rounds: int = 2
    prompt_overrides: dict[str, UserPath] = {}
    worker: RoleConfig
    reviewer: RoleConfig

    def role(self, name: Role) -> RoleConfig:
        return self.worker if name == "worker" else self.reviewer


class SchedulerConfig(StrictModel):
    max_parallel: int = 3
    retry_infra_failures: int = 2


class HooksConfig(StrictModel):
    after_worktree: list[Command] = []
    before_pr: list[Command] = []


class Config(StrictModel):
    version: Literal[1]
    state_dir: UserPath | None = None
    """Where runs, cache, and the SQLite store live. Defaults to the worktree root's parent."""
    tracker: TrackerConfig
    confluence: ConfluenceConfig | None = None
    repo: RepoConfig
    shares: dict[str, ShareConfig] = {}
    mcp: MCPConfig = MCPConfig()
    build: BuildConfig = BuildConfig()
    test: TestConfig = TestConfig()
    agents: AgentsConfig
    scheduler: SchedulerConfig = SchedulerConfig()
    hooks: HooksConfig = HooksConfig()

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        for role_name in ("worker", "reviewer"):
            role = self.agents.role(role_name)  # type: ignore[arg-type]
            for share, mode in role.shares.items():
                if share not in self.shares:
                    raise ValueError(f"agents.{role_name}.shares: '{share}' is not declared under shares")
                if mode == "read-write" and role.access != "workspace-write":
                    raise ValueError(
                        f"agents.{role_name}.shares.{share}: read-write needs access: workspace-write"
                    )
        if self.test.selection.strategy == "named-suite" and not self.test.selection.commands:
            if self.test.selection.fallback:
                object.__setattr__(self.test.selection, "commands", list(self.test.selection.fallback))
        return self

    @property
    def resolved_state_dir(self) -> Path:
        return self.state_dir or self.repo.worktree_root.parent

    @property
    def runs_dir(self) -> Path:
        return self.resolved_state_dir / "runs"

    @property
    def cache_dir(self) -> Path:
        return self.resolved_state_dir / "cache"

    @property
    def db_path(self) -> Path:
        return self.resolved_state_dir / "state.db"
