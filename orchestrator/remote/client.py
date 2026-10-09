"""The orchestrator's side of a build machine: run the node script's operations there over ssh.

Every operation is one `ssh` invocation with fixed options: batch mode, strict host-key checking,
and keepalives. A `run` holds the connection's stdin open for the job's whole life; when the
orchestrator kills ssh (a timeout or a cancelled task), the node script sees EOF and kills the
job's process group, and a `cancel` follows as a second line of defence.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orchestrator.config.schema import NodeConfig, PlatformConfig
from orchestrator.remote.node import VERSION as NODE_VERSION

NODE_SCRIPT = Path(__file__).with_name("node.py")
SSH_UNREACHABLE = 255
"""ssh's own exit code for a connection, authentication, or host-key failure."""
OP_TIMEOUT = 300.0
KILL_GRACE = 10.0


class NodeError(Exception):
    """A build machine could not do what was asked. `transient` when the connection itself failed."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class Machine:
    platform: str
    cfg: PlatformConfig
    node: NodeConfig

    @property
    def host(self) -> str:
        return self.node.host

    @property
    def root(self) -> str:
        return self.cfg.root_for(self.node)

    @property
    def checkout(self) -> str | None:
        return self.cfg.checkout_for(self.node)

    @property
    def script(self) -> str:
        return f"{self.root}/node.py"

    def task_src(self, run_id: str, key: str) -> str:
        return f"{self.root}/{run_id}/{key}/src"


def machines(platform: str, cfg: PlatformConfig) -> list[Machine]:
    return [Machine(platform, cfg, n) for n in cfg.nodes]


class Transport(Protocol):
    def argv(self, machine: Machine, remote: list[str]) -> list[str]:
        """The local argv that runs `remote` (an argv) on the machine."""
        ...

    def install_argv(self, machine: Machine) -> list[str]:
        """The local argv that writes stdin to the machine's node script."""
        ...


class SshTransport:
    def _ssh(self, machine: Machine) -> list[str]:
        ssh = machine.cfg.ssh
        argv = [
            "ssh",
            "-T",
            "-p",
            str(ssh.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={ssh.connect_timeout_seconds}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=4",
        ]
        if ssh.identity_file:
            argv += ["-i", str(ssh.identity_file), "-o", "IdentitiesOnly=yes"]
        if ssh.known_hosts:
            argv += ["-o", f"UserKnownHostsFile={ssh.known_hosts}"]
        return [*argv, f"{ssh.user}@{machine.host}", "--"]

    def argv(self, machine: Machine, remote: list[str]) -> list[str]:
        # ssh hands the remote side one string for its shell, so each word is quoted
        return [*self._ssh(machine), shlex.join(remote)]

    def install_argv(self, machine: Machine) -> list[str]:
        root, tmp = shlex.quote(machine.root), shlex.quote(machine.script + ".tmp")
        return [
            *self._ssh(machine),
            f"mkdir -p {root} && cat > {tmp} && mv {tmp} {shlex.quote(machine.script)}",
        ]


class DirectTransport:
    """Runs the node script as a local subprocess, against a local root: for tests, no ssh involved."""

    def argv(self, machine: Machine, remote: list[str]) -> list[str]:
        assert remote[0] == machine.cfg.python and remote[1] == machine.script
        return [sys.executable, str(NODE_SCRIPT), *remote[2:]]

    def install_argv(self, machine: Machine) -> list[str]:
        script = machine.script
        return [
            sys.executable,
            "-c",
            f"import os,sys; os.makedirs({machine.root!r}, exist_ok=True); "
            f"open({script!r}, 'wb').write(sys.stdin.buffer.read())",
        ]


@dataclass
class RunOutcome:
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        pass


async def _stop(proc: asyncio.subprocess.Process) -> None:
    _kill_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE)
    except TimeoutError:
        _kill_group(proc.pid, signal.SIGKILL)
        await proc.wait()


