"""The daemon and its status page: store ownership, agent rows, the status rules, the API, the lifecycle."""

from __future__ import annotations

import asyncio
import base64
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestrator.build.runner import BuildSemaphore
from orchestrator.config.schema import Config
from orchestrator.pipeline.task import TaskState
from orchestrator.reporting.audit import Redactor
from orchestrator.state.store import (
    AgentRow,
    RunInUse,
    RunRow,
    Store,
    effective_status,
    this_process,
)
from orchestrator.web import daemon
from orchestrator.web import runs as runs_module
from orchestrator.web.api import Health, create_app, host_name
from orchestrator.web.runs import RunManager
from orchestrator.web.status import (
    DaemonInfo,
    RunSnapshot,
    agent_rows,
    in_scope,
    role_status,
    run_actions,
)
from tests import fakes
from tests.test_pipeline import _run

NOW = datetime(2026, 9, 24, 15, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _config(config_dict: dict, **web) -> Config:
    if web:
        config_dict = {**config_dict, "web": web}
    return Config.model_validate(config_dict)


def _agent(ended: str | None = None, **kw) -> AgentRow:
    defaults = dict(
        id=1,
        run_id="r",
        key="PROJ-1",
        role="worker",
        label="work",
        runner="claude-code",
        model=None,
        started=_iso(NOW - timedelta(minutes=5)),
        ended=ended,
        ok=None,
        termination=None,
        cost_usd=None,
        turns=None,
        session=None,
        error=None,
    )
    defaults.update(kw)
    return AgentRow(**defaults)  # type: ignore[arg-type]


# --- store: ownership, migration, agent rows ----------------------------------------------


def test_claim_refuses_a_run_another_live_process_drives(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run("r1", Path("c.yaml"), ["PROJ-1"], False)
    assert store.get_run("r1").owner == this_process()
    now = _iso(datetime.now(UTC))
    store._write("UPDATE runs SET owner = 'elsewhere:1', heartbeat = ? WHERE run_id = 'r1'", (now,))
    with pytest.raises(RunInUse, match="elsewhere:1"):
        store.claim_run("r1")
    stale = _iso(datetime.now(UTC) - timedelta(minutes=5))
    store._write("UPDATE runs SET heartbeat = ? WHERE run_id = 'r1'", (stale,))
    store.claim_run("r1")  # the other process died; the run is free
    run = store.get_run("r1")
    assert run.owner == this_process() and run.status == "running"


def test_release_records_how_the_process_left(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run("r1", Path("c.yaml"), [], False)
    store.release_run("r1", "paused")
    assert store.get_run("r1").status == "paused" and store.get_run("r1").finished is None
    store.claim_run("r1")
    store.finish_run("r1")
    run = store.get_run("r1")
    assert run.status == "finished" and run.finished


def test_an_existing_database_upgrades_in_place(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, started TEXT NOT NULL, finished TEXT, "
        "config_path TEXT NOT NULL, keys TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO runs VALUES ('old', '2026-09-01T00:00:00+00:00', NULL, 'c.yaml', '[\"PROJ-1\"]', 0);"
    )
    conn.commit()
    conn.close()
    store = Store(path)
    run = store.get_run("old")
    assert run is not None and run.status is None and run.owner is None
    assert effective_status(run) == "interrupted"
    assert effective_status(run, any_paused=True) == "paused"
    store.claim_run("old")  # an unowned run can always be claimed


def test_agent_rows_open_and_close(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    first = store.agent_started("r1", "PROJ-1", "worker", "work", "claude-code", "m")
    store.agent_ended(first, ok=True, termination="completed", cost_usd=1.5, turns=7, session="s")
    second = store.agent_started("r1", "PROJ-1", "worker", "fix-1", "claude-code", "m")
    latest = store.latest_agents("r1")
    assert latest[("PROJ-1", "worker")].id == second and latest[("PROJ-1", "worker")].ended is None
    store.agent_ended(second, ok=False, termination="killed", error="cancelled")
    row = store.latest_agents("r1")[("PROJ-1", "worker")]
    assert row.ended and row.ok is False and row.termination == "killed"


def test_effective_status_treats_a_silent_owner_as_interrupted() -> None:
    fresh = RunRow("r", _iso(NOW), None, "c", [], False, "h:1", _iso(NOW - timedelta(seconds=20)), "running")
    silent = RunRow("r", _iso(NOW), None, "c", [], False, "h:1", _iso(NOW - timedelta(minutes=3)), "running")
    assert effective_status(fresh, now=NOW) == "running"
    assert effective_status(silent, now=NOW) == "interrupted"


# --- build semaphore ----------------------------------------------------------------------


async def test_build_semaphore_survives_a_second_run_with_the_same_limit() -> None:
    BuildSemaphore.configure(2)
    first = BuildSemaphore.get()
    BuildSemaphore.configure(2)  # a second run starting in the same daemon
    assert BuildSemaphore.get() is first
    BuildSemaphore.configure(3)
    assert BuildSemaphore.get() is not first


# --- status rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "state", "ended", "run_status", "expected"),
    [
        ("worker", "WORKING", None, "running", "running"),
        ("worker", "WORKING", None, "interrupted", "orphaned"),
        ("worker", "WORKING", "done", "running", "waiting"),
        ("reviewer", "WORKING", "done", "running", "idle"),
        ("reviewer", "BUILDING", "done", "running", "waiting"),
        ("worker", "BUILDING", "done", "running", "idle"),
        ("worker", "AWAITING_APPROVAL", "done", "paused", "needs approval"),
        ("reviewer", "AWAITING_APPROVAL", "done", "paused", "idle"),
        ("worker", "QUEUED", "done", "paused", "idle"),
        ("worker", "FIXING", "done", "interrupted", "interrupted"),
        ("worker", "DONE", "done", "finished", "done"),
        ("reviewer", "BLOCKED", "done", "finished", "blocked"),
        ("worker", "FAILED", "done", "interrupted", "failed"),
    ],
)
def test_role_status(role: str, state: str, ended: str | None, run_status: str, expected: str) -> None:
    task = TaskState(key="PROJ-1", state=state)  # type: ignore[arg-type]
    latest = _agent(ended=None if ended is None else _iso(NOW), role=role)
    assert role_status(role, task, latest, run_status) == expected  # type: ignore[arg-type]


def test_a_role_that_has_not_run_yet_still_has_a_status() -> None:
    assert role_status("worker", TaskState(key="PROJ-1", state="CONTEXT"), None, "running") == "waiting"


def test_scope_keeps_running_and_paused_runs_and_drops_old_finished_ones() -> None:
    def snap(status: str, last: datetime) -> RunSnapshot:
        run = RunRow("r", _iso(last), None, "c", [], False, "h:1", _iso(last), status)  # type: ignore[arg-type]
        return RunSnapshot(run, {}, {}, _iso(last))

    hour = timedelta(minutes=60)
    old = NOW - timedelta(hours=3)
    assert in_scope(snap("paused", old), hour, NOW)
    assert not in_scope(snap("finished", old), hour, NOW)
    assert in_scope(snap("finished", NOW - timedelta(minutes=10)), hour, NOW)
    assert not in_scope(snap("running", old), hour, NOW)  # its owner went silent hours ago


# --- web config ---------------------------------------------------------------------------


def test_a_network_address_needs_the_operator_token(config_dict: dict) -> None:
    with pytest.raises(ValueError, match="web.auth is required"):
        _config(config_dict, host="0.0.0.0")
    cfg = _config(config_dict)
    with pytest.raises(ValueError, match="web.auth is required"):
        daemon.effective_web(cfg, "0.0.0.0", None)
    assert daemon.effective_web(cfg, None, 9000).port == 9000


def test_host_name_parsing() -> None:
    assert host_name("127.0.0.1:8765") == "127.0.0.1"
    assert host_name("[::1]:8765") == "::1"
    assert host_name("localhost") == "localhost"


# --- API ----------------------------------------------------------------------------------

SECRET = "hunter2-operator-secret"


def _seeded_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state.db")
    store.create_run("20260924-141500-a1b2c3", Path("c.yaml"), ["PROJ-1", "PROJ-2"], True)
    working = TaskState(key="PROJ-1", summary="Fix the thing", state="FIXING", round=2, cost_usd=3.1)
    blocked = TaskState(key="PROJ-2", summary="Other", state="BLOCKED", error=f"token {SECRET} rejected")
    store.save_task("20260924-141500-a1b2c3", working)
    store.save_task("20260924-141500-a1b2c3", blocked)
    done = store.agent_started("20260924-141500-a1b2c3", "PROJ-1", "reviewer", "review-1", "codex", None)
    store.agent_ended(done, ok=True, termination="completed", turns=4)
    store.agent_started("20260924-141500-a1b2c3", "PROJ-1", "worker", "fix-2", "claude-code", "opus")
    return store


def _client(config_dict: dict, tmp_path: Path, token: str | None = None, **web) -> TestClient:
    cfg = _config(config_dict, **web)
    redactor = Redactor()
    redactor.add(SECRET)
    info = DaemonInfo(pid=1, host="h", port=8765, version="t", config="c.yaml", repo="example/target")
    store = _seeded_store(tmp_path)
    runs = RunManager(cfg, Path("c.yaml"), store)
    app = create_app(cfg, cfg.web, store, redactor, info, Health(), token, runs, lambda client: None)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_agents_endpoint_shape_and_order(config_dict: dict, tmp_path: Path) -> None:
    body = _client(config_dict, tmp_path).get("/api/agents").json()
    rows = [(a["key"], a["role"], a["status"]) for a in body["agents"]]
    assert rows == [
        ("PROJ-1", "worker", "running"),
        ("PROJ-1", "reviewer", "idle"),
        ("PROJ-2", "worker", "blocked"),
        ("PROJ-2", "reviewer", "blocked"),
    ]
    worker = body["agents"][0]
    assert worker["label"] == "fix-2" and worker["runner"] == "claude-code" and worker["model"] == "opus"
    assert worker["elapsed_seconds"] is not None and worker["turns"] is None
    assert worker["task_cost_usd"] == 3.1 and worker["round"] == 2
    assert worker["issue_url"] == "https://example.atlassian.net/browse/PROJ-1"
    assert body["agents"][1]["turns"] == 4 and body["agents"][1]["runner"] == "codex"
    assert body["daemon"]["repo"] == "example/target"


def test_responses_are_redacted(config_dict: dict, tmp_path: Path) -> None:
    text = _client(config_dict, tmp_path).get("/api/agents").text
    assert SECRET not in text and "<redacted>" in text


def test_page_and_assets_are_served(config_dict: dict, tmp_path: Path) -> None:
    client = _client(config_dict, tmp_path)
    page = client.get("/")
    assert page.status_code == 200 and "Agents" in page.text and page.headers["cache-control"] == "no-store"
    assert client.get("/static/app.js").status_code == 200


def test_loopback_daemon_rejects_foreign_host_headers(config_dict: dict, tmp_path: Path) -> None:
    client = _client(config_dict, tmp_path)
    assert client.get("/api/agents", headers={"Host": "evil.example:8765"}).status_code == 400
    assert client.get("/api/agents", headers={"Host": "localhost:8765"}).status_code == 200


def test_operator_token_is_required(
    config_dict: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_TOKEN", "tok-123456")
    client = _client(config_dict, tmp_path, token="tok-123456", auth={"token_env": "WEB_TOKEN"})
    denied = client.get("/api/agents")
    assert denied.status_code == 401 and denied.headers["www-authenticate"].startswith("Basic")
    assert client.get("/", headers=_basic("operator", "wrong")).status_code == 401
    assert client.get("/", headers=_basic("someone", "tok-123456")).status_code == 401
    assert client.get("/api/agents", headers=_basic("operator", "tok-123456")).status_code == 200


def test_public_read_opens_gets_only(
    config_dict: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_TOKEN", "tok-123456")
    client = _client(
        config_dict, tmp_path, token="tok-123456", auth={"token_env": "WEB_TOKEN"}, public_read=True
    )
    assert client.get("/api/agents").status_code == 200
    assert client.post("/api/agents").status_code == 401
    assert client.post("/api/shutdown", json={}).status_code == 401


def test_runs_endpoint_lists_runs_with_task_counts(config_dict: dict, tmp_path: Path) -> None:
    body = _client(config_dict, tmp_path).get("/api/runs").json()
    (run,) = body["runs"]
    assert run["run_id"] == "20260924-141500-a1b2c3" and run["status"] == "running"
    assert run["tasks"] == {"FIXING": 1, "BLOCKED": 1} and run["by_this_daemon"] and run["dry_run"]
    assert run["cost_usd"] == 3.1


def test_control_requests_must_be_same_origin_json(config_dict: dict, tmp_path: Path) -> None:
    client = _client(config_dict, tmp_path)
    body = {"keys": ["PROJ-9"], "dry_run": True}
    evil = client.post("/api/runs", json=body, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    form = client.post("/api/runs", content=b"keys=PROJ-9", headers={"Content-Type": "text/plain"})
    assert form.status_code == 415
    missing = client.post("/api/runs", json={"keys": ["PROJ-9"]})
    assert missing.status_code == 422  # dry_run must be said explicitly
    bad = client.post("/api/runs", json={"keys": ["not a key", "proj-x"], "dry_run": True})
    assert bad.status_code == 400 and "not Jira keys: NOT A KEY, PROJ-X" in bad.json()["detail"]
    busy = client.post(
        "/api/runs", json={"keys": ["proj-1"], "dry_run": True}, headers={"Origin": "http://127.0.0.1:8765"}
    )
    assert (
        busy.status_code == 400 and "PROJ-1 (running in run 20260924-141500-a1b2c3)" in busy.json()["detail"]
    )


# --- submitting runs to the daemon --------------------------------------------------------


def _daemon_app(cfg: Config, config_path: Path, runs: RunManager, store: Store) -> httpx.AsyncClient:
    info = DaemonInfo(pid=1, host="h", port=8765, version="t", config=str(config_path), repo="example/target")
    app = create_app(cfg, cfg.web, store, Redactor(), info, Health(), None, runs, lambda client: None)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765")


@pytest.mark.usefixtures("fake_runners")
async def test_submitted_issues_are_queued_then_driven_to_done(config_path: Path, config_dict: dict) -> None:
    cfg = _config(config_dict)
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1"), "PROJ-2": fakes.issue("PROJ-2")})
    store = Store(cfg.db_path)
    probes: list[str] = []
    gate = asyncio.Event()

    async def probe() -> list[str]:
        probes.append("probe")
        if not gate.is_set():
            return ["agents.worker: not logged in"]
        return []

    runs = RunManager(
        cfg, config_path, store, login_probe=probe, tracker_factory=lambda: tracker, login_retry_seconds=0.01
    )
    async with _daemon_app(cfg, config_path, runs, store) as client:
        r = await client.post("/api/runs", json={"keys": ["proj-1", "PROJ-2", "PROJ-1"], "dry_run": True})
        assert r.status_code == 202, r.text
        submitted = r.json()
        assert submitted["keys"] == ["PROJ-1", "PROJ-2"] and submitted["status"] == "queued"
        run_id = submitted["run_id"]

        # the login probe fails, so the run waits in the queue with its tasks already listed
        while len(probes) < 2:
            await asyncio.sleep(0.01)
        agents = (await client.get("/api/agents")).json()["agents"]
        assert {(a["key"], a["status"]) for a in agents} == {("PROJ-1", "queued"), ("PROJ-2", "queued")}
        assert (await client.get("/api/runs")).json()["runs"][0]["status"] == "queued"
        again = await client.post("/api/runs", json={"keys": ["PROJ-2"], "dry_run": True})
        assert again.status_code == 400 and "PROJ-2 (queued in run" in again.json()["detail"]

        gate.set()
        await asyncio.wait_for(runs.wait(run_id), 120)
        run = (await client.get("/api/runs")).json()["runs"][0]
        assert run["status"] == "finished" and run["tasks"] == {"DONE": 2}, run
    audit = (cfg.runs_dir / run_id / "audit.jsonl").read_text()
    assert '"event": "submitted"' in audit and '"client": "127.0.0.1"' in audit
    assert (cfg.runs_dir / run_id / "report.md").exists()
    assert runs.run_ids == []


@pytest.mark.usefixtures("fake_runners")
async def test_an_unknown_issue_is_refused_without_leaving_a_run(
    config_path: Path, config_dict: dict
) -> None:
    cfg = _config(config_dict)
    store = Store(cfg.db_path)
    runs = RunManager(cfg, config_path, store, tracker_factory=lambda: fakes.FakeTracker({}))
    async with _daemon_app(cfg, config_path, runs, store) as client:
        r = await client.post("/api/runs", json={"keys": ["PROJ-404"], "dry_run": True})
    assert r.status_code == 400 and "could not read the issues from Jira" in r.json()["detail"]
    assert store.list_runs() == [] and not any(cfg.runs_dir.iterdir())


@pytest.mark.usefixtures("fake_runners")
async def test_runs_beyond_the_limit_wait_and_shutdown_interrupts_them(
    config_path: Path, config_dict: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(config_dict)
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1"), "PROJ-2": fakes.issue("PROJ-2")})
    store = Store(cfg.db_path)
    runs = RunManager(cfg, config_path, store, tracker_factory=lambda: tracker)
    started = asyncio.Event()

    async def hang(request):  # noqa: ANN001
        started.set()
        await asyncio.sleep(3600)

    real_build = runs_module.build_runtime

    def build(*args, **kwargs):  # noqa: ANN002, ANN003
        rt = real_build(*args, **kwargs)
        rt.roles["worker"].runner.run = hang  # type: ignore[method-assign]
        return rt

    monkeypatch.setattr(runs_module, "build_runtime", build)
    first = await runs.submit(["PROJ-1"], workflow=None, dry_run=True, client="test")
    await asyncio.wait_for(started.wait(), 30)
    second = await runs.submit(["PROJ-2"], workflow=None, dry_run=True, client="test")
    await asyncio.sleep(0.1)
    assert store.get_run(first.run_id).status == "running"
    assert store.get_run(second.run_id).status == "queued"  # web.max_concurrent_runs is 1
    await runs.shutdown()
    assert store.get_run(first.run_id).status == "interrupted"
    assert store.get_run(second.run_id).status == "interrupted"
    assert store.latest_agents(first.run_id)[("PROJ-1", "worker")].termination == "killed"


class _Hanging:
    """Makes every worker agent of runs the manager builds hang until `release()`; counts the calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started = asyncio.Event()
        self.hanging = True
        real_build = runs_module.build_runtime

        def build(*args, **kwargs):  # noqa: ANN002, ANN003
            rt = real_build(*args, **kwargs)
            real_run = rt.roles["worker"].runner.run

            async def run(request):  # noqa: ANN001
                if not self.hanging:
                    return await real_run(request)
                self.started.set()
                await asyncio.sleep(3600)

            rt.roles["worker"].runner.run = run  # type: ignore[method-assign]
            return rt

        monkeypatch.setattr(runs_module, "build_runtime", build)

    def release(self) -> None:
        self.hanging = False


def _states(store: Store, run_id: str) -> dict[str, str]:
    return {k: t.state for k, t in store.load_tasks(run_id).items()}


@pytest.mark.parametrize(
    ("status", "states", "driven_here", "expected"),
    [
        ("running", ["WORKING"], True, ["cancel"]),
        ("queued", ["QUEUED"], True, ["cancel"]),
        ("running", ["WORKING"], False, []),  # a CLI run cannot be cancelled from the page
        ("interrupted", ["WORKING", "DONE"], False, ["resume"]),
        ("cancelled", ["QUEUED"], False, ["resume"]),
        ("finished", ["DONE", "FAILED", "BLOCKED"], False, ["retry-failed", "retry-blocked"]),
        ("paused", ["AWAITING_APPROVAL"], False, []),  # approvals are a later milestone
        ("interrupted", ["DONE"], False, []),
    ],
)
def test_run_actions(status: str, states: list[str], driven_here: bool, expected: list[str]) -> None:
    tasks = {f"PROJ-{i}": TaskState(key=f"PROJ-{i}", state=st) for i, st in enumerate(states)}  # type: ignore[arg-type]
    assert run_actions(status, tasks, driven_here) == expected  # type: ignore[arg-type]


@pytest.mark.usefixtures("fake_runners")
async def test_cancel_stops_a_run_and_resume_finishes_it(
    config_path: Path, config_dict: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(config_dict)
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1")})
    store = Store(cfg.db_path)
    runs = RunManager(cfg, config_path, store, tracker_factory=lambda: tracker)
    agents = _Hanging(monkeypatch)
    async with _daemon_app(cfg, config_path, runs, store) as client:
        run_id = (await client.post("/api/runs", json={"keys": ["PROJ-1"], "dry_run": True})).json()["run_id"]
        await asyncio.wait_for(agents.started.wait(), 30)
        listed = (await client.get("/api/runs")).json()["runs"][0]
        assert listed["status"] == "running" and listed["actions"] == ["cancel"]
        early = await client.post(f"/api/runs/{run_id}/resume", json={})
        assert early.status_code == 409 and "already queued or running" in early.json()["detail"]

        r = await client.post(f"/api/runs/{run_id}/cancel", json={})
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        assert store.get_run(run_id).status == "cancelled" and runs.run_ids == []
        assert store.latest_agents(run_id)[("PROJ-1", "worker")].termination == "killed"
        assert _states(store, run_id) == {"PROJ-1": "WORKING"}  # checkpointed where it stopped
        listed = (await client.get("/api/runs")).json()["runs"][0]
        assert listed["actions"] == ["resume"]
        agents_tab = (await client.get("/api/agents")).json()["agents"]
        assert {a["status"] for a in agents_tab} == {"cancelled"}
        again = await client.post(f"/api/runs/{run_id}/cancel", json={})
        assert again.status_code == 409

        agents.release()
        r = await client.post(f"/api/runs/{run_id}/resume", json={})
        assert r.status_code == 202, r.text
        await asyncio.wait_for(runs.wait(run_id), 120)
        assert _states(store, run_id) == {"PROJ-1": "DONE"}
        run = store.get_run(run_id)
        assert run.status == "finished" and run.via == "daemon"
        nothing = await client.post(f"/api/runs/{run_id}/resume", json={})
        assert nothing.status_code == 400 and "nothing to resume" in nothing.json()["detail"]
        assert (await client.post("/api/runs/20990101-000000-000000/cancel", json={})).status_code == 404
    events = [line for line in (cfg.runs_dir / run_id / "audit.jsonl").read_text().splitlines()]
    assert any('"event": "cancelled"' in e and '"client": "127.0.0.1"' in e for e in events)
    assert any('"event": "resumed"' in e for e in events)


@pytest.mark.usefixtures("fake_runners")
async def test_cancelling_a_queued_run_never_starts_it(
    config_path: Path, config_dict: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(config_dict)
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1"), "PROJ-2": fakes.issue("PROJ-2")})
    store = Store(cfg.db_path)
    runs = RunManager(cfg, config_path, store, tracker_factory=lambda: tracker)
    agents = _Hanging(monkeypatch)
    first = await runs.submit(["PROJ-1"], workflow=None, dry_run=True, client="test")
    await asyncio.wait_for(agents.started.wait(), 30)
    second = await runs.submit(["PROJ-2"], workflow=None, dry_run=True, client="test")
    await runs.cancel(second.run_id, client="test")
    assert store.get_run(second.run_id).status == "cancelled"
    assert _states(store, second.run_id) == {"PROJ-2": "QUEUED"}
    assert store.get_run(first.run_id).status == "running"
    await runs.shutdown()


@pytest.mark.usefixtures("fake_runners")
async def test_retry_reopens_failed_tasks(config_path: Path, config_dict: dict) -> None:
    cfg = _config(config_dict)
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1"), "PROJ-2": fakes.issue("PROJ-2")})
    store = Store(cfg.db_path)
    store.create_run("r1", config_path, ["PROJ-1", "PROJ-2"], True)
    store.save_task("r1", TaskState(key="PROJ-1", state="DONE"))
    failed = TaskState(key="PROJ-2", summary="Do the thing")
    failed.transition("CONTEXT")
    failed.error = "boom"
    failed.transition("FAILED")
    store.save_task("r1", failed)
    store.release_run("r1", "finished")
    runs = RunManager(cfg, config_path, store, tracker_factory=lambda: tracker)
    with pytest.raises(runs_module.ControlError, match="nothing to resume"):
        await runs.resume("r1", client="test")
    resumed = await runs.resume("r1", retry_failed=True, client="test")
    assert resumed.keys == ["PROJ-1", "PROJ-2"]
    await asyncio.wait_for(runs.wait("r1"), 120)
    assert _states(store, "r1") == {"PROJ-1": "DONE", "PROJ-2": "DONE"}


@pytest.mark.usefixtures("fake_runners")
async def test_resume_on_start_picks_up_only_interrupted_daemon_runs(
    config_path: Path, config_dict: dict
) -> None:
    cfg = _config(config_dict, resume_on_start=True)
    keys = ["PROJ-1", "PROJ-2", "PROJ-3"]
    tracker = fakes.FakeTracker({k: fakes.issue(k) for k in keys})
    store = Store(cfg.db_path)
    for run_id, key, via, status in (
        ("r-daemon", "PROJ-1", "daemon", "interrupted"),
        ("r-cancelled", "PROJ-2", "daemon", "cancelled"),
        ("r-cli", "PROJ-3", "cli", "interrupted"),
    ):
        store.create_run(run_id, config_path, [key], True, via=via)  # type: ignore[arg-type]
        store.save_task(run_id, TaskState(key=key, summary="Do the thing"))
        store.release_run(run_id, status)  # type: ignore[arg-type]
    runs = RunManager(cfg, config_path, store, tracker_factory=lambda: tracker)
    assert await runs.resume_interrupted() == ["r-daemon"]
    await asyncio.wait_for(runs.wait("r-daemon"), 120)
    assert _states(store, "r-daemon") == {"PROJ-1": "DONE"}
    assert store.get_run("r-cancelled").status == "cancelled"
    assert store.get_run("r-cli").status == "interrupted"


def test_a_cli_claim_records_the_cli(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run("r1", Path("c.yaml"), [], False, via="daemon")
    store.release_run("r1", "interrupted")
    store.claim_run("r1")
    assert store.get_run("r1").via == "cli"


# --- the pipeline records agent invocations -----------------------------------------------


@pytest.mark.usefixtures("fake_runners")
async def test_a_run_leaves_closed_agent_rows(config_path: Path, config_dict: dict) -> None:
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1")})
    rt, results = await _run(config_path, tracker, ["PROJ-1"])
    assert results[0].state == "DONE", results[0].error
    latest = rt.store.latest_agents(rt.run_id)
    worker, reviewer = latest[("PROJ-1", "worker")], latest[("PROJ-1", "reviewer")]
    assert worker.ended and worker.ok and worker.label and worker.runner == "fake-worker"
    assert reviewer.ended and reviewer.ok and reviewer.turns == 3
    rt.store.finish_run(rt.run_id)
    rows = agent_rows(rt.store, _config(config_dict))
    assert {(r.key, r.role, r.status) for r in rows} == {
        ("PROJ-1", "worker", "done"),
        ("PROJ-1", "reviewer", "done"),
    }


@pytest.mark.usefixtures("fake_runners")
async def test_a_cancelled_agent_is_recorded_as_killed(config_path: Path) -> None:
    from orchestrator.config.loader import load_config
    from orchestrator.intake.base import ExplicitKeys
    from orchestrator.pipeline.runtime import build_runtime
    from orchestrator.pipeline.scheduler import run_all

    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1")})
    rt = build_runtime(
        load_config(config_path), config_path, dry_run=True, keep_worktrees=True, tracker=tracker
    )
    rt.store.create_run(rt.run_id, config_path, ["PROJ-1"], True)
    started = asyncio.Event()

    async def hang(request):  # noqa: ANN001
        started.set()
        await asyncio.sleep(3600)

    rt.roles["worker"].runner.run = hang  # type: ignore[method-assign]
    specs = await ExplicitKeys(tracker, ["PROJ-1"]).tasks()
    run = asyncio.create_task(run_all(rt, specs))
    await asyncio.wait_for(started.wait(), 30)
    assert rt.store.latest_agents(rt.run_id)[("PROJ-1", "worker")].ended is None
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    row = rt.store.latest_agents(rt.run_id)[("PROJ-1", "worker")]
    assert row.ended and row.termination == "killed"


# --- the daemon's lifecycle ---------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.usefixtures("fake_runners")
async def test_daemon_serves_runs_locks_and_stops(
    config_dict: dict, config_path: Path, tmp_path: Path
) -> None:
    cfg = _config(config_dict)
    web = daemon.effective_web(cfg, None, _free_port())
    ready: list = []
    said: list[str] = []
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1")})
    serving = asyncio.create_task(
        daemon.serve(
            cfg,
            config_path,
            tmp_path,
            web=web,
            checks=False,
            say=said.append,
            on_ready=ready.append,
            tracker_factory=lambda: tracker,
        )
    )
    for _ in range(200):
        if ready or serving.done():
            break
        await asyncio.sleep(0.05)
    assert ready, said

    record = daemon.read_record(cfg)
    assert record and record.ready and record.port == web.port and record.url.endswith(f":{web.port}/")
    async with httpx.AsyncClient(base_url=record.url) as client:
        assert (await client.get("/api/agents")).json()["agents"] == []
        assert (await client.get("/api/health")).json()["status"] == "ok"
        r = await client.post("/api/runs", json={"keys": ["PROJ-1"], "dry_run": True})
        assert r.status_code == 202, r.text
        for _ in range(1200):
            run = (await client.get("/api/runs")).json()["runs"][0]
            if run["status"] == "finished":
                break
            await asyncio.sleep(0.1)
        assert run["status"] == "finished" and run["tasks"] == {"DONE": 1}, run
    # a second daemon on the same state directory refuses to start
    assert await daemon.serve(cfg, config_path, tmp_path, web=web, checks=False, say=said.append) == 1
    assert any("already serving" in s for s in said)

    ready[0].should_exit = True
    assert await asyncio.wait_for(serving, 30) == 0
    assert daemon.read_record(cfg) is None


@pytest.mark.usefixtures("fake_runners")
async def test_shutdown_from_the_page_interrupts_runs_and_stops_the_daemon(
    config_dict: dict, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(config_dict)
    web = daemon.effective_web(cfg, None, _free_port())
    ready: list = []
    said: list[str] = []
    agents = _Hanging(monkeypatch)
    tracker = fakes.FakeTracker({"PROJ-1": fakes.issue("PROJ-1")})
    serving = asyncio.create_task(
        daemon.serve(
            cfg,
            config_path,
            tmp_path,
            web=web,
            checks=False,
            say=said.append,
            on_ready=ready.append,
            tracker_factory=lambda: tracker,
        )
    )
    for _ in range(200):
        if ready or serving.done():
            break
        await asyncio.sleep(0.05)
    assert ready, said
    url = daemon.read_record(cfg).url
    async with httpx.AsyncClient(base_url=url) as client:
        run_id = (await client.post("/api/runs", json={"keys": ["PROJ-1"], "dry_run": True})).json()["run_id"]
        await asyncio.wait_for(agents.started.wait(), 30)
        r = await client.post("/api/shutdown", json={})
        assert r.status_code == 202 and r.json() == {"status": "stopping", "interrupted": [run_id]}
    assert await asyncio.wait_for(serving, 30) == 0
    assert daemon.read_record(cfg) is None
    assert any("shutdown requested from 127.0.0.1" in line for line in said)
    store = Store(cfg.db_path)
    assert store.get_run(run_id).status == "interrupted"
    assert store.latest_agents(run_id)[("PROJ-1", "worker")].termination == "killed"
    audit = (cfg.runs_dir / run_id / "audit.jsonl").read_text()
    assert '"event": "daemon_shutdown"' in audit and '"client": "127.0.0.1"' in audit
