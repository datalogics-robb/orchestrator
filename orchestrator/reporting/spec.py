"""Markdown rendering of a feature task's specification and its review, for people and prompts."""

from __future__ import annotations

from orchestrator.pipeline.task import TaskState


def spec_markdown(task: TaskState, *, with_review: bool = True) -> str:
    spec = task.spec or {}
    lines = [
        f"# {task.key}: specification (revision {task.spec_revision})",
        "",
        spec.get("summary", "").strip(),
        "",
        "## Acceptance criteria",
        "",
    ]
    lines += [f"{i}. {c}" for i, c in enumerate(spec.get("acceptance_criteria", []), 1)] or ["(none)"]
    lines += ["", "## Public API and interface changes", ""]
    lines += [f"- {a}" for a in spec.get("api_surface", [])] or ["- none"]
    lines += ["", "## Tests that will prove it", ""]
    for t in spec.get("tests", []):
        proves = f": {t['proves']}" if t.get("proves") else ""
        lines.append(f"- `{t.get('name', '')}`{proves}")
    if not spec.get("tests"):
        lines.append("- (none)")
    if spec.get("assumptions"):
        lines += ["", "## Assumptions", ""] + [f"- {a}" for a in spec["assumptions"]]
    if spec.get("risks"):
        lines += ["", "## Risks", ""] + [f"- {r}" for r in spec["risks"]]
    if spec.get("questions_for_reporter"):
        lines += ["", "## Questions for the approver", ""] + [
            f"- {q}" for q in spec["questions_for_reporter"]
        ]
    if with_review and task.spec_review:
        review = task.spec_review
        lines += ["", "## Reviewer's critique", "", f"Verdict: **{review.get('verdict', '')}**", ""]
        if review.get("summary_markdown"):
            lines += [review["summary_markdown"], ""]
        for f in review.get("findings", []):
            lines.append(
                f"- **{f.get('severity')}** {f.get('title')}"
                + (f": {f.get('detail')}" if f.get("detail") else "")
                + (f" Suggested: {f.get('suggested_fix')}" if f.get("suggested_fix") else "")
            )
    if task.decisions:
        lines += ["", "## Approver's decisions", "", task.decisions.strip()]
    lines.append("")
    return "\n".join(lines)
