"""Maps runner names to adapter classes; third parties register via entry points."""

from __future__ import annotations

from importlib.metadata import entry_points

from orchestrator.agents.base import AgentRunner

ENTRY_POINT_GROUP = "orchestrator.runners"

_BUILTIN = {
    "claude-code": "orchestrator.agents.runners.claude_code:ClaudeCodeRunner",
    "codex": "orchestrator.agents.runners.codex:CodexRunner",
    "gemini-cli": "orchestrator.agents.runners.gemini_cli:GeminiCliRunner",
    "opencode": "orchestrator.agents.runners.opencode:OpenCodeRunner",
    "hermes": "orchestrator.agents.runners.hermes:HermesRunner",
}


class UnknownRunner(Exception):
    pass


def _load(target: str) -> type[AgentRunner]:
    module_name, _, attr = target.partition(":")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)  # type: ignore[no-any-return]


def available() -> dict[str, str]:
    """Runner name -> import target, built-ins first, then installed entry points."""
    found = dict(_BUILTIN)
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        found.setdefault(ep.name, ep.value)
    return found


def get_runner(name: str) -> AgentRunner:
    targets = available()
    if name not in targets:
        raise UnknownRunner(f"unknown runner '{name}'; available: {', '.join(sorted(targets))}")
    cls = _load(targets[name])
    return cls()
