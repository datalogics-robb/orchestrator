"""Fake agent runners and tracker for pipeline tests. No tokens are spent."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestrator.agents.base import AgentRequest, AgentResult, Capabilities, Problem
from orchestrator.config.schema import RoleConfig
from orchestrator.trackers.base import Attachment, Issue

FULL = Capabilities(True, True, True, True, True, True, True, True)
NONE = Capabilities(False, False, False, False, False, False, False, False)

CALLS: dict[str, list[AgentRequest]] = {"worker": [], "reviewer": []}
SCRIPT: dict[str, list[dict]] = {"worker": [], "reviewer": []}
"""Queued structured outputs per role; when empty, defaults are used."""


def reset() -> None:
    CALLS["worker"].clear()
    CALLS["reviewer"].clear()
    SCRIPT["worker"].clear()
    SCRIPT["reviewer"].clear()


def default_worker_output(request: AgentRequest) -> dict:
    target = request.cwd / "agent_change.txt"
    target.write_text(f"changed by fake worker, round {len(CALLS['worker'])}\n")
    copied = []
    grants = (
        json.loads(Path(request.env["ORCHESTRATOR_GRANTS"]).read_text())
        if "ORCHESTRATOR_GRANTS" in request.env
        else []
    )
    if any(g["mode"] == "read-write" for g in grants):
        src = next(g for g in grants if g["mode"] == "read")
        dst = next(g for g in grants if g["mode"] == "read-write")
        sample = Path(src["path"]) / "cases" / "SF1" / "input.pdf"
        if sample.exists():
            proc = subprocess.run(
                [
                    "orchestrator-cp",
                    f"{src['name']}:cases/SF1/input.pdf",
                    f"{dst['name']}:agent-drops/SF1/input.pdf",
                ],
                env=request.env,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, proc.stderr
            copied.append(
                {
                    "from": f"{src['name']}:cases/SF1/input.pdf",
                    "to": f"{dst['name']}:agent-drops/SF1/input.pdf",
                }
            )
    return {
        "status": "completed",
        "summary": "Added agent_change.txt as asked.",
        "changed_paths": ["agent_change.txt"],
        "tests_selected": ["python3 -c pass"],
        "test_rationale": "smoke",
        "copied_files": copied,
    }


class _Fake:
    name = "fake"
    capabilities = FULL
    role = "worker"

    def preflight(self, role: RoleConfig) -> list[Problem]:
        return []

    async def run(self, request: AgentRequest) -> AgentResult:
        CALLS[self.role].append(request)
        if SCRIPT[self.role]:
            out = SCRIPT[self.role].pop(0)
            if out.get("_error"):
                # the runtime died under the agent: no output, but a resumable session
                return AgentResult(
                    False,
                    "error",
                    raw_text=out["_error"],
                    session_id=f"{self.role}-session",
                    error=out["_error"],
                )
            if out.get("_write"):
                (request.cwd / "agent_change.txt").write_text(out.pop("_write"))
            if out.get("_touch"):
                (request.cwd / out.pop("_touch")).write_text("")
            if out.get("_delete"):
                (request.cwd / out.pop("_delete")).unlink()
        elif self.role == "worker":
            out = default_worker_output(request)
        else:
            out = {"verdict": "approve", "findings": [], "summary_markdown": "Looks fine."}
        if self.capabilities.structured_output:
            return AgentResult(
                True,
                "completed",
                raw_text=json.dumps(out),
                structured_output=out,
                session_id=f"{self.role}-session",
                cost_usd=0.01,
                num_turns=3,
            )
        # prompt-and-parse runtimes return fenced text and the pipeline extracts it
        from orchestrator.agents.base import extract_json

        text = "Here you go:\n```json\n" + json.dumps(out) + "\n```\n"
        return AgentResult(
            True,
            "completed",
            raw_text=text,
            structured_output=extract_json(text),
            session_id=None,
            cost_usd=None,
        )


class FakeWorker(_Fake):
    name = "fake-worker"
    role = "worker"


class FakeReviewer(_Fake):
    name = "fake-reviewer"
    role = "reviewer"


class FakeBareReviewer(_Fake):
    """A reviewer with no native capabilities, to exercise the gap-filling paths."""

    name = "fake-bare"
    role = "reviewer"
    capabilities = NONE


class FakeTracker:
    def __init__(self, issues: dict[str, Issue] | None = None) -> None:
        self.issues = issues or {}
        self.comments: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, str]] = []
        self.attachments: list[tuple[str, str]] = []

    async def get_issue(self, key: str) -> Issue:
        return self.issues[key]

    async def children(self, epic_key: str) -> list[Issue]:
        return [i for i in self.issues.values() if i.raw.get("parent") == epic_key]

    async def comment(self, key: str, body: str) -> None:
        self.comments.append((key, body))

    async def transition(self, key: str, to_status: str) -> None:
        self.transitions.append((key, to_status))

    async def attach(self, key: str, path: Path) -> None:
        self.attachments.append((key, path.name))

    async def download_attachment(self, attachment: Attachment, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"data")

    async def check(self) -> list[str]:
        return []


def issue(key: str, summary: str = "Do the thing", issue_type: str = "Task", **kw) -> Issue:
    return Issue(
        key=key,
        summary=summary,
        description_markdown="Please add agent_change.txt to the repo.",
        issue_type=issue_type,
        status="To Do",
        url=f"https://example.atlassian.net/browse/{key}",
        **kw,
    )
