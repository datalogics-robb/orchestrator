"""Derive the Agents tab: one row per (run, issue, role), from the store's runs, tasks, and agent rows.

Pure functions over store rows, so the rules are testable without a database or a server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from orchestrator.config.schema import Config
from orchestrator.pipeline.task import TaskState
from orchestrator.state.store import AgentRow, RunRow, RunStatus, Store, effective_status

AgentStatus = Literal[
    "running",
    "orphaned",
    "needs approval",
    "waiting",
    "idle",
    "queued",
    "interrupted",
    "cancelled",
    "done",
    "blocked",
    "failed",
]

ORDER: dict[str, int] = {
    s: i
    for i, s in enumerate(
        (
            "running",
            "orphaned",
            "needs approval",
            "waiting",
            "idle",
            "queued",
            "interrupted",
            "cancelled",
            "done",
            "blocked",
            "failed",
        )
    )
}

WORKER_STATES = frozenset(
    {"QUEUED", "CONTEXT", "WORKTREE", "WORKING", "FIXING", "SPECIFYING", "TEST_WRITING", "IMPLEMENTING"}
)
"""Task states in which the worker is the next agent to act (or is acting)."""
REVIEWER_STATES = frozenset({"BUILDING", "TESTING", "COMMITTING", "RED_CHECK", "REVIEWING"})
"""Task states in which the reviewer is the next agent to act (or is acting)."""

ROLES = ("worker", "reviewer")


class AgentView(BaseModel):
    run_id: str
    run_status: RunStatus
    dry_run: bool
    key: str
    summary: str
    role: str
    runner: str
    model: str | None
    label: str | None
    """The latest invocation's label: work, fix-2, review-1, spec, red, green."""
    status: AgentStatus
    task_state: str
    round: int
    started: str | None
    elapsed_seconds: int | None
    """Seconds the running invocation has been going; null when nothing is running."""
    turns: int | None
    task_cost_usd: float
    issue_url: str
    pr_url: str | None
    error: str | None


class DaemonInfo(BaseModel):
    pid: int
    host: str
    port: int
    version: str
    config: str
    repo: str


class AgentsResponse(BaseModel):
    generated_at: str
    daemon: DaemonInfo
    agents: list[AgentView]


@dataclass
class RunSnapshot:
    run: RunRow
    tasks: dict[str, TaskState]
    agents: dict[tuple[str, str], AgentRow]
    last_activity: str | None


def role_status(role: str, task: TaskState, latest: AgentRow | None, run_status: RunStatus) -> AgentStatus:
    if latest is not None and latest.ended is None:
        return "running" if run_status == "running" else "orphaned"
    if task.state == "DONE":
        return "done"
    if task.state in ("BLOCKED", "FAILED"):
        return task.state.lower()  # type: ignore[return-value]
    if task.paused:
        return "needs approval" if role == "worker" else "idle"
    if run_status == "queued":
        return "queued"
    if run_status == "interrupted":
        return "interrupted"
    if run_status == "cancelled":
        return "cancelled"
    if run_status != "running":
        return "idle"  # a paused run's other tasks wait for the resume
    next_states = WORKER_STATES if role == "worker" else REVIEWER_STATES
    return "waiting" if task.state in next_states else "idle"


def _elapsed(started: str, now: datetime) -> int:
    return max(0, int((now - datetime.fromisoformat(started)).total_seconds()))


def rows_for(snapshot: RunSnapshot, cfg: Config, now: datetime) -> list[AgentView]:
    run = snapshot.run
    run_status = effective_status(run, any_paused=any(t.paused for t in snapshot.tasks.values()), now=now)
    browse = cfg.tracker.base_url.rstrip("/") + "/browse/"
    out: list[AgentView] = []
    for task in snapshot.tasks.values():
        for role in ROLES:
            latest = snapshot.agents.get((task.key, role))
            role_cfg = cfg.agents.role(role)  # type: ignore[arg-type]
            status = role_status(role, task, latest, run_status)
            running = status in ("running", "orphaned") and latest is not None
            out.append(
                AgentView(
                    run_id=run.run_id,
                    run_status=run_status,
                    dry_run=run.dry_run,
                    key=task.key,
                    summary=task.summary,
                    role=role,
                    runner=latest.runner if latest else role_cfg.runner,
                    model=latest.model if latest else role_cfg.model,
                    label=latest.label if latest else None,
                    status=status,
                    task_state=task.state,
                    round=task.round,
                    started=latest.started if latest else None,
                    elapsed_seconds=_elapsed(latest.started, now) if running and latest else None,
                    turns=None if running or latest is None else latest.turns,
                    task_cost_usd=round(task.cost_usd, 2),
                    issue_url=browse + task.key,
                    pr_url=task.pr_url,
                    error=task.error if role == "worker" or status in ("blocked", "failed") else None,
                )
            )
    return out


