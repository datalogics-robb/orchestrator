"""What a sync sends: the worktree's changes since the last sync, computed by git.

Each task keeps a private index file. `git add -A` into it snapshots the worktree as a tree (ignored
files excluded, unchanged files skipped by their stat data), and `git diff-tree` against the tree
the machine last received gives exactly the files to send and to delete. The first sync diffs
against the base commit's tree when the machine starts from a checkout, or the empty tree when it
starts empty.
"""

from __future__ import annotations

import gzip
import json
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

from orchestrator.scm.git import git

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
GITLINK = "160000"
SYMLINK = "120000"


@dataclass(frozen=True)
class Change:
    path: str
    status: str
    """A, M, T (type change), or D, as `git diff-tree` reports them."""
    mode: str
    """The new mode (for D, the old one)."""


async def snapshot(worktree: Path, index: Path, base: str | None) -> str:
    """The worktree's current contents as a tree id. A new index starts from `base` (a commit), if given."""
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    if not index.exists():
        index.parent.mkdir(parents=True, exist_ok=True)
        if base:
            await git("read-tree", base, cwd=worktree, env=env)
    await git("add", "-A", ".", cwd=worktree, env=env)
    return (await git("write-tree", cwd=worktree, env=env)).out.strip()


async def changes(worktree: Path, old_tree: str, new_tree: str) -> list[Change]:
    out = (await git("diff-tree", "-r", "-z", "--no-renames", old_tree, new_tree, cwd=worktree)).out
    fields = out.split("\0")
    result: list[Change] = []
    i = 0
    while i + 1 < len(fields) and fields[i].startswith(":"):
        old_mode, new_mode, _old_sha, _new_sha, status = fields[i][1:].split(" ")
        path = fields[i + 1]
        mode = old_mode if status == "D" else new_mode
        if mode != GITLINK:
            result.append(Change(path, status[0], mode))
        i += 2
    return result


def write_payload(worktree: Path, delta: list[Change], out: Path) -> dict[str, int]:
    """Write the sync request (a JSON header line, then a gzipped tar of the new files) to `out`."""
    deleted = [c.path for c in delta if c.status == "D"]
    sent = [c for c in delta if c.status != "D"]
    size = 0
    with out.open("wb") as fh:
        fh.write(json.dumps({"delete": deleted}).encode() + b"\n")
        with (
            gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=6) as gz,
            tarfile.open(fileobj=gz, mode="w|", encoding="utf-8") as tar,
        ):
            for c in sent:
                local = worktree / c.path
                info = tarfile.TarInfo(c.path)
                if c.mode == SYMLINK:
                    info.type = tarfile.SYMTYPE
                    info.linkname = os.readlink(local)
                    tar.addfile(info)
                    continue
                info.size = local.stat().st_size
                info.mode = 0o755 if c.mode == "100755" else 0o644
                with local.open("rb") as data:
                    tar.addfile(info, data)
                size += info.size
    return {"files": len(sent), "deleted": len(deleted), "bytes": size}
