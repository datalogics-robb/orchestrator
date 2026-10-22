"""Derive the Agents tab: one row per (run, issue, role), from the store's runs, tasks, and agent rows.

Pure functions over store rows, so the rules are testable without a database or a server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
                cost_usd=round(sum(t.cost_usd for t in tasks.values()), 2),
                actions=run_actions(status, tasks, run.run_id in driven),
            )
        )
    return out
