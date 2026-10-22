"""SQLite store for runs, task checkpoints, and agent invocations, so interrupted runs can resume.

Several processes share one database (a daemon and CLI runs), so it runs in WAL mode with a busy
timeout, and each run records which process owns it and when that process last checked in.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from orchestrator.pipeline.task import TaskState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started TEXT NOT NULL,
    finished TEXT,
    config_path TEXT NOT NULL,
    keys TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    state TEXT NOT NULL,
    updated TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (run_id, key)
);
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    role TEXT NOT NULL,
    label TEXT NOT NULL,
    runner TEXT NOT NULL,
    model TEXT,
    started TEXT NOT NULL,
    ended TEXT,
    ok INTEGER,
    termination TEXT,
    cost_usd REAL,
    turns INTEGER,
    session TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS agents_run ON agents (run_id, key, role);
"""

# columns added to runs after the first release; an existing state.db gains them in place
_RUN_COLUMNS = {"owner": "TEXT", "heartbeat": "TEXT", "status": "TEXT", "via": "TEXT"}

RunStatus = Literal["queued", "running", "paused", "interrupted", "cancelled", "finished"]
"""`interrupted`: its process stopped or died mid-run. `cancelled`: the operator stopped it on purpose."""
RunVia = Literal["cli", "daemon"]
"""What last drove a run; a daemon resumes on start only the runs a daemon was driving."""
LIVE_STATUSES = frozenset({"queued", "running"})
"""Statuses whose owner must keep its heartbeat fresh; a silent owner means the run was interrupted."""

