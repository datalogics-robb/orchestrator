"""Feature workflow stages: specify, pause for approval, write failing tests, prove red, implement.

The default (bugfix) flow goes WORKTREE -> WORKING. A feature task goes
WORKTREE -> SPECIFYING -> AWAITING_APPROVAL -> TEST_WRITING -> RED_CHECK -> IMPLEMENTING -> BUILDING
and then shares the build, test, commit, review, and PR stages. The red commit made in RED_CHECK
survives the final squash so the pull request shows the tests failing before the implementation.
"""

from __future__ import annotations

import re
import shlex

from orchestrator.agents.contracts import (
    REVIEWER_SCHEMA,
    SPEC_SCHEMA,
    WORKER_SCHEMA,
    ReviewerResult,
    SpecResult,
    WorkerResult,
    parse_reviewer,
    parse_spec,
    parse_worker,
)
from orchestrator.build.runner import run_step
from orchestrator.build.selection import selector_for
from orchestrator.intake.base import TaskSpec
from orchestrator.pipeline import stages
from orchestrator.pipeline.runtime import Runtime
from orchestrator.pipeline.task import Blocked, Failed, State, TaskState
from orchestrator.reporting.spec import spec_markdown
from orchestrator.scm.git import GitError

MAX_EVIDENCE = 6000


def _write_spec_context(rt: Runtime, task: TaskState) -> None:
    """The specification and the approver's decisions travel with the worktree like the issue does."""
    wt = stages._worktree(rt, task)
    ctx = stages._context_dir(wt)
    md = spec_markdown(task)
    (ctx / "spec.md").write_text(md)
    (rt.task_dir(task.key) / "spec.md").write_text(md)
    if task.decisions:
        (ctx / "decisions.md").write_text(
            f"# Approver's decisions for {task.key}\n\n{task.decisions.strip()}\n"
        )


def _blocked_from(result: WorkerResult | SpecResult) -> Blocked:
    b = result.blocked
    return Blocked(
        b.reason if b else "technical",
        b.details_markdown
        if b
        else (result.summary or "The worker declared the task blocked without details."),
        b.questions_for_reporter if b else [],
    )


