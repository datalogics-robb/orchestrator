"""Append-only JSONL audit log of every external side effect, with secret redaction."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Redactor:
    """Replaces known secret values in any string before it is logged or saved."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, value: str | None) -> None:
        if value and len(value) >= 6:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "<redacted>")
        return text

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        return obj


class AuditLog:
    def __init__(self, path: Path, run_id: str, redactor: Redactor | None = None) -> None:
        self.path = path
        self.run_id = run_id
        self.redactor = redactor or Redactor()
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, key: str | None = None, **payload: Any) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "key": key,
            "event": event,
            **self.redactor.redact_obj(payload),
        }
        line = json.dumps(entry, default=str)
        with self._lock, self.path.open("a") as f:
            f.write(line + "\n")
