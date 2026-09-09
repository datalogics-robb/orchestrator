"""Git worktree lifecycle: create from the base branch, commit, push, remove."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config.schema import RepoConfig
from orchestrator.scm.git import GitError, git

# Files the orchestrator never commits even if the agent created them.
COMMIT_DENYLIST = [".env", "*.pem", "*.key", "*.p12", "id_rsa*", "*.orig", ".orchestrator/*"]
MAX_FILE_BYTES = 25 * 1024 * 1024

_fetch_locks: dict[Path, asyncio.Lock] = {}


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def branch_name(repo: RepoConfig, key: str, summary: str) -> str:
    return repo.branch_template.format(key=key.lower(), slug=slugify(summary))


@dataclass
class Worktree:
    path: Path
    branch: str
    base: str
    base_sha: str | None = None
    """The commit the branch was created from. `origin/<base>` keeps moving; this does not."""

    @property
    def base_ref(self) -> str:
        return self.base_sha or f"origin/{self.base}"


class WorktreeManager:
    def __init__(self, repo: RepoConfig) -> None:
        self.repo = repo
        self.clone = repo.clone_path
        self.root = repo.worktree_root

    def _lock(self) -> asyncio.Lock:
        return _fetch_locks.setdefault(self.clone, asyncio.Lock())

    async def fetch_base(self) -> None:
        async with self._lock():
            await git("fetch", "origin", self.repo.base_branch, cwd=self.clone)

    async def create(self, key: str, summary: str) -> Worktree:
        await self.fetch_base()
        branch = branch_name(self.repo, key, summary)
        path = self.root / key
        self.root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            await self.remove(path, force=True)
        # a stale branch from an earlier run is replaced
        await git("branch", "-D", branch, cwd=self.clone, check=False)
        await git(
            "worktree",
            "add",
            str(path),
            "-b",
            branch,
            f"origin/{self.repo.base_branch}",
            cwd=self.clone,
        )
        common = Path(
            (await git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)).out.strip()
        )
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing_lines = exclude.read_text().splitlines() if exclude.exists() else []
        if ".orchestrator/" not in existing_lines:
            with exclude.open("a") as f:
                f.write("\n.orchestrator/\n")
        base_sha = (await git("rev-parse", "HEAD", cwd=path)).out.strip()
        return Worktree(path=path, branch=branch, base=self.repo.base_branch, base_sha=base_sha)

    async def existing(self, key: str, base_sha: str | None = None) -> Worktree | None:
        path = self.root / key
        if not path.exists():
            return None
        res = await git("rev-parse", "--abbrev-ref", "HEAD", cwd=path, check=False)
        if res.code != 0:
            return None
        return Worktree(path=path, branch=res.out.strip(), base=self.repo.base_branch, base_sha=base_sha)

    async def changed_paths(self, wt: Worktree) -> list[str]:
        await git("add", "-A", "--intent-to-add", cwd=wt.path, check=False)
        res = await git("diff", "--name-only", wt.base_ref, cwd=wt.path)
        return [p for p in res.out.splitlines() if p.strip()]

    async def diff(self, wt: Worktree) -> str:
        res = await git("diff", wt.base_ref, cwd=wt.path)
        return res.out

    async def _filter_staged(self, wt: Worktree) -> list[str]:
        """Unstage denylisted and oversized files; return what was excluded."""
        res = await git("diff", "--cached", "--name-only", cwd=wt.path)
        excluded: list[str] = []
        for rel in res.out.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            p = wt.path / rel
            bad = any(
                fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat)
                for pat in COMMIT_DENYLIST
            )
            if not bad and p.is_file() and p.stat().st_size > MAX_FILE_BYTES:
                bad = True
            if bad:
                await git("reset", "-q", "--", rel, cwd=wt.path, check=False)
                excluded.append(rel)
        return excluded

    async def stage_all(self, wt: Worktree, reset_to: str | None = None) -> list[str]:
        """Fold agent commits back into the index and stage the working tree. Returns excluded paths.

        `reset_to` keeps commits up to that SHA (a red commit) and squashes only what follows.
        """
        await git("reset", "--soft", reset_to or wt.base_ref, cwd=wt.path)
        await git("add", "-A", cwd=wt.path)
        return await self._filter_staged(wt)

    async def has_staged_changes(self, wt: Worktree) -> bool:
        res = await git("diff", "--cached", "--quiet", cwd=wt.path, check=False)
        return res.code != 0

    async def staged_files(self, wt: Worktree) -> list[str]:
        """Staged paths that still exist, which is what hooks can be run on; deletions are omitted."""
        res = await git("diff", "--cached", "--name-only", "--diff-filter=ACMR", cwd=wt.path)
        return [p for p in res.out.splitlines() if p.strip()]

    async def restage(self, wt: Worktree, files: list[str]) -> None:
        """Re-add files a hook rewrote in place."""
        if files:
            await git("add", "--", *files, cwd=wt.path)

    async def commit_staged(
        self, wt: Worktree, message: str, env: dict[str, str] | None = None
    ) -> str | None:
        """Commit the index as one commit; None when nothing is staged.

        `env` is the build environment: git hooks installed in the worktree (pre-commit) run
        inside this commit and need the worktree venv on PATH, exactly like the build does.
        """
        staged = await git("diff", "--cached", "--quiet", cwd=wt.path, check=False)
        if staged.code == 0:
            return None
        await git("-c", "commit.gpgsign=false", "commit", "-q", "-m", message, cwd=wt.path, env=env)
        return (await git("rev-parse", "HEAD", cwd=wt.path)).out.strip()

    async def commit_all(self, wt: Worktree, message: str) -> tuple[str | None, list[str]]:
        """Squash any agent commits and the working tree into one commit. Returns (sha, excluded)."""
        excluded = await self.stage_all(wt)
        return await self.commit_staged(wt, message), excluded

    async def push(self, wt: Worktree, token: str) -> None:
        """Push with a one-off credential helper so the token never lands in config."""
        helper = f"!f() {{ echo username=x-access-token; echo password={token}; }}; f"
        await git(
            "-c",
            f"credential.helper={helper}",
            "push",
            "--force-with-lease",
            "-u",
            "origin",
            f"{wt.branch}:{wt.branch}",
            cwd=wt.path,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    async def remove(self, path: Path, *, force: bool = False) -> None:
        args = ["worktree", "remove", str(path)]
        if force:
            args.append("--force")
        res = await git(*args, cwd=self.clone, check=False)
        if res.code != 0 and path.exists():
            shutil.rmtree(path, ignore_errors=True)
            await git("worktree", "prune", cwd=self.clone, check=False)

    async def snapshot_copy(self, wt: Worktree, dest: Path) -> Path:
        """A detached throwaway worktree at the same commit, for reviewers without read-only mode."""
        if dest.exists():
            await self.remove(dest, force=True)
        sha = (await git("rev-parse", "HEAD", cwd=wt.path)).out.strip()
        await git("worktree", "add", "--detach", str(dest), sha, cwd=self.clone)
        return dest


async def check_clone(repo: RepoConfig) -> list[str]:
    problems = []
    if not (repo.clone_path / ".git").exists():
        return [f"repo.clone_path {repo.clone_path} is not a git clone"]
    try:
        res = await git("remote", "get-url", "origin", cwd=repo.clone_path)
        if repo.github.lower() not in res.out.lower():
            problems.append(f"origin remote {res.out.strip()} does not match repo.github {repo.github}")
        await git(
            "ls-remote", "--exit-code", "--heads", "origin", repo.base_branch, cwd=repo.clone_path, timeout=60
        )
    except GitError as e:
        problems.append(str(e))
    return problems
