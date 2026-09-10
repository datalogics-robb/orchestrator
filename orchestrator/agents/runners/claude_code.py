"""Adapter for Claude Code in print mode (`claude -p`)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from orchestrator.agents import hooks
from orchestrator.agents.base import (
    AgentRequest,
    AgentResult,
    Capabilities,
    Problem,
    run_process,
    tail,
)
from orchestrator.agents.runners import common
from orchestrator.config.schema import RoleConfig

MIN_VERSION = "2.1.200"
WRITE_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]


class ClaudeCodeRunner:
    name = "claude-code"
    api_key_var = "ANTHROPIC_API_KEY"
    capabilities = Capabilities(
        structured_output=True,
        session_resume=True,
        turn_cap=True,
        budget_cap=True,
        usage_report=True,
        read_only_mode=True,
        command_deny_hooks=True,
        config_dir_isolation=True,
    )

    def preflight(self, role: RoleConfig) -> list[Problem]:
        problems = common.binary_problems("claude", MIN_VERSION, role)
        if role.auth.use_cli_login:
            problems.append(
                Problem(
                    "warning",
                    "claude-code: using the Claude Code login from the default config dir; per-run config-dir "
                    "isolation is off (MCP servers, hooks, and tools are still restricted per run)",
                )
            )
        if role.auth.token_env and role.auth.token_env != "ANTHROPIC_API_KEY":
            problems.append(
                Problem(
                    "warning",
                    "claude-code: --bare reads only ANTHROPIC_API_KEY; the configured token_env "
                    "will be exported under that name",
                )
            )
        return problems

    def _settings(self, request: AgentRequest, home: Path) -> Path:
        rules_path = home / "hook-rules.json"
        rules_path.write_text(json.dumps(common.hook_rules(request), indent=2))
        hook_script = Path(hooks.__file__).parent / "claude_pretool.py"
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{sys.executable} {hook_script} {rules_path}",
                                "timeout": 20,
                            }
                        ],
                    }
                ]
            }
        }
        path = home / "settings.json"
        path.write_text(json.dumps(settings, indent=2))
        return path

    def _mcp_config(self, request: AgentRequest, home: Path) -> Path:
        path = home / "mcp.json"
        path.write_text(json.dumps({"mcpServers": request.mcp_servers}, indent=2))
        return path

    def argv(self, request: AgentRequest, home: Path) -> list[str]:
        settings = self._settings(request, home)
        mcp = self._mcp_config(request, home)
        schema_path = home / "schema.json"
        schema_path.write_text(json.dumps(request.schema))
        argv = ["claude", "-p", "--output-format", "json"]
        if request.options.get("bare", True) and not request.cli_login:
            argv.append("--bare")
        argv += ["--json-schema", json.dumps(request.schema)]
        argv += ["--permission-mode", "bypassPermissions", "--settings", str(settings)]
        argv += ["--mcp-config", str(mcp), "--strict-mcp-config"]
        if request.model:
            argv += ["--model", request.model]
        if request.limits.max_turns:
            argv += ["--max-turns", str(request.limits.max_turns)]
        if request.limits.max_budget_usd:
            argv += ["--max-budget-usd", str(request.limits.max_budget_usd)]
        if request.options.get("effort"):
            argv += ["--effort", str(request.options["effort"])]
        if request.system_prompt:
            sp = home / "system-prompt.md"
            sp.write_text(request.system_prompt)
            argv += ["--append-system-prompt-file", str(sp)]
        argv += ["--add-dir", str(request.cwd)]
        for p in request.access.readable_paths:
            argv += ["--add-dir", str(p)]
        disallowed = [f"mcp__{srv}__{tool}" for srv, tools in request.deny_tools.items() for tool in tools]
        if request.access.worktree == "read-only":
            disallowed += WRITE_TOOLS
        if disallowed:
            argv += ["--disallowedTools", *disallowed]
        if request.session:
            argv += ["--resume", request.session]
        return argv

    async def run(self, request: AgentRequest) -> AgentResult:
        home = common.config_home(request, "claude-home")
        env = dict(request.env)
        if not request.cli_login:
            env["CLAUDE_CONFIG_DIR"] = str(home)
        elif "CLAUDE_CONFIG_DIR" in os.environ:
            # the subscription login lives in the user's config dir; keep pointing at it
            env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
        argv = self.argv(request, home)
        (request.run_dir / "argv.json").write_text(json.dumps(argv, indent=2))
        outcome = await run_process(
            argv,
            cwd=request.cwd,
            env=env,
            timeout=request.limits.timeout_seconds,
            stdin_text=request.prompt,
            stdout_path=request.run_dir / "stdout.json",
            stderr_path=request.run_dir / "stderr.log",
        )
        if outcome.timed_out:
            return AgentResult(False, "timeout", stderr_tail=tail(outcome.stderr), exit_code=None)
        try:
            data = json.loads(outcome.stdout)
        except json.JSONDecodeError:
            return AgentResult(
                False,
                "error",
                raw_text=outcome.stdout,
                exit_code=outcome.exit_code,
                stderr_tail=tail(outcome.stderr),
                error="claude did not return JSON",
            )
        return result_from_output(data, exit_code=outcome.exit_code, stderr_tail=tail(outcome.stderr))


def result_from_output(data: dict, *, exit_code: int | None, stderr_tail: str = "") -> AgentResult:
    """Map the CLI's final `result` object onto an AgentResult.

    `subtype` names the stop reason. The CLI reports an API or network failure mid-run as
    `subtype: success` with `is_error: true` and the message in `result`; that is a runtime error,
    not a completion, and the session is resumable.
    """
    subtype = data.get("subtype", "")
    termination = {
        "success": "completed",
        "error_max_turns": "max_turns",
        "error_max_budget_usd": "max_budget",
        "error_max_structured_output_retries": "schema",
    }.get(subtype, "error")
    ok = subtype == "success" and not data.get("is_error", False)
    if not ok and termination == "completed":
        termination = "error"
    error = None
    if not ok:
        reasons = list(data.get("errors") or [])
        if data.get("terminal_reason") and data["terminal_reason"] != "success":
            reasons.append(str(data["terminal_reason"]))
        if data.get("is_error") and data.get("result"):
            reasons.append(str(data["result"]))
        error = "; ".join(reasons) or subtype or "unknown error"
    return AgentResult(
        ok=ok,
        termination=termination,  # type: ignore[arg-type]
        raw_text=data.get("result") or "",
        structured_output=data.get("structured_output"),
        session_id=data.get("session_id"),
        cost_usd=data.get("total_cost_usd"),
        num_turns=data.get("num_turns"),
        usage=data.get("usage") or {},
        exit_code=exit_code,
        stderr_tail=stderr_tail,
        error=error,
    )
