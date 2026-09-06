from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from orchestrator.agents import registry
from tests import fakes


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


FAKE_PRECOMMIT_SCRIPT = """#!/bin/sh
mkdir -p .orchestrator
echo "$*" >> .orchestrator/precommit.log
if [ "$1" = run ] && [ -e precommit-fail ]; then echo "fake-hook.....Failed"; exit 1; fi
exit 0
"""
FAKE_PRECOMMIT_SETUP = (
    "python3 -c \"import pathlib,stat;b=pathlib.Path('python-env-fake/bin');b.mkdir(parents=True,exist_ok=True);"
    f"p=b/'pre-commit';p.write_text({FAKE_PRECOMMIT_SCRIPT!r});p.chmod(p.stat().st_mode|stat.S_IXUSR);"
    "q=b/'python';q.write_text('#!/bin/sh\\nexit 0\\n');q.chmod(q.stat().st_mode|stat.S_IXUSR)\""
)


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
            "support": {"paths": {"darwin": str(support), "linux": str(support)}},
            "raid": {"paths": {"darwin": str(raid), "linux": str(raid)}, "write_under": ["agent-drops"]},
        },
        "build": {
            # setup stands in for mkenv: it creates a fake worktree venv holding a fake pre-commit that
            # logs its invocations and fails `run` while a precommit-fail marker exists in the worktree
            "setup": [FAKE_PRECOMMIT_SETUP],
            "commands": ["python3 -c pass"],
            "timeout_minutes": 1,
        },
        "test": {
            "timeout_minutes": 1,
            "selection": {
                "strategy": "changed-paths",
                "map": {"agent_change": ["python3 -c pass"]},
                "fallback": ["python3 -c pass"],
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
        "scheduler": {"max_parallel": 2},
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
