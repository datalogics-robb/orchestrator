"""Per-run summary in Markdown and JSON."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.pipeline.task import TaskState


def run_report_markdown(run_id: str, tasks: list[TaskState], dry_run: bool) -> str:
    lines = [f"# Orchestrator run {run_id}" + (" (dry run)" if dry_run else ""), ""]
    counts = {"DONE": 0, "BLOCKED": 0, "FAILED": 0}
    for t in tasks:
        counts[t.state] = counts.get(t.state, 0) + 1
    lines.append(
        f"Completed: {counts.get('DONE', 0)}  Blocked: {counts.get('BLOCKED', 0)}  Failed: {counts.get('FAILED', 0)}  Cost: ${sum(t.cost_usd for t in tasks):.2f}"
    )
    lines += ["", "| Issue | State | Rounds | Result |", "|---|---|---|---|"]
    for t in tasks:
        result = t.pr_url or (t.findings_path and f"findings: {t.findings_path}") or t.error or ""
        lines.append(f"| {t.key} | {t.state} | {t.round} | {result} |")
    lines.append("")
    return "\n".join(lines)


def write_run_report(run_dir: Path, run_id: str, tasks: list[TaskState], dry_run: bool) -> Path:
    md = run_report_markdown(run_id, tasks, dry_run)
    (run_dir / "report.md").write_text(md)
    (run_dir / "report.json").write_text(json.dumps([t.to_json() for t in tasks], indent=2, default=str))
    return run_dir / "report.md"
