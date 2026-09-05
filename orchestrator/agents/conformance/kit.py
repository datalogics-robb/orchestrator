"""Adapter conformance kit: drives a real runtime through a canned task.

Spends real tokens, so it is opt-in: `orchestrator conformance <runner> --config <file>`.
The task is small on purpose: read a file, edit it, return the contract JSON, and resume
once when the adapter claims session resume.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.agents.base import Access, AgentRequest, AgentRunner, Limits
from orchestrator.config.schema import RoleConfig
from orchestrator.environment import build_env

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "greeting": {"type": "string"},
        "line_count": {"type": "integer"},
        "edited": {"type": "boolean"},
    },
    "required": ["greeting", "line_count", "edited"],
}

PROMPT = """You are being tested by an orchestrator conformance kit.

1. Read the file `notes.txt` in the current directory and count its lines.
2. Append the line `conformance: edited` to `notes.txt`.
3. Reply with only a JSON object: {"greeting": "<the first line of notes.txt>", "line_count": <the number of lines before your edit>, "edited": true}
"""

RESUME_PROMPT = """Same task as before. Reply with only a JSON object: {"greeting": "<first line of notes.txt>", "line_count": <number of lines now>, "edited": true}"""


@dataclass
class ConformanceReport:
    runner: str
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.steps)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.steps.append((name, passed, detail))


async def run_conformance(
    runner: AgentRunner, role: RoleConfig, api_key: str, *, keep: bool = False
) -> ConformanceReport:
    report = ConformanceReport(runner.name)
    tmp = Path(tempfile.mkdtemp(prefix="orchestrator-conformance-"))
    work = tmp / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "notes.txt").write_text("hello conformance\nsecond line\nthird line\n")
    run_dir = tmp / "run"
    run_dir.mkdir()
    secrets: dict[str, str] = {}
    if api_key:
        secrets = {role.auth.token_env or "API_KEY": api_key}
        expected = getattr(runner, "api_key_var", None)
        if expected:
            secrets[expected] = api_key
    request = AgentRequest(
        cwd=work,
        prompt=PROMPT,
        role="worker",
        schema=SCHEMA,
        limits=Limits(
            timeout_seconds=role.timeout_minutes * 60,
            max_turns=role.max_turns or 30,
            max_budget_usd=role.max_budget_usd or 1.0,
        ),
        access=Access(worktree="workspace-write"),
        env=build_env(secrets=secrets),
        run_dir=run_dir,
        model=role.model,
        options=dict(role.options),
        prompt_and_parse=not runner.capabilities.structured_output,
        cli_login=role.auth.use_cli_login,
    )
    result = await runner.run(request)
    report.add("run completes", result.ok, result.error or result.termination)
    out = result.structured_output or {}
    report.add(
        "structured output parses",
        isinstance(out, dict) and set(SCHEMA["required"]) <= set(out),
        json.dumps(out)[:200],
    )
    report.add(
        "read the file",
        out.get("greeting") == "hello conformance" and out.get("line_count") == 3,
        str(out.get("greeting")),
    )
    text = (work / "notes.txt").read_text()
    report.add(
        "edited the file",
        "conformance: edited" in text,
        text.strip().splitlines()[-1] if text.strip() else "",
    )
    if runner.capabilities.usage_report:
        report.add(
            "reports usage",
            bool(result.usage) or result.cost_usd is not None,
            f"cost={result.cost_usd} usage_keys={list(result.usage)[:5]}",
        )
    if runner.capabilities.session_resume:
        report.add("returns a session id", bool(result.session_id), str(result.session_id))
        if result.session_id:
            request.session = result.session_id
            request.prompt = RESUME_PROMPT
            request.run_dir = tmp / "run-resume"
            request.run_dir.mkdir()
            resumed = await runner.run(request)
            out2 = resumed.structured_output or {}
            report.add(
                "resume works",
                resumed.ok and out2.get("line_count") == 4,
                resumed.error or json.dumps(out2)[:200],
            )
    if not keep:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    else:
        report.add("kept files at", True, str(tmp))
    return report
