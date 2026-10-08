"""RunManager: runs the daemon drives in-process, queued under `web.max_concurrent_runs`.

A submission is fetched from the tracker (epics expand to their children) and recorded as a
`queued` run with its tasks at once, so it appears on the page before it starts. A resumed run is
queued the same way, from its checkpoints. Each run then waits for a slot, and for a recent
successful login probe, before `run_all` drives it. The daemon owns every run it queues: it keeps
their heartbeats fresh. A run the operator cancels is left `cancelled`; on shutdown the unfinished
ones are left `interrupted`, which `web.resume_on_start` picks up again.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config.schema import Config
from orchestrator.intake.base import KEY_PATTERN, ExplicitKeys, TaskSpec
from orchestrator.pipeline.runtime import Runtime, build_runtime
from orchestrator.pipeline.scheduler import new_task, reopen_for_retry, run_all
from orchestrator.pipeline.task import TaskState, Workflow
from orchestrator.reporting.run_report import write_run_report
from orchestrator.state.store import HEARTBEAT_SECONDS, RunInUse, RunStatus, Store, effective_status
from orchestrator.trackers.base import Tracker
from orchestrator.web.status import resumable

LOGIN_MAX_AGE = 30 * 60.0
"""A run starts without probing the agents' logins again if the last successful probe is this recent."""
LOGIN_RETRY_SECONDS = 60.0

ACTIVE_RUNS: frozenset[RunStatus] = frozenset({"queued", "running", "paused"})
"""A key with an unfinished task in a run like this is already being worked on."""

LoginProbe = Callable[[], Awaitable[list[str]]]
"""Probes the agents' logins; returns the failures, empty when every login answered."""


class ControlError(Exception):
    """A control request was refused; the message says why, for the operator, and `status` is the HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Submitted:
    run_id: str
    keys: list[str]
    """The run's tasks, after epics expanded to their children."""
    dry_run: bool


def parse_keys(keys: list[str]) -> list[str]:
    """Upper-cased, de-duplicated keys in the order given; raises ControlError naming any that are not keys."""
    out = list(dict.fromkeys(k.strip().upper() for k in keys if k.strip()))
    if not out:
        raise ControlError("give at least one issue or epic key, e.g. PROJ-123")
    bad = [k for k in out if not KEY_PATTERN.match(k)]
    if bad:
        raise ControlError(f"not Jira keys: {', '.join(bad)}; expected the form PROJ-123")
    return out


def _print(line: str) -> None:
    print(line, flush=True)


