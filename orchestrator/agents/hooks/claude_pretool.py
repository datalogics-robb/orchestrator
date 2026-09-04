#!/usr/bin/env python3
"""Claude Code PreToolUse hook. Standard library only; invoked with the rules file path.

Reads the hook payload from stdin, exits 2 with a reason on stderr to block the tool call,
exits 0 to allow it.
"""

from __future__ import annotations

import json
import os
import re
import sys


def _under(path: str, roots: list[str]) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip("/") + "/"):
            return True
    return False


def decide(payload: dict, rules: dict) -> str | None:
    """Return a denial reason, or None to allow."""
    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input", {}) or {}

    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        squashed = re.sub(r"\s+", " ", cmd).strip()
        for prefix in rules.get("deny_commands", []):
            if re.search(r"(^|[;&|]\s*|\$\(\s*|`\s*)" + re.escape(prefix.strip()) + r"(\s|$)", squashed):
                return f"'{prefix.strip()}' is reserved for the orchestrator"
        if rules.get("read_only_worktree"):
            for pat in rules.get("write_patterns", []):
                if re.search(pat, squashed):
                    return "this role has read-only access; no commands that write"
        for ro in rules.get("read_only_paths", []):
            if ro in cmd:
                for pat in rules.get("write_patterns", []):
                    if re.search(pat, squashed):
                        return f"{ro} is a read-only share"
        return None

    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        if rules.get("read_only_worktree"):
            return "this role has read-only access"
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if path and not _under(path, rules.get("write_roots", [])):
            return f"{path} is outside the writable roots for this task"
        for ro in rules.get("read_only_paths", []):
            if path and _under(path, [ro]):
                return f"{ro} is a read-only share"
    return None


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    try:
        with open(sys.argv[1]) as f:
            rules = json.load(f)
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 0
    reason = decide(payload, rules)
    if reason:
        sys.stderr.write(f"Blocked by orchestrator policy: {reason}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
