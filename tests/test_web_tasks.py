"""A task's detail view, and the operator's questions and instructions to its worker."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.config.schema import Config
from orchestrator.pipeline.scheduler import reopen_with_instruction
from orchestrator.pipeline.task import TaskState
from orchestrator.state.store import Store
from orchestrator.web import status as status_module
from orchestrator.web.runs import RunManager
from orchestrator.web.status import task_actions
from tests import fakes
from tests.test_web import _config, _daemon_app, _states

BLOCKED_OUTPUT = {
    "status": "blocked",
    "summary": "Added a regression test; it passes.",
    "blocked": {
        "reason": "technical",
        "details_markdown": "## Summary\nCould not reproduce the mismatch.",
        "questions_for_reporter": ["Which API produced the files?"],
    },
}


async def _blocked_run(cfg: Config, config_path: Path, store: Store) -> tuple[RunManager, str]:
    """A finished dry run whose one task, PROJ-1, the worker reported blocked."""
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1")})
    runs = RunManager(cfg, config_path, store, tracker_factory=lambda: tracker)
    fakes.SCRIPT["worker"].append(dict(BLOCKED_OUTPUT))
    submitted = await runs.submit(["PROJ-1"], workflow=None, dry_run=True, client="test")
    await asyncio.wait_for(runs.wait(submitted.run_id), 120)
    assert _states(store, submitted.run_id) == {"PROJ-1": "BLOCKED"}
    return runs, submitted.run_id


@pytest.mark.usefixtures("fake_runners")
async def test_a_blocked_task_explains_itself(config_path: Path, config_dict: dict) -> None:
    cfg = _config(config_dict)
    store = Store(cfg.db_path)
    runs, run_id = await _blocked_run(cfg, config_path, store)
    async with _daemon_app(cfg, config_path, runs, store) as client:
        d = (await client.get(f"/api/runs/{run_id}/tasks/PROJ-1")).json()
        missing = await client.get(f"/api/runs/{run_id}/tasks/PROJ-9")
        listed = (await client.get("/api/runs")).json()["runs"][0]
    assert listed["task_states"] == {"PROJ-1": "BLOCKED"}  # the Runs tab links to every task
    assert d["state"] == "BLOCKED" and d["stopped_at"] == "WORKING" and d["reason"] == "technical"
    assert "reported that the task cannot be done" in d["headline"] and "no pull request" in d["headline"]
    assert d["questions"] == ["Which API produced the files?"]
    assert "Could not reproduce the mismatch." in d["blocked_details"]
    assert d["findings_markdown"] and "Could not reproduce the mismatch." in d["findings_markdown"]
    assert d["worktree_exists"] and d["actions"] == ["ask", "instruct"] and d["conversation"] == []
    assert missing.status_code == 404


@pytest.mark.usefixtures("fake_runners")
async def test_a_question_is_answered_read_only_and_leaves_the_task_as_it_was(
    config_path: Path, config_dict: dict
) -> None:
    cfg = _config(config_dict)
    store = Store(cfg.db_path)
    runs, run_id = await _blocked_run(cfg, config_path, store)
    fakes.SCRIPT["worker"].append({"answer": "The save path reads the clock **once**."})
    async with _daemon_app(cfg, config_path, runs, store) as client:
        r = await client.post(f"/api/runs/{run_id}/tasks/PROJ-1/ask", json={"question": "Why no fix?"})
        assert r.status_code == 202, r.text
        await asyncio.wait_for(runs.wait(run_id), 120)
        d = (await client.get(f"/api/runs/{run_id}/tasks/PROJ-1")).json()
        blank = await client.post(f"/api/runs/{run_id}/tasks/PROJ-1/ask", json={"question": "  "})
    asked = fakes.CALLS["worker"][-1]
    assert asked.access.worktree == "read-only" and asked.session == "worker-session"
    assert "Why no fix?" in asked.prompt and asked.run_dir.name == "ask-1"
    assert [(m["kind"], m["text"]) for m in d["conversation"]] == [
        ("question", "Why no fix?"),
        ("answer", "The save path reads the clock **once**."),
    ]
    assert d["conversation"][0]["by"] == "127.0.0.1"
    assert d["state"] == "BLOCKED" and not d["answering"] and d["actions"] == ["ask", "instruct"]
    assert store.get_run(run_id).status == "finished"
    assert blank.status_code == 400
    audit = (cfg.runs_dir / run_id / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "operator_question"' in audit and '"event": "operator_answer"' in audit


@pytest.mark.usefixtures("fake_runners")
async def test_an_instruction_sends_the_blocked_task_back_to_the_worker(
    config_path: Path, config_dict: dict
) -> None:
    cfg = _config(config_dict)
    store = Store(cfg.db_path)
    runs, run_id = await _blocked_run(cfg, config_path, store)
    async with _daemon_app(cfg, config_path, runs, store) as client:
        r = await client.post(
            f"/api/runs/{run_id}/tasks/PROJ-1/instruct",
            json={"instruction": "Fix it in SetModifiedDate anyway."},
        )
        assert r.status_code == 202, r.text
        await asyncio.wait_for(runs.wait(run_id), 120)
        d = (await client.get(f"/api/runs/{run_id}/tasks/PROJ-1")).json()
        again = await client.post(f"/api/runs/{run_id}/tasks/PROJ-1/instruct", json={"instruction": "more"})
    resumed = next(c for c in fakes.CALLS["worker"] if "Fix it in SetModifiedDate anyway." in c.prompt)
    assert resumed.session == "worker-session" and "## The operator's instructions" in resumed.prompt
    assert resumed.access.worktree == "workspace-write"
    assert d["state"] == "DONE" and [m["kind"] for m in d["conversation"]] == ["instruction"]
    assert again.status_code == 409 and "only a blocked task" in again.json()["detail"]
    audit = (cfg.runs_dir / run_id / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "instructed"' in audit


def test_reopening_with_an_instruction_returns_to_the_worker() -> None:
    worked = TaskState(key="PROJ-1")
    for state in ("CONTEXT", "WORKTREE", "WORKING", "BLOCKED"):
        worked.transition(state)  # type: ignore[arg-type]
    reopen_with_instruction(worked, "  try the other API  ", "op")
    assert worked.state == "WORKING" and worked.instructions == ["try the other API"]

    reviewed = TaskState(key="PROJ-2", fix_reason="## Review findings to address")
    for state in ("WORKING", "BUILDING", "TESTING", "COMMITTING", "REVIEWING", "BLOCKED"):
        reviewed.transition(state)  # type: ignore[arg-type]
    reopen_with_instruction(reviewed, "ignore the style finding", "op")
    assert reviewed.state == "FIXING" and reviewed.fix_reason == "## Review findings to address"

    feature = TaskState(key="PROJ-3", workflow="feature", state="BLOCKED")
    with pytest.raises(ValueError, match="only bugfix tasks"):
        reopen_with_instruction(feature, "x", "op")
    with pytest.raises(ValueError, match="not BLOCKED"):
        reopen_with_instruction(TaskState(key="PROJ-4", state="DONE"), "x", "op")


@pytest.mark.parametrize(
    ("state", "workflow", "run_status", "worktree", "expected"),
    [
        ("BLOCKED", "bugfix", "finished", True, ["ask", "instruct"]),
        ("BLOCKED", "bugfix", "finished", False, ["instruct"]),
        ("BLOCKED", "feature", "finished", True, ["ask"]),
        ("BLOCKED", "bugfix", "running", True, []),
        ("BLOCKED", "bugfix", "queued", True, []),
        ("FAILED", "bugfix", "interrupted", True, ["ask"]),
        ("DONE", "bugfix", "finished", False, []),
    ],
)
def test_task_actions(
    state: str, workflow: str, run_status: str, worktree: bool, expected: list[str]
) -> None:
    task = TaskState(key="PROJ-1", state=state, workflow=workflow)  # type: ignore[arg-type]
    assert task_actions(task, run_status, worktree) == expected  # type: ignore[arg-type]


def test_findings_are_read_only_from_the_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    (run_dir / "PROJ-1").mkdir(parents=True)
    inside = run_dir / "PROJ-1" / "findings.md"
    inside.write_text("# Findings", encoding="utf-8")
    outside = tmp_path / "secret.md"
    outside.write_text("not for the page", encoding="utf-8")
    assert status_module._findings(str(inside), run_dir) == "# Findings"
    assert status_module._findings(str(outside), run_dir) is None
    assert (
        status_module._findings(str(run_dir / "PROJ-1" / ".." / ".." / ".." / "secret.md"), run_dir) is None
    )
    assert status_module._findings(None, run_dir) is None
