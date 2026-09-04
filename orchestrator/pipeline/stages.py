"""One function per pipeline stage; each returns the next state."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from orchestrator.agents.base import Access, AgentRequest, AgentResult, Limits
from orchestrator.agents.contracts import (
    REVIEWER_SCHEMA,
    WORKER_SCHEMA,
    ContractError,
    ReviewerResult,
    WorkerResult,
    parse_reviewer,
    parse_worker,
)
from orchestrator.build.runner import StepResult, run_step
from orchestrator.build.selection import selector_for
from orchestrator.config.schema import Role
from orchestrator.docs.confluence import ConfluenceError
from orchestrator.environment import build_env, worktree_venv_bin
from orchestrator.intake.base import TaskSpec
from orchestrator.pipeline.runtime import Runtime
from orchestrator.pipeline.task import Blocked, Failed, State, TaskState
from orchestrator.reporting import findings as findings_report
from orchestrator.scm.git import GitError
from orchestrator.scm.github import ScmError
from orchestrator.scm.worktree import Worktree
from orchestrator.shares.cp import TASK_ENV
from orchestrator.shares.grants import stage_helper
from orchestrator.trackers.base import TrackerError

MAX_INLINE_DIFF = 120_000


# ---------------------------------------------------------------------------
# helpers


def _worktree(rt: Runtime, task: TaskState) -> Worktree:
    if not task.worktree_path or not task.branch:
        raise Failed("task has no worktree; cannot continue from this state")
    return Worktree(Path(task.worktree_path), task.branch, rt.cfg.repo.base_branch)


def _context_dir(wt: Worktree) -> Path:
    d = wt.path / ".orchestrator" / "context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _agent_env(rt: Runtime, role: Role, task: TaskState, wt_path: Path, run_dir: Path) -> dict[str, str]:
    rr = rt.role(role)
    bin_dir, helper_env = stage_helper(run_dir, rr.grants, rt.audit.path)
    extra = dict(rt.cfg.build.env)
    extra.update(helper_env)
    extra[TASK_ENV] = task.key
    extra["PIP_CACHE_DIR"] = str(rt.cfg.cache_dir / "pip")
    prepend = [bin_dir]
    venv_bin = worktree_venv_bin(wt_path)
    if venv_bin:
        prepend.append(venv_bin)
    return build_env(secrets=rr.secrets, extra=extra, prepend_path=prepend)


def _build_env(rt: Runtime, task: TaskState, wt_path: Path) -> dict[str, str]:
    extra = dict(rt.cfg.build.env)
    extra["PIP_CACHE_DIR"] = str(rt.cfg.cache_dir / "pip")
    prepend = []
    venv_bin = worktree_venv_bin(wt_path)
    if venv_bin:
        prepend.append(venv_bin)
    return build_env(extra=extra, prepend_path=prepend)


def _limits(rt: Runtime, role: Role) -> Limits:
    rc = rt.cfg.agents.role(role)
    return Limits(
        timeout_seconds=rc.timeout_minutes * 60,
        max_turns=rc.max_turns,
        max_budget_usd=rc.max_budget_usd,
    )


def _access(rt: Runtime, role: Role) -> Access:
    rc = rt.cfg.agents.role(role)
    return Access(worktree=rc.access, grants=rt.role(role).grants)


async def _run_agent(
    rt: Runtime,
    role: Role,
    task: TaskState,
    *,
    cwd: Path,
    prompt: str,
    schema: dict[str, Any],
    session: str | None,
    label: str,
) -> AgentResult:
    rr = rt.role(role)
    rc = rt.cfg.agents.role(role)
    run_dir = rt.task_dir(task.key) / label
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.md").write_text(prompt)
    system_prompt = rt.render(f"{role}.system.md", role=rc, task=task)
    request = AgentRequest(
        cwd=cwd,
        prompt=prompt,
        role=role,
        schema=schema,
        limits=_limits(rt, role),
        access=_access(rt, role),
        env=_agent_env(rt, role, task, cwd, run_dir),
        run_dir=run_dir,
        model=rc.model,
        session=session if rr.runner.capabilities.session_resume else None,
        system_prompt=system_prompt,
        mcp_servers=rr.mcp_servers,
        deny_tools=rr.deny_tools,
        options=dict(rc.options),
        prompt_and_parse=not rr.runner.capabilities.structured_output,
    )
    rt.audit.record("agent_start", task.key, role=role, runner=rr.runner.name, label=label, model=rc.model)
    result = await rr.runner.run(request)
    (run_dir / "result.json").write_text(
        json.dumps(rt.redactor.redact_obj(result.__dict__), indent=2, default=str)
    )
    if result.cost_usd:
        task.cost_usd += result.cost_usd
    rt.audit.record(
        "agent_end",
        task.key,
        role=role,
        label=label,
        ok=result.ok,
        termination=result.termination,
        cost_usd=result.cost_usd,
        turns=result.num_turns,
        session=result.session_id,
        error=result.error,
    )
    budget = rc.max_budget_usd
    if budget and result.cost_usd and result.cost_usd > budget and not rr.runner.capabilities.budget_cap:
        raise Failed(f"{role} spent ${result.cost_usd:.2f}, over the ${budget:.2f} budget")
    return result


def _reprompt_text(error: str) -> str:
    return (
        "Your previous final message did not match the required JSON contract:\n\n"
        f"{error}\n\nReply again with only the corrected JSON object."
    )


async def _agent_with_contract(
    rt: Runtime,
    role: Role,
    task: TaskState,
    parse: Any,
    **kwargs: Any,
) -> tuple[Any, AgentResult]:
    result = await _run_agent(rt, role, task, **kwargs)
    if result.termination == "timeout":
        raise Failed(f"{role} timed out after {_limits(rt, role).timeout_seconds // 60} minutes")
    if result.termination in ("max_turns", "max_budget"):
        raise Failed(f"{role} hit its {result.termination.replace('_', ' ')} limit")
    if not result.ok and result.structured_output is None:
        raise Failed(f"{role} failed: {result.error or result.termination}\n{result.stderr_tail}")
    try:
        return parse(result.structured_output or {}), result
    except ContractError as e:
        # one corrective pass, resuming the same session where possible
        retry = await _run_agent(
            rt,
            role,
            task,
            cwd=kwargs["cwd"],
            prompt=_reprompt_text(str(e)),
            schema=kwargs["schema"],
            session=result.session_id or kwargs.get("session"),
            label=kwargs["label"] + "-reformat",
        )
        try:
            return parse(retry.structured_output or {}), retry
        except ContractError as e2:
            raise Failed(f"{role} output did not match the contract twice: {e2}") from e2


# ---------------------------------------------------------------------------
# stages


async def stage_context(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    """Fetch issue details and Confluence pages into the task dir (copied into the worktree later)."""
    issue = spec.issue
    ctx_dir = rt.task_dir(task.key) / "context"
    ctx_dir.mkdir(exist_ok=True)
    lines = [
        f"# {issue.key}: {issue.summary}",
        "",
        f"Type: {issue.issue_type}  ",
        f"Status: {issue.status}  ",
        f"URL: {issue.url}",
        "",
    ]
    lines += ["## Description", "", issue.description_markdown or "(none)", ""]
    if issue.acceptance_criteria:
        lines += ["## Acceptance criteria", "", issue.acceptance_criteria, ""]
    if issue.links:
        lines += (
            ["## Linked issues", ""]
            + [f"- {rel}: {key} {summary}" for rel, key, summary in issue.links]
            + [""]
        )
    if issue.attachments:
        lines += ["## Attachments", ""]
    (ctx_dir / "issue.md").write_text("\n".join(lines))
    (ctx_dir / "issue.json").write_text(json.dumps(issue.raw, indent=2))
    if issue.comments:
        parts = ["# Comments", ""]
        for author, created, body in issue.comments:
            parts += [f"## {author} ({created})", "", body, ""]
        (ctx_dir / "comments.md").write_text("\n".join(parts))
    downloaded = []
    for att in issue.attachments:
        if att.size <= rt.cfg.tracker.attachment_max_bytes and att.url:
            dest = ctx_dir / "attachments" / att.filename
            try:
                await rt.tracker.download_attachment(att, dest)
                downloaded.append(
                    f"- {att.filename} ({att.size} bytes, {att.mime_type}) -> attachments/{att.filename}"
                )
            except TrackerError as e:
                downloaded.append(f"- {att.filename}: not downloaded ({e})")
        else:
            downloaded.append(f"- {att.filename} ({att.size} bytes): over the size cap, not downloaded")
    if downloaded:
        with (ctx_dir / "issue.md").open("a") as f:
            f.write("\n".join(downloaded) + "\n")
    if rt.confluence and rt.cfg.confluence:
        conf_dir = ctx_dir / "confluence"
        conf_dir.mkdir(exist_ok=True)
        for url in rt.cfg.confluence.context_pages:
            try:
                title, md = await rt.confluence.fetch_page_markdown(url, rt.cfg.cache_dir)
                safe = (
                    "".join(c if c.isalnum() or c in "-_ " else "_" for c in title).strip().replace(" ", "-")
                )
                (conf_dir / f"{safe}.md").write_text(f"# {title}\n\nSource: {url}\n\n{md}")
            except ConfluenceError as e:
                (conf_dir / "errors.md").open("a").write(f"- {url}: {e}\n")
    task.summary = issue.summary
    return "WORKTREE"


async def stage_worktree(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    try:
        wt = await rt.worktrees.create(task.key, task.summary or spec.issue.summary)
    except GitError as e:
        raise Failed(f"worktree: {e}", transient=True) from e
    task.branch, task.worktree_path = wt.branch, str(wt.path)
    rt.audit.record("worktree_created", task.key, path=str(wt.path), branch=wt.branch)
    # context files move into the worktree so the agent reads files, not a giant prompt
    src = rt.task_dir(task.key) / "context"
    dst = _context_dir(wt)
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    env = _build_env(rt, task, wt.path)
    if rt.cfg.build.setup:
        step = await run_step(
            "setup",
            rt.cfg.build.setup,
            cwd=wt.path,
            env=env,
            timeout_seconds=rt.cfg.build.timeout_minutes * 60,
            log_dir=rt.task_dir(task.key) / "logs",
            serialize=True,
        )
        if not step.ok:
            raise Failed(f"build.setup failed:\n{step.failure_excerpt()}")
    if rt.cfg.hooks.after_worktree:
        step = await run_step(
            "after_worktree",
            rt.cfg.hooks.after_worktree,
            cwd=wt.path,
            env=env,
            timeout_seconds=rt.cfg.build.timeout_minutes * 60,
            log_dir=rt.task_dir(task.key) / "logs",
        )
        if not step.ok:
            raise Failed(f"hooks.after_worktree failed:\n{step.failure_excerpt()}")
    return "WORKING"


def _prompt_common(rt: Runtime, spec: TaskSpec, task: TaskState, wt: Worktree, role: Role) -> dict[str, Any]:
    ctx = _context_dir(wt)
    rr = rt.role(role)
    files = sorted(str(p.relative_to(wt.path)) for p in ctx.rglob("*") if p.is_file())
    return {
        "issue": spec.issue,
        "task": task,
        "worktree": str(wt.path),
        "base_branch": wt.base,
        "context_files": files,
        "build": rt.cfg.build,
        "test": rt.cfg.test,
        "shares": [
            {
                "name": g.name,
                "path": str(g.path),
                "mode": g.mode,
                "write_under": [str(p) for p in g.write_under],
            }
            for g in rr.grants
        ],
        "mcp_servers": list(rr.mcp_servers),
        "deny_commands": ["git push", "gh", "curl", "wget"],
        "review_rounds": rt.cfg.agents.review_rounds,
    }


async def stage_work(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    wt = _worktree(rt, task)
    prompt = rt.render("worker.md", **_prompt_common(rt, spec, task, wt, "worker"))
    result_model: WorkerResult
    result_model, result = await _agent_with_contract(
        rt,
        "worker",
        task,
        parse_worker,
        cwd=wt.path,
        prompt=prompt,
        schema=WORKER_SCHEMA,
        session=None,
        label="worker",
    )
    task.worker_session = result.session_id
    task.worker_result = result_model.model_dump(by_alias=True)
    if result_model.status == "blocked":
        b = result_model.blocked
        raise Blocked(
            b.reason if b else "technical",
            b.details_markdown
            if b
            else result_model.summary or "The worker declared the task blocked without details.",
            b.questions_for_reporter if b else [],
        )
    return "BUILDING"


async def stage_fix(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    wt = _worktree(rt, task)
    rr = rt.role("worker")
    ctx = _prompt_common(rt, spec, task, wt, "worker")
    ctx["fix_reason"] = task.fix_reason or ""
    ctx["previous_summary"] = (task.worker_result or {}).get("summary", "")
    ctx["resumed"] = bool(task.worker_session and rr.runner.capabilities.session_resume)
    prompt = rt.render("fixer.md", **ctx)
    result_model, result = await _agent_with_contract(
        rt,
        "worker",
        task,
        parse_worker,
        cwd=wt.path,
        prompt=prompt,
        schema=WORKER_SCHEMA,
        session=task.worker_session,
        label=f"fix-{task.round}",
    )
    task.worker_session = result.session_id or task.worker_session
    merged = result_model.model_dump(by_alias=True)
    prev = task.worker_result or {}
    merged["copied_files"] = prev.get("copied_files", []) + merged.get("copied_files", [])
    task.worker_result = merged
    if result_model.status == "blocked":
        b = result_model.blocked
        raise Blocked(
            b.reason if b else "technical",
            b.details_markdown if b else result_model.summary,
            b.questions_for_reporter if b else [],
        )
    return "BUILDING"


def _fail_or_fix(rt: Runtime, task: TaskState, reason_md: str, kind: str, details: str) -> State:
    if task.round < rt.cfg.agents.review_rounds:
        task.round += 1
        task.fix_reason = reason_md
        rt.audit.record("fix_round", task.key, round=task.round, kind=kind)
        return "FIXING"
    raise Blocked(
        "technical",
        f"{details}\n\nThe allowed number of fix rounds ({rt.cfg.agents.review_rounds}) was used up.\n\n{reason_md}",
    )


async def stage_build(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    wt = _worktree(rt, task)
    if not rt.cfg.build.commands:
        return "TESTING"
    step = await run_step(
        f"build-r{task.round}",
        rt.cfg.build.commands,
        cwd=wt.path,
        env=_build_env(rt, task, wt.path),
        timeout_seconds=rt.cfg.build.timeout_minutes * 60,
        log_dir=rt.task_dir(task.key) / "logs",
        serialize=True,
    )
    rt.audit.record("build", task.key, ok=step.ok, round=task.round)
    if step.ok:
        return "TESTING"
    return _fail_or_fix(
        rt,
        task,
        f"## Build failed\n\n{step.summary()}\n\n```\n{step.failure_excerpt()}\n```",
        "build",
        "The build failed and the worker could not repair it.",
    )


async def stage_test(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    wt = _worktree(rt, task)
    changed = await rt.worktrees.changed_paths(wt)
    agent_choice = (task.worker_result or {}).get("tests_selected", [])
    commands = selector_for(rt.cfg.test.selection).select(changed, agent_choice)
    task.tests_run = commands
    if not commands:
        rt.audit.record("tests", task.key, ok=True, commands=[], note="no tests selected")
        return "COMMITTING"
    step: StepResult = await run_step(
        f"test-r{task.round}",
        commands,
        cwd=wt.path,
        env=_build_env(rt, task, wt.path),
        timeout_seconds=rt.cfg.test.timeout_minutes * 60,
        log_dir=rt.task_dir(task.key) / "logs",
    )
    rt.audit.record("tests", task.key, ok=step.ok, commands=commands, round=task.round)
    if step.ok:
        return "COMMITTING"
    return _fail_or_fix(
        rt,
        task,
        f"## Tests failed\n\n{step.summary()}\n\n```\n{step.failure_excerpt()}\n```",
        "test",
        "The selected tests failed and the worker could not repair them.",
    )


async def stage_commit(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    wt = _worktree(rt, task)
    summary = (task.worker_result or {}).get("summary") or spec.issue.summary
    model = rt.cfg.agents.worker.model or rt.role("worker").runner.name
    message = f"{task.key}: {spec.issue.summary}\n\n{summary}\n\nOrchestrator-Run: {rt.run_id}\nAgent: {rt.role('worker').runner.name} {model}"
    try:
        sha, excluded = await rt.worktrees.commit_all(wt, message)
    except GitError as e:
        raise Failed(f"commit: {e}") from e
    task.excluded_from_commit = excluded
    if sha is None:
        raise Blocked(
            "technical", "The worker reported completion but the working tree has no changes to commit."
        )
    task.commit_sha = sha
    rt.audit.record("commit", task.key, sha=sha, excluded=excluded)
    return "REVIEWING"


async def stage_review(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    wt = _worktree(rt, task)
    rr = rt.role("reviewer")
    diff = await rt.worktrees.diff(wt)
    diff_path = rt.task_dir(task.key) / f"diff-r{task.round}.patch"
    diff_path.write_text(diff)
    cwd = wt.path
    if rt.cfg.agents.reviewer.access == "read-only" and not rr.runner.capabilities.read_only_mode:
        cwd = await rt.worktrees.snapshot_copy(wt, rt.cfg.repo.worktree_root / f"{task.key}-review")
    ctx = _prompt_common(rt, spec, task, wt, "reviewer")
    ctx.update(
        diff=diff if len(diff) <= MAX_INLINE_DIFF else "",
        diff_path=str(diff_path),
        diff_truncated=len(diff) > MAX_INLINE_DIFF,
        worker=task.worker_result or {},
        tests_run=[" ".join(c) for c in task.tests_run],
        round=task.round,
        cwd=str(cwd),
    )
    prompt = rt.render("reviewer.md", **ctx)
    review: ReviewerResult
    review, result = await _agent_with_contract(
        rt,
        "reviewer",
        task,
        parse_reviewer,
        cwd=cwd,
        prompt=prompt,
        schema=REVIEWER_SCHEMA,
        session=None,
        label=f"review-r{task.round}",
    )
    task.reviewer_session = result.session_id
    task.review_result = review.model_dump()
    rt.audit.record(
        "review", task.key, verdict=review.verdict, findings=len(review.findings), round=task.round
    )
    if cwd != wt.path:
        await rt.worktrees.remove(cwd, force=True)
    if review.verdict == "approve" or not review.actionable:
        return "PUSHING"
    md = ["## Review findings to address", ""]
    for f in review.actionable:
        loc = f" ({f.path}:{f.line})" if f.path and f.line else f" ({f.path})" if f.path else ""
        md.append(
            f"- **{f.severity}** {f.title}{loc}\n  {f.detail}"
            + (f"\n  Suggested fix: {f.suggested_fix}" if f.suggested_fix else "")
        )
    return _fail_or_fix(rt, task, "\n".join(md), "review", "The reviewer still had blocking findings.")


async def stage_push(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    wt = _worktree(rt, task)
    if rt.cfg.hooks.before_pr:
        step = await run_step(
            "before_pr",
            rt.cfg.hooks.before_pr,
            cwd=wt.path,
            env=_build_env(rt, task, wt.path),
            timeout_seconds=600,
            log_dir=rt.task_dir(task.key) / "logs",
        )
        if not step.ok:
            raise Failed(f"hooks.before_pr failed:\n{step.failure_excerpt()}")
    if rt.dry_run:
        rt.audit.record("push_skipped", task.key, branch=wt.branch, dry_run=True)
        return "OPENING_PR"
    if not rt.github_token:
        raise Failed("no GitHub token available to push")
    try:
        await rt.worktrees.push(wt, rt.github_token)
    except GitError as e:
        raise Failed(f"push: {rt.redactor.redact(str(e))}", transient=True) from e
    rt.audit.record("push", task.key, branch=wt.branch, sha=task.commit_sha)
    return "OPENING_PR"


def pr_body(rt: Runtime, spec: TaskSpec, task: TaskState) -> str:
    worker = task.worker_result or {}
    review = task.review_result or {}
    lines = [
        f"Resolves [{task.key}]({spec.issue.url}): {spec.issue.summary}",
        "",
        "## Summary",
        "",
        worker.get("summary", ""),
        "",
    ]
    lines += ["## Tests run", ""]
    lines += [f"- `{' '.join(c)}`" for c in task.tests_run] or ["- none selected"]
    if worker.get("test_rationale"):
        lines += ["", worker["test_rationale"]]
    lines += [
        "",
        "- [ ] The full test suite has **not** been run by the orchestrator; CI remains the authority.",
        "",
    ]
    if worker.get("copied_files"):
        lines += (
            ["## Files copied between shares", ""]
            + [f"- `{c['from']}` -> `{c['to']}`" for c in worker["copied_files"]]
            + [""]
        )
    if review:
        lines += [
            "## Automated review",
            "",
            f"Verdict: **{review.get('verdict', '')}** after {task.round} fix round(s).",
            "",
        ]
        if review.get("summary_markdown"):
            lines += [review["summary_markdown"], ""]
        carried = [f for f in review.get("findings", []) if f.get("severity") in ("minor", "nit")]
        if carried:
            lines += ["Minor notes left for the human reviewer:", ""]
            lines += [
                f"- **{f['severity']}** {f['title']}"
                + (f" (`{f['path']}`)" if f.get("path") else "")
                + (f": {f['detail']}" if f.get("detail") else "")
                for f in carried
            ]
            lines.append("")
    if task.excluded_from_commit:
        lines += ["## Excluded from the commit", ""] + [f"- `{p}`" for p in task.excluded_from_commit] + [""]
    lines += [
        "---",
        f"Orchestrator run `{rt.run_id}` · worker {rt.role('worker').runner.name} ({rt.cfg.agents.worker.model or 'default model'}) · reviewer {rt.role('reviewer').runner.name} ({rt.cfg.agents.reviewer.model or 'default model'})",
    ]
    return "\n".join(lines)


async def stage_pr(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    wt = _worktree(rt, task)
    title = rt.cfg.repo.pr.title_template.format(key=task.key, summary=spec.issue.summary)[:250]
    body = pr_body(rt, spec, task)
    (rt.task_dir(task.key) / "pr-body.md").write_text(body)
    if rt.dry_run or rt.github is None:
        rt.audit.record("pr_skipped", task.key, title=title, dry_run=True)
        task.pr_url = None
        return "REPORTING"
    try:
        pr = await rt.github.open_pr(
            head=wt.branch, base=wt.base, title=title, body=body, draft=rt.cfg.repo.pr.draft
        )
    except ScmError as e:
        raise Failed(f"pull request: {e}", transient=True) from e
    task.pr_url = pr.url
    rt.audit.record("pr_opened", task.key, url=pr.url, number=pr.number)
    return "REPORTING"


async def stage_report(rt: Runtime, spec: TaskSpec, task: TaskState) -> State:
    """Completed path: Jira comment and transition, optional Confluence page."""
    task.outcome = "completed"
    body = f"Agent run {rt.run_id} completed {task.key}."
    body += f"\nPull request: {task.pr_url}" if task.pr_url else "\n(dry run: no pull request opened)"
    if task.tests_run:
        body += "\nTests run: " + "; ".join(" ".join(c) for c in task.tests_run)
    await _jira_writeback(
        rt,
        task,
        comment=body if "pr_opened" in rt.cfg.tracker.comment_on else None,
        status=rt.cfg.tracker.statuses.in_review,
    )
    if (
        rt.confluence
        and rt.cfg.confluence
        and rt.cfg.confluence.publish
        and "completed" in rt.cfg.confluence.publish.when
    ):
        md = findings_report.run_page(rt, spec, task)
        await _confluence_publish(rt, task, f"{task.key} agent run {rt.run_id}", md)
    return "DONE"


async def _jira_writeback(
    rt: Runtime, task: TaskState, *, comment: str | None, status: str | None, attach: Path | None = None
) -> None:
    if rt.dry_run:
        rt.audit.record(
            "jira_skipped",
            task.key,
            comment=comment,
            status=status,
            attach=str(attach) if attach else None,
            dry_run=True,
        )
        return
    try:
        if comment:
            await rt.tracker.comment(task.key, comment)
            rt.audit.record("jira_comment", task.key, body=comment)
        if attach:
            await rt.tracker.attach(task.key, attach)
            rt.audit.record("jira_attach", task.key, file=attach.name)
        if status:
            await rt.tracker.transition(task.key, status)
            rt.audit.record("jira_transition", task.key, status=status)
    except TrackerError as e:
        rt.audit.record("jira_error", task.key, error=str(e))
        task.error = (task.error + "\n" if task.error else "") + f"Jira write-back failed: {e}"


async def _confluence_publish(rt: Runtime, task: TaskState, title: str, markdown: str) -> None:
    if rt.dry_run or not rt.confluence:
        rt.audit.record("confluence_skipped", task.key, title=title, dry_run=True)
        return
    try:
        url = await rt.confluence.publish(title, markdown)
        task.confluence_url = url
        rt.audit.record("confluence_page", task.key, url=url, title=title)
    except ConfluenceError as e:
        rt.audit.record("confluence_error", task.key, error=str(e))
        task.error = (task.error + "\n" if task.error else "") + f"Confluence publish failed: {e}"


async def handle_blocked(rt: Runtime, spec: TaskSpec, task: TaskState, blocked: Blocked) -> None:
    task.outcome = "blocked"
    task.error = blocked.reason
    md = findings_report.findings_markdown(rt, spec, task, blocked)
    path = rt.task_dir(task.key) / "findings.md"
    path.write_text(md)
    task.findings_path = str(path)
    comment = f"Agent run {rt.run_id} could not complete {task.key} ({blocked.reason}). See the attached findings.md."
    await _jira_writeback(
        rt,
        task,
        comment=comment if "blocked" in rt.cfg.tracker.comment_on else None,
        status=rt.cfg.tracker.statuses.blocked,
        attach=path,
    )
    if (
        rt.confluence
        and rt.cfg.confluence
        and rt.cfg.confluence.publish
        and "blocked" in rt.cfg.confluence.publish.when
    ):
        await _confluence_publish(rt, task, f"{task.key} agent findings {rt.run_id}", md)


async def handle_failed(rt: Runtime, spec: TaskSpec, task: TaskState, failed: Failed) -> None:
    task.outcome = "failed"
    task.error = rt.redactor.redact(str(failed))
    if "failed" in rt.cfg.tracker.comment_on:
        await _jira_writeback(
            rt,
            task,
            comment=f"Agent run {rt.run_id} hit an orchestrator error on {task.key}: {task.error[:500]}",
            status=None,
        )


STAGES = {
    "CONTEXT": stage_context,
    "WORKTREE": stage_worktree,
    "WORKING": stage_work,
    "BUILDING": stage_build,
    "TESTING": stage_test,
    "COMMITTING": stage_commit,
    "REVIEWING": stage_review,
    "FIXING": stage_fix,
    "PUSHING": stage_push,
    "OPENING_PR": stage_pr,
    "REPORTING": stage_report,
}
