"""`orchestrator serve`: one long-lived process per config, serving the page and driving submitted runs.

The daemon holds an exclusive lock on `<state_dir>/serve.pid` for its whole life, so a second
daemon on the same state directory refuses to start. The pidfile holds JSON naming the pid, host,
port, and URL; `ready` turns true once the port is listening, which is what `--detach` waits for.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass
from pathlib import Path

import uvicorn

from orchestrator import __version__
from orchestrator.build.runner import BuildSemaphore
from orchestrator.config.loader import resolve_secret
from orchestrator.config.schema import Config, WebConfig
from orchestrator.doctor import Check, check_agents_live, check_remote, run_doctor
from orchestrator.pipeline.runtime import redactor_for
from orchestrator.state.store import Store
from orchestrator.trackers.base import Tracker
from orchestrator.web.api import Health, create_app
from orchestrator.web.runs import RunManager
from orchestrator.web.status import DaemonInfo

PIDFILE = "serve.pid"
LOGFILE = "serve.log"
READY_TIMEOUT = 600.0
"""How long `--detach` waits for the port; startup runs doctor's live probes, which can take minutes."""
STOP_TIMEOUT = 30.0


@dataclass
class DaemonRecord:
    pid: int
    host: str
    port: int
    url: str
    ready: bool = False


class PidLock:
    """An exclusive, non-blocking flock on the pidfile, held until release or process exit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def write(self, record: DaemonRecord) -> None:
        assert self._fd is not None
        data = json.dumps(asdict(record)).encode()
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, data, 0)

    def release(self) -> None:
        if self._fd is not None:
            os.ftruncate(self._fd, 0)
            os.close(self._fd)
            self._fd = None


def pidfile(cfg: Config) -> Path:
    return cfg.resolved_state_dir / PIDFILE


def logfile(cfg: Config) -> Path:
    return cfg.resolved_state_dir / LOGFILE


def read_record(cfg: Config) -> DaemonRecord | None:
    """The running daemon's record, or None when no process holds the lock (a leftover file is ignored)."""
    path = pidfile(cfg)
    if not path.exists():
        return None
    probe = PidLock(path)
    if probe.acquire():
        probe.release()
        return None
    try:
        return DaemonRecord(**json.loads(path.read_text()))
    except (ValueError, TypeError):
        return DaemonRecord(pid=0, host="", port=0, url="", ready=False)  # starting; not written yet


def effective_web(cfg: Config, host: str | None, port: int | None) -> WebConfig:
    """The web config with command-line overrides, validated again (an override can need auth)."""
    data = cfg.web.model_dump()
    if host is not None:
        data["host"] = host
    if port is not None:
        data["port"] = port
    return WebConfig.model_validate(data)


def url_for(web: WebConfig, port: int) -> str:
    scheme = "https" if web.tls else "http"
    host = socket.gethostname() if web.host in ("0.0.0.0", "::") else web.host
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}:{port}/"


class _Server(uvicorn.Server):
    """uvicorn re-raises a caught SIGINT/SIGTERM after shutting down; the daemon exits cleanly instead."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)


async def probe_logins(cfg: Config, health: Health) -> list[Check]:
    """Each role's live login probe, recorded in `health`. Returns every check the probes produced."""
    live = await check_agents_live(cfg)
    for role in ("worker", "reviewer"):
        area = f"agents.{role}"
        mine = [c for c in live if c.area == area]
        if mine:
            failures = [c.detail for c in mine if c.status == "fail"]
            health.record(role, cfg.agents.role(role).runner, failures)  # type: ignore[arg-type]
    return live


async def startup_checks(cfg: Config, repo_root: Path, health: Health, *, online: bool) -> list[Check]:
    """Doctor's checks, recording each probed login in `health`. Returns the failures."""
    checks = await run_doctor(cfg, repo_root, online=False)
    if online:
        checks += await probe_logins(cfg, health) + await check_remote(cfg)
    return [c for c in checks if c.status == "fail"]


