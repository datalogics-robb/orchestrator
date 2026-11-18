"""The operator's questions to a task's worker, answered without changing the task.

An answer comes from the worker's own session when its runtime can resume one, so it answers from
what it did and saw; otherwise a fresh session is given the findings report and the conversation so
far. Either way the worker runs with read-only access to the worktree and its shares, and the task's
state is left as it was: a question is a conversation, not another attempt.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from orchestrator.agents.base import Access
from orchestrator.agents.contracts import ANSWER_SCHEMA
from orchestrator.pipeline import stages
from orchestrator.pipeline.runtime import Runtime
from orchestrator.pipeline.task import TaskState

FINDINGS_PROMPT_CHARS = 30_000
"""How much of the findings report a fresh session is given inline."""


class CannotAsk(Exception):
    """The task has nothing a worker could answer from; the message says why."""


def _read_only(rt: Runtime) -> Access:
    grants = tuple(dataclasses.replace(g, mode="read", write_under=()) for g in rt.role("worker").grants)
    return Access(worktree="read-only", grants=grants)


def _findings(task: TaskState) -> str:
    if not task.findings_path:
        return ""
    try:
        return Path(task.findings_path).read_text(encoding="utf-8")[:FINDINGS_PROMPT_CHARS]
    except OSError:
        return ""


def check_askable(task: TaskState) -> Path:
    """The worktree the worker answers in; raises CannotAsk when there is none."""
    if not task.worktree_path:
        raise CannotAsk(f"{task.key} has no worktree yet, so there is no work to ask about")
    wt = Path(task.worktree_path)
    if not wt.is_dir():
        raise CannotAsk(f"{task.key}'s worktree {wt} has been removed")
    return wt


async def answer(rt: Runtime, task: TaskState, question: str) -> str:
    """Ask the task's worker `question`; returns its answer in Markdown and records the exchange.

    The question must already be in `task.conversation`. Agent failures are recorded as an `error`
    entry and re-raised; the caller saves the task either way.
    """
    wt = check_askable(task)
    rr = rt.role("worker")
    resumed = bool(task.worker_session and rr.runner.capabilities.session_resume)
    asked = sum(1 for m in task.conversation if m["kind"] == "question")
    prompt = rt.render(
        "ask.md",
        key=task.key,
        summary=task.summary,
        task=task,
        resumed=resumed,
        question=question,
        findings="" if resumed else _findings(task),
        earlier=[] if resumed else task.conversation[:-1],
    )
    interrupted = task.interrupted
    try:
        result = await stages._run_agent(
            rt,
            "worker",
            task,
            cwd=wt,
            prompt=prompt,
            schema=ANSWER_SCHEMA,
            session=task.worker_session if resumed else None,
            label=f"ask-{asked}",
            access=_read_only(rt),
        )
    except Exception as e:
        task.say("error", f"The worker could not answer: {rt.redactor.redact(repr(e))}", "worker")
        raise
    finally:
        task.interrupted = interrupted  # a question never marks the task's work as interrupted
    structured = result.structured_output or {}
    text = str(structured.get("answer") or "").strip() or result.raw_text.strip()
    if not result.ok and not text:
        message = rt.redactor.redact(result.error or result.termination)
        task.say("error", f"The worker could not answer: {message}", "worker")
        rt.audit.record("operator_answer_failed", task.key, error=message)
        return ""
    if resumed and result.session_id:
        task.worker_session = result.session_id  # the session now holds the exchange too
    text = rt.redactor.redact(text)
    task.say("answer", text, "worker")
    rt.audit.record("operator_answer", task.key, chars=len(text), cost_usd=result.cost_usd)
    return text
