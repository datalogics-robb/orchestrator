"""Tracker protocol and the issue model the pipeline works with."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Attachment:
    filename: str
    size: int
    mime_type: str
    url: str


@dataclass
class Issue:
    key: str
    summary: str
    description_markdown: str
    issue_type: str
    status: str
    url: str
    acceptance_criteria: str = ""
    comments: list[tuple[str, str, str]] = field(default_factory=list)
    """(author, created, body_markdown)"""
    links: list[tuple[str, str, str]] = field(default_factory=list)
    """(relation, key, summary)"""
    blocked_by: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def is_epic(self) -> bool:
        return self.issue_type.lower() == "epic"


class Tracker(Protocol):
    async def get_issue(self, key: str) -> Issue: ...
    async def children(self, epic_key: str) -> list[Issue]: ...
    async def comment(self, key: str, body: str) -> None: ...
    async def transition(self, key: str, to_status: str) -> None: ...
    async def attach(self, key: str, path: Path) -> None: ...
    async def download_attachment(self, attachment: Attachment, dest: Path) -> None: ...
    async def check(self) -> list[str]: ...


class TrackerError(Exception):
    pass
