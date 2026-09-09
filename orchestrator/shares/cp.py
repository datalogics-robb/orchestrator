#!/usr/bin/env python3
"""orchestrator-cp: audited share-to-share copy for agents. Standard library only.

Usage: orchestrator-cp <share>:<relative-path> <share>:<relative-path>

Sources must lie under a granted share; destinations under a read-write grant and, when
write roots are configured, under one of them. Symlinks that escape a share are refused.
Every copied file is appended to the audit log with its size and SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

GRANTS_ENV = "ORCHESTRATOR_GRANTS"
AUDIT_ENV = "ORCHESTRATOR_AUDIT"
TASK_ENV = "ORCHESTRATOR_TASK"


class CopyError(Exception):
    pass


def load_grants() -> list[dict]:
    path = os.environ.get(GRANTS_ENV)
    if not path:
        raise CopyError(f"{GRANTS_ENV} is not set; orchestrator-cp only works inside an orchestrator run")
    with open(path) as f:
        return json.load(f)


def parse_ref(ref: str, grants: list[dict]) -> tuple[dict, Path]:
    share, sep, rel = ref.partition(":")
    if not sep:
        raise CopyError(f"'{ref}' must look like <share>:<relative-path>")
    grant = next((g for g in grants if g["name"] == share), None)
    if grant is None:
        raise CopyError(f"share '{share}' is not granted to this task")
    root = Path(grant["path"]).resolve()
    target = (root / rel.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise CopyError(f"'{ref}' escapes share '{share}'")
    return grant, target


def check_destination(grant: dict, target: Path) -> None:
    if grant["mode"] != "read-write":
        raise CopyError(f"share '{grant['name']}' is read-only")
    roots = [Path(p).resolve() for p in grant.get("write_under", [])]
    if roots and not any(target == r or r in target.parents for r in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise CopyError(f"destination must be under one of: {allowed}")


def write_target(grant: dict, path: Path) -> Path:
    """The real path a write to `path` lands on, checked against the grant.

    Symlinks already present under the destination are followed before checking, so a link
    that points outside the share or the write roots is refused. A final component that is
    itself a symlink is refused outright: copying onto it would write through the link.
    """
    if path.is_symlink():
        raise CopyError(f"refusing to write through symlink {path}")
    resolved = path.resolve()
    root = Path(grant["path"]).resolve()
    if resolved != root and root not in resolved.parents:
        raise CopyError(f"destination {path} resolves outside share '{grant['name']}'")
    check_destination(grant, resolved)
    return resolved


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audit(entries: list[dict]) -> None:
    path = os.environ.get(AUDIT_ENV)
    if not path:
        return
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def copy(src_ref: str, dst_ref: str) -> list[dict]:
    grants = load_grants()
    _, src = parse_ref(src_ref, grants)
    dst_grant, dst = parse_ref(dst_ref, grants)
    check_destination(dst_grant, dst)
    if not src.exists():
        raise CopyError(f"source does not exist: {src}")
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    task = os.environ.get(TASK_ENV)
    entries: list[dict] = []

    def record(s: Path, d: Path) -> None:
        entries.append(
            {
                "ts": ts,
                "key": task,
                "event": "share_copy",
                "from": str(s),
                "to": str(d),
                "bytes": d.stat().st_size,
                "sha256": sha256(d),
            }
        )

    if src.is_dir():
        for root, _dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            target_dir = write_target(dst_grant, dst / rel)
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in files:
                s = Path(root) / name
                if s.is_symlink():
                    continue
                d = write_target(dst_grant, target_dir / name)
                shutil.copy2(s, d)
                record(s, d)
    else:
        if src.is_symlink():
            raise CopyError("refusing to copy a symlink")
        if dst.is_dir():
            dst = dst / src.name
        dst = write_target(dst_grant, dst)
        write_target(dst_grant, dst.parent).mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        record(src, dst)
    audit(entries)
    return entries


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        sys.stderr.write(__doc__ or "")
        return 2
    try:
        entries = copy(argv[0], argv[1])
    except CopyError as e:
        sys.stderr.write(f"orchestrator-cp: {e}\n")
        return 1
    except OSError as e:
        sys.stderr.write(f"orchestrator-cp: {e}\n")
        return 1
    for entry in entries:
        print(f"copied {entry['from']} -> {entry['to']} ({entry['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