def in_scope(snapshot: RunSnapshot, recent: timedelta, now: datetime) -> bool:
    """Queued, running, and paused runs always show; finished and interrupted ones while recently active."""
    status = effective_status(
        snapshot.run, any_paused=any(t.paused for t in snapshot.tasks.values()), now=now
    )
    if status in ("queued", "running", "paused"):
        return True
    last = snapshot.last_activity or snapshot.run.heartbeat or snapshot.run.finished or snapshot.run.started
    return now - datetime.fromisoformat(last) <= recent


def sort_key(row: AgentView) -> tuple:
    return (ORDER[row.status], row.run_id, row.key, ROLES.index(row.role))


def agent_rows(store: Store, cfg: Config, now: datetime | None = None, scan: int = 50) -> list[AgentView]:
    """Every agent row the Agents tab shows, most urgent first. `scan` bounds how many recent runs are read."""
    now = now or datetime.now(UTC)
    recent = timedelta(minutes=cfg.web.recent_minutes)
    rows: list[AgentView] = []
    for run in store.list_runs(scan):
        snapshot = RunSnapshot(
            run=run,
            tasks=store.load_tasks(run.run_id),
            agents=store.latest_agents(run.run_id),
            last_activity=store.last_task_update(run.run_id),
        )
        if in_scope(snapshot, recent, now):
            rows.extend(rows_for(snapshot, cfg, now))
    return sorted(rows, key=sort_key)


RunAction = Literal["cancel", "resume", "retry-failed", "retry-blocked"]


def resumable(tasks: dict[str, TaskState]) -> bool:
    """Some task would move if the run were driven again (paused ones wait for an approval instead)."""
    return any(not t.terminal and not t.paused for t in tasks.values())


def run_actions(status: RunStatus, tasks: dict[str, TaskState], driven_here: bool) -> list[RunAction]:
    """What the operator can do to a run from the page.

    A queued or running run can be cancelled only by the daemon driving it; one that nothing drives
    can be resumed while a task would move, and retried while it holds FAILED or BLOCKED tasks.
    """
    if status in ("queued", "running"):
        return ["cancel"] if driven_here else []
    actions: list[RunAction] = []
    if status in ("interrupted", "cancelled") and resumable(tasks):
        actions.append("resume")
    states = {t.state for t in tasks.values()}
    if "FAILED" in states:
        actions.append("retry-failed")
    if "BLOCKED" in states:
        actions.append("retry-blocked")
    return actions


class RunView(BaseModel):
    run_id: str
    status: RunStatus
    dry_run: bool
    started: str
    finished: str | None
    keys: list[str]
    """What was asked for; an epic's children are the tasks."""
    owner: str | None
    by_this_daemon: bool
    """False for runs started with `orchestrator run` or by another daemon; the page cannot control them."""
    tasks: dict[str, int]
    """Task count per task state."""
    task_states: dict[str, str]
    """Each task's state by key, so the page can open any task's details."""
    cost_usd: float
    actions: list[RunAction]


class RunsResponse(BaseModel):
    generated_at: str
    runs: list[RunView]


def run_views(
    store: Store, owner: str, driven: set[str], now: datetime | None = None, limit: int = 20
) -> list[RunView]:
    """The most recent runs, newest first. `owner` is this daemon's owner string; `driven`, the runs it drives."""
    now = now or datetime.now(UTC)
    out: list[RunView] = []
    for run in store.list_runs(limit):
        tasks = store.load_tasks(run.run_id)
        counts: dict[str, int] = {}
        for t in tasks.values():
            counts[t.state] = counts.get(t.state, 0) + 1
        status = effective_status(run, any_paused=any(t.paused for t in tasks.values()), now=now)
        out.append(
            RunView(
                run_id=run.run_id,
                status=status,
                dry_run=run.dry_run,
                started=run.started,
                finished=run.finished,
                keys=run.keys,
                owner=run.owner,
                by_this_daemon=run.owner == owner,
                tasks=counts,
                task_states={t.key: t.state for t in tasks.values()},
                cost_usd=round(sum(t.cost_usd for t in tasks.values()), 2),
                actions=run_actions(status, tasks, run.run_id in driven),
            )
        )
    return out


TaskAction = Literal["ask", "instruct"]

FINDINGS_MAX_BYTES = 512 * 1024
"""A findings report larger than this is cut short in the task view."""


class Message(BaseModel):
    ts: str
    kind: Literal["question", "answer", "instruction", "error"]
    text: str
    by: str


