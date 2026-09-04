"""Test selection strategies: changed-paths, named-suite, agent-chosen."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Protocol

from orchestrator.config.schema import TestSelection


class TestSelector(Protocol):
    def select(self, changed_paths: list[str], agent_choice: list[str]) -> list[list[str]]: ...


class ChangedPathsSelector:
    def __init__(self, cfg: TestSelection) -> None:
        self.cfg = cfg

    def select(self, changed_paths: list[str], agent_choice: list[str]) -> list[list[str]]:
        chosen: list[list[str]] = []
        for prefix, commands in self.cfg.map.items():
            if any(p.startswith(prefix) for p in changed_paths):
                for c in commands:
                    if c not in chosen:
                        chosen.append(c)
        if not chosen:
            chosen = list(self.cfg.fallback)
        return chosen[: self.cfg.max_commands]


class NamedSuiteSelector:
    def __init__(self, cfg: TestSelection) -> None:
        self.cfg = cfg

    def select(self, changed_paths: list[str], agent_choice: list[str]) -> list[list[str]]:
        return list(self.cfg.commands or self.cfg.fallback)[: self.cfg.max_commands]


class AgentChosenSelector:
    """The worker's tests_selected, kept only when they start with an allowed prefix."""

    def __init__(self, cfg: TestSelection) -> None:
        self.cfg = cfg

    def select(self, changed_paths: list[str], agent_choice: list[str]) -> list[list[str]]:
        chosen: list[list[str]] = []
        for item in agent_choice:
            argv = shlex.split(item)
            text = " ".join(argv)
            if not self.cfg.allowed_prefixes or any(text.startswith(p) for p in self.cfg.allowed_prefixes):
                # bare test ids are run with the first allowed prefix
                if (
                    argv
                    and not any(text.startswith(p) for p in self.cfg.allowed_prefixes)
                    and self.cfg.allowed_prefixes
                ):
                    argv = shlex.split(self.cfg.allowed_prefixes[0]) + argv
                if argv not in chosen:
                    chosen.append(argv)
            elif self.cfg.allowed_prefixes:
                argv = shlex.split(self.cfg.allowed_prefixes[0]) + argv
                if argv not in chosen:
                    chosen.append(argv)
        if not chosen:
            chosen = list(self.cfg.fallback)
        return chosen[: self.cfg.max_commands]


_STRATEGIES: dict[str, Callable[[TestSelection], TestSelector]] = {
    "changed-paths": ChangedPathsSelector,
    "named-suite": NamedSuiteSelector,
    "agent-chosen": AgentChosenSelector,
}


def selector_for(cfg: TestSelection) -> TestSelector:
    return _STRATEGIES[cfg.strategy](cfg)
