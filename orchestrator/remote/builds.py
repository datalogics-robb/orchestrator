"""Remote builds for the pipeline: pin a task to a machine, keep its directory in sync, run steps there.

A task pinned to a platform keeps one machine for its whole life (`task.platform`, `task.node`).
Its gates (setup, build, red check, tests) sync the local worktree first and then run on that
machine, each command holding one of the machine's `max_concurrent_builds` permits. Output
naming the machine's source directory is rewritten to the local worktree, so excerpts and logs
point at files the agents can open.
"""

from __future__ import annotations

import asyncio
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.build.runner import CommandResult, StepResult
from orchestrator.config.schema import Config, PlatformConfig
from orchestrator.pipeline.task import Failed, TaskState
from orchestrator.remote import delta
from orchestrator.remote.client import Machine, NodeClient, NodeError, Transport, machines

if TYPE_CHECKING:
    from orchestrator.pipeline.runtime import Runtime
    from orchestrator.scm.worktree import Worktree

INDEX_FILE = "node-index"
PAYLOAD_FILE = "node-sync.tar"


def _failed(e: NodeError) -> Failed:
    return Failed(str(e), transient=e.transient)


class RemoteBuilds:
    """Every build machine the config names. Each machine's command limit is shared across the event loop."""

    _semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = (
        weakref.WeakKeyDictionary()
    )

    def __init__(self, cfg: Config, transport: Transport | None = None) -> None:
        self.cfg = cfg
        self.transport = transport

    def platform(self, name: str) -> PlatformConfig:
        try:
            return self.cfg.platforms[name]
        except KeyError:
            raise Failed(f"platform {name!r} is not configured under platforms") from None

    def machine(self, task: TaskState) -> Machine:
        assert task.platform and task.node
        cfg = self.platform(task.platform)
        node = cfg.node(task.node)
        if node is None:
            raise Failed(f"{task.node} is no longer configured for platform {task.platform}")
        return Machine(task.platform, cfg, node)

    def client(self, machine: Machine) -> NodeClient:
        return NodeClient(machine, self.transport)

    def _semaphore(self, machine: Machine) -> asyncio.Semaphore:
        per_loop = RemoteBuilds._semaphores.setdefault(asyncio.get_running_loop(), {})
        key = f"{machine.platform}/{machine.host}"
        sem = per_loop.get(key)
        if sem is None:
            sem = per_loop[key] = asyncio.Semaphore(machine.cfg.max_concurrent_builds)
        return sem

    async def pick(self, platform: str) -> tuple[Machine, dict[str, object]]:
        """The reachable machine of the platform with the fewest running jobs (config order breaks ties)."""
        candidates = machines(platform, self.platform(platform))
        answers = await asyncio.gather(
            *(self.client(m).ensure_installed() for m in candidates), return_exceptions=True
        )
        usable: list[tuple[int, int, Machine, dict[str, object]]] = []
        problems: list[str] = []
        for i, (m, info) in enumerate(zip(candidates, answers, strict=True)):
            if isinstance(info, BaseException):
                problems.append(str(info))
                continue
            checkout = info.get("checkout")
            if m.checkout and not (isinstance(checkout, dict) and checkout.get("exists")):
                problems.append(f"{m.host}: checkout {m.checkout} does not exist")
                continue
            usable.append((int(info.get("running_jobs", 0)), i, m, info))  # type: ignore[call-overload]
        if not usable:
            raise Failed(f"no build machine for {platform} is usable: " + "; ".join(problems), transient=True)
        _, _, machine, info = min(usable, key=lambda u: (u[0], u[1]))
        return machine, info

    def fetch_url(self, machine: Machine) -> str:
        return machine.cfg.fetch_url or f"git@github.com:{self.cfg.repo.github}.git"

    async def prepare(self, rt: Runtime, task: TaskState) -> None:
        """A fresh task directory on the task's machine at the base commit; the next sync starts from there."""
        machine = self.machine(task)
        assert task.base_sha
        try:
            await self.client(machine).prepare(
                rt.run_id, task.key, task.base_sha, self.fetch_url(machine), self.cfg.repo.base_branch
            )
        except NodeError as e:
            raise _failed(e) from e
        (rt.task_dir(task.key) / INDEX_FILE).unlink(missing_ok=True)
        task.node_tree = None
        rt.audit.record("node_prepare", task.key, machine=machine.host, base_sha=task.base_sha)

    async def sync(self, rt: Runtime, task: TaskState, wt: Worktree) -> None:
        """Send the worktree's changes since the last sync to the machine."""
        machine = self.machine(task)
        base = task.base_sha if machine.checkout else None
        new_tree = await delta.snapshot(wt.path, rt.task_dir(task.key) / INDEX_FILE, base)
        old_tree = task.node_tree or (f"{base}^{{tree}}" if base else delta.EMPTY_TREE)
        changes = await delta.changes(wt.path, old_tree, new_tree)
        if changes:
            payload = rt.task_dir(task.key) / PAYLOAD_FILE
            counts = delta.write_payload(wt.path, changes, payload)
            try:
                await self.client(machine).sync(rt.run_id, task.key, payload)
            except NodeError as e:
                raise _failed(e) from e
            finally:
                payload.unlink(missing_ok=True)
            rt.audit.record("node_sync", task.key, machine=machine.host, **counts)
        task.node_tree = new_tree
        rt.store.save_task(rt.run_id, task)

    async def run_step(
        self,
        rt: Runtime,
        task: TaskState,
        wt: Worktree,
        name: str,
        commands: list[list[str]],
        *,
        timeout_seconds: int,
        log_dir: Path,
    ) -> StepResult:
        """Like `build.runner.run_step`, on the task's machine: sync, then run each command until one fails."""
        machine = self.machine(task)
        client = self.client(machine)
        await self.sync(rt, task, wt)
        step = StepResult(name)
        log_dir.mkdir(parents=True, exist_ok=True)
        remote_src = machine.task_src(rt.run_id, task.key)
        for i, argv in enumerate(commands):
            job = f"{name}-{i:02d}"
            async with self._semaphore(machine):
                try:
                    outcome = await client.run(
                        rt.run_id, task.key, job, argv, dict(machine.cfg.env), timeout_seconds
                    )
                except NodeError as e:
                    raise _failed(e) from e
            text = outcome.stdout + ("\n--- stderr ---\n" + outcome.stderr if outcome.stderr else "")
            log_path = log_dir / f"{job}.log"
            log_path.write_text(text.replace(remote_src, str(wt.path)), encoding="utf-8")
            step.results.append(CommandResult(argv, outcome.exit_code, outcome.timed_out, log_path))
            rt.audit.record(
                "remote_command",
                task.key,
                machine=machine.host,
                argv=argv,
                exit_code=outcome.exit_code,
                timed_out=outcome.timed_out,
            )
            if not step.results[-1].ok:
                break
        return step

    async def clean(self, rt: Runtime, task: TaskState) -> None:
        """Remove the task's directory on its machine. A failure is audited, never raised."""
        try:
            machine = self.machine(task)
            await self.client(machine).clean(rt.run_id, task.key)
            rt.audit.record("node_clean", task.key, machine=machine.host)
        except (NodeError, Failed) as e:
            rt.audit.record("node_clean_error", task.key, machine=task.node, error=str(e))