HEARTBEAT_SECONDS = 15
STALE_AFTER = timedelta(seconds=60)
"""A running run whose heartbeat is older than this has lost its owning process."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def this_process() -> str:
    """The owner string recorded for runs this process drives: `<host>:<pid>`."""
    return f"{socket.gethostname()}:{os.getpid()}"


def heartbeat_fresh(heartbeat: str | None, now: datetime | None = None) -> bool:
    if not heartbeat:
        return False
    now = now or datetime.now(UTC)
    return now - datetime.fromisoformat(heartbeat) < STALE_AFTER


class RunInUse(Exception):
    """Another live process owns the run."""


@dataclass
class RunRow:
    run_id: str
    started: str
    finished: str | None
    config_path: str
    keys: list[str]
    dry_run: bool
    owner: str | None = None
    heartbeat: str | None = None
    status: RunStatus | None = None
    via: RunVia | None = None


@dataclass
class AgentRow:
    id: int
    run_id: str
    key: str
    role: str
    label: str
    runner: str
    model: str | None
    started: str
    ended: str | None
    ok: bool | None
    termination: str | None
    cost_usd: float | None
    turns: int | None
    session: str | None
    error: str | None


def effective_status(run: RunRow, *, any_paused: bool = False, now: datetime | None = None) -> RunStatus:
    """What a run is doing now. A running run whose owner stopped checking in was interrupted.

    Runs recorded before ownership was tracked have no status; `any_paused` says whether one of
    their tasks waits for approval.
    """
    if run.status in LIVE_STATUSES:
        return run.status if heartbeat_fresh(run.heartbeat, now) else "interrupted"
    if run.status:
        return run.status
    if run.finished:
        return "finished"
    return "paused" if any_paused else "interrupted"


_RUN_SELECT = (
    "SELECT run_id, started, finished, config_path, keys, dry_run, owner, heartbeat, status, via FROM runs"
)
_AGENT_SELECT = (
    "SELECT id, run_id, key, role, label, runner, model, started, ended, ok, termination, cost_usd, turns, "
    "session, error FROM agents"
)


def _run_row(r: tuple) -> RunRow:
    return RunRow(r[0], r[1], r[2], r[3], json.loads(r[4]), bool(r[5]), r[6], r[7], r[8], r[9])


def _agent_row(r: tuple) -> AgentRow:
    return AgentRow(
        id=r[0],
        run_id=r[1],
        key=r[2],
        role=r[3],
        label=r[4],
        runner=r[5],
        model=r[6],
        started=r[7],
        ended=r[8],
        ok=None if r[9] is None else bool(r[9]),
        termination=r[10],
        cost_usd=r[11],
        turns=r[12],
        session=r[13],
        error=r[14],
    )


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(runs)")}
        for column, kind in _RUN_COLUMNS.items():
            if column not in have:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {kind}")
        self._conn.commit()

    def _write(self, sql: str, params: tuple) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _read(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # runs

    def create_run(
        self,
        run_id: str,
        config_path: Path,
        keys: list[str],
        dry_run: bool,
        status: RunStatus = "running",
        via: RunVia = "cli",
    ) -> None:
        now = _now()
        self._write(
            "INSERT OR REPLACE INTO runs (run_id, started, finished, config_path, keys, dry_run, owner, heartbeat, "
            "status, via) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, now, str(config_path), json.dumps(keys), int(dry_run), this_process(), now, status, via),
        )

    def claim_run(self, run_id: str, status: RunStatus = "running", via: RunVia = "cli") -> None:
        """Take ownership of an existing run for this process; refuses a run another live process drives."""
        me = this_process()
        with self._lock:
            row = self._conn.execute(
                "SELECT owner, heartbeat, status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            owner, heartbeat, current = row
            if current in LIVE_STATUSES and owner != me and heartbeat_fresh(heartbeat):
                raise RunInUse(f"run {run_id} is being driven by {owner} (last seen {heartbeat})")
            self._conn.execute(
                "UPDATE runs SET owner = ?, heartbeat = ?, status = ?, via = ?, finished = NULL "
                "WHERE run_id = ?",
                (me, _now(), status, via, run_id),
            )
            self._conn.commit()

    def heartbeat(self, run_id: str) -> None:
        self._write(
            "UPDATE runs SET heartbeat = ? WHERE run_id = ? AND owner = ?", (_now(), run_id, this_process())
        )

    def release_run(self, run_id: str, status: RunStatus) -> None:
        """Record how this process left the run. `finished` also stamps the finish time."""
        finished = _now() if status == "finished" else None
        self._write(
            "UPDATE runs SET status = ?, finished = COALESCE(?, finished), heartbeat = ? WHERE run_id = ?",
            (status, finished, _now(), run_id),
        )

    def finish_run(self, run_id: str) -> None:
        self.release_run(run_id, "finished")

    def get_run(self, run_id: str) -> RunRow | None:
        rows = self._read(_RUN_SELECT + " WHERE run_id = ?", (run_id,))
        return _run_row(rows[0]) if rows else None

    def list_runs(self, limit: int = 20) -> list[RunRow]:
        rows = self._read(_RUN_SELECT + " ORDER BY started DESC LIMIT ?", (limit,))
        return [_run_row(r) for r in rows]

    def latest_run_id(self) -> str | None:
        runs = self.list_runs(1)
        return runs[0].run_id if runs else None

    # tasks

    def save_task(self, run_id: str, task: TaskState) -> None:
        self._write(
            "INSERT OR REPLACE INTO tasks (run_id, key, state, updated, data) VALUES (?, ?, ?, ?, ?)",
            (run_id, task.key, task.state, task.updated, json.dumps(task.to_json(), default=str)),
        )

    def load_tasks(self, run_id: str) -> dict[str, TaskState]:
        rows = self._read("SELECT data FROM tasks WHERE run_id = ? ORDER BY key", (run_id,))
        return {t.key: t for t in (TaskState.from_json(json.loads(r[0])) for r in rows)}

    def last_task_update(self, run_id: str) -> str | None:
        rows = self._read("SELECT MAX(updated) FROM tasks WHERE run_id = ?", (run_id,))
        return rows[0][0] if rows else None

    # agent invocations

    def agent_started(
        self, run_id: str, key: str, role: str, label: str, runner: str, model: str | None
    ) -> int:
        cur = self._write(
            "INSERT INTO agents (run_id, key, role, label, runner, model, started) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, key, role, label, runner, model, _now()),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def agent_ended(
        self,
        agent_id: int,
        *,
        ok: bool,
        termination: str,
        cost_usd: float | None = None,
        turns: int | None = None,
        session: str | None = None,
        error: str | None = None,
    ) -> None:
        self._write(
            "UPDATE agents SET ended = ?, ok = ?, termination = ?, cost_usd = ?, turns = ?, session = ?, error = ? "
            "WHERE id = ?",
            (_now(), int(ok), termination, cost_usd, turns, session, error, agent_id),
        )

    def latest_agents(self, run_id: str) -> dict[tuple[str, str], AgentRow]:
        """The most recent invocation per (issue key, role) in a run."""
        rows = self._read(
            _AGENT_SELECT + " WHERE id IN (SELECT MAX(id) FROM agents WHERE run_id = ? GROUP BY key, role)",
            (run_id,),
        )
        return {(a.key, a.role): a for a in map(_agent_row, rows)}

    def close(self) -> None:
        self._conn.close()
