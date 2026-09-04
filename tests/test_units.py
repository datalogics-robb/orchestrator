from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.agents.base import extract_json
from orchestrator.agents.contracts import (
    REVIEWER_SCHEMA,
    WORKER_SCHEMA,
    ContractError,
    parse_reviewer,
    parse_worker,
)
from orchestrator.agents.hooks.claude_pretool import decide
from orchestrator.agents.runners.codex import render_config_toml, render_rules
from orchestrator.build.selection import selector_for
from orchestrator.config.schema import TestSelection as Selection
from orchestrator.environment import build_env
from orchestrator.intake.base import TaskSpec, order_by_dependencies
from orchestrator.mcp import passthrough
from orchestrator.reporting.audit import Redactor
from orchestrator.shares import cp
from orchestrator.trackers.adf import adf_to_markdown
from tests.fakes import issue

# --- contracts ---------------------------------------------------------------


def test_schemas_are_self_contained() -> None:
    assert "$defs" not in WORKER_SCHEMA and "$ref" not in json.dumps(WORKER_SCHEMA)
    assert "$defs" not in REVIEWER_SCHEMA
    assert "copied_files" in WORKER_SCHEMA["properties"]


def test_parse_worker_alias_and_blocked() -> None:
    w = parse_worker(
        {
            "status": "blocked",
            "copied_files": [{"from": "a:b", "to": "c:d"}],
            "blocked": {"reason": "missing-access", "details_markdown": "no"},
        }
    )
    assert w.copied_files[0].from_ == "a:b"
    assert w.blocked and w.blocked.reason == "missing-access"
    with pytest.raises(ContractError):
        parse_worker({"status": "nope"})


def test_reviewer_actionable_split() -> None:
    r = parse_reviewer(
        {
            "verdict": "request_changes",
            "findings": [
                {"severity": "major", "title": "x"},
                {"severity": "nit", "title": "y"},
            ],
        }
    )
    assert [f.title for f in r.actionable] == ["x"]
    assert [f.title for f in r.carried] == ["y"]


def test_extract_json_prefers_last_fenced_block() -> None:
    text = 'thinking {"a": 1}\n```json\n{"b": 2}\n```\nmore\n```\n{"c": 3}\n```'
    assert extract_json(text) == {"c": 3}
    assert extract_json("no json here") is None
    assert extract_json('  {"bare": true} ') == {"bare": True}


# --- hook -------------------------------------------------------------------

RULES = {
    "deny_commands": ["git push", "gh "],
    "read_only_worktree": False,
    "read_only_paths": ["/Volumes/support"],
    "write_roots": ["/work/tree", "/Volumes/raid/agent-drops"],
    "write_patterns": [r"\brm\b", r"\bcp\b", r">>", r"(^|[^>])>(?!>)"],
}


@pytest.mark.parametrize(
    "command,blocked",
    [
        ("git push origin x", True),
        ("git status && git push", True),
        ("gh pr create", True),
        ("git log --oneline", False),
        ("echo hi > /Volumes/support/x", True),
        ("cat /Volumes/support/x", False),
        ("rm -rf build", False),
    ],
)
def test_hook_bash(command: str, blocked: bool) -> None:
    reason = decide({"tool_name": "Bash", "tool_input": {"command": command}}, RULES)
    assert bool(reason) is blocked, reason


def test_hook_edit_paths() -> None:
    assert decide({"tool_name": "Edit", "tool_input": {"file_path": "/work/tree/a.py"}}, RULES) is None
    assert decide({"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"}}, RULES)
    assert decide({"tool_name": "Write", "tool_input": {"file_path": "/Volumes/support/x"}}, RULES)
    ro = {**RULES, "read_only_worktree": True}
    assert decide({"tool_name": "Edit", "tool_input": {"file_path": "/work/tree/a.py"}}, ro)
    assert decide({"tool_name": "Bash", "tool_input": {"command": "rm x"}}, ro)


# --- codex config rendering ---------------------------------------------------


def test_codex_toml_and_rules() -> None:
    toml = render_config_toml(
        {
            "jenkins": {
                "url": "https://j/mcp",
                "http_headers": {"Authorization": "Bearer t"},
                "tools": {"getJob": {"enabled": True}},
            }
        },
        {"jenkins": ["triggerBuild"]},
    )
    import tomllib

    data = tomllib.loads(toml)
    assert data["mcp_servers"]["jenkins"]["url"] == "https://j/mcp"
    assert data["mcp_servers"]["jenkins"]["http_headers"]["Authorization"] == "Bearer t"
    assert data["mcp_servers"]["jenkins"]["tools"]["triggerBuild"]["enabled"] is False
    assert data["mcp_servers"]["jenkins"]["tools"]["getJob"]["enabled"] is True
    rules = render_rules(["git push", "gh "])
    assert 'prefix_rule(pattern=["git", "push"], decision="forbidden")' in rules


# --- selection ---------------------------------------------------------------


def test_changed_paths_selector() -> None:
    sel = selector_for(
        Selection(
            strategy="changed-paths",
            map={"src/a/": ["pytest tests/a"], "src/b/": ["pytest tests/b"]},
            fallback=["pytest smoke"],
            max_commands=1,
        )
    )
    assert sel.select(["src/b/x.py"], []) == [["pytest", "tests/b"]]
    assert sel.select(["docs/x.md"], []) == [["pytest", "smoke"]]


