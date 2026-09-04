"""Adapter for Gemini CLI (`gemini -p`). Structured output is prompt-and-parse."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.agents.base import (
    AgentRequest,
    AgentResult,
    Capabilities,
    Problem,
    extract_json,
    run_process,
    schema_instructions,
    tail,
)
from orchestrator.agents.runners import common
from orchestrator.config.schema import RoleConfig

MIN_VERSION = "0.40.0"


class GeminiCliRunner:
    name = "gemini-cli"
    api_key_var = "GEMINI_API_KEY"
    capabilities = Capabilities(
        structured_output=False,
        session_resume=True,
        turn_cap=True,
        budget_cap=False,
        usage_report=True,
        read_only_mode=True,
        command_deny_hooks=False,
        config_dir_isolation=True,
    )

    def preflight(self, role: RoleConfig) -> list[Problem]:
        problems = common.binary_problems("gemini", MIN_VERSION, role)
        problems += common.soft_limit_warnings(role, turn_cap=True, budget_cap=False)
        problems.append(Problem("warning", "gemini-cli: command denial relies on prompt instructions only"))
        return problems

    def argv(self, request: AgentRequest) -> list[str]:
        mode = "plan" if request.access.worktree == "read-only" else "yolo"
        argv = ["gemini", "-p", "Follow the instructions provided on stdin.", "--output-format", "json"]
        argv += ["--approval-mode", mode]
        if request.model:
            argv += ["--model", request.model]
        dirs = [str(p) for p in request.access.readable_paths]
        if dirs:
            argv += ["--include-directories", ",".join(dirs)]
        if request.mcp_servers:
            argv += ["--allowed-mcp-server-names", *request.mcp_servers.keys()]
        if request.session:
            argv += ["--resume", request.session]
        return argv

    async def run(self, request: AgentRequest) -> AgentResult:
        home = common.config_home(request, "gemini-home")
        settings_dir = home / ".gemini"
        settings_dir.mkdir(exist_ok=True)
        settings = {
            "mcpServers": request.mcp_servers,
            "model": {"maxSessionTurns": request.limits.max_turns or -1},
        }
        (settings_dir / "settings.json").write_text(json.dumps(settings, indent=2))
        env = dict(request.env)
        env["GEMINI_CLI_HOME"] = str(home)
        prompt = request.prompt + schema_instructions(request.schema)
        if request.system_prompt:
            prompt = request.system_prompt + "\n\n" + prompt
        argv = self.argv(request)
        (request.run_dir / "argv.json").write_text(json.dumps(argv, indent=2))
        outcome = await run_process(
            argv,
            cwd=request.cwd,
            env=env,
            timeout=request.limits.timeout_seconds,
            stdin_text=prompt,
            stdout_path=request.run_dir / "stdout.json",
            stderr_path=request.run_dir / "stderr.log",
        )
        if outcome.timed_out:
            return AgentResult(False, "timeout", stderr_tail=tail(outcome.stderr))
        response: str = outcome.stdout
        stats: dict[str, Any] = {}
        session_id: str | None = None
        try:
            data = json.loads(outcome.stdout)
            response = data.get("response") or ""
            stats = data.get("stats") or {}
            session_id = data.get("session_id") or data.get("sessionId")
        except json.JSONDecodeError:
            pass
        structured = extract_json(response)
        ok = outcome.exit_code == 0 and structured is not None
        return AgentResult(
            ok=ok,
            termination="completed"
            if outcome.exit_code == 0
            else ("max_turns" if outcome.exit_code == 53 else "error"),
            raw_text=response,
            structured_output=structured,
            session_id=session_id,
            usage=stats,
            exit_code=outcome.exit_code,
            stderr_tail=tail(outcome.stderr),
            error=None
            if ok
            else f"gemini exited {outcome.exit_code}"
            if outcome.exit_code
            else "no JSON result",
        )
