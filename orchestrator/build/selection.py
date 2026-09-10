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
        bare_ids: list[str] = []
        prefixes = self.cfg.allowed_prefixes
        for item in agent_choice:
            argv = shlex.split(item)
            if not argv:
                continue
            text = " ".join(argv)
            if not prefixes or any(text.startswith(p) for p in prefixes):
                if argv not in chosen:
                    chosen.append(argv)
            elif len(argv) == 1 and self.cfg.bare_test_template:
                if argv[0] not in bare_ids:
                    bare_ids.append(argv[0])
            else:
                # anything else the worker named is run under the first allowed prefix
                argv = shlex.split(prefixes[0]) + argv
                if argv not in chosen:
                    chosen.append(argv)
        if bare_ids and self.cfg.bare_test_template:
            command = shlex.split(self.cfg.bare_test_template.replace("{ids}", ",".join(bare_ids)))
            if command not in chosen:
                chosen.append(command)
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