class TaskDetail(BaseModel):
    run_id: str
    run_status: RunStatus
    dry_run: bool
    key: str
    summary: str
    issue_url: str
    workflow: str
    state: str
    stopped_at: str | None
    """The stage a finished task was in when it ended."""
    headline: str
    """One sentence on where the task stands and why, for a person."""
    reason: str | None
    """The task's error: a blocked reason such as `technical`, or a failure message."""
    worker_summary: str
    blocked_details: str
    """The worker's own explanation of why it stopped, in Markdown."""
    questions: list[str]
    """What the worker would ask the issue's reporter."""
    changed_paths: list[str]
    round: int
    branch: str | None
    worktree_path: str | None
    worktree_exists: bool
    pr_url: str | None
    confluence_url: str | None
    findings_markdown: str | None
    """The findings report written when the task was blocked, from the run directory."""
    cost_usd: float
    conversation: list[Message]
    answering: bool
    """The worker is answering the operator's latest question."""
    actions: list[TaskAction]


def headline(task: TaskState, dry_run: bool) -> str:
    """Where a task stands, said plainly: above all, why it ended without a pull request."""
    reason = (task.error or "").splitlines()[0] if task.error else ""
    worker = task.worker_result or {}
    if task.state == "DONE":
        if task.pr_url:
            return "Completed: the change passed its build, tests, and review, and a pull request was opened."
        if dry_run:
            return "Completed as a dry run: the change passed its gates, and a dry run opens no pull request."
        return "Completed without a pull request."
    if task.state == "BLOCKED":
        if worker.get("status") == "blocked":
            return (
                f"The worker stopped in {task.stopped_at} and reported that the task cannot be done as asked "
                f"({reason}), so no pull request was opened. Its explanation and questions are below."
            )
        return (
            f"The task was blocked in {task.stopped_at} ({reason or 'no reason recorded'}), so no pull request "
            "was opened. The findings report below says why."
        )
    if task.state == "FAILED":
        return (
            f"The orchestrator hit an error in {task.stopped_at}: {reason}. This is an infrastructure problem, "
            "not the work item; retry the task from the Runs tab once it is fixed."
        )
    if task.paused:
        return f"Waiting for a person to approve or revise ({task.state})."
    return f"In progress: {task.state}."


def task_actions(task: TaskState, run_status: RunStatus, worktree_exists: bool) -> list[TaskAction]:
    """What the operator can say to a task's worker: nothing while its run is queued or running."""
    if run_status in ("queued", "running"):
        return []
    actions: list[TaskAction] = []
    if worktree_exists:
        actions.append("ask")
    if task.state == "BLOCKED" and task.workflow == "bugfix":
        actions.append("instruct")
    return actions


def _findings(path: str | None, run_dir: Path) -> str | None:
    """The findings report, read only from inside its run's directory."""
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(run_dir.resolve()):
            return None
        with resolved.open("rb") as f:
            data = f.read(FINDINGS_MAX_BYTES + 1)
    except OSError:
        return None
    text = data[:FINDINGS_MAX_BYTES].decode("utf-8", errors="replace")
    return text + ("\n\n… (cut short)" if len(data) > FINDINGS_MAX_BYTES else "")


def task_detail(
    store: Store, cfg: Config, run_id: str, key: str, now: datetime | None = None
) -> TaskDetail | None:
    """Everything the page shows about one task; None when the run or the task does not exist."""
    run = store.get_run(run_id)
    if run is None:
        return None
    tasks = store.load_tasks(run_id)
    task = tasks.get(key)
    if task is None:
        return None
    run_status = effective_status(run, any_paused=any(t.paused for t in tasks.values()), now=now)
    worker = task.worker_result or {}
    blocked = worker.get("blocked") or {}
    worktree_exists = bool(task.worktree_path and Path(task.worktree_path).is_dir())
    latest = store.latest_agents(run_id).get((key, "worker"))
    return TaskDetail(
        run_id=run_id,
        run_status=run_status,
        dry_run=run.dry_run,
        key=key,
        summary=task.summary,
        issue_url=cfg.tracker.base_url.rstrip("/") + "/browse/" + key,
        workflow=task.workflow,
        state=task.state,
        stopped_at=task.stopped_at,
        headline=headline(task, run.dry_run),
        reason=task.error,
        worker_summary=str(worker.get("summary") or ""),
        blocked_details=str(blocked.get("details_markdown") or ""),
        questions=[str(q) for q in blocked.get("questions_for_reporter") or []],
        changed_paths=[str(p) for p in worker.get("changed_paths") or []],
        round=task.round,
        branch=task.branch,
        worktree_path=task.worktree_path,
        worktree_exists=worktree_exists,
        pr_url=task.pr_url,
        confluence_url=task.confluence_url,
        findings_markdown=_findings(task.findings_path, cfg.runs_dir / run_id),
        cost_usd=round(task.cost_usd, 2),
        conversation=[Message.model_validate(m) for m in task.conversation],
        answering=bool(
            latest and latest.ended is None and latest.label.startswith("ask-") and run_status == "running"
        ),
        actions=task_actions(task, run_status, worktree_exists),
    )
