"""Run tasks concurrently with a parallelism cap, dependency ordering, and retries."""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable

from orchestrator.intake.base import TaskSpec, cyclic_keys
from orchestrator.pipeline import feature, stages
from orchestrator.pipeline.runtime import Runtime
from orchestrator.pipeline.task import Blocked, Failed, TaskState, Workflow

STAGES = {**stages.STAGES, **feature.STAGES}


async def _report(rt: Runtime, task: TaskState, handler: Awaitable[None]) -> None:
    """Reporting a terminal outcome (Jira, Confluence) must never take the run down with it."""
    try:
        await handler
    except Exception as e:  # noqa: BLE001
        rt.audit.record("report_error", task.key, error=repr(e))
        task.error = (task.error + "\n" if task.error else "") + f"reporting failed: {e!r}"


async def run_task(rt: Runtime, spec: TaskSpec, task: TaskState) -> TaskState:
    """Drive one task from its current state to a terminal state."""
    if task.state == "QUEUED":
        task.transition("CONTEXT")
        rt.store.save_task(rt.run_id, task)
        if "started" in rt.cfg.tracker.comment_on and not rt.dry_run:
            try:
                await rt.tracker.comment(task.key, f"Agent run {rt.run_id} started work on {task.key}.")
                if rt.cfg.tracker.statuses.in_progress:
                    await rt.tracker.transition(task.key, rt.cfg.tracker.statuses.in_progress)
            except Exception as e:  # noqa: BLE001 - never let write-back stop the work
                rt.audit.record("jira_error", task.key, error=str(e))
    attempts = 0
    while not task.terminal and not task.paused:
        rt.event(task.key, task.state, "")
        stage = STAGES[task.state]
        try:
            next_state = await stage(rt, spec, task)
            task.transition(next_state)
        except Blocked as b:
            rt.audit.record("blocked", task.key, reason=b.reason)
            await _report(rt, task, stages.handle_blocked(rt, spec, task, b))
            task.transition("BLOCKED")
        except Failed as f:
            attempts += 1
            if f.transient and attempts <= rt.cfg.scheduler.retry_infra_failures:
                rt.audit.record("retry", task.key, state=task.state, attempt=attempts, error=str(f))
                rt.store.save_task(rt.run_id, task)
                await asyncio.sleep(min(300, rt.cfg.scheduler.retry_backoff_seconds * attempts))
                continue
            rt.audit.record("failed", task.key, state=task.state, error=str(f))
            await _report(rt, task, stages.handle_failed(rt, spec, task, f))
            task.transition("FAILED")
        except Exception as e:  # noqa: BLE001
            rt.audit.record("failed", task.key, state=task.state, error=repr(e), trace=traceback.format_exc())
            failed = Failed(f"unexpected error in {task.state}: {e!r}")
            await _report(rt, task, stages.handle_failed(rt, spec, task, failed))
            task.transition("FAILED")
        rt.store.save_task(rt.run_id, task)
    if task.paused:
        rt.audit.record("paused", task.key, state=task.state)
        rt.event(task.key, task.state, f"resume {rt.run_id} --approve {task.key}")
        return task
    rt.event(task.key, task.state, task.error or task.pr_url or "")
    if task.state == "DONE" and task.worktree_path and not rt.keep_worktrees:
        from pathlib import Path

        await rt.worktrees.remove(Path(task.worktree_path))
    return task


async def run_all(
    rt: Runtime,
    specs: list[TaskSpec],
    existing: dict[str, TaskState] | None = None,
    workflow: Workflow | None = None,
) -> list[TaskState]:
    """Run every spec to a terminal or paused state. `workflow` overrides issue-type routing for new tasks."""
    existing = existing or {}
    tasks: dict[str, TaskState] = {}
    for spec in specs:
        state = existing.get(spec.key) or TaskState(
            key=spec.key,
            summary=spec.issue.summary,
            depends_on=spec.depends_on,
            epic_key=spec.epic_key,
            workflow=workflow or rt.cfg.workflows.workflow_for(spec.issue.issue_type),  # type: ignore[arg-type]
        )
        tasks[spec.key] = state
        rt.store.save_task(rt.run_id, state)
    sem = asyncio.Semaphore(rt.cfg.scheduler.max_parallel)
    done_events = {k: asyncio.Event() for k in tasks}
    # a dependency cycle would wait forever; its members are blocked before anything starts
    unrunnable = set(cyclic_keys(specs))
    for key in unrunnable:
        task = tasks[key]
        if not task.terminal:
            task.outcome = "blocked"
            task.error = "dependency cycle among: " + ", ".join(sorted(unrunnable))
            task.transition("BLOCKED")
            rt.store.save_task(rt.run_id, task)
            rt.audit.record("blocked", key, reason="dependency-cycle", members=sorted(unrunnable))
        done_events[key].set()

    async def one(spec: TaskSpec) -> TaskState:
        task = tasks[spec.key]
        if task.terminal:
            done_events[spec.key].set()
            return task
        for dep in spec.depends_on:
            if dep in done_events:
                await done_events[dep].wait()
                if tasks[dep].paused:
                    # the dependency waits for a person; this task stays queued for the resume
                    done_events[spec.key].set()
                    return task
                if tasks[dep].state != "DONE":
                    task.outcome = "blocked"
                    task.error = f"depends on {dep}, which ended {tasks[dep].state}"
                    task.transition("BLOCKED")
                    rt.store.save_task(rt.run_id, task)
                    done_events[spec.key].set()
                    return task
        async with sem:
            try:
                return await run_task(rt, spec, task)
            finally:
                done_events[spec.key].set()

    results = await asyncio.gather(*(one(s) for s in specs))
    return list(results)
