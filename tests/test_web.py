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
from orchestrator.web.api import Health, create_app, host_name
from orchestrator.web.status import DaemonInfo, RunSnapshot, agent_rows, in_scope, role_status
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
    app = create_app(cfg, cfg.web, _seeded_store(tmp_path), redactor, info, Health(), token)
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


async def test_daemon_serves_locks_and_stops(config_dict: dict, config_path: Path, tmp_path: Path) -> None:
    cfg = _config(config_dict)
    web = daemon.effective_web(cfg, None, _free_port())
    ready: list = []
    said: list[str] = []
    serving = asyncio.create_task(
        daemon.serve(
            cfg, config_path, tmp_path, web=web, checks=False, say=said.append, on_ready=ready.append
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
    # a second daemon on the same state directory refuses to start
    assert await daemon.serve(cfg, config_path, tmp_path, web=web, checks=False, say=said.append) == 1
    assert any("already serving" in s for s in said)

    ready[0].should_exit = True
    assert await asyncio.wait_for(serving, 30) == 0
    assert daemon.read_record(cfg) is None
