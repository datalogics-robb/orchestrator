"""Intake: turn issue keys (and epics) into ordered task specs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from orchestrator.trackers.base import Issue, Tracker


@dataclass
class TaskSpec:
    issue: Issue
    depends_on: list[str] = field(default_factory=list)
    epic_key: str | None = None

    @property
    def key(self) -> str:
        return self.issue.key


class IntakeSource(Protocol):
    async def tasks(self) -> list[TaskSpec]: ...


class ExplicitKeys:
    """Keys passed on the command line; epics expand to their children."""

    def __init__(self, tracker: Tracker, keys: list[str]) -> None:
        self.tracker = tracker
        self.keys = keys

    async def tasks(self) -> list[TaskSpec]:
        specs: dict[str, TaskSpec] = {}
        for key in self.keys:
            issue = await self.tracker.get_issue(key)
            if issue.is_epic:
                children = await self.tracker.children(issue.key)
                child_keys = {c.key for c in children}
                for child in children:
                    deps = [b for b in child.blocked_by if b in child_keys]
                    specs.setdefault(child.key, TaskSpec(child, deps, epic_key=issue.key))
            else:
                specs.setdefault(issue.key, TaskSpec(issue, []))
        return order_by_dependencies(list(specs.values()))


def cyclic_keys(specs: list[TaskSpec]) -> list[str]:
    """Keys that can never become ready: they are in a dependency cycle or depend on one."""
    remaining = {s.key: s for s in specs}
    while remaining:
        ready = [s for s in remaining.values() if not any(d in remaining for d in s.depends_on)]
        if not ready:
            break
        for s in ready:
            del remaining[s.key]
    return sorted(remaining)


def order_by_dependencies(specs: list[TaskSpec]) -> list[TaskSpec]:
    """Stable topological order; dependencies on keys outside the batch are ignored.

    Members of a cycle come last in input order; the scheduler refuses to run them.
    """
    remaining = {s.key: s for s in specs}
    ordered: list[TaskSpec] = []
    while remaining:
        ready = [s for s in remaining.values() if not any(d in remaining for d in s.depends_on)]
        if not ready:
            ordered.extend(remaining.values())
            break
        for s in ready:
            ordered.append(s)
            del remaining[s.key]
    return ordered
