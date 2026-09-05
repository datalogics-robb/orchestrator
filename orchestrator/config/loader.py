"""Load and validate the YAML config; resolve secret references at startup."""

from __future__ import annotations

import netrc
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from orchestrator.config.schema import AuthRef, Config

# Shapes of tokens that must never appear anywhere in a config file.
_TOKEN_SHAPES = [
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bATATT3[A-Za-z0-9_\-=]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
]


class ConfigError(Exception):
    pass


class SecretError(ConfigError):
    pass


def _walk_strings(node: Any, path: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(_walk_strings(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_walk_strings(v, f"{path}[{i}]"))
    elif isinstance(node, str):
        out.append((path, node))
    return out


def scan_for_secrets(raw: Any) -> list[str]:
    """Return config paths whose string value looks like a credential."""
    hits = []
    for path, value in _walk_strings(raw):
        if any(shape.search(value) for shape in _TOKEN_SHAPES):
            hits.append(path)
    return hits


def load_raw(path: Path) -> dict[str, Any]:
    try:
        with path.open() as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"could not parse {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return raw


def load_config(path: Path) -> Config:
    raw = load_raw(path)
    hits = scan_for_secrets(raw)
    if hits:
        raise SecretError(
            f"{path}: values that look like credentials at {', '.join(hits)}; "
            "reference secrets with token_env or netrc_machine instead"
        )
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        lines = [f"{path}: invalid configuration"]
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            lines.append(f"  {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from e


GH_TOKEN_COMMAND = ["gh", "auth", "token"]


def resolve_secret(
    ref: AuthRef, *, env: Mapping[str, str] | None = None, cli_command: list[str] | None = None
) -> str:
    """Resolve an AuthRef to its secret value from the environment or ~/.netrc."""
    source: Mapping[str, str] = os.environ if env is None else env
    if ref.use_cli_login:
        if cli_command is None:
            raise SecretError("use_cli_login is not supported for this credential")
        try:
            out = subprocess.run(cli_command, capture_output=True, text=True, timeout=30, check=True)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise SecretError(f"{' '.join(cli_command)} failed: {e}") from e
        token = out.stdout.strip()
        if not token:
            raise SecretError(f"{' '.join(cli_command)} returned nothing")
        return token
    if ref.token_env:
        value = source.get(ref.token_env)
        if not value:
            raise SecretError(f"environment variable {ref.token_env} is not set")
        return value
    assert ref.netrc_machine
    try:
        auth = netrc.netrc().authenticators(ref.netrc_machine)
    except FileNotFoundError as e:
        raise SecretError("no ~/.netrc file") from e
    except netrc.NetrcParseError as e:
        raise SecretError(f"could not parse ~/.netrc: {e}") from e
    if not auth or not auth[2]:
        raise SecretError(f"no password for machine {ref.netrc_machine} in ~/.netrc")
    return auth[2]


def netrc_login(machine: str) -> str | None:
    """The login name paired with a netrc machine, if any (Jira basic auth needs it)."""
    try:
        auth = netrc.netrc().authenticators(machine)
    except (FileNotFoundError, netrc.NetrcParseError):
        return None
    return auth[0] if auth else None