class RunManager:
    def __init__(
        self,
        cfg: Config,
        config_path: Path,
        store: Store,
        *,
        log: Callable[[str], None] = _print,
        login_probe: LoginProbe | None = None,
        tracker_factory: Callable[[], Tracker] | None = None,
        login_retry_seconds: float = LOGIN_RETRY_SECONDS,
    ) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.store = store
        self.log = log
        self.login_probe = login_probe
        self.tracker_factory = tracker_factory
        self.login_retry_seconds = login_retry_seconds
        self.last_login_ok: float | None = None
        """`time.monotonic()` of the last successful login probe."""
        self._slots = asyncio.Semaphore(cfg.web.max_concurrent_runs)
        self._submitting = asyncio.Lock()
        self._runs: dict[str, asyncio.Task[None]] = {}
        self._cancelled: dict[str, str] = {}
        self._stopped_by: str | None = None
        """Runs the operator cancelled, with the client that asked."""
        self._heartbeat: asyncio.Task[None] | None = None

    @property
    def run_ids(self) -> list[str]:
        """Runs this daemon accepted and has not yet let go of."""
        return list(self._runs)

    def start(self) -> None:
        self._heartbeat = asyncio.create_task(self._beat())

    async def shutdown(self, *, client: str | None = None) -> None:
        """Cancel every run; each is left `interrupted` with its tasks checkpointed.

        `client` is who asked from the page; each interrupted run's audit log records it.
        """
        self._stopped_by = client
        if self._heartbeat:
            self._heartbeat.cancel()
        runs = list(self._runs.values())
        for task in runs:
            task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)

    async def wait(self, run_id: str) -> None:
        """Until the run has been driven to its end (or cancelled)."""
        task = self._runs.get(run_id)
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)

    async def cancel(self, run_id: str, *, client: str) -> None:
        """Stop a queued or running run this daemon drives, killing its agents; returns once it has stopped."""
        task = self._runs.get(run_id)
        if task is None:
            if self.store.get_run(run_id) is None:
                raise ControlError(f"no run {run_id}", 404)
            raise ControlError(f"run {run_id} is not queued or running in this daemon", 409)
        self._cancelled[run_id] = client
        task.cancel()
        await self.wait(run_id)

    async def submit(
        self, keys: list[str], *, workflow: Workflow | None, dry_run: bool, client: str
    ) -> Submitted:
        keys = parse_keys(keys)
        async with self._submitting:
            self._refuse_busy(keys)
            try:
                rt = await asyncio.to_thread(
                    build_runtime,
                    self.cfg,
                    self.config_path,
                    dry_run=dry_run,
                    tracker=self.tracker_factory() if self.tracker_factory else None,
                )
            except Exception as e:  # noqa: BLE001 - a missing secret, an unreachable clone
                raise ControlError(f"could not set up the run: {e}") from e
            try:
                specs = await ExplicitKeys(rt.tracker, keys).tasks()
                if not specs:
                    raise ControlError(f"no issues to work on: {', '.join(keys)} has no child issues")
                self._refuse_busy([s.key for s in specs])
            except Exception as e:
                await rt.aclose()
                shutil.rmtree(rt.run_dir, ignore_errors=True)
                if isinstance(e, ControlError):
                    raise
                raise ControlError(f"could not read the issues from Jira: {e}") from e
            rt.store.create_run(rt.run_id, self.config_path, keys, dry_run, status="queued", via="daemon")
            tasks = {s.key: new_task(rt, s, workflow) for s in specs}
            for task in tasks.values():
                rt.store.save_task(rt.run_id, task)
            rt.audit.record(
                "submitted",
                client=client,
                keys=keys,
                tasks=list(tasks),
                workflow=workflow or "auto",
                dry_run=dry_run,
            )
            self.log(f"run {rt.run_id} queued: {' '.join(tasks)}" + (" (dry run)" if dry_run else ""))
            await self._start(rt, specs, tasks, workflow)
            return Submitted(rt.run_id, list(tasks), dry_run)

    async def resume(
        self, run_id: str, *, retry_failed: bool = False, retry_blocked: bool = False, client: str
    ) -> Submitted:
        """Queue a run nothing is driving, from its checkpoints; retried FAILED or BLOCKED tasks are reopened."""
        async with self._submitting:
            row = self.store.get_run(run_id)
            if row is None:
                raise ControlError(f"no run {run_id}", 404)
            if run_id in self._runs:
                raise ControlError(f"run {run_id} is already queued or running in this daemon", 409)
            existing = self.store.load_tasks(run_id)
            retried = {"FAILED"} if retry_failed else set()
            retried |= {"BLOCKED"} if retry_blocked else set()
            if not (resumable(existing) or any(t.state in retried for t in existing.values())):
                raise ControlError(
                    f"nothing to resume in run {run_id}: every task is done, blocked, failed, or waiting for "
                    "approval (retry the failed or blocked ones instead)"
                )
            self._refuse_busy(list(existing), except_run=run_id)
            try:
                rt = await asyncio.to_thread(
                    build_runtime,
                    self.cfg,
                    self.config_path,
                    run_id=run_id,
                    dry_run=row.dry_run,
                    tracker=self.tracker_factory() if self.tracker_factory else None,
                )
            except Exception as e:  # noqa: BLE001 - a missing secret, an unreachable clone
                raise ControlError(f"could not set up the run: {e}") from e
            try:
                specs = await ExplicitKeys(rt.tracker, row.keys).tasks()
                rt.store.claim_run(run_id, status="queued", via="daemon")
            except Exception as e:
                await rt.aclose()
                if isinstance(e, RunInUse):
                    raise ControlError(str(e), 409) from e
                raise ControlError(f"could not read the issues from Jira: {e}") from e
            reopened = reopen_for_retry(rt, existing, retry_failed=retry_failed, retry_blocked=retry_blocked)
            rt.audit.record("resumed", client=client, reopened=reopened, dry_run=row.dry_run)
            self.log(
                f"run {run_id} queued to resume" + (f", retrying {' '.join(reopened)}" if reopened else "")
            )
            await self._start(rt, specs, existing, None)
            return Submitted(run_id, [s.key for s in specs], row.dry_run)

    async def resume_interrupted(self, scan: int = 50) -> list[str]:
        """Queue every interrupted run a daemon was driving (`web.resume_on_start`). Returns their ids."""
        queued: list[str] = []
        for run in reversed(self.store.list_runs(scan)):
            if run.via != "daemon" or effective_status(run) != "interrupted":
                continue
            try:
                await self.resume(run.run_id, client="resume_on_start")
            except ControlError as e:
                self.log(f"run {run.run_id} not resumed: {e}")
                continue
            queued.append(run.run_id)
        return queued

    def _refuse_busy(self, keys: list[str], except_run: str | None = None) -> None:
        """Two runs on one key would fight over its branch, so a key already in progress is refused."""
        wanted = set(keys)
        busy: list[str] = []
        for run in self.store.list_runs(50):
            if run.run_id == except_run:
                continue
            tasks = self.store.load_tasks(run.run_id)
            status = effective_status(run, any_paused=any(t.paused for t in tasks.values()))
            if status not in ACTIVE_RUNS:
                continue
            busy += [
                f"{t.key} ({status} in run {run.run_id})"
                for t in tasks.values()
                if t.key in wanted and not t.terminal
            ]
        if busy:
            raise ControlError("already in progress: " + ", ".join(busy))

    async def _start(
        self, rt: Runtime, specs: list[TaskSpec], tasks: dict[str, TaskState], workflow: Workflow | None
    ) -> None:
        self._runs[rt.run_id] = asyncio.create_task(self._drive(rt, specs, tasks, workflow))
        # a task cancelled before its first step never runs its finally; let it enter the try first
        await asyncio.sleep(0)

    async def _drive(
        self, rt: Runtime, specs: list[TaskSpec], tasks: dict[str, TaskState], workflow: Workflow | None
    ) -> None:
        left: RunStatus = "interrupted"

        def on_event(key: str, state: str, note: str) -> None:
            self.log(rt.redactor.redact(f"run {rt.run_id} {key} {state} {note}".rstrip()))

        rt.on_event = on_event
        try:
            async with self._slots:
                await self._wait_for_logins(rt.run_id)
                rt.store.claim_run(rt.run_id, via="daemon")
                rt.audit.record("started")
                self.log(f"run {rt.run_id} started")
                results = await run_all(rt, specs, tasks, workflow=workflow)
                left = "paused" if any(r.paused for r in results) else "finished"
                write_run_report(rt.run_dir, rt.run_id, results, rt.dry_run)
                outcome = ", ".join(f"{r.key} {r.state}" for r in results)
                self.log(f"run {rt.run_id} {left}: {outcome}")
        except Exception as e:  # noqa: BLE001 - the daemon outlives any one run
            rt.audit.record("run_error", error=repr(e))
            self.log(rt.redactor.redact(f"run {rt.run_id} stopped: {e!r}"))
        except asyncio.CancelledError:
            client = self._cancelled.pop(rt.run_id, None)
            if client is not None:
                left = "cancelled"
                rt.audit.record("cancelled", client=client)
                self.log(f"run {rt.run_id} cancelled")
            elif self._stopped_by is not None:
                rt.audit.record("daemon_shutdown", client=self._stopped_by)
            raise
        finally:
            rt.store.release_run(rt.run_id, left)
            await rt.aclose()
            self._runs.pop(rt.run_id, None)

    async def _wait_for_logins(self, run_id: str) -> None:
        """Every task would fail at its first agent call on an expired login, so the run waits instead."""
        if self.login_probe is None:
            return
        while self.last_login_ok is None or time.monotonic() - self.last_login_ok > LOGIN_MAX_AGE:
            failures = await self.login_probe()
            if not failures:
                self.last_login_ok = time.monotonic()
                return
            self.log(f"run {run_id} waits for the agents' logins: {'; '.join(failures)}")
            await asyncio.sleep(self.login_retry_seconds)

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            for run_id in self.run_ids:
                self.store.heartbeat(run_id)