class NodeClient:
    def __init__(self, machine: Machine, transport: Transport | None = None) -> None:
        self.machine = machine
        self.transport = transport or SshTransport()

    def _remote(self, op: str, *args: str) -> list[str]:
        m = self.machine
        argv = [m.cfg.python, m.script, op, "--root", m.root]
        if m.checkout:
            argv += ["--checkout", m.checkout]
        return [*argv, *args]

    async def _call(
        self, argv: list[str], *, stdin: bytes | Path | None = None, timeout: float = OP_TIMEOUT
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        data = stdin.read_bytes() if isinstance(stdin, Path) else stdin
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), timeout)
        except TimeoutError as e:
            await _stop(proc)
            raise NodeError(f"{self.machine.host}: no answer within {timeout:.0f}s", transient=True) from e
        except asyncio.CancelledError:
            await _stop(proc)
            raise
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def _op(
        self, op: str, *args: str, stdin: bytes | Path | None = None, timeout: float = OP_TIMEOUT
    ) -> Any:
        code, out, err = await self._call(
            self.transport.argv(self.machine, self._remote(op, *args)), stdin=stdin, timeout=timeout
        )
        if code == SSH_UNREACHABLE:
            raise NodeError(f"{self.machine.host}: {err.strip() or 'ssh failed'}", transient=True)
        if code != 0:
            detail = err.strip() or out.strip() or f"exit {code}"
            if "No such file or directory" in detail and self.machine.script in detail:
                detail += " (install it with `orchestrator nodes install`)"
            raise NodeError(f"{self.machine.host}: {op} failed: {detail}")
        return json.loads(out) if out.strip() else None

    async def install(self) -> None:
        code, _, err = await self._call(
            self.transport.install_argv(self.machine), stdin=NODE_SCRIPT.read_bytes()
        )
        if code != 0:
            raise NodeError(
                f"{self.machine.host}: installing the node script failed: {err.strip()}",
                transient=code == SSH_UNREACHABLE,
            )

    async def info(self, timeout: float = 60) -> dict[str, Any]:
        info: dict[str, Any] = await self._op("info", timeout=timeout)
        return info

    async def ensure_installed(self) -> dict[str, Any]:
        """The machine's info, installing or upgrading the node script first when it is missing or older."""
        try:
            info = await self.info()
        except NodeError as e:
            if e.transient:
                raise
            info = {"version": 0}
        if info.get("version", 0) < NODE_VERSION:
            await self.install()
            info = await self.info()
        return info

    async def prepare(
        self, run_id: str, key: str, base_sha: str, fetch_url: str, fetch_ref: str
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._op(
            "prepare", run_id, key, base_sha, "--fetch-url", fetch_url, "--fetch-ref", fetch_ref, timeout=1800
        )
        return result

    async def sync(self, run_id: str, key: str, payload: Path) -> dict[str, Any]:
        result: dict[str, Any] = await self._op("sync", run_id, key, stdin=payload, timeout=1800)
        return result

    async def cancel(self, run_id: str, key: str, job: str) -> None:
        await self._op("cancel", run_id, key, job, timeout=60)

    async def clean(self, run_id: str, key: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = await self._op("clean", run_id, *([key] if key else []))
        return result

    async def clean_older_than(self, days: float) -> dict[str, Any]:
        result: dict[str, Any] = await self._op("clean", "--older-than-days", str(days), timeout=1800)
        return result

    async def run(
        self, run_id: str, key: str, job: str, argv: list[str], env: dict[str, str], timeout: float
    ) -> RunOutcome:
        """Run argv in the task's src/ on the machine. A timeout or cancellation kills it there too."""
        remote = self._remote(
            "run", run_id, key, job, *(f"--env={k}={v}" for k, v in env.items()), "--", *argv
        )
        proc = await asyncio.create_subprocess_exec(
            *self.transport.argv(self.machine, remote),
            stdin=asyncio.subprocess.PIPE,  # held open: EOF tells the node to kill the job
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout and proc.stderr
        reads = asyncio.gather(proc.stdout.read(), proc.stderr.read())
        timed_out = False
        try:
            out, err = await asyncio.wait_for(asyncio.shield(reads), timeout)
            await proc.wait()
        except TimeoutError:
            timed_out = True
            await self._abandon(proc, run_id, key, job)
            out, err = await reads
        except asyncio.CancelledError:
            await self._abandon(proc, run_id, key, job)
            reads.cancel()
            raise
        code = None if timed_out else proc.returncode
        if code == SSH_UNREACHABLE and err.startswith(b"ssh:"):
            raise NodeError(f"{self.machine.host}: {err.decode(errors='replace').strip()}", transient=True)
        return RunOutcome(code, timed_out, out.decode(errors="replace"), err.decode(errors="replace"))

    async def _abandon(self, proc: asyncio.subprocess.Process, run_id: str, key: str, job: str) -> None:
        await _stop(proc)
        try:
            await asyncio.shield(self.cancel(run_id, key, job))
        except (NodeError, asyncio.CancelledError):
            pass
