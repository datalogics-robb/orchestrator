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


# --- feature workflow ------------------------------------------------------------


def _feature_config(config_dict: dict, config_path: Path, **feature: object) -> None:
    import yaml

    config_dict["test"]["selection"] = {"strategy": "agent-chosen", "allowed_prefixes": ["python3"]}
    config_dict["workflows"] = {"feature": feature} if feature else {}
    config_path.write_text(yaml.safe_dump(config_dict))


def _failing_until(flag: Path) -> str:
    return f"python3 -c \"import os, sys; sys.exit(0 if os.path.exists('{flag}') else 1)\""


SPEC = {
    "status": "completed",
    "summary": "Add a frob option to the converter.",
    "acceptance_criteria": ["1. frob() exists and returns 7", "2. frob(-1) is rejected"],
    "api_surface": ["frob(): new public call; appended to the table so existing callers are unaffected"],
    "tests": [{"name": "test_frob", "proves": "1, 2"}],
    "assumptions": ["frob is off by default"],
    "risks": [],
    "questions_for_reporter": ["Should frob be on by default?"],
}


@pytest.mark.usefixtures("fake_runners")
async def test_feature_workflow_pauses_for_approval_then_goes_red_then_green(
    config_path: Path, config_dict: dict, tmp_path: Path
) -> None:
    from orchestrator.pipeline import feature

    _feature_config(config_dict, config_path)
    flag = tmp_path / "frob.implemented"
    red_cmd = _failing_until(flag)
    fakes.SCRIPT["worker"].extend(
        [
            SPEC,
            {
                "_write": "test_frob: asserts frob() == 7",
                "status": "completed",
                "summary": "Added test_frob and a frob() stub that fails.",
                "changed_paths": ["agent_change.txt"],
                "tests_selected": [red_cmd],
                "test_rationale": "fails until frob exists",
            },
            {
                "_touch": str(flag),
                "_write": "frob implemented; test_frob unchanged",
                "status": "completed",
                "summary": "Implemented frob.",
                "changed_paths": ["agent_change.txt"],
                "tests_selected": ["python3 -c pass"],
                "test_rationale": "red tests plus the converter group",
            },
        ]
    )
    fakes.SCRIPT["reviewer"].extend(
        [
            {
                "verdict": "request_changes",
                "findings": [{"severity": "major", "title": "criterion 2 names no error code"}],
                "summary_markdown": "Say which error frob(-1) raises.",
            },
            {
                "verdict": "approve",
                "findings": [
                    {"severity": "major", "title": "should frob also handle NaN?", "spec_gap": True}
                ],
                "summary_markdown": "Meets the specification.",
            },
        ]
    )
    tracker = fakes.FakeTracker({"PROJ-9": fakes.issue("PROJ-9", issue_type="Story")})
    rt, results = await _run(config_path, tracker, ["PROJ-9"])
    task = results[0]
    assert task.workflow == "feature"
    assert task.state == "AWAITING_APPROVAL", task.error
    assert task.spec["acceptance_criteria"] == SPEC["acceptance_criteria"]
    assert task.spec_review["verdict"] == "request_changes"
    spec_md = (rt.task_dir("PROJ-9") / "spec.md").read_text()
    assert "criterion 2 names no error code" in spec_md and "Should frob be on by default?" in spec_md
    wt = Path(task.worktree_path)
    assert (wt / ".orchestrator" / "context" / "spec.md").exists()
    assert not (wt / "agent_change.txt").exists()  # nothing written during specification
    audit = [json.loads(line) for line in rt.audit.path.read_text().splitlines()]
    assert any(e["event"] == "jira_skipped" and (e.get("attach") or "").endswith("spec.md") for e in audit)
    assert len(fakes.CALLS["worker"]) == 1 and len(fakes.CALLS["reviewer"]) == 1
    assert "do not modify any file" in fakes.CALLS["worker"][0].prompt

    # a person approves with a decision; the run resumes from the store
    feature.approve(task, "Yes: frob is on by default.")
    rt.store.save_task(rt.run_id, task)
    specs = await ExplicitKeys(tracker, ["PROJ-9"]).tasks()
    results = await run_all(rt, specs, rt.store.load_tasks(rt.run_id))
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.round == 0
    assert (
        (wt / ".orchestrator" / "context" / "decisions.md").read_text().endswith("frob is on by default.\n")
    )
    # red commit survives the squash; green is the only commit after it
    log = _git("log", "--format=%H %s", "origin/develop..HEAD", cwd=wt).strip().splitlines()
    assert len(log) == 2
    green_sha, green_msg = log[0].split(" ", 1)
    red_sha, red_msg = log[1].split(" ", 1)
    assert red_msg.endswith("(red)") and green_msg.endswith("(green)")
    assert task.phase_commits == [red_sha] and task.commit_sha == green_sha
    assert "exit 1" in task.red_evidence or red_cmd.split()[0] in task.red_evidence
    # the red tests ran again in the green check, ahead of what the worker listed
    assert task.tests_run[0][0] == "python3" and "os.path.exists" in " ".join(task.tests_run[0])
    assert ["python3", "-c", "pass"] in task.tests_run
    red_req, green_req = fakes.CALLS["worker"][1], fakes.CALLS["worker"][2]
    assert (
        "tests only" in red_req.prompt
        and "frob is on by default" in (wt / ".orchestrator/context/decisions.md").read_text()
    )
    assert "make the tests pass" in green_req.prompt and "os.path.exists" in green_req.prompt
    review_req = fakes.CALLS["reviewer"][1]
    assert "approved specification" in review_req.prompt and "frob is on by default" in review_req.prompt
    body = (rt.task_dir("PROJ-9") / "pr-body.md").read_text()
    assert "Red, then green" in body and red_sha in body and green_sha in body
    assert "Open design questions" in body and "NaN" in body
    assert "1. frob() exists and returns 7" in body