async def serve(
    cfg: Config,
    config_path: Path,
    repo_root: Path,
    *,
    web: WebConfig,
    checks: bool = True,
    online: bool = True,
    say: Callable[[str], None] = print,
    on_ready: Callable[[_Server], None] | None = None,
    tracker_factory: Callable[[], Tracker] | None = None,
) -> int:
    """Run the daemon until SIGTERM or SIGINT. Returns the process exit code.

    `tracker_factory` replaces the Jira client of submitted runs (tests use a fake).
    """
    token = resolve_secret(web.auth) if web.auth else None
    lock = PidLock(pidfile(cfg))
    if not lock.acquire():
        running = read_record(cfg)
        where = f" (pid {running.pid}, {running.url})" if running and running.pid else ""
        say(f"a daemon is already serving {cfg.resolved_state_dir}{where}")
        return 1
    try:
        health = Health()
        if checks:
            failures = await startup_checks(cfg, repo_root, health, online=online)
            if failures:
                for c in failures:
                    say(f"doctor: {c.area}: {c.detail}")
                say("not starting: fix the problems above (`orchestrator doctor` shows every check)")
                return 1
        BuildSemaphore.configure(cfg.build.max_concurrent_builds)
        store = Store(cfg.db_path)
        info = DaemonInfo(
            pid=os.getpid(),
            host=socket.gethostname(),
            port=web.port,
            version=__version__,
            config=str(config_path),
            repo=cfg.repo.github,
        )

        async def login_probe() -> list[str]:
            return [f"{c.area}: {c.detail}" for c in await probe_logins(cfg, health) if c.status == "fail"]

        runs = RunManager(
            cfg,
            config_path,
            store,
            login_probe=login_probe if online else None,
            tracker_factory=tracker_factory,
        )
        if checks and online:
            runs.last_login_ok = time.monotonic()  # the startup probe just passed
        stopped_by: list[str] = []

        def request_shutdown(client: str) -> None:
            say(f"shutdown requested from {client}")
            stopped_by.append(client)
            server.should_exit = True

        app = create_app(cfg, web, store, redactor_for(cfg), info, health, token, runs, request_shutdown)
        server = _Server(
            uvicorn.Config(
                app,
                host=web.host,
                port=web.port,
                ssl_certfile=str(web.tls.certfile) if web.tls else None,
                ssl_keyfile=str(web.tls.keyfile) if web.tls else None,
                lifespan="off",
                access_log=False,
                log_level="warning",
            )
        )
        url = url_for(web, web.port)
        lock.write(DaemonRecord(os.getpid(), info.host, web.port, url, ready=False))
        runs.start()
        if cfg.web.resume_on_start:
            for run_id in await runs.resume_interrupted():
                say(f"resuming run {run_id}")
        task = asyncio.create_task(server.serve())
        while not server.started and not task.done():
            await asyncio.sleep(0.05)
        if server.started:
            lock.write(DaemonRecord(os.getpid(), info.host, web.port, url, ready=True))
            say(f"serving {url} (pid {os.getpid()})")
            if not web.loopback and web.tls is None:
                say(
                    "warning: plain HTTP on a non-loopback address; the operator token crosses the network unencrypted"
                )
            if on_ready:
                on_ready(server)
        try:
            await task
        finally:
            await runs.shutdown(client=stopped_by[0] if stopped_by else None)
        store.close()
        return 0 if server.started else 1
    finally:
        lock.release()


def detach(cfg: Config, argv: list[str], say: Callable[[str], None] = print) -> int:
    """Start `serve` again in its own session with output to serve.log; return once it is listening."""
    log_path = logfile(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with log_path.open("a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "orchestrator", *argv],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log_path.read_text().splitlines()[-15:]
            say("the daemon exited during startup:\n" + "\n".join(tail))
            return 1
        record = read_record(cfg)
        if record and record.pid == proc.pid and record.ready:
            say(f"serving {record.url} (pid {proc.pid}); log: {log_path}")
            return 0
        time.sleep(0.5)
    say(f"the daemon (pid {proc.pid}) is still starting; see {log_path}")
    return 1


def stop(cfg: Config, say: Callable[[str], None] = print) -> int:
    record = read_record(cfg)
    if record is None or not record.pid:
        say(f"no daemon is serving {cfg.resolved_state_dir}")
        return 1
    os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if read_record(cfg) is None:
            say(f"stopped pid {record.pid}")
            return 0
        time.sleep(0.2)
    say(f"pid {record.pid} did not stop within {STOP_TIMEOUT:.0f}s")
    return 1