async def stage_specify(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    """The worker turns the issue into acceptance criteria, an API surface, and a test list; the reviewer critiques it."""
    wt = stages._worktree(rt, task)
    ctx = stages._prompt_common(rt, spec, task, wt, "worker")
    ctx["previous_spec_md"] = spec_markdown(task, with_review=True) if task.spec else ""
    prompt = rt.render("spec.md", **ctx)
    result_model: SpecResult
    result_model, result = await stages._agent_with_contract(
        rt,
        "worker",
        task,
        parse_spec,
        cwd=wt.path,
        prompt=prompt,
        schema=SPEC_SCHEMA,
        session=None,
        label=f"spec-{task.spec_revision}",
    )
    task.worker_session = result.session_id
    if result_model.status == "blocked":
        raise _blocked_from(result_model)
    if not result_model.acceptance_criteria or not result_model.tests:
        raise Blocked(
            "ambiguous-requirements",
            "The specification names no acceptance criteria or no tests, so there is nothing to build to.\n\n"
            + result_model.summary,
            result_model.questions_for_reporter,
        )
    task.spec = result_model.model_dump()
    task.spec_review = None
    rt.audit.record(
        "spec",
        task.key,
        revision=task.spec_revision,
        criteria=len(result_model.acceptance_criteria),
        tests=len(result_model.tests),
        questions=len(result_model.questions_for_reporter),
    )
    if rt.cfg.workflows.feature.spec_review:
        cwd = await stages.review_cwd(rt, task, wt)
        rctx = stages._prompt_common(rt, spec, task, wt, "reviewer")
        rctx.update(spec_md=spec_markdown(task, with_review=False), cwd=str(cwd))
        review: ReviewerResult
        review, rresult = await stages._agent_with_contract(
            rt,
            "reviewer",
            task,
            parse_reviewer,
            cwd=cwd,
            prompt=rt.render("spec_review.md", **rctx),
            schema=REVIEWER_SCHEMA,
            session=None,
            label=f"spec-review-{task.spec_revision}",
        )
        if cwd != wt.path:
            await rt.worktrees.remove(cwd, force=True)
        task.spec_review = review.model_dump()
        rt.audit.record(
            "spec_review",
            task.key,
            verdict=review.verdict,
            findings=len(review.findings),
            revision=task.spec_revision,
        )
    _write_spec_context(rt, task)
    if not rt.cfg.workflows.feature.require_approval:
        return "TEST_WRITING"
    path = rt.task_dir(task.key) / "spec.md"
    comment = (
        f"Agent run {rt.run_id} drafted a specification for {task.key}; see the attached spec.md. "
        f"Approve it with `orchestrator resume {rt.run_id} --approve {task.key}` (add `--decisions FILE` for "
        f"answers and instructions) or send it back with `--revise {task.key} --decisions FILE`."
    )
    await stages._jira_writeback(rt, task, comment=comment, status=None, attach=path)
    return "AWAITING_APPROVAL"


def approve(task: TaskState, decisions: str) -> None:
    """A person accepted the specification; any text they add is binding on the worker."""
    if not task.paused:
        raise ValueError(f"{task.key} is {task.state}, not awaiting approval")
    if decisions.strip():
        task.decisions = (task.decisions + "\n\n" if task.decisions else "") + decisions.strip()
    task.transition("TEST_WRITING")


def revise(task: TaskState, decisions: str, max_revisions: int) -> None:
    """A person sent the specification back with instructions; the worker rewrites it."""
    if not task.paused:
        raise ValueError(f"{task.key} is {task.state}, not awaiting approval")
    if not decisions.strip():
        raise ValueError("--revise needs --decisions with what to change")
    if task.spec_revision >= max_revisions:
        raise ValueError(f"{task.key} has used all {max_revisions} specification revisions")
    task.spec_revision += 1
    task.decisions = (task.decisions + "\n\n" if task.decisions else "") + decisions.strip()
    task.transition("SPECIFYING")


async def stage_test_writing(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    """Red: the tests from the specification plus the smallest interface that lets them compile and fail."""
    wt = stages._worktree(rt, task)
    _write_spec_context(rt, task)
    rr = rt.role("worker")
    ctx = stages._prompt_common(rt, spec, task, wt, "worker")
    ctx["fix_reason"] = task.fix_reason or ""
    ctx["resumed"] = bool(task.fix_reason and task.worker_session and rr.runner.capabilities.session_resume)
    ctx["require_red"] = rt.cfg.workflows.feature.require_red
    prompt = rt.render("red.md", **ctx)
    result_model: WorkerResult
    result_model, result = await stages._agent_with_contract(
        rt,
        "worker",
        task,
        parse_worker,
        cwd=wt.path,
        prompt=prompt,
        schema=WORKER_SCHEMA,
        session=task.worker_session if ctx["resumed"] else None,
        label=f"red-{task.round}",
    )
    task.worker_session = result.session_id or task.worker_session
    merged = result_model.model_dump(by_alias=True)
    prev = task.worker_result or {}
    merged["copied_files"] = prev.get("copied_files", []) + merged.get("copied_files", [])
    task.worker_result = merged
    task.fix_reason = None
    if result_model.status == "blocked":
        raise _blocked_from(result_model)
    return "RED_CHECK"


def _red_retry(rt: Runtime, task: TaskState, reason_md: str, kind: str) -> State:
    return stages._fail_or_fix(
        rt,
        task,
        reason_md,
        kind,
        "The tests could not be brought to a failing (red) state before the implementation.",
        next_state="TEST_WRITING",
    )


async def stage_red_check(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    """Build the tests, run them, require them to fail for the right reason, and commit them as the red commit."""
    wt = stages._worktree(rt, task)
    cfg = rt.cfg.workflows.feature
    env = stages._build_env(rt, task, wt.path)
    log_dir = rt.task_dir(task.key) / "logs"
    if rt.cfg.build.commands:
        build = await run_step(
            f"red-build-r{task.round}",
            rt.cfg.build.commands,
            cwd=wt.path,
            env=env,
            timeout_seconds=rt.cfg.build.timeout_minutes * 60,
            log_dir=log_dir,
            serialize=True,
        )
        rt.audit.record("red_build", task.key, ok=build.ok, round=task.round)
        if not build.ok:
            return _red_retry(
                rt,
                task,
                "## The tests do not build\n\nThe red commit must compile: add the declarations and stub "
                f"implementations the tests need, without implementing the behaviour.\n\n{build.summary()}\n\n"
                f"```\n{build.failure_excerpt()}\n```",
                "red-build",
            )
    changed = await rt.worktrees.changed_paths(wt)
    agent_choice = (task.worker_result or {}).get("tests_selected", [])
    commands = selector_for(rt.cfg.test.selection).select(changed, agent_choice)
    task.tests_run = commands
    if not commands:
        return _red_retry(
            rt,
            task,
            "## No tests selected\n\nReport the exact test commands for the new tests in `tests_selected`.",
            "red-select",
        )
    step = await run_step(
        f"red-test-r{task.round}",
        commands,
        cwd=wt.path,
        env=env,
        timeout_seconds=rt.cfg.test.timeout_minutes * 60,
        log_dir=log_dir,
    )
    rt.audit.record("red_tests", task.key, failed=not step.ok, commands=commands, round=task.round)
    if cfg.require_red:
        if step.ok:
            return _red_retry(
                rt,
                task,
                "## The new tests pass before the implementation\n\nA test that passes without the feature "
                "proves nothing. Make the tests assert the behaviour the specification promises so they fail "
                "now and pass once it exists:\n\n" + "\n".join(f"- `{' '.join(c)}`" for c in commands),
                "red-passed",
            )
        excerpt = step.failure_excerpt()
        for pattern in cfg.red_reject_patterns:
            if re.search(pattern, excerpt, re.IGNORECASE | re.MULTILINE):
                return _red_retry(
                    rt,
                    task,
                    f"## The failure is not a real red\n\nThe test output matches `{pattern}`, which marks a "
                    "missing resource or a skipped test rather than a failed assertion. Make the tests run and "
                    f"fail on the behaviour itself.\n\n```\n{excerpt}\n```",
                    "red-rejected",
                )
    task.red_evidence = step.failure_excerpt()[:MAX_EVIDENCE] if not step.ok else ""
    summary = (task.worker_result or {}).get("summary") or "Tests for the specification."
    message = stages.commit_message(rt, spec, task, summary, phase="red")
    try:
        excluded = await rt.worktrees.stage_all(wt)
        has_changes = await rt.worktrees.has_staged_changes(wt)
        files = await rt.worktrees.staged_files(wt)
    except GitError as e:
        raise Failed(f"red commit: {e}") from e
    task.excluded_from_commit = excluded
    if not has_changes:
        return _red_retry(
            rt,
            task,
            "## Nothing to commit\n\nThe worker reported tests but the tree is unchanged.",
            "red-empty",
        )
    hook_output = await stages.precommit_gate(rt, task, wt, files, f"red-r{task.round}")
    if hook_output is not None:
        return _red_retry(
            rt,
            task,
            "## Pre-commit hooks failed on the tests\n\nFix what they report; do not bypass them.\n\n"
            f"```\n{hook_output}\n```",
            "red-pre-commit",
        )
    try:
        sha = await rt.worktrees.commit_staged(wt, message, env=env)
    except GitError as e:
        raise Failed(f"red commit: {e}") from e
    if sha is None:
        return _red_retry(rt, task, "## Nothing remained to commit after the hooks ran.", "red-empty")
    task.phase_commits.append(sha)
    task.red_tests = commands
    rt.audit.record("red_commit", task.key, sha=sha, tests=commands, round=task.round)
    return "IMPLEMENTING"


async def stage_implement(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    """Green: implement the specification so the red tests pass, without weakening them."""
    wt = stages._worktree(rt, task)
    rr = rt.role("worker")
    ctx = stages._prompt_common(rt, spec, task, wt, "worker")
    ctx.update(
        red_tests=[shlex.join(c) for c in task.red_tests],
        red_evidence=task.red_evidence,
        previous_summary=(task.worker_result or {}).get("summary", ""),
        resumed=bool(task.worker_session and rr.runner.capabilities.session_resume),
    )
    prompt = rt.render("green.md", **ctx)
    result_model: WorkerResult
    result_model, result = await stages._agent_with_contract(
        rt,
        "worker",
        task,
        parse_worker,
        cwd=wt.path,
        prompt=prompt,
        schema=WORKER_SCHEMA,
        session=task.worker_session if ctx["resumed"] else None,
        label="green",
    )
    task.worker_session = result.session_id or task.worker_session
    merged = result_model.model_dump(by_alias=True)
    prev = task.worker_result or {}
    merged["copied_files"] = prev.get("copied_files", []) + merged.get("copied_files", [])
    task.worker_result = merged
    if result_model.status == "blocked":
        raise _blocked_from(result_model)
    return "BUILDING"


STAGES = {
    "SPECIFYING": stage_specify,
    "TEST_WRITING": stage_test_writing,
    "RED_CHECK": stage_red_check,
    "IMPLEMENTING": stage_implement,
}