@pytest.mark.usefixtures("fake_runners")
async def test_feature_red_check_rejects_passing_and_resource_failures(
    config_path: Path, config_dict: dict, tmp_path: Path
) -> None:
    _feature_config(
        config_dict,
        config_path,
        require_approval=False,
        spec_review=False,
        red_reject_patterns=["ResourceProblem"],
    )
    flag = tmp_path / "frob.implemented"
    red_cmd = _failing_until(flag)
    base = {"status": "completed", "changed_paths": ["agent_change.txt"], "test_rationale": ""}
    fakes.SCRIPT["worker"].extend(
        [
            SPEC,
            {**base, "_write": "t1", "summary": "tests", "tests_selected": ["python3 -c pass"]},
            {
                **base,
                "_write": "t2",
                "summary": "tests",
                "tests_selected": ["python3 -c \"import sys; print('ResourceProblem 55'); sys.exit(1)\""],
            },
            {**base, "_write": "t3", "summary": "tests", "tests_selected": [red_cmd]},
            {**base, "_touch": str(flag), "_write": "impl", "summary": "implemented", "tests_selected": []},
        ]
    )
    tracker = fakes.FakeTracker({"PROJ-10": fakes.issue("PROJ-10", issue_type="Improvement")})
    rt, results = await _run(config_path, tracker, ["PROJ-10"])
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.round == 2 and len(task.phase_commits) == 1
    prompts = [r.prompt for r in fakes.CALLS["worker"]]
    assert "pass before the implementation" in prompts[2]
    assert "not a real red" in prompts[3] and "ResourceProblem" in prompts[3]
    assert "attempt 3" in prompts[3]
    assert len(fakes.CALLS["reviewer"]) == 1  # code review only; spec review was off


@pytest.mark.usefixtures("fake_runners")
async def test_workflow_routing_and_override(config_path: Path, config_dict: dict) -> None:
    _feature_config(config_dict, config_path)
    cfg = load_config(config_path)
    assert cfg.workflows.workflow_for("Story") == "feature"
    assert cfg.workflows.workflow_for("bug") == "bugfix"
    fakes.SCRIPT["worker"].append({**SPEC, "summary": "spec for a Bug run as a feature"})
    tracker = fakes.FakeTracker({"PROJ-11": fakes.issue("PROJ-11", issue_type="Bug")})
    rt = build_runtime(cfg, config_path, dry_run=True, keep_worktrees=True, tracker=tracker)
    rt.store.create_run(rt.run_id, config_path, ["PROJ-11"], True)
    specs = await ExplicitKeys(tracker, ["PROJ-11"]).tasks()
    results = await run_all(rt, specs, workflow="feature")
    assert results[0].workflow == "feature" and results[0].state == "AWAITING_APPROVAL"


