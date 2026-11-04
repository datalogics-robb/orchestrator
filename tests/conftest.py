from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestrator.agents import registry
from tests import fakes


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


# The fake pre-commit: logs its invocations, `install` writes a git hook that calls it back
# through PATH, and `run` fails while a precommit-fail marker exists in the worktree.
FAKE_PRECOMMIT_SCRIPT = """
import os, subprocess, sys
from pathlib import Path

Path(".orchestrator").mkdir(exist_ok=True)
with open(".orchestrator/precommit.log", "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["install"]:
    out = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], capture_output=True, check=True)
    hooks = Path(out.stdout.decode().strip())
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_bytes(
        b"#!/bin/sh\\n"
        b'command -v pre-commit >/dev/null || { echo "pre-commit not on PATH in git hook"; exit 1; }\\n'
        b"exec pre-commit run --hook-stage commit\\n"
    )
    hook.chmod(0o755)
if sys.argv[1:2] == ["run"] and os.path.exists("precommit-fail"):
    print("fake-hook.....Failed")
    sys.exit(1)
"""

# Stands in for mkenv: a fake worktree venv holding the fake pre-commit and a python stub. The
# extension-less sh wrapper is what git's hook (run by sh, Git Bash on Windows) finds on PATH;
# Windows also gets a .cmd, which is what shutil.which finds there.
FAKE_VENV_SETUP = """
import sys
from pathlib import Path

script = sys.argv[1]
python = Path(sys.executable).as_posix()
win = sys.platform == "win32"
b = Path("python-env-fake") / ("Scripts" if win else "bin")
b.mkdir(parents=True, exist_ok=True)
sh = b / "pre-commit"
sh.write_bytes(f'#!/bin/sh\\nexec "{python}" "{script}" "$@"\\n'.encode())
sh.chmod(0o755)
if win:
    (b / "pre-commit.cmd").write_bytes(f'@"{python}" "{script}" %*\\r\\n'.encode())
    (b / "python.exe").write_bytes(b"")
else:
    q = b / "python"
    q.write_bytes(b"#!/bin/sh\\nexit 0\\n")
    q.chmod(0o755)
"""


def fake_precommit_setup(tmp_path: Path) -> str:
    """A build.setup command that plants the fake pre-commit (and a python stub) in a fake worktree venv."""
    script = tmp_path / "fake-pre-commit.py"
    script.write_text(FAKE_PRECOMMIT_SCRIPT, encoding="utf-8")
    setup = tmp_path / "fake-venv-setup.py"
    setup.write_text(FAKE_VENV_SETUP, encoding="utf-8")
    return f"{fakes.PYQ} {shlex.quote(setup.as_posix())} {shlex.quote(script.as_posix())}"


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' and a clone with one commit on branch develop."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-q", "-b", "develop", str(origin), cwd=tmp_path)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)
    _git("checkout", "-q", "-b", "develop", cwd=clone)
    (clone / "README.md").write_text("# target\n")
    (clone / ".pre-commit-config.yaml").write_text("repos: []\n")
    (clone / ".gitignore").write_text("python-env-*/\nprecommit-fail\n")
    _git("add", "README.md", ".pre-commit-config.yaml", ".gitignore", cwd=clone)
    _git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "init", cwd=clone)
    _git("push", "-q", "-u", "origin", "develop", cwd=clone)
    return origin, clone


@pytest.fixture
def shares(tmp_path: Path) -> tuple[Path, Path]:
    support = tmp_path / "support"
    raid = tmp_path / "raid"
    (support / "cases" / "SF1").mkdir(parents=True)
    (support / "cases" / "SF1" / "input.pdf").write_bytes(b"%PDF-1.7 fake")
    (raid / "agent-drops").mkdir(parents=True)
    return support, raid


@pytest.fixture
def config_dict(git_repo: tuple[Path, Path], shares: tuple[Path, Path], tmp_path: Path) -> dict:
    _, clone = git_repo
    support, raid = shares
    return {
        "version": 1,
        "state_dir": str(tmp_path / "state"),
        "tracker": {
            "base_url": "https://example.atlassian.net",
            "project": "PROJ",
            "auth": {"token_env": "TEST_JIRA_TOKEN"},
        },
        "repo": {
            "github": "example/target",
            "base_branch": "develop",
            "clone_path": str(clone),
            "worktree_root": str(tmp_path / "worktrees"),
            "auth": {"token_env": "TEST_GITHUB_TOKEN"},
        },
        "shares": {
            "support": {"paths": {"darwin": str(support), "linux": str(support), "windows": str(support)}},
            "raid": {
                "paths": {"darwin": str(raid), "linux": str(raid), "windows": str(raid)},
                "write_under": ["agent-drops"],
            },
        },
        "build": {
            "setup": [fake_precommit_setup(tmp_path)],
            "commands": [f"{fakes.PYQ} -c pass"],
            "timeout_minutes": 1,
        },
        "test": {
            "timeout_minutes": 1,
            "selection": {
                "strategy": "changed-paths",
                "map": {"agent_change": [f"{fakes.PYQ} -c pass"]},
                "fallback": [f"{fakes.PYQ} -c pass"],
            },
        },
        "agents": {
            "review_rounds": 2,
            "worker": {
                "runner": "fake-worker",
                "auth": {"token_env": "TEST_WORKER_KEY"},
                "shares": {"support": "read", "raid": "read-write"},
                "timeout_minutes": 1,
            },
            "reviewer": {
                "runner": "fake-reviewer",
                "access": "read-only",
                "auth": {"token_env": "TEST_REVIEWER_KEY"},
                "shares": {"support": "read", "raid": "read"},
                "timeout_minutes": 1,
            },
        },
        "scheduler": {"max_parallel": 2, "retry_backoff_seconds": 0},
    }


@pytest.fixture
def config_path(config_dict: dict, tmp_path: Path) -> Path:
    p = tmp_path / "orchestrator.yaml"
    p.write_text(yaml.safe_dump(config_dict))
    return p


@pytest.fixture
def fake_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes.reset()
    monkeypatch.setitem(registry._BUILTIN, "fake-worker", "tests.fakes:FakeWorker")
    monkeypatch.setitem(registry._BUILTIN, "fake-reviewer", "tests.fakes:FakeReviewer")
    monkeypatch.setitem(registry._BUILTIN, "fake-bare", "tests.fakes:FakeBareReviewer")
    for var in ("TEST_JIRA_TOKEN", "TEST_GITHUB_TOKEN", "TEST_WORKER_KEY", "TEST_REVIEWER_KEY"):
        monkeypatch.setenv(var, f"secret-{var.lower()}-0123456789")
