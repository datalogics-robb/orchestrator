"""What a sync sends: the worktree's changes since the last sync, computed by git.

Each task keeps a private index file. `git add -A` into it snapshots the worktree as a tree (ignored
files excluded, unchanged files skipped by their stat data), and `git diff-tree` against the tree
the machine last received gives exactly the files to send and to delete. The first sync diffs
against the base commit's tree when the machine starts from a checkout, or the empty tree when it
starts empty.

A file's content is what a checkout on the build machine would hold, not the host's working-tree
bytes: the blob `git add` stored, run through the repository's filters with LF as the native line
ending. A Windows host with core.autocrlf checks text out with CRLF, and a build machine is POSIX.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
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
    sha: str = ""
    """The new blob id (empty for D)."""


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
        old_mode, new_mode, _old_sha, new_sha, status = fields[i][1:].split(" ")
        path = fields[i + 1]
        mode = old_mode if status == "D" else new_mode
        if mode != GITLINK:
            result.append(Change(path, status[0], mode, "" if status == "D" else new_sha))
        i += 2
    return result


# attributes under which a checkout writes something other than the stored blob, even with LF line endings
_CONVERTING = ("eol", "filter", "ident", "working-tree-encoding")


def _converted_paths(worktree: Path, paths: list[str]) -> set[str]:
    """The paths whose checkout differs from the stored blob: eol=crlf, a smudge filter, ident, or an encoding."""
    out = subprocess.run(
        ["git", "check-attr", "-z", "--stdin", *_CONVERTING],
        cwd=worktree,
        input="".join(p + "\0" for p in paths).encode(),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    fields = out.split("\0")
    converted = set()
    for i in range(0, len(fields) - 2, 3):
        path, attr, value = fields[i], fields[i + 1], fields[i + 2]
        if attr == "eol":
            needs = value == "crlf"
        elif attr == "ident":
            needs = value == "set"
        else:
            needs = value not in ("unspecified", "unset")
        if needs:
            converted.add(path)
    return converted


def posix_contents(worktree: Path, files: list[Change]) -> list[bytes]:
    """Each file as a checkout with LF line endings would write it.

    The stored blobs, read in one batch, are that already, except for paths whose attributes convert
    on checkout (eol=crlf, a smudge filter such as Git LFS, ident, working-tree-encoding); those go
    through git's own filters one at a time.
    """
    if not files:
        return []
    out = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=worktree,
        input="".join(f"{c.sha}\n" for c in files).encode(),
        capture_output=True,
        check=True,
    ).stdout
    contents: list[bytes] = []
    pos = 0
    for c in files:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise RuntimeError(f"git cat-file could not read {c.path}: {out[pos:end]!r}")
        size = int(header[2])
        contents.append(out[end + 1 : end + 1 + size])
        pos = end + 1 + size + 1  # the content is followed by a newline
    converted = _converted_paths(worktree, [c.path for c in files])
    for i, c in enumerate(files):
        if c.path in converted:
            contents[i] = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.eol=lf",
                    "cat-file",
                    "--filters",
                    f"--path={c.path}",
                    c.sha,
                ],
                cwd=worktree,
                capture_output=True,
                check=True,
            ).stdout
    return contents


def write_payload(worktree: Path, delta: list[Change], out: Path) -> dict[str, int]:
    """Write the sync request (a JSON header line, then a gzipped tar of the new files) to `out`."""
    deleted = [c.path for c in delta if c.status == "D"]
    sent = [c for c in delta if c.status != "D"]
    files = [c for c in sent if c.mode != SYMLINK]
    contents = dict(zip((c.path for c in files), posix_contents(worktree, files), strict=True))
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
                data = contents[c.path]
                info.size = len(data)
                info.mode = 0o755 if c.mode == "100755" else 0o644
                tar.addfile(info, io.BytesIO(data))
                size += info.size
    return {"files": len(sent), "deleted": len(deleted), "bytes": size}
