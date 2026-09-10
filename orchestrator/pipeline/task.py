"""Per-issue task state: the checkpoint persisted after every transition."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

State = Literal[
    "QUEUED",
    "CONTEXT",
    "WORKTREE",
    "SPECIFYING",
    "AWAITING_APPROVAL",
    "RED_REVIEW",
    "TEST_WRITING",
    "RED_CHECK",
    "IMPLEMENTING",
    "WORKING",
    "BUILDING",
    "TESTING",
    "COMMITTING",
    "REVIEWING",
    "FIXING",
    "PUSHING",
    "OPENING_PR",
    "REPORTING",
    "DONE",
    "BLOCKED",
    "FAILED",
]

TERMINAL: frozenset[str] = frozenset({"DONE", "BLOCKED", "FAILED"})
PAUSED: frozenset[str] = frozenset({"AWAITING_APPROVAL", "RED_REVIEW"})
"""States where the task waits for a person; `resume --approve` or `--revise` moves it on.

AWAITING_APPROVAL: the specification is written. RED_REVIEW: the failing tests are committed.
"""

Workflow = Literal["bugfix", "feature"]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class TaskState:
    key: str
    summary: str = ""
    state: State = "QUEUED"
    round: int = 0
    """Fix rounds used so far (build/test failures and review findings both count)."""
    outcome: Literal["completed", "blocked", "failed", ""] = ""
    workflow: Workflow = "bugfix"
    branch: str | None = None
    worktree_path: str | None = None
    base_sha: str | None = None
    spec: dict[str, Any] | None = None
    """Feature workflow: the specification the worker produced (acceptance criteria, API, tests)."""
    spec_review: dict[str, Any] | None = None
    spec_revision: int = 0
    decisions: str = ""
    """Feature workflow: the approver's answers and instructions, verbatim."""
    phase_commits: list[str] = field(default_factory=list)
    """Feature workflow: SHAs of the red commit(s); the green squash resets to the last one."""
    red_evidence: str = ""
    """Feature workflow: excerpt of the failing test output that proved the tests were red."""
    red_tests: list[list[str]] = field(default_factory=list)
    """Feature workflow: the commands that were red; every later test stage runs them first."""
    worker_session: str | None = None
    reviewer_session: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    cost_usd: float = 0.0
    started: str = field(default_factory=now)
    updated: str = field(default_factory=now)
    error: str | None = None
    fix_reason: str | None = None
    """Why the next fix round runs: build/test failure excerpt or review findings, as Markdown."""
    worker_result: dict[str, Any] | None = None
    review_result: dict[str, Any] | None = None
    tests_run: list[list[str]] = field(default_factory=list)
    excluded_from_commit: list[str] = field(default_factory=list)
    findings_path: str | None = None
    confluence_url: str | None = None
    depends_on: list[str] = field(default_factory=list)
    epic_key: str | None = None
    history: list[tuple[str, str]] = field(default_factory=list)
    """(timestamp, state) transitions, for the run report."""

    def transition(self, state: State) -> None:
        self.state = state
        self.updated = now()
        self.history.append((self.updated, state))

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def paused(self) -> bool:
        return self.state in PAUSED

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TaskState:
        data = dict(data)
        data["history"] = [tuple(h) for h in data.get("history", [])]
        return cls(**data)


class Blocked(Exception):
    """The work item cannot be completed; a findings report is produced."""

    def __init__(self, reason: str, details_markdown: str, questions: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details_markdown = details_markdown
        self.questions = questions or []


class Failed(Exception):
    """An orchestrator or infrastructure problem, never blamed on the work item."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
