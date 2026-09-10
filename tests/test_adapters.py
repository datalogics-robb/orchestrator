"""Offline checks of what each real adapter would execute: flags, generated files, parsing."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.agents.base import Access, AgentRequest, Limits, PathGrant
from orchestrator.agents.contracts import REVIEWER_SCHEMA, WORKER_SCHEMA
from orchestrator.agents.runners.claude_code import ClaudeCodeRunner
from orchestrator.agents.runners.codex import CodexRunner
from orchestrator.agents.runners.gemini_cli import GeminiCliRunner
from orchestrator.agents.runners.hermes import HermesRunner
from orchestrator.agents.runners.opencode import OpenCodeRunner


def _request(tmp_path: Path, role: str = "worker", **kw) -> AgentRequest:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    raid_mode = "read-write" if role == "worker" else "read"
    grants = (
        PathGrant("support", Path("/Volumes/support"), "read"),
        PathGrant("raid", Path("/Volumes/raid"), raid_mode, (Path("/Volumes/raid/agent-drops"),)),
    )
    access = Access("workspace-write" if role == "worker" else "read-only", grants)
    defaults = dict(
        cwd=tmp_path / "wt",
        prompt="do it",
        role=role,
        schema=WORKER_SCHEMA if role == "worker" else REVIEWER_SCHEMA,
        limits=Limits(timeout_seconds=60, max_turns=50, max_budget_usd=2.5),
        access=access,
        env={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "k"},
        run_dir=run_dir,
        model="some-model",
        mcp_servers={"ragflow": {"type": "http", "url": "http://r/mcp"}},
        deny_tools={"mcp-jenkins": ["triggerBuild"]},
    )
    defaults.update(kw)
    (tmp_path / "wt").mkdir(exist_ok=True)
    return AgentRequest(**defaults)


def test_claude_worker_argv(tmp_path: Path) -> None:
    req = _request(tmp_path, options={"effort": "high"})
    home = req.run_dir / "claude-home"
    home.mkdir()
    argv = ClaudeCodeRunner().argv(req, home)
    assert argv[:2] == ["claude", "-p"]
    assert "--bare" in argv and "--output-format" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--max-turns") + 1] == "50"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5"
    assert argv[argv.index("--effort") + 1] == "high"
    add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert add_dirs == [str(req.cwd), "/Volumes/support", "/Volumes/raid"]
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__mcp-jenkins__triggerBuild"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == WORKER_SCHEMA
    settings = json.loads((home / "settings.json").read_text())
    hook_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook_cmd.endswith("hook-rules.json") and "claude_pretool.py" in hook_cmd
    rules = json.loads((home / "hook-rules.json").read_text())
    assert "/Volumes/support" in rules["read_only_paths"]
    assert "/Volumes/raid/agent-drops" in rules["write_roots"] and str(req.cwd) in rules["write_roots"]
    assert json.loads((home / "mcp.json").read_text()) == {"mcpServers": req.mcp_servers}


def test_claude_reviewer_is_read_only_and_resumes(tmp_path: Path) -> None:
    req = _request(tmp_path, role="reviewer", session="abc")
    home = req.run_dir / "claude-home"
    home.mkdir()
    argv = ClaudeCodeRunner().argv(req, home)
    disallowed = argv[argv.index("--disallowedTools") + 1 :]
    assert {"Edit", "Write", "MultiEdit", "NotebookEdit"} <= set(disallowed)
    assert argv[argv.index("--resume") + 1] == "abc"
    assert json.loads((home / "hook-rules.json").read_text())["read_only_worktree"] is True


def test_codex_reviewer_argv(tmp_path: Path) -> None:
    req = _request(tmp_path, role="reviewer", options={"reasoning_effort": "high"})
    home = req.run_dir / "codex-home"
    home.mkdir()
    argv = CodexRunner().argv(req, home)
    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in argv and "--add-dir" not in argv
    assert argv[argv.index("--output-schema") + 1].endswith("schema.json")
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert argv[-1] == "-"
    assert "--ignore-user-config" not in argv


def test_codex_worker_gets_writable_shares_and_resume(tmp_path: Path) -> None:
    req = _request(tmp_path)
    home = req.run_dir / "codex-home"
    home.mkdir()
    argv = CodexRunner().argv(req, home)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--add-dir") + 1] == "/Volumes/raid"
    assert "--ephemeral" not in argv
    req.session = "thread-1"
    argv = CodexRunner().argv(req, home)
    assert argv[:4] == ["codex", "exec", "resume", "thread-1"]
    assert "--cd" not in argv and "--sandbox" not in argv and "--json" in argv
    assert argv[argv.index("-c") + 1] == 'sandbox_mode="workspace-write"'


def test_codex_event_parsing() -> None:
    events = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            "not json",
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}}),
        ]
    )
    session, usage = CodexRunner._parse_events(events)
    assert session == "t-1" and usage == {"input_tokens": 13, "output_tokens": 6}


def test_gemini_argv(tmp_path: Path) -> None:
    req = _request(tmp_path, role="reviewer")
    argv = GeminiCliRunner().argv(req)
    assert argv[0] == "gemini" and argv[argv.index("--approval-mode") + 1] == "plan"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "ragflow" in argv[argv.index("--allowed-mcp-server-names") + 1 :]


def test_opencode_config_and_argv(tmp_path: Path) -> None:
    runner = OpenCodeRunner()
    req = _request(tmp_path, role="reviewer")
    cfg = runner._config(req)
    assert cfg["permission"]["edit"] == "deny" and cfg["mcp"] == req.mcp_servers
    argv = runner.argv(req, "PROMPT")
    assert "--auto" not in argv and argv[argv.index("--dir") + 1] == str(req.cwd) and argv[-1] == "PROMPT"
    (tmp_path / "x").mkdir()
    cfg_w = runner._config(_request(tmp_path / "x", role="worker"))
    assert cfg_w["permission"]["edit"] == "allow" and cfg_w["permission"]["bash"]["git push*"] == "deny"


def test_hermes_config_and_argv(tmp_path: Path) -> None:
    runner = HermesRunner()
    req = _request(tmp_path, options={"provider": "anthropic", "reasoning": "high"})
    cfg = runner._config(req)
    assert cfg["agent"]["yolo_mode"] is True and cfg["agent"]["max_turns"] == 50
    assert any("git push" in h["command"] for h in cfg["hooks"]["pre_tool_call"])
    argv = runner.argv(req, "PROMPT")
    assert argv[:3] == ["hermes", "-z", "PROMPT"] and "--yolo" in argv
    assert argv[argv.index("--provider") + 1] == "anthropic"
    assert argv[argv.index("--in") + 1] == str(req.cwd)


def test_claude_result_mapping_treats_api_error_as_runtime_error() -> None:
    from orchestrator.agents.runners.claude_code import result_from_output

    died = result_from_output(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "terminal_reason": "api_error",
            "result": "API Error: Can't reach the API server (ENOTFOUND)",
            "session_id": "s-1",
            "total_cost_usd": 1.5,
            "num_turns": 9,
        },
        exit_code=1,
    )
    assert not died.ok and died.termination == "error" and died.session_id == "s-1"
    assert "api_error" in (died.error or "") and "ENOTFOUND" in (died.error or "")
    fine = result_from_output(
        {
            "subtype": "success",
            "is_error": False,
            "result": "{}",
            "structured_output": {"a": 1},
            "session_id": "s-2",
        },
        exit_code=0,
    )
    assert fine.ok and fine.termination == "completed" and fine.error is None
    turns = result_from_output({"subtype": "error_max_turns", "is_error": True}, exit_code=1)
    assert turns.termination == "max_turns" and turns.error == "error_max_turns"
