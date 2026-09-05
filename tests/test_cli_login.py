"""use_cli_login: reuse the gh, Claude Code, and Codex logins instead of API keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.agents.base import Access, AgentRequest, Limits
from orchestrator.agents.contracts import WORKER_SCHEMA
from orchestrator.agents.runners.claude_code import ClaudeCodeRunner
from orchestrator.agents.runners.codex import CodexRunner
from orchestrator.config.loader import SecretError, resolve_secret
from orchestrator.config.schema import AuthRef


def test_authref_accepts_exactly_one_mode() -> None:
    assert AuthRef(use_cli_login=True).use_cli_login
    with pytest.raises(ValidationError):
        AuthRef(use_cli_login=True, token_env="X")
    with pytest.raises(ValidationError):
        AuthRef()


def test_resolve_secret_cli_command() -> None:
    ref = AuthRef(use_cli_login=True)
    assert resolve_secret(ref, cli_command=["echo", "tok-123"]) == "tok-123"
    with pytest.raises(SecretError):
        resolve_secret(ref)  # no command: not supported for this credential
    with pytest.raises(SecretError):
        resolve_secret(ref, cli_command=["false"])


def _request(tmp_path: Path, role: str, schema: dict) -> AgentRequest:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (tmp_path / "wt").mkdir()
    return AgentRequest(
        cwd=tmp_path / "wt",
        prompt="p",
        role=role,
        schema=schema,
        limits=Limits(timeout_seconds=10),
        access=Access("workspace-write" if role == "worker" else "read-only"),
        env={"PATH": "/usr/bin"},
        run_dir=run_dir,
        cli_login=True,
    )


def test_claude_cli_login_drops_bare(tmp_path: Path) -> None:
    req = _request(tmp_path, "worker", WORKER_SCHEMA)
    home = req.run_dir / "claude-home"
    home.mkdir()
    argv = ClaudeCodeRunner().argv(req, home)
    assert "--bare" not in argv
    assert "--strict-mcp-config" in argv and "--settings" in argv


async def test_codex_cli_login_copies_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_home = tmp_path / "user-codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text(json.dumps({"tokens": "x"}))
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    await CodexRunner()._ensure_login(run_home, {}, None, cli_login=True)
    assert json.loads((run_home / "auth.json").read_text()) == {"tokens": "x"}
    # without a login to copy, the failure is explicit
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    with pytest.raises(RuntimeError, match="codex login"):
        await CodexRunner()._ensure_login(tmp_path / "other", {}, None, cli_login=True)