@pytest.mark.usefixtures("fake_runners")
async def test_feature_pauses_after_red_when_configured(
    config_path: Path, config_dict: dict, tmp_path: Path
) -> None:
    from orchestrator.pipeline import feature

    _feature_config(config_dict, config_path, require_approval=False, spec_review=False, pause_after_red=True)
    flag = tmp_path / "frob.implemented"
    red_cmd = _failing_until(flag)
    base = {"status": "completed", "changed_paths": ["agent_change.txt"], "test_rationale": ""}
    fakes.SCRIPT["worker"].extend(
        [
            SPEC,
            {**base, "_write": "t1", "summary": "tests v1", "tests_selected": [red_cmd]},
            {**base, "_write": "t2", "summary": "tests v2, stronger", "tests_selected": [red_cmd]},
            {**base, "_touch": str(flag), "_write": "impl", "summary": "implemented", "tests_selected": []},
        ]
    )
    tracker = fakes.FakeTracker({"PROJ-12": fakes.issue("PROJ-12", issue_type="Story")})
    rt, results = await _run(config_path, tracker, ["PROJ-12"])
    task = results[0]
    assert task.state == "RED_REVIEW", task.error
    first_red = task.phase_commits[0]
    red_md = (rt.task_dir("PROJ-12") / "red.md").read_text()
    assert (
        "tests v1" in red_md and "os.path.exists" in red_md and "exit 1" in red_md or first_red[:10] in red_md
    )
    audit = [json.loads(line) for line in rt.audit.path.read_text().splitlines()]
    assert any(e["event"] == "jira_skipped" and (e.get("attach") or "").endswith("red.md") for e in audit)
    # the approver sends the tests back; the rewritten tests replace the red commit
    feature.revise(task, "Assert the return value too.", 2)
    assert task.state == "TEST_WRITING" and task.spec_revision == 0
    rt.store.save_task(rt.run_id, task)
    specs = await ExplicitKeys(tracker, ["PROJ-12"]).tasks()
    task = (await run_all(rt, specs, rt.store.load_tasks(rt.run_id)))[0]
    assert task.state == "RED_REVIEW", task.error
    assert task.phase_commits != [first_red] and len(task.phase_commits) == 1
    assert "sent the tests back" in fakes.CALLS["worker"][2].prompt
    # approval at RED_REVIEW starts the implementation
    feature.approve(task, "")
    assert task.state == "IMPLEMENTING"
    rt.store.save_task(rt.run_id, task)
    task = (await run_all(rt, specs, rt.store.load_tasks(rt.run_id)))[0]
    assert task.state == "DONE", task.error
    log = (
        _git("log", "--format=%s", "origin/develop..HEAD", cwd=Path(task.worktree_path)).strip().splitlines()
    )
    assert len(log) == 2 and log[1].endswith("(red)") and log[0].endswith("(green)")


@pytest.mark.usefixtures("fake_runners")
async def test_worker_runtime_failure_resumes_the_same_session(config_path: Path) -> None:
    fakes.SCRIPT["worker"].append({"_error": "API Error: Can't reach the API server (ENOTFOUND)"})
    tracker = fakes.FakeTracker({"PROJ-13": fakes.issue("PROJ-13")})
    rt, results = await _run(config_path, tracker, ["PROJ-13"])
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.interrupted is None
    first, second = fakes.CALLS["worker"][0], fakes.CALLS["worker"][1]
    assert first.session is None and second.session == "worker-session"
    assert "previous session was interrupted" in second.prompt and "ENOTFOUND" in second.prompt
    events = [json.loads(line)["event"] for line in rt.audit.path.read_text().splitlines()]
    assert "interrupted" in events and "retry" in events