def test_agent_chosen_selector_enforces_prefix() -> None:
    sel = selector_for(
        Selection(strategy="agent-chosen", allowed_prefixes=["pytest"], fallback=["pytest smoke"])
    )
    assert sel.select([], ["pytest tests/x.py::t", "rm -rf /", "tests/y.py"]) == [
        ["pytest", "tests/x.py::t"],
        ["pytest", "rm", "-rf", "/"],
        ["pytest", "tests/y.py"],
    ]


# --- intake ------------------------------------------------------------------


def test_dependency_order() -> None:
    a, b, c = issue("P-1"), issue("P-2"), issue("P-3")
    specs = [TaskSpec(a, ["P-3"]), TaskSpec(b, []), TaskSpec(c, ["P-2"])]
    assert [s.key for s in order_by_dependencies(specs)] == ["P-2", "P-3", "P-1"]


# --- adf ---------------------------------------------------------------------


def test_adf_to_markdown() -> None:
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Title"}]},
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "bold", "marks": [{"type": "strong"}]}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}],
                    }
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "py"},
                "content": [{"type": "text", "text": "x = 1"}],
            },
        ],
    }
    md = adf_to_markdown(doc)
    assert "## Title" in md and "**bold**" in md and "- one" in md and "```py\nx = 1\n```" in md


# --- environment -------------------------------------------------------------


def test_build_env_scrubs() -> None:
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/h",
        "GITHUB_TOKEN": "x",
        "VIRTUAL_ENV": "/v",
        "PYTHONPATH": "/p",
        "LANG": "C",
    }
    env = build_env(
        base,
        secrets={"ANTHROPIC_API_KEY": "k"},
        extra={"CC": "clang", "PYTHONWARNINGS": "x", "PIP_CACHE_DIR": "/c"},
        prepend_path=[Path("/tools")],
    )
    assert "GITHUB_TOKEN" not in env and "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env
    assert "PYTHONWARNINGS" not in env and env["PIP_CACHE_DIR"] == "/c"
    assert env["ANTHROPIC_API_KEY"] == "k" and env["CC"] == "clang"
    assert env["PATH"].startswith("/tools:")


def test_redactor() -> None:
    r = Redactor()
    r.add("supersecret")
    assert r.redact_obj({"a": ["x supersecret y"]}) == {"a": ["x <redacted> y"]}


# --- copy helper -------------------------------------------------------------


def test_cp_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    support = tmp_path / "support"
    raid = tmp_path / "raid"
    (support / "cases").mkdir(parents=True)
    (support / "cases" / "a.txt").write_text("hello")
    (raid / "drops").mkdir(parents=True)
    grants = [
        {"name": "support", "path": str(support), "mode": "read", "write_under": []},
        {"name": "raid", "path": str(raid), "mode": "read-write", "write_under": [str(raid / "drops")]},
    ]
    (tmp_path / "grants.json").write_text(json.dumps(grants))
    monkeypatch.setenv(cp.GRANTS_ENV, str(tmp_path / "grants.json"))
    monkeypatch.setenv(cp.AUDIT_ENV, str(tmp_path / "audit.jsonl"))
    assert cp.main(["support:cases/a.txt", "raid:drops/a.txt"]) == 0
    assert (raid / "drops" / "a.txt").read_text() == "hello"
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["event"] == "share_copy" and entry["bytes"] == 5
    # refusals
    assert cp.main(["raid:drops/a.txt", "support:cases/b.txt"]) == 1  # support is read-only
    assert cp.main(["support:cases/a.txt", "raid:elsewhere/a.txt"]) == 1  # outside write root
    assert cp.main(["support:../raid/drops/a.txt", "raid:drops/b.txt"]) == 1  # escape
    # runs standalone with the system interpreter, no orchestrator import needed
    proc = subprocess.run(
        [sys.executable, cp.__file__, "support:cases/a.txt", "raid:drops/c.txt"],
        env={**os.environ},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


# --- mcp passthrough ---------------------------------------------------------


def test_mcp_passthrough(config_dict: dict, tmp_path: Path) -> None:
    from orchestrator.config.schema import Config

    claude = tmp_path / "claude.json"
    claude.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ragflow": {
                        "type": "http",
                        "url": "http://r/mcp",
                        "headers": {"Authorization": "Bearer tok12345678"},
                    },
                    "other": {"type": "stdio", "command": "x"},
                }
            }
        )
    )
    codex = tmp_path / "config.toml"
    codex.write_text('[mcp_servers.ragflow]\nurl = "http://r/mcp"\n')
    config_dict["mcp"] = {"sources": {"fake-worker": [str(claude)], "fake-reviewer": [str(codex)]}}
    config_dict["agents"]["worker"]["mcp_servers"] = ["ragflow"]
    config_dict["agents"]["reviewer"]["mcp_servers"] = ["other"]
    cfg = Config.model_validate(config_dict)
    worker = passthrough.servers_for_role(cfg, "worker")
    assert list(worker) == ["ragflow"] and worker["ragflow"]["url"] == "http://r/mcp"
    assert passthrough.secret_values(worker) == ["tok12345678"]
    with pytest.raises(passthrough.MCPError, match="other"):
        passthrough.servers_for_role(cfg, "reviewer")
    assert any(p.level == "error" for p in passthrough.check_mcp(cfg))
