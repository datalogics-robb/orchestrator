"""Adapter for OpenCode (`opencode run`). Structured output is prompt-and-parse."""

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

MIN_VERSION = "1.10.0"


class OpenCodeRunner:
    name = "opencode"
    capabilities = Capabilities(
        structured_output=False,
        session_resume=True,
        turn_cap=False,
        budget_cap=False,
        usage_report=True,
        read_only_mode=True,
        command_deny_hooks=True,
        config_dir_isolation=True,
    )

    def preflight(self, role: RoleConfig) -> list[Problem]:
        problems = common.binary_problems("opencode", MIN_VERSION, role)
        problems += common.soft_limit_warnings(role, turn_cap=False, budget_cap=False)
        return problems

    def _config(self, request: AgentRequest) -> dict[str, Any]:
        bash: dict[str, str] = {"*": "allow"}
        for cmd in request.deny_commands or common.DEFAULT_DENY_COMMANDS:
            bash[f"{cmd.strip()}*"] = "deny"
        permission: dict[str, Any] = {"bash": bash}
        if request.access.worktree == "read-only":
            permission["edit"] = "deny"
            permission["bash"] = {
                "*": "ask",
                "git diff*": "allow",
                "git log*": "allow",
                "git status*": "allow",
                "cat *": "allow",
                "ls*": "allow",
                "grep *": "allow",
                "rg *": "allow",
                "find *": "allow",
            }
        else:
            permission["edit"] = "allow"
        return {
            "$schema": "https://opencode.ai/config.json",
            "mcp": request.mcp_servers,
            "permission": permission,
        }

    def argv(self, request: AgentRequest, prompt: str) -> list[str]:
        argv = ["opencode", "run", "--dir", str(request.cwd), "--format", "json", "--pure"]
        if request.model:
            argv += ["--model", request.model]
        if request.options.get("agent"):
            argv += ["--agent", str(request.options["agent"])]
        if request.options.get("variant"):
            argv += ["--variant", str(request.options["variant"])]
        if request.session:
            argv += ["--session", request.session]
        if request.access.worktree == "workspace-write":
            argv.append("--auto")
        argv.append(prompt)
        return argv

    async def run(self, request: AgentRequest) -> AgentResult:
        home = common.config_home(request, "opencode-config")
        (home / "opencode.json").write_text(json.dumps(self._config(request), indent=2))
        env = dict(request.env)
        env["OPENCODE_CONFIG_DIR"] = str(home)
        env["OPENCODE_CONFIG"] = str(home / "opencode.json")
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        prompt = request.prompt + schema_instructions(request.schema)
        if request.system_prompt:
            prompt = request.system_prompt + "\n\n" + prompt
        argv = self.argv(request, prompt)
        (request.run_dir / "argv.json").write_text(json.dumps(argv[:-1] + ["<prompt>"], indent=2))
        outcome = await run_process(
            argv,
            cwd=request.cwd,
            env=env,
            timeout=request.limits.timeout_seconds,
            stdout_path=request.run_dir / "events.jsonl",
            stderr_path=request.run_dir / "stderr.log",
        )
        if outcome.timed_out:
            return AgentResult(False, "timeout", stderr_tail=tail(outcome.stderr))
        text, session_id, usage = self._parse_events(outcome.stdout)
        structured = extract_json(text) or extract_json(outcome.stdout)
        ok = outcome.exit_code == 0 and structured is not None
        return AgentResult(
            ok=ok,
            termination="completed" if outcome.exit_code == 0 else "error",
            raw_text=text,
            structured_output=structured,
            session_id=session_id,
            usage=usage,
            exit_code=outcome.exit_code,
            stderr_tail=tail(outcome.stderr),
            error=None
            if ok
            else f"opencode exited {outcome.exit_code}"
            if outcome.exit_code
            else "no JSON result",
        )

    @staticmethod
    def _parse_events(stdout: str) -> tuple[str, str | None, dict[str, Any]]:
        texts: list[str] = []
        session_id = None
        usage: dict[str, Any] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = (
                ev.get("sessionID")
                or ev.get("sessionId")
                or (ev.get("part") or {}).get("sessionID")
                or session_id
            )
            part = ev.get("part") or ev
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            tokens = part.get("tokens") or ev.get("tokens")
            if isinstance(tokens, dict):
                for k, v in tokens.items():
                    if isinstance(v, (int, float)):
                        usage[k] = usage.get(k, 0) + v
        return "\n".join(texts), session_id, usage
