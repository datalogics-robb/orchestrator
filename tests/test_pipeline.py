"""End-to-end dry runs of the pipeline with fake agents and a temporary git repo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator.config.loader import load_config
from orchestrator.intake.base import ExplicitKeys, TaskSpec
from orchestrator.pipeline.runtime import build_runtime
from orchestrator.pipeline.scheduler import run_all
from orchestrator.scm.worktree import WorktreeManager
from tests import fakes


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


async def _run(config_path: Path, tracker: fakes.FakeTracker, keys: list[str]):
    cfg = load_config(config_path)
    rt = build_runtime(cfg, config_path, dry_run=True, keep_worktrees=True, tracker=tracker)
    rt.store.create_run(rt.run_id, config_path, keys, True)
    specs = await ExplicitKeys(tracker, keys).tasks()
    results = await run_all(rt, specs)
    return rt, results


@pytest.mark.usefixtures("fake_runners")
async def test_happy_path(config_path: Path, git_repo: tuple[Path, Path], shares: tuple[Path, Path]) -> None:
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1")})
    rt, results = await _run(config_path, tracker, ["PROJ-1"])
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.outcome == "completed" and task.commit_sha and task.branch == "agent/proj-1-do-the-thing"
    # one squashed commit on the branch containing the agent's change
    wt = Path(task.worktree_path)
    log = subprocess.run(
        ["git", "log", "--oneline", "origin/develop..HEAD"], cwd=wt, capture_output=True, text=True
    ).stdout
    assert len(log.strip().splitlines()) == 1
    assert (wt / "agent_change.txt").exists()
    # context files landed in the worktree and are excluded from git
    assert (wt / ".orchestrator" / "context" / "issue.md").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True).stdout
    assert ".orchestrator" not in status
    # the share copy happened through the audited helper
    _, raid = shares
    assert (raid / "agent-drops" / "SF1" / "input.pdf").exists()
    audit = [json.loads(line) for line in rt.audit.path.read_text().splitlines()]
    assert any(e["event"] == "share_copy" for e in audit)
    assert task.worker_result["copied_files"][0]["to"].startswith("raid:")
    # dry run: nothing external
    assert tracker.comments == [] and tracker.transitions == []
    assert any(e["event"] == "push_skipped" for e in audit) and any(e["event"] == "pr_skipped" for e in audit)
    # the reviewer saw the diff and ran read-only
    review_req = fakes.CALLS["reviewer"][0]
    assert review_req.access.worktree == "read-only"
    assert "agent_change.txt" in review_req.prompt
    # the worker never received the orchestrator's tokens
    worker_env = fakes.CALLS["worker"][0].env
    assert "TEST_GITHUB_TOKEN" not in worker_env and "TEST_JIRA_TOKEN" not in worker_env
    assert worker_env["TEST_WORKER_KEY"].startswith("secret-") and "TEST_REVIEWER_KEY" not in worker_env
    assert (rt.task_dir("PROJ-1") / "pr-body.md").read_text().startswith("Resolves [PROJ-1]")
    # secrets do not appear in the audit log
    assert "secret-test" not in rt.audit.path.read_text()


@pytest.mark.usefixtures("fake_runners")
async def test_review_requests_changes_then_approves(config_path: Path) -> None:
    fakes.SCRIPT["reviewer"].extend(
        [
            {
                "verdict": "request_changes",
                "findings": [
                    {
                        "severity": "major",
                        "title": "needs a test",
                        "path": "agent_change.txt",
                        "detail": "add one",
                    }
                ],
                "summary_markdown": "",
            },
            {
                "verdict": "approve",
                "findings": [{"severity": "nit", "title": "style"}],
                "summary_markdown": "ok now",
            },
        ]
    )
    tracker = fakes.FakeTracker({"PROJ-2": fakes.issue("PROJ-2")})
    rt, results = await _run(config_path, tracker, ["PROJ-2"])
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.round == 1
    assert len(fakes.CALLS["worker"]) == 2
    fix_req = fakes.CALLS["worker"][1]
    assert "needs a test" in fix_req.prompt and fix_req.session == "worker-session"
    body = (rt.task_dir("PROJ-2") / "pr-body.md").read_text()
    assert "style" in body and "after 1 fix round" in body


@pytest.mark.usefixtures("fake_runners")
async def test_worker_blocked_produces_findings(config_path: Path) -> None:
    fakes.SCRIPT["worker"].append(
        {
            "status": "blocked",
            "summary": "",
            "blocked": {
                "reason": "ambiguous-requirements",
                "details_markdown": "Which file?",
                "questions_for_reporter": ["Which file?"],
            },
        }
    )
    tracker = fakes.FakeTracker({"PROJ-3": fakes.issue("PROJ-3")})
    rt, results = await _run(config_path, tracker, ["PROJ-3"])
    task = results[0]
    assert task.state == "BLOCKED" and task.outcome == "blocked"
    findings = Path(task.findings_path).read_text()
    assert "ambiguous-requirements" in findings and "Which file?" in findings
    assert Path(task.worktree_path).exists()  # kept for inspection
    assert len(fakes.CALLS["reviewer"]) == 0


@pytest.mark.usefixtures("fake_runners")
async def test_rounds_exhausted_blocks(config_path: Path) -> None:
    fakes.SCRIPT["reviewer"].extend(
        [
            {"verdict": "request_changes", "findings": [{"severity": "blocking", "title": f"bad {i}"}]}
            for i in range(3)
        ]
    )
    tracker = fakes.FakeTracker({"PROJ-4": fakes.issue("PROJ-4")})
    rt, results = await _run(config_path, tracker, ["PROJ-4"])
    task = results[0]
    assert task.state == "BLOCKED"
    assert task.round == 2 and len(fakes.CALLS["worker"]) == 3
    assert "fix rounds (2)" in Path(task.findings_path).read_text()


@pytest.mark.usefixtures("fake_runners")
async def test_build_failure_triggers_fix_round(config_path: Path, config_dict: dict, tmp_path: Path) -> None:
    import yaml

    flag = tmp_path / "pass.flag"
    config_dict["build"]["commands"] = [
        f"python3 -c \"import sys,os; sys.exit(0 if os.path.exists('{flag}') else 1)\""
    ]
    config_path.write_text(yaml.safe_dump(config_dict))
    # first worker call leaves the build failing; the fix round creates the flag
    fakes.SCRIPT["worker"].extend(
        [
            {
                "status": "completed",
                "summary": "first try",
                "changed_paths": ["agent_change.txt"],
                "tests_selected": [],
                "test_rationale": "",
                "copied_files": [],
                "_write": "v1",
            },
        ]
    )
    original = fakes.default_worker_output

    def fixing(request):
        flag.write_text("ok")
        return original(request)

    fakes.default_worker_output = fixing
    try:
        tracker = fakes.FakeTracker({"PROJ-5": fakes.issue("PROJ-5")})
        rt, results = await _run(config_path, tracker, ["PROJ-5"])
    finally:
        fakes.default_worker_output = original
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.round == 1
    assert "Build failed" in fakes.CALLS["worker"][1].prompt


@pytest.mark.usefixtures("fake_runners")
async def test_epic_expands_and_orders(config_path: Path) -> None:
    epic = fakes.issue("PROJ-10", "Epic", issue_type="Epic")
    a = fakes.issue("PROJ-11", "child a", raw={"parent": "PROJ-10"})
    b = fakes.issue("PROJ-12", "child b", raw={"parent": "PROJ-10"}, blocked_by=["PROJ-11"])
    tracker = fakes.FakeTracker({"PROJ-10": epic, "PROJ-11": a, "PROJ-12": b})
    rt, results = await _run(config_path, tracker, ["PROJ-10"])
    assert [t.key for t in results] == ["PROJ-11", "PROJ-12"]
    assert all(t.state == "DONE" for t in results), [t.error for t in results]
    assert results[1].depends_on == ["PROJ-11"]
    # the dependent task started only after the first finished
    started = {
        k: next(ts for ts, s in t.history if s == "WORKING") for k, t in zip(("a", "b"), results, strict=True)
    }
    finished_a = results[0].history[-1][0]
    assert started["b"] >= finished_a


@pytest.mark.usefixtures("fake_runners")
async def test_bare_reviewer_uses_prompt_and_parse_and_snapshot(config_path: Path, config_dict: dict) -> None:
    import yaml

    config_dict["agents"]["reviewer"]["runner"] = "fake-bare"
    config_path.write_text(yaml.safe_dump(config_dict))
    tracker = fakes.FakeTracker({"PROJ-6": fakes.issue("PROJ-6")})
    rt, results = await _run(config_path, tracker, ["PROJ-6"])
    task = results[0]
    assert task.state == "DONE", task.error
    req = fakes.CALLS["reviewer"][0]
    assert req.prompt_and_parse is True
    assert req.cwd != Path(task.worktree_path)  # throwaway snapshot for a runtime without read-only mode
    assert not req.cwd.exists()  # removed afterwards


@pytest.mark.usefixtures("fake_runners")
async def test_resume_from_checkpoint(config_path: Path) -> None:
    tracker = fakes.FakeTracker({"PROJ-7": fakes.issue("PROJ-7")})
    rt, results = await _run(config_path, tracker, ["PROJ-7"])
    task = results[0]
    assert task.state == "DONE"
    saved = rt.store.load_tasks(rt.run_id)["PROJ-7"]
    assert saved.state == "DONE" and saved.commit_sha == task.commit_sha
    assert rt.store.get_run(rt.run_id).keys == ["PROJ-7"]


@pytest.mark.usefixtures("fake_runners")
async def test_pre_commit_hooks_gate_the_commit(config_path: Path) -> None:
    tracker = fakes.FakeTracker({"PROJ-8": fakes.issue("PROJ-8")})
    rt, results = await _run(config_path, tracker, ["PROJ-8"])
    task = results[0]
    assert task.state == "DONE", task.error
    log = (Path(task.worktree_path) / ".orchestrator" / "precommit.log").read_text().splitlines()
    # installed into the worktree after setup, then run on the staged files before the commit
    assert log[0].startswith("install")
    assert any(line.startswith("run --files") and "agent_change.txt" in line for line in log)
    # the installed git hook ran inside the orchestrator's commit, with the venv on PATH
    assert any(line.startswith("run --hook-stage commit") for line in log)
    audit = [json.loads(line) for line in rt.audit.path.read_text().splitlines()]
    assert any(e["event"] == "pre_commit_install" and e["ok"] for e in audit)
    assert any(e["event"] == "pre_commit" and e["ok"] for e in audit)


@pytest.mark.usefixtures("fake_runners")
async def test_pre_commit_failure_becomes_a_fix_round(config_path: Path) -> None:
    # the first worker pass leaves a marker that makes the fake hook fail; the fix round removes it
    fakes.SCRIPT["worker"].append(
        {
            "status": "completed",
            "summary": "first try",
            "changed_paths": ["agent_change.txt"],
            "tests_selected": [],
            "test_rationale": "",
            "copied_files": [],
            "_write": "v1",
            "_touch": "precommit-fail",
        }
    )
    original = fakes.default_worker_output

    def fixing(request):
        (request.cwd / "precommit-fail").unlink(missing_ok=True)
        return original(request)

    fakes.default_worker_output = fixing
    try:
        tracker = fakes.FakeTracker({"PROJ-9": fakes.issue("PROJ-9")})
        rt, results = await _run(config_path, tracker, ["PROJ-9"])
    finally:
        fakes.default_worker_output = original
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.round == 1
    assert "Pre-commit hooks failed" in fakes.CALLS["worker"][1].prompt
    assert "fake-hook" in fakes.CALLS["worker"][1].prompt


async def test_moving_base_does_not_stage_reverts_of_upstream(
    config_path: Path, git_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    origin, _clone = git_repo
    cfg = load_config(config_path)
    manager = WorktreeManager(cfg.repo)
    wt = await manager.create("PROJ-7", "Do the thing")
    assert wt.base_sha == _git("rev-parse", "HEAD", cwd=wt.path).strip()
    # upstream moves while this worktree is busy, and another worktree's fetch picks it up
    other = tmp_path / "other"
    _git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=other)
    _git("config", "user.name", "Test", cwd=other)
    (other / "upstream.txt").write_text("landed after the worktree was created\n")
    _git("add", "upstream.txt", cwd=other)
    _git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "upstream", cwd=other)
    _git("push", "-q", "origin", "develop", cwd=other)
    await manager.fetch_base()
    # nothing changed in the worktree: nothing to commit, and no deletion of upstream.txt
    assert await manager.stage_all(wt) == []
    assert not await manager.has_staged_changes(wt)
    (wt.path / "mine.txt").write_text("my change\n")
    await manager.stage_all(wt)
    assert await manager.staged_files(wt) == ["mine.txt"]
    status = _git("diff", "--cached", "--name-status", cwd=wt.path)
    assert "upstream.txt" not in status
    assert await manager.changed_paths(wt) == ["mine.txt"]


@pytest.mark.usefixtures("fake_runners")
async def test_deletion_only_change_is_committed(config_path: Path) -> None:
    fakes.SCRIPT["worker"].append(
        {
            "_delete": "README.md",
            "status": "completed",
            "summary": "Removed the stale README.",
            "changed_paths": ["README.md"],
            "tests_selected": ["python3 -c pass"],
            "test_rationale": "nothing to run",
        }
    )
    tracker = fakes.FakeTracker({"PROJ-2": fakes.issue("PROJ-2")})
    _rt, results = await _run(config_path, tracker, ["PROJ-2"])
    task = results[0]
    assert task.state == "DONE", task.error
    show = _git("show", "--name-status", "--format=", "HEAD", cwd=Path(task.worktree_path))
    assert show.split() == ["D", "README.md"]


@pytest.mark.usefixtures("fake_runners")
async def test_dependency_cycle_blocks_its_members_instead_of_hanging(config_path: Path) -> None:
    cfg = load_config(config_path)
    tracker = fakes.FakeTracker({k: fakes.issue(k) for k in ["PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4"]})
    rt = build_runtime(cfg, config_path, dry_run=True, keep_worktrees=True, tracker=tracker)
    rt.store.create_run(rt.run_id, config_path, ["PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4"], True)
    specs = [
        TaskSpec(await tracker.get_issue("PROJ-4"), []),
        TaskSpec(await tracker.get_issue("PROJ-1"), ["PROJ-2"]),
        TaskSpec(await tracker.get_issue("PROJ-2"), ["PROJ-1"]),
        TaskSpec(await tracker.get_issue("PROJ-3"), ["PROJ-2"]),
    ]
    results = {t.key: t for t in await run_all(rt, specs)}
    assert results["PROJ-4"].state == "DONE", results["PROJ-4"].error
    for key in ["PROJ-1", "PROJ-2", "PROJ-3"]:
        assert results[key].state == "BLOCKED"
        assert "dependency cycle" in (results[key].error or "")
    assert fakes.CALLS["worker"] and all(r.cwd.name == "PROJ-4" for r in fakes.CALLS["worker"])
