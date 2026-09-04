"""Markdown for the findings report (blocked work) and the per-task run page."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestrator.intake.base import TaskSpec
from orchestrator.pipeline.task import Blocked, TaskState

if TYPE_CHECKING:
    from orchestrator.pipeline.runtime import Runtime


def _copied(task: TaskState) -> list[str]:
    files = (task.worker_result or {}).get("copied_files", [])
    return [f"- `{c['from']}` -> `{c['to']}`" for c in files]


def findings_markdown(rt: Runtime, spec: TaskSpec, task: TaskState, blocked: Blocked) -> str:
    issue = spec.issue
    lines = [
        f"# {task.key}: work could not be completed",
        "",
        f"Issue: [{task.key}]({issue.url}) {issue.summary}  ",
        f"Run: `{rt.run_id}`  ",
        f"Reason: **{blocked.reason}**  ",
        f"Fix rounds used: {task.round} of {rt.cfg.agents.review_rounds}  ",
        f"Worker: {rt.role('worker').runner.name} ({rt.cfg.agents.worker.model or 'default model'})  ",
        f"Reviewer: {rt.role('reviewer').runner.name} ({rt.cfg.agents.reviewer.model or 'default model'})",
        "",
        "## Findings",
        "",
        blocked.details_markdown.strip(),
        "",
    ]
    if blocked.questions:
        lines += ["## Questions for the reporter", ""] + [f"- {q}" for q in blocked.questions] + [""]
    if task.worker_result and task.worker_result.get("summary"):
        lines += ["## What the worker did before stopping", "", task.worker_result["summary"], ""]
    if task.worker_result and task.worker_result.get("changed_paths"):
        lines += ["Changed paths:", ""] + [f"- `{p}`" for p in task.worker_result["changed_paths"]] + [""]
    if task.tests_run:
        lines += ["## Tests run", ""] + [f"- `{' '.join(c)}`" for c in task.tests_run] + [""]
    if _copied(task):
        lines += ["## Files copied between shares", ""] + _copied(task) + [""]
    if task.review_result:
        lines += ["## Last review", "", f"Verdict: {task.review_result.get('verdict')}", ""]
        for f in task.review_result.get("findings", []):
            lines.append(
                f"- **{f.get('severity')}** {f.get('title')}"
                + (f" (`{f.get('path')}`)" if f.get("path") else "")
                + (f": {f.get('detail')}" if f.get("detail") else "")
            )
        lines.append("")
    if task.worktree_path:
        lines += [
            "## Inspection",
            "",
            f"The worktree was kept at `{task.worktree_path}` on branch `{task.branch}`.",
            f"Logs and agent transcripts: `{rt.task_dir(task.key)}`",
            "",
        ]
    return "\n".join(lines)


def run_page(rt: Runtime, spec: TaskSpec, task: TaskState) -> str:
    issue = spec.issue
    lines = [
        f"# {task.key} agent run {rt.run_id}",
        "",
        f"Issue: [{task.key}]({issue.url}) {issue.summary}  ",
        f"Outcome: **{task.outcome or task.state}**  ",
        f"Pull request: {task.pr_url or '(none, dry run)'}  ",
        f"Branch: `{task.branch}`  ",
        f"Fix rounds: {task.round}  ",
        f"Estimated cost: ${task.cost_usd:.2f}",
        "",
        "## Summary",
        "",
        (task.worker_result or {}).get("summary", ""),
        "",
    ]
    if task.tests_run:
        lines += (
            ["## Tests run", ""]
            + [f"- `{' '.join(c)}`" for c in task.tests_run]
            + ["", "The full suite was not run; CI remains the authority.", ""]
        )
    if _copied(task):
        lines += ["## Files copied between shares", ""] + _copied(task) + [""]
    if task.review_result:
        lines += [
            "## Automated review",
            "",
            f"Verdict: {task.review_result.get('verdict')}",
            "",
            task.review_result.get("summary_markdown", ""),
            "",
        ]
    lines += ["## Timeline", ""] + [f"- {ts} {state}" for ts, state in task.history] + [""]
    return "\n".join(lines)
