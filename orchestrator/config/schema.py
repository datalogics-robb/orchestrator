"""Pydantic models for the orchestrator YAML configuration.

Unknown keys are errors everywhere so typos surface immediately. Commands are stored as
argv lists; a YAML string is split with shlex, a YAML list is taken as-is. Every field
carries a description; `orchestrator config-reference` renders them.
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


Command = Annotated[
    list[str],
    BeforeValidator(_to_argv),
    Field(json_schema_extra={"format": "command"}),
]
"""A shell command: either one string (split like a shell would) or a list of arguments."""


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return Path(value).expanduser()
    return value


UserPath = Annotated[Path, BeforeValidator(_expand), Field(json_schema_extra={"format": "path"})]
"""A filesystem path; `~` is expanded."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthRef(StrictModel):
    """Names where a secret lives. The secret itself never appears in YAML."""

    netrc_machine: str | None = Field(
        None,
        description="Machine name in ~/.netrc whose password is the token (and whose login is the account, for Jira).",
    )
    token_env: str | None = Field(None, description="Environment variable holding the token.")
    use_cli_login: bool = Field(
        False,
        description=(
            "Reuse the tool's own login instead of a token: `gh auth token` for the repository, the Claude Code "
            "login for claude-code, the Codex ChatGPT login for codex. Not valid for Jira or Confluence."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> AuthRef:
        if sum(map(bool, (self.netrc_machine, self.token_env, self.use_cli_login))) != 1:
            raise ValueError("auth needs exactly one of netrc_machine, token_env, or use_cli_login: true")
        return self


class Statuses(StrictModel):
    """Jira status names the orchestrator transitions issues to."""

    in_progress: str = Field("In Progress", description="Status set when work on an issue starts.")
    in_review: str = Field("In Review", description="Status set after the pull request is opened.")
    blocked: str = Field(
        "Blocked", description="Status set when the work cannot be completed. Empty string: no transition."
    )


class TrackerConfig(StrictModel):
    """The issue tracker that supplies work and receives results."""

    kind: Literal["jira"] = Field("jira", description="Tracker type. Only Jira Cloud is implemented.")
    base_url: str = Field(description="Jira site URL, e.g. https://example.atlassian.net.")
    project: str = Field(description="Jira project key the issues belong to.")
    auth: AuthRef = Field(description="Where the Jira API token lives.")
    statuses: Statuses = Field(Statuses(), description="Status names used for transitions.")
    comment_on: list[Outcome] = Field(
        ["started", "pr_opened", "blocked", "failed"],
        description="Which events post a comment on the issue.",
    )
    acceptance_field: str | None = Field(
        None, description="Custom field id (e.g. customfield_10042) holding acceptance criteria, if any."
    )
    epic_children_jql: str = Field(
        'parent = "{key}" ORDER BY rank',
        description="JQL used to list an epic's children; {key} is the epic key.",
    )
    attachment_max_bytes: int = Field(
        10 * 1024 * 1024, description="Attachments larger than this are listed but not downloaded."
    )

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class ConfluencePublish(StrictModel):
    """Where run and findings pages are published."""

    space: str = Field(description="Confluence space key.")
    parent_page_id: str = Field(description="Numeric id of the parent page new pages are created under.")
    when: list[Literal["blocked", "completed", "failed"]] = Field(
        ["blocked", "completed"],
        description="Which task outcomes produce a page. (Named `when` because YAML reads `on` as a boolean.)",
    )


class ConfluenceConfig(StrictModel):
    """Confluence pages read as context and, optionally, written as reports."""

    base_url: str = Field(description="Confluence site URL, e.g. https://example.atlassian.net/wiki.")
    auth: AuthRef = Field(description="Where the Confluence API token lives (usually the same as Jira).")
    context_pages: list[str] = Field(
        [], description="Page URLs fetched read-only and given to both agents as reference material."
    )
    publish: ConfluencePublish | None = Field(
        None, description="Publishing target; omit to never write pages."
    )

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class PRConfig(StrictModel):
    """How pull requests are opened."""

    draft: bool = Field(True, description="Open pull requests as drafts.")
    labels: list[str] = Field(["agent-generated"], description="Labels added to every pull request.")
    reviewers: list[str] = Field([], description="GitHub logins requested as reviewers.")
    title_template: str = Field(
        "{key}: {summary}", description="Pull request title; {key} and {summary} come from the issue."
    )


class RepoConfig(StrictModel):
    """The GitHub repository being worked on."""

    github: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", description="Repository as owner/name.")
    base_branch: str = Field("main", description="Branch that worktrees start from and PRs target.")
    clone_path: UserPath = Field(description="An existing local clone; worktrees are created from it.")
    worktree_root: UserPath = Field(description="Directory that receives one worktree per issue.")
    branch_template: str = Field(
        "agent/{key}-{slug}",
        description="Branch name; {key} is the lower-cased issue key, {slug} the summary.",
    )
    pr: PRConfig = Field(PRConfig(), description="Pull request settings.")
    auth: AuthRef = Field(
        description="Where the GitHub token lives. Used only by the orchestrator, never by agents."
    )


class ShareConfig(StrictModel):
    """A network share agents may read from or write to."""

    paths: dict[Platform, UserPath] = Field(
        description="Mount path per platform, e.g. {darwin: /Volumes/support, linux: /support}."
    )
    expect_read_only_mount: bool = Field(
        False, description="doctor warns when this share is mounted writable."
    )
    write_under: list[str] = Field(
        [],
        description="Relative subdirectories where read-write grants may write. Empty allows the whole share.",
    )

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
    """Pass-through of MCP servers already configured for each agent CLI."""

    sources: dict[str, list[UserPath]] = Field(
        {},
        description=(
            "Per runner name, the config files to read MCP definitions from. Defaults: claude-code "
            "~/.claude.json and .mcp.json; codex ~/.codex/config.toml; gemini-cli ~/.gemini/settings.json; "
            "opencode ~/.config/opencode/opencode.json; hermes ~/.hermes/config.yaml."
        ),
    )
    deny_tools: Annotated[dict[str, list[str]], BeforeValidator(_normalize_deny)] = Field(
        {}, description="Per server, tool names kept off even when the server is allowed."
    )


class BuildConfig(StrictModel):
    """How the target project is built."""

    setup: list[Command] = Field([], description="Commands run once per new worktree, e.g. python mkenv.py.")
    commands: list[Command] = Field(
        [], description="Build commands run after the worker finishes and after each fix."
    )
    timeout_minutes: int = Field(60, description="Wall-clock limit for each setup or build command.")
    env: dict[str, str] = Field(
        {}, description="Extra environment variables for build, test, and agent processes."
    )
    max_concurrent_builds: int = Field(1, description="Global cap on simultaneous builds across all tasks.")


class TestSelection(StrictModel):
    """Which subset of tests runs after a build."""

    strategy: Literal["changed-paths", "named-suite", "agent-chosen"] = Field(
        "named-suite",
        description=(
            "changed-paths: pick from `map` by changed file prefixes; named-suite: always run `commands`; "
            "agent-chosen: run the worker's tests_selected, filtered by allowed_prefixes."
        ),
    )
    map: dict[str, list[Command]] = Field({}, description="changed-paths: path prefix to test commands.")
    fallback: list[Command] = Field([], description="Commands run when the strategy selects nothing.")
    commands: list[Command] = Field([], description="named-suite: the commands to run.")
    allowed_prefixes: list[str] = Field(
        [], description="agent-chosen: a worker-selected command must start with one of these."
    )
    bare_test_template: str | None = Field(
        None,
        description="agent-chosen: workers often list bare test ids (`SF12345`) instead of commands. With this set, "
        "all bare ids become one command with `{ids}` replaced by the comma-joined ids, e.g. "
        "`invoke -e test --config=Release --tests={ids}`. Without it a bare id is appended to the first allowed prefix.",
    )
    max_commands: int = Field(3, description="Upper bound on test commands per round.")


class TestConfig(StrictModel):
    """Test settings."""

    timeout_minutes: int = Field(30, description="Wall-clock limit for each test command.")
    selection: TestSelection = Field(TestSelection(), description="Test selection strategy.")
    full_suite: list[Command] = Field(
        [], description="The complete suite. Never run by the orchestrator; documented in the PR body."
    )


class RoleConfig(StrictModel):
    """Settings for one agent role (worker or reviewer)."""

    runner: str = Field(
        description="Adapter name: claude-code, codex, gemini-cli, opencode, hermes, or a registered third-party adapter."
    )
    model: str | None = Field(None, description="Model passed to the runtime; omit for its default.")
    access: WorktreeAccess = Field(
        "workspace-write",
        description="workspace-write lets the role edit the worktree; read-only forbids writes.",
    )
    timeout_minutes: int = Field(
        60, description="Wall-clock limit per agent invocation, enforced by the orchestrator."
    )
    max_turns: int | None = Field(
        None, description="Turn limit; passed to runtimes that support it, otherwise soft."
    )
    max_budget_usd: float | None = Field(
        None, description="Spend limit; passed to runtimes that support it, otherwise checked after the run."
    )
    auth: AuthRef = Field(
        description="Where this role's API key lives. It is the only secret the agent process sees."
    )
    shares: dict[str, ShareMode] = Field(
        {}, description="Share name to read or read-write. read-write requires access: workspace-write."
    )
    mcp_servers: list[str] = Field([], description="MCP server names this role may use, from mcp.sources.")
    options: dict[str, Any] = Field(
        {},
        description=(
            "Adapter-specific settings. claude-code: effort, bare. codex: reasoning_effort. "
            "opencode: agent, variant. hermes: provider, reasoning."
        ),
    )


class AgentsConfig(StrictModel):
    """Agent roles and the review loop."""

    review_rounds: int = Field(
        2,
        description="Fix rounds allowed before the task is blocked; build, test, and review failures all count.",
    )
    prompt_overrides: dict[str, UserPath] = Field(
        {}, description="Replace a built-in prompt template (worker, reviewer, fixer) with a file."
    )
    worker: RoleConfig = Field(description="The agent that implements the change.")
    reviewer: RoleConfig = Field(description="The agent that reviews the change; ideally a different vendor.")

    def role(self, name: Role) -> RoleConfig:
        return self.worker if name == "worker" else self.reviewer


class SchedulerConfig(StrictModel):
    """Concurrency and retries."""

    max_parallel: int = Field(3, description="Issues processed at the same time.")
    retry_infra_failures: int = Field(
        2, description="Retries for transient infrastructure errors such as git or network failures."
    )
    retry_backoff_seconds: int = Field(
        60,
        description="Wait before the first retry; doubles per attempt and is capped at five minutes. "
        "Network and API outages usually need minutes to clear.",
    )


class CommitConfig(StrictModel):
    """How the orchestrator commits, and what it requires of agents that commit."""

    squash: Literal["phases", "all", "none"] = Field(
        "phases",
        description=(
            "How the agent's checkpoint commits are folded when the orchestrator commits. "
            "phases: one commit per workflow phase; in the feature workflow the red commit (the failing "
            "tests) stays a separate commit and only the work after it is folded into the green commit, so "
            "the pull request shows red, then green. all: fold everything since the base into one commit; "
            "the red commit is still kept, this only affects checkpoints the agent made itself. "
            "none: keep the agent's own commits as they are and add one commit for whatever is uncommitted."
        ),
    )
    pre_commit: bool = Field(
        True,
        description=(
            "Run the repository's pre-commit hooks on the staged files before the orchestrator commits, "
            "when .pre-commit-config.yaml exists. Hook failures go back to the worker as a fix round."
        ),
    )
    install_hooks: bool = Field(
        True,
        description=(
            "Install pre-commit's git hook in each worktree so an agent's own `git commit` runs the hooks. "
            "Bypassing them (--no-verify, -n, core.hooksPath) is denied to agents regardless."
        ),
    )


class HooksConfig(StrictModel):
    """Shell hooks run by the orchestrator at fixed points."""

    after_worktree: list[Command] = Field([], description="Run in each new worktree after build.setup.")
    before_pr: list[Command] = Field([], description="Run in the worktree before pushing.")


class FeatureWorkflowConfig(StrictModel):
    """The red/green workflow for feature work: specify, approve, write failing tests, implement."""

    require_approval: bool = Field(
        True,
        description="Pause after the specification so a person approves it (resume --approve) before any code "
        "is written. With false, the approved spec is the worker's own.",
    )
    spec_review: bool = Field(
        True, description="Have the reviewer critique the specification before the pause."
    )
    require_red: bool = Field(
        True,
        description="The new tests must build and then fail before the implementation starts. A passing or "
        "non-building test set goes back to the worker.",
    )
    pause_after_red: bool = Field(
        False,
        description="Pause at RED_REVIEW once the failing tests are committed, so a person can inspect the tests "
        "and their inputs (red.md, attached to the issue) before the implementation starts. "
        "`resume --approve KEY` continues; `--revise KEY --decisions FILE` sends the tests back.",
    )
    red_reject_patterns: list[str] = Field(
        [],
        description="Regexes matched against the failing test output. A match means the failure is not a real "
        "red (missing data, skipped test) and is sent back to the worker.",
    )
    review_rounds: int = Field(
        3, description="Fix rounds allowed for feature tasks; overrides agents.review_rounds."
    )
    spec_revisions: int = Field(2, description="How many times a rejected specification may be rewritten.")
    max_cost_usd: float | None = Field(
        None, description="Total agent spend per feature task before it is blocked as over budget."
    )


class WorkflowsConfig(StrictModel):
    """Which workflow an issue gets. Bugs follow the default flow; features add spec and red/green phases."""

    feature_issue_types: list[str] = Field(
        ["Story", "New Feature", "Feature", "Improvement", "Enhancement"],
        description="Jira issue types (case-insensitive) routed to the feature workflow. "
        "`run --workflow feature` overrides for any issue.",
    )
    feature: FeatureWorkflowConfig = Field(FeatureWorkflowConfig(), description="Feature workflow settings.")

    def workflow_for(self, issue_type: str) -> str:
        lowered = {t.lower() for t in self.feature_issue_types}
        return "feature" if issue_type.lower() in lowered else "bugfix"


class Config(StrictModel):
    """Top-level configuration: one file per target repository."""

    version: Literal[1] = Field(description="Config format version. Must be 1.")
    state_dir: UserPath | None = Field(
        None,
        description="Where runs, cache, and the SQLite store live. Defaults to the worktree root's parent.",
    )
    tracker: TrackerConfig = Field(description="Issue tracker settings.")
    confluence: ConfluenceConfig | None = Field(None, description="Confluence settings; omit to disable.")
    repo: RepoConfig = Field(description="Target repository settings.")
    shares: dict[str, ShareConfig] = Field({}, description="Network shares, by name.")
    mcp: MCPConfig = Field(MCPConfig(), description="MCP server pass-through settings.")
    build: BuildConfig = Field(BuildConfig(), description="Build settings.")
    test: TestConfig = Field(TestConfig(), description="Test settings.")
    agents: AgentsConfig = Field(description="Agent roles.")
    scheduler: SchedulerConfig = Field(SchedulerConfig(), description="Concurrency and retries.")
    commit: CommitConfig = Field(CommitConfig(), description="Commit rules: pre-commit hooks.")
    hooks: HooksConfig = Field(HooksConfig(), description="Shell hooks.")
    workflows: WorkflowsConfig = Field(
        WorkflowsConfig(), description="Bugfix versus feature workflow routing."
    )

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

    def review_rounds(self, workflow: str) -> int:
        return self.workflows.feature.review_rounds if workflow == "feature" else self.agents.review_rounds

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
