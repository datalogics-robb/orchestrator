"""Remote builds without ssh: the node script, the sync delta, and the pipeline pinned to a platform.

`DirectTransport` runs the node script as a local subprocess against a temporary root, and a clone
of the test repository stands in for the machine's checkout.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from orchestrator.config.loader import load_config
from orchestrator.config.schema import Config, PlatformConfig
from orchestrator.intake.base import ExplicitKeys
from orchestrator.pipeline.runtime import build_runtime
from orchestrator.pipeline.scheduler import run_all
from orchestrator.remote import delta
from orchestrator.remote.builds import RemoteBuilds
from orchestrator.remote.client import DirectTransport, Machine, NodeClient, NodeError, SshTransport
from tests import fakes


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def machine_checkout(git_repo: tuple[Path, Path], tmp_path: Path) -> Path:
    origin, _ = git_repo
    checkout = tmp_path / "machine-checkout"
    _git("clone", "-q", str(origin), str(checkout), cwd=tmp_path)
    return checkout


def _platform(tmp_path: Path, checkout: Path | None, **extra: object) -> dict:
    return {
        "aliases": ["aarch64"],
        "ssh": {"user": "builder"},
        "root": str(tmp_path / "machine-root"),
        **({"checkout": str(checkout)} if checkout else {}),
        "env": {"REMOTE_FLAG": "on"},
        "nodes": [{"host": "arm-1"}],
        **extra,
    }


def _machine(tmp_path: Path, checkout: Path | None) -> Machine:
    cfg = PlatformConfig.model_validate(_platform(tmp_path, checkout))
    return Machine("armtest", cfg, cfg.nodes[0])


# --- config -------------------------------------------------------------------------------------


def test_platform_paths_must_be_absolute_and_local_is_reserved(config_dict: dict, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute path on the build machine"):
        PlatformConfig.model_validate(_platform(tmp_path, None, root="relative/root"))
    with pytest.raises(ValueError, match="no root"):
        PlatformConfig.model_validate({**_platform(tmp_path, None), "root": None})
    with pytest.raises(ValueError, match="'local' is reserved"):
        Config.model_validate({**config_dict, "platforms": {"local": _platform(tmp_path, None)}})


def test_ssh_argv_checks_host_keys_and_quotes_the_remote_command(tmp_path: Path) -> None:
    raw = _platform(tmp_path, None)
    raw["ssh"] = {"user": "rocky", "identity_file": "~/.ssh/k.pem", "known_hosts": "/etc/kh"}
    cfg = PlatformConfig.model_validate(raw)
    m = Machine("armtest", cfg, cfg.nodes[0])
    argv = SshTransport().argv(m, ["python3", m.script, "run", "--", "echo", "a b; rm -rf /"])
    assert argv[0] == "ssh" and "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    assert "UserKnownHostsFile=/etc/kh" in argv and argv[argv.index("-i") + 1].endswith("/.ssh/k.pem")
    assert argv[-3:-1] == ["rocky@arm-1", "--"]
    assert argv[-1].endswith("echo 'a b; rm -rf /'")


# --- the node script ----------------------------------------------------------------------------


async def _sync(client: NodeClient, wt: Path, index: Path, old: str, base: str | None, out: Path) -> str:
    new = await delta.snapshot(wt, index, base)
    changes = await delta.changes(wt, old, new)
    delta.write_payload(wt, changes, out)
    await client.sync("r1", "PROJ-1", out)
    return new


async def test_prepare_from_checkout_then_sync_only_the_delta(
    git_repo: tuple[Path, Path], machine_checkout: Path, tmp_path: Path
) -> None:
    _, clone = git_repo
    base = _git("rev-parse", "HEAD", cwd=clone)
    machine = _machine(tmp_path, machine_checkout)
    client = NodeClient(machine, DirectTransport())
    info = await client.ensure_installed()
    assert info["checkout"]["exists"] and info["running_jobs"] == 0
    await client.prepare("r1", "PROJ-1", base, "unused", "develop")
    src = Path(machine.task_src("r1", "PROJ-1"))
    assert (src / "README.md").read_text() == "# target\n"  # a detached worktree at the base commit
    assert str(src) in _git("worktree", "list", cwd=machine_checkout)

    (clone / "README.md").write_text("# changed\n")
    (clone / "new.sh").write_text("#!/bin/sh\necho hi\n")
    (clone / "new.sh").chmod(0o755)
    (clone / "precommit-fail").write_text("ignored, never sent\n")
    index, payload = tmp_path / "index", tmp_path / "payload"
    first = await _sync(client, clone, index, f"{base}^{{tree}}", base, payload)
    changes = await delta.changes(clone, f"{base}^{{tree}}", first)
    assert sorted(c.path for c in changes) == ["README.md", "new.sh"]
    assert (src / "README.md").read_text() == "# changed\n" and (src / "new.sh").stat().st_mode & 0o111
    assert not (src / "precommit-fail").exists()

    (src / "build-output.o").write_text("kept: untracked on the machine")
    (clone / "new.sh").unlink()
    (clone / ".gitignore").write_text((clone / ".gitignore").read_text() + "extra\n")
    second = await _sync(client, clone, index, first, None, payload)
    assert [(c.path, c.status) for c in await delta.changes(clone, first, second)] == [
        (".gitignore", "M"),
        ("new.sh", "D"),
    ]
    assert not (src / "new.sh").exists() and (src / "build-output.o").exists()

    await client.clean("r1", "PROJ-1")
    assert not src.exists() and str(src) not in _git("worktree", "list", cwd=machine_checkout)


async def test_prepare_fetches_a_missing_base_commit(
    git_repo: tuple[Path, Path], machine_checkout: Path, tmp_path: Path
) -> None:
    origin, clone = git_repo
    (clone / "later.txt").write_text("after the machine cloned\n")
    _git("add", "later.txt", cwd=clone)
    _git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "later", cwd=clone)
    _git("push", "-q", "origin", "develop", cwd=clone)
    base = _git("rev-parse", "HEAD", cwd=clone)
    client = NodeClient(_machine(tmp_path, machine_checkout), DirectTransport())
    with pytest.raises(NodeError, match="not in"):
        await client._op("prepare", "r1", "PROJ-1", base)
    await client.prepare("r1", "PROJ-1", base, str(origin), "develop")
    assert (tmp_path / "machine-root" / "r1" / "PROJ-1" / "src" / "later.txt").exists()
    assert (
        _git("rev-parse", "--abbrev-ref", "HEAD", cwd=machine_checkout) == "develop"
    )  # its branch is untouched


def _payload(path: Path, members: list[tarfile.TarInfo], delete: list[str] | None = None) -> Path:
    with path.open("wb") as fh:
        fh.write(json.dumps({"delete": delete or []}).encode() + b"\n")
        with gzip.GzipFile(fileobj=fh, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w|") as tar:
            for m in members:
                tar.addfile(m, io.BytesIO(b"x" * m.size) if m.isfile() else None)
    return path


@pytest.mark.parametrize(
    ("name", "kind", "link", "delete"),
    [
        ("../escape.txt", tarfile.REGTYPE, "", None),
        ("/etc/passwd", tarfile.REGTYPE, "", None),
        ("a/../../escape.txt", tarfile.REGTYPE, "", None),
        (".git/config", tarfile.REGTYPE, "", None),
        ("link", tarfile.SYMTYPE, "../../outside", None),
        ("link", tarfile.SYMTYPE, "/etc", None),
        ("ok.txt", tarfile.REGTYPE, "", ["../outside.txt"]),
        ("dev", tarfile.CHRTYPE, "", None),
    ],
)
async def test_sync_refuses_paths_that_leave_the_source(
    tmp_path: Path, name: str, kind: bytes, link: str, delete: list[str] | None
) -> None:
    client = NodeClient(_machine(tmp_path, None), DirectTransport())
    await client.prepare("r1", "PROJ-1", "abcdef1", "unused", "develop")
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    info.size = 3 if kind == tarfile.REGTYPE else 0
    with pytest.raises(NodeError, match="refused"):
        await client.sync("r1", "PROJ-1", _payload(tmp_path / "p", [info], delete))
    assert not (tmp_path / "machine-root" / "r1" / "escape.txt").exists()


async def test_run_streams_output_and_exit_code_with_the_platform_env(tmp_path: Path) -> None:
    client = NodeClient(_machine(tmp_path, None), DirectTransport())
    await client.prepare("r1", "PROJ-1", "abcdef1", "unused", "develop")
    script = "import os, sys; print('flag', os.environ['REMOTE_FLAG'], os.getcwd()); sys.exit(7)"
    out = await client.run("r1", "PROJ-1", "j1", ["python3", "-c", script], {"REMOTE_FLAG": "on"}, 60)
    assert out.exit_code == 7 and not out.timed_out
    assert out.stdout.startswith("flag on ") and out.stdout.strip().endswith("/r1/PROJ-1/src")
    missing = await client.run("r1", "PROJ-1", "j2", ["no-such-command"], {}, 60)
    assert missing.exit_code == 127 and "command not found" in missing.stderr


async def test_a_timeout_kills_the_job_on_the_machine(tmp_path: Path) -> None:
    client = NodeClient(_machine(tmp_path, None), DirectTransport())
    await client.prepare("r1", "PROJ-1", "abcdef1", "unused", "develop")
    marker = tmp_path / "still-running"
    script = f"import time, pathlib\ntime.sleep(3)\npathlib.Path({str(marker)!r}).write_text('x')"
    out = await client.run("r1", "PROJ-1", "slow", ["python3", "-c", script], {}, 0.5)
    assert out.timed_out and out.exit_code is None
    await asyncio.sleep(3.5)
    assert not marker.exists()
    assert (await client.info())["running_jobs"] == 0


# --- the pipeline on a platform -----------------------------------------------------------------


def _remote_config(
    config_dict: dict, tmp_path: Path, checkout: Path | None, *, needs_flag: bool = True
) -> Path:
    raw = dict(config_dict)
    flag_check = "import os; assert os.environ['REMOTE_FLAG'] == 'on'" if needs_flag else "pass"
    raw["build"] = {
        **raw["build"],
        "local_setup": raw["build"]["setup"],  # the fake pre-commit venv the host's commit gate needs
        "setup": ["python3 -c \"open('remote-setup-ran', 'w').write('yes')\""]
        if needs_flag
        else raw["build"]["setup"],  # a local task runs only setup, which must cover local_setup
        "commands": [f'python3 -c "{flag_check}"'],
    }
    raw["platforms"] = {"armtest": _platform(tmp_path, checkout)}
    path = tmp_path / "remote.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


async def _remote_run(path: Path, keys: list[str], *, platform: str | None, dry_run: bool = True):
    tracker = fakes.FakeTracker({k: fakes.issue(k) for k in keys})
    cfg = load_config(path)
    rt = build_runtime(cfg, path, dry_run=dry_run, keep_worktrees=dry_run, tracker=tracker, platform=platform)
    rt.remote = RemoteBuilds(cfg, DirectTransport())
    rt.store.create_run(rt.run_id, path, keys, dry_run)
    results = await run_all(rt, await ExplicitKeys(tracker, keys).tasks())
    events = [json.loads(line) for line in rt.audit.path.read_text().splitlines()]
    return rt, results, events


@pytest.mark.usefixtures("fake_runners")
async def test_a_pinned_task_runs_its_gates_on_the_machine(
    config_dict: dict, machine_checkout: Path, tmp_path: Path
) -> None:
    path = _remote_config(config_dict, tmp_path, machine_checkout)
    rt, results, events = await _remote_run(path, ["PROJ-1"], platform="armtest")
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.platform == "armtest" and task.node == "arm-1" and task.platform_trigger == "operator"
    src = tmp_path / "machine-root" / rt.run_id / "PROJ-1" / "src"
    wt = Path(task.worktree_path)
    # setup ran there and not here; the agent's change was synced; the commit gate ran here
    assert (src / "remote-setup-ran").exists() and not (wt / "remote-setup-ran").exists()
    assert (src / "agent_change.txt").read_text() == (wt / "agent_change.txt").read_text()
    assert (wt / "python-env-fake").exists() and not (src / "python-env-fake").exists()
    names = [e["event"] for e in events]
    assert names.index("pinned") < names.index("node_prepare") < names.index("node_sync")
    commands = [e for e in events if e["event"] == "remote_command"]
    assert {e["machine"] for e in commands} == {"arm-1"} and all(e["exit_code"] == 0 for e in commands)
    assert any("REMOTE_FLAG" in " ".join(e["argv"]) for e in commands)  # the build
    assert any(e["argv"] == ["python3", "-c", "pass"] for e in commands)  # the selected test
    assert src.exists()  # a dry run keeps the machine's directory, like its worktree


@pytest.mark.usefixtures("fake_runners")
async def test_a_done_task_cleans_its_machine_directory(
    config_dict: dict, machine_checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _remote_config(config_dict, tmp_path, machine_checkout)
    rt, results, events = await _remote_run(path, ["PROJ-1"], platform="armtest", dry_run=True)
    assert results[0].state == "DONE"
    rt.dry_run = rt.keep_worktrees = False
    await rt.remote.clean(rt, results[0])
    assert not (tmp_path / "machine-root" / rt.run_id / "PROJ-1").exists()
    assert "machine-root" not in _git("worktree", "list", cwd=machine_checkout)


@pytest.mark.usefixtures("fake_runners")
async def test_a_failing_remote_build_becomes_a_fix_round_with_local_paths(
    config_dict: dict, tmp_path: Path
) -> None:
    path = _remote_config(config_dict, tmp_path, None)  # no checkout: the first sync sends everything
    raw = yaml.safe_load(path.read_text())
    raw["build"]["commands"] = [
        "python3 -c \"import os, sys; sys.exit(0 if os.path.exists('fixed') else "
        "print(os.path.join(os.getcwd(), 'agent_change.txt'), 'is wrong') or 1)\""
    ]
    path.write_text(yaml.safe_dump(raw))

    output = {
        "status": "completed",
        "summary": "Changed agent_change.txt.",
        "changed_paths": ["agent_change.txt"],
        "tests_selected": ["python3 -c pass"],
        "test_rationale": "smoke",
        "copied_files": [],
    }
    fakes.SCRIPT["worker"] += [{**output, "_write": "v1"}, {**output, "_write": "v2", "_touch": "fixed"}]
    rt, results, _ = await _remote_run(path, ["PROJ-1"], platform="armtest")
    task = results[0]
    assert task.state == "DONE", task.error
    assert task.round == 1
    fix_prompt = fakes.CALLS["worker"][1].prompt
    assert f"{task.worktree_path}/agent_change.txt is wrong" in fix_prompt
    assert "machine-root" not in fix_prompt


@pytest.mark.usefixtures("fake_runners")
async def test_without_a_platform_nothing_is_contacted(config_dict: dict, tmp_path: Path) -> None:
    path = _remote_config(config_dict, tmp_path, None, needs_flag=False)
    for platform in (None, "local"):
        rt, results, events = await _remote_run(path, ["PROJ-1"], platform=platform)
        assert results[0].state == "DONE" and results[0].node is None
        assert not any(e["event"].startswith(("node_", "pinned", "remote_")) for e in events)
    assert not (tmp_path / "machine-root").exists()


@pytest.mark.usefixtures("fake_runners")
async def test_an_unreachable_platform_fails_the_task(config_dict: dict, tmp_path: Path) -> None:
    raw = yaml.safe_load(_remote_config(config_dict, tmp_path, tmp_path / "no-such-checkout").read_text())
    path = tmp_path / "remote.yaml"
    path.write_text(yaml.safe_dump(raw))
    rt, results, _ = await _remote_run(path, ["PROJ-1"], platform="armtest")
    assert results[0].state == "FAILED"
    assert "no build machine for armtest is usable" in results[0].error
    assert "checkout" in results[0].error
