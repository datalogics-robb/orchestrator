"""Pull request creation through the `gh` CLI with the orchestrator's own token."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from orchestrator.config.schema import RepoConfig


class ScmError(Exception):
    pass


@dataclass
class PullRequest:
    number: int
    url: str


class ScmHost(Protocol):
    async def open_pr(self, *, head: str, base: str, title: str, body: str, draft: bool) -> PullRequest: ...


class GitHubHost:
    def __init__(self, repo: RepoConfig, token: str, cwd: Path) -> None:
        self.repo = repo
        self.token = token
        self.cwd = cwd

    def _env(self) -> dict[str, str]:
        return {**os.environ, "GH_TOKEN": self.token, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}

    async def _gh(self, *args: str, timeout: float = 120) -> str:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            cwd=str(self.cwd),
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            raise ScmError(f"gh {' '.join(args)} failed: {err_b.decode(errors='replace').strip()}")
        return out_b.decode()

    async def existing_pr(self, head: str) -> PullRequest | None:
        out = await self._gh(
            "pr",
            "list",
            "--repo",
            self.repo.github,
            "--head",
            head,
            "--state",
            "open",
            "--json",
            "number,url",
            "--limit",
            "1",
        )
        items = json.loads(out or "[]")
        if items:
            return PullRequest(items[0]["number"], items[0]["url"])
        return None

    async def open_pr(self, *, head: str, base: str, title: str, body: str, draft: bool) -> PullRequest:
        existing = await self.existing_pr(head)
        if existing:
            await self._gh("pr", "edit", str(existing.number), "--repo", self.repo.github, "--body", body)
            return existing
        args = [
            "pr",
            "create",
            "--repo",
            self.repo.github,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            args.append("--draft")
        for label in self.repo.pr.labels:
            args += ["--label", label]
        for reviewer in self.repo.pr.reviewers:
            args += ["--reviewer", reviewer]
        url = (await self._gh(*args)).strip().splitlines()[-1]
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        return PullRequest(number, url)

    async def check(self) -> list[str]:
        problems = []
        try:
            await self._gh("auth", "status", timeout=30)
        except ScmError as e:
            problems.append(f"gh auth: {e}")
        try:
            await self._gh("repo", "view", self.repo.github, "--json", "name", timeout=30)
        except ScmError as e:
            problems.append(f"cannot view {self.repo.github}: {e}")
        return problems
