"""SQLite store for runs and task checkpoints, so interrupted runs can resume."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
"""


@dataclass
class RunRow:
    run_id: str
    started: str
    finished: str | None
    config_path: str
    keys: list[str]
    dry_run: bool


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def create_run(self, run_id: str, config_path: Path, keys: list[str], dry_run: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, started, finished, config_path, keys, dry_run) VALUES (?, ?, NULL, ?, ?, ?)",
                (
                    run_id,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    str(config_path),
                    json.dumps(keys),
                    int(dry_run),
                ),
            )
            self._conn.commit()

    def finish_run(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished = ? WHERE run_id = ?",
                (datetime.now(UTC).isoformat(timespec="seconds"), run_id),
            )
            self._conn.commit()

    def save_task(self, run_id: str, task: TaskState) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks (run_id, key, state, updated, data) VALUES (?, ?, ?, ?, ?)",
                (run_id, task.key, task.state, task.updated, json.dumps(task.to_json(), default=str)),
            )
            self._conn.commit()

    def load_tasks(self, run_id: str) -> dict[str, TaskState]:
        rows = self._conn.execute(
            "SELECT data FROM tasks WHERE run_id = ? ORDER BY key", (run_id,)
        ).fetchall()
        return {t.key: t for t in (TaskState.from_json(json.loads(r[0])) for r in rows)}

    def get_run(self, run_id: str) -> RunRow | None:
        row = self._conn.execute(
            "SELECT run_id, started, finished, config_path, keys, dry_run FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return RunRow(row[0], row[1], row[2], row[3], json.loads(row[4]), bool(row[5])) if row else None

    def list_runs(self, limit: int = 20) -> list[RunRow]:
        rows = self._conn.execute(
            "SELECT run_id, started, finished, config_path, keys, dry_run FROM runs ORDER BY started DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [RunRow(r[0], r[1], r[2], r[3], json.loads(r[4]), bool(r[5])) for r in rows]

    def latest_run_id(self) -> str | None:
        runs = self.list_runs(1)
        return runs[0].run_id if runs else None

    def close(self) -> None:
        self._conn.close()
