"""Read MCP server definitions from the CLIs' existing configs and select a per-role subset.

Definitions stay in each runner's native format; a server must be defined in the sources
for the runner that will use it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import yaml

from orchestrator.agents.base import Problem
from orchestrator.config.schema import Config, Role

DEFAULT_SOURCES: dict[str, list[str]] = {
    "claude-code": ["~/.claude.json", ".mcp.json"],
    "codex": ["~/.codex/config.toml"],
    "gemini-cli": ["~/.gemini/settings.json", ".gemini/settings.json"],
    "opencode": ["~/.config/opencode/opencode.json", "opencode.json"],
    "hermes": ["~/.hermes/config.yaml"],
}

_KEYS: dict[str, tuple[str, ...]] = {
    "claude-code": ("mcpServers",),
    "codex": ("mcp_servers",),
    "gemini-cli": ("mcpServers",),
    "opencode": ("mcp",),
    "hermes": ("mcp_servers", "mcp"),
}


class MCPError(Exception):
    pass


def _load_file(path: Path) -> dict[str, Any]:
    text = path.read_text()
    suffix = path.suffix.lower()
    if suffix == ".toml":
        return tomllib.loads(text)
    if suffix in (".yaml", ".yml"):
        return yaml.safe_load(text) or {}
    # JSON, tolerating JSONC-style comments used by some CLIs
    cleaned = "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))
    return json.loads(cleaned)


def sources_for(cfg: Config, runner: str, repo_root: Path | None = None) -> list[Path]:
    configured = cfg.mcp.sources.get(runner)
    raw = [str(p) for p in configured] if configured else DEFAULT_SOURCES.get(runner, [])
    out: list[Path] = []
    for r in raw:
        p = Path(r).expanduser()
        if not p.is_absolute() and repo_root is not None:
            p = repo_root / p
        out.append(p)
    return out


def load_definitions(cfg: Config, runner: str, repo_root: Path | None = None) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in sources_for(cfg, runner, repo_root):
        if not path.exists():
            continue
        try:
            data = _load_file(path)
        except (OSError, ValueError, tomllib.TOMLDecodeError, yaml.YAMLError) as e:
            raise MCPError(f"could not parse MCP source {path}: {e}") from e
        for key in _KEYS.get(runner, ("mcpServers",)):
            servers = data.get(key) if isinstance(data, dict) else None
            if isinstance(servers, dict):
                for name, definition in servers.items():
                    if isinstance(definition, dict):
                        found.setdefault(name, definition)
    return found


def servers_for_role(cfg: Config, role: Role, repo_root: Path | None = None) -> dict[str, dict[str, Any]]:
    role_cfg = cfg.agents.role(role)
    if not role_cfg.mcp_servers:
        return {}
    defs = load_definitions(cfg, role_cfg.runner, repo_root)
    missing = [n for n in role_cfg.mcp_servers if n not in defs]
    if missing:
        raise MCPError(
            f"agents.{role}.mcp_servers: {', '.join(missing)} not defined in the "
            f"{role_cfg.runner} sources {[str(p) for p in sources_for(cfg, role_cfg.runner, repo_root)]}"
        )
    return {n: defs[n] for n in role_cfg.mcp_servers}


def deny_tools_for(cfg: Config, servers: dict[str, Any]) -> dict[str, list[str]]:
    return {s: tools for s, tools in cfg.mcp.deny_tools.items() if s in servers}


def secret_values(defs: dict[str, dict[str, Any]]) -> list[str]:
    """Values inside MCP definitions that look like secrets, for the redactor."""
    out: list[str] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str):
            lowered = key.lower()
            if any(w in lowered for w in ("token", "key", "secret", "password", "authorization")):
                out.append(node.split(" ")[-1])

    walk(defs)
    return [v for v in out if len(v) >= 8]


def check_mcp(cfg: Config, repo_root: Path | None = None) -> list[Problem]:
    problems: list[Problem] = []
    for role in ("worker", "reviewer"):
        try:
            servers_for_role(cfg, role, repo_root)  # type: ignore[arg-type]
        except MCPError as e:
            problems.append(Problem("error", str(e)))
    return problems
