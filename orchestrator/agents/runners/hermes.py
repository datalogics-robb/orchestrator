"""Adapter for Hermes Agent (`hermes -z`). Structured output is prompt-and-parse."""

from __future__ import annotations

import json
from typing import Any

import yaml

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

MIN_VERSION = "0.20.0"


class HermesRunner:
    name = "hermes"
    capabilities = Capabilities(
        structured_output=False,
        session_resume=True,
        turn_cap=True,
        budget_cap=False,
        usage_report=True,
        read_only_mode=False,
        command_deny_hooks=True,
        config_dir_isolation=True,
    )

    def preflight(self, role: RoleConfig) -> list[Problem]:
        problems = common.binary_problems("hermes", MIN_VERSION, role)
        problems += common.soft_limit_warnings(role, turn_cap=True, budget_cap=False)
        if role.access == "read-only":
            problems.append(
                Problem(
                    "warning",
                    "hermes: no read-only mode; the reviewer runs in a throwaway worktree copy",
                )
            )
        return problems

    def _config(self, request: AgentRequest) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "mcp_servers": request.mcp_servers,
            "agent": {"yolo_mode": request.access.worktree == "workspace-write"},
        }
        if request.limits.max_turns:
            cfg["agent"]["max_turns"] = request.limits.max_turns
        hooks = []
        for cmd in request.deny_commands or common.DEFAULT_DENY_COMMANDS:
            hooks.append(
                {
                    "matcher": "terminal",
                    "command": f"grep -q {json.dumps(cmd.strip())} && exit 2 || exit 0",
                    "fail_closed": True,
                }
            )
        cfg["hooks"] = {"pre_tool_call": hooks}
        return cfg

    def argv(self, request: AgentRequest, prompt: str) -> list[str]:
        argv = ["hermes", "-z", prompt, "--in", str(request.cwd), "--ignore-user-config", "--accept-hooks"]
        argv += ["--usage-file", str(request.run_dir / "usage.json")]
        if request.model:
            argv += ["--model", request.model]
        if request.options.get("provider"):
            argv += ["--provider", str(request.options["provider"])]
        if request.options.get("reasoning"):
            argv += ["--reasoning", str(request.options["reasoning"])]
        if request.access.worktree == "workspace-write":
            argv.append("--yolo")
        if request.session:
            argv += ["--resume", request.session]
        return argv

    async def run(self, request: AgentRequest) -> AgentResult:
        home = common.config_home(request, "hermes-home")
        (home / "config.yaml").write_text(yaml.safe_dump(self._config(request), sort_keys=False))
        env = dict(request.env)
        env["HERMES_HOME"] = str(home)
        prompt = request.prompt + schema_instructions(request.schema)
        if request.system_prompt:
            prompt = request.system_prompt + "\n\n" + prompt
        argv = self.argv(request, prompt)
        (request.run_dir / "argv.json").write_text(json.dumps(argv[:2] + ["<prompt>"] + argv[3:], indent=2))
        outcome = await run_process(
            argv,
            cwd=request.cwd,
            env=env,
            timeout=request.limits.timeout_seconds,
            stdout_path=request.run_dir / "stdout.txt",
            stderr_path=request.run_dir / "stderr.log",
        )
        if outcome.timed_out:
            return AgentResult(False, "timeout", stderr_tail=tail(outcome.stderr))
        usage: dict[str, Any] = {}
        usage_path = request.run_dir / "usage.json"
        if usage_path.exists():
            try:
                usage = json.loads(usage_path.read_text())
            except json.JSONDecodeError:
                usage = {}
        structured = extract_json(outcome.stdout)
        ok = outcome.exit_code == 0 and structured is not None
        return AgentResult(
            ok=ok,
            termination="completed" if outcome.exit_code == 0 else "error",
            raw_text=outcome.stdout,
            structured_output=structured,
            session_id=usage.get("session_id"),
            cost_usd=usage.get("estimated_cost") or usage.get("cost_usd"),
            usage=usage,
            exit_code=outcome.exit_code,
            stderr_tail=tail(outcome.stderr),
            error=None
            if ok
            else f"hermes exited {outcome.exit_code}"
            if outcome.exit_code
            else "no JSON result",
        )
