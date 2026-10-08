"""The daemon's HTTP surface: the static page and its JSON endpoints.

One operator controls the daemon with one token, sent as HTTP Basic auth (user `operator`). With
`web.public_read`, GET requests are served without it. Every response passes through the Redactor,
because stored task state is not redacted.

Control requests (anything but GET and HEAD) must be JSON and same-origin. A page on another site
can make a browser send a form POST to a loopback daemon, but not a cross-origin JSON one without a
CORS preflight, which the daemon never answers; that is what protects a loopback daemon with no
token.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.config.schema import Config, WebConfig
from orchestrator.reporting.audit import Redactor
from orchestrator.state.store import Store, this_process
from orchestrator.web.runs import ControlError, RunManager
from orchestrator.web.status import AgentsResponse, DaemonInfo, RunsResponse, agent_rows, run_views

STATIC_DIR = Path(__file__).parent / "static"
OPERATOR = "operator"
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
READ_METHODS = frozenset({"GET", "HEAD"})
SHUTDOWN_DELAY = 0.2


@dataclass
class LoginState:
    runner: str
    status: Literal["ok", "failed", "unchecked"] = "unchecked"
    message: str = ""
    checked: str | None = None


@dataclass
class Health:
    """What the daemon knows about itself; the daemon updates it, the API reports it."""

    logins: dict[str, LoginState] = field(default_factory=dict)

    def record(self, role: str, runner: str, problems: list[str]) -> None:
        self.logins[role] = LoginState(
            runner=runner,
            status="failed" if problems else "ok",
            message="; ".join(problems),
            checked=datetime.now(UTC).isoformat(timespec="seconds"),
        )


class RunRequest(BaseModel):
    """`POST /api/runs`: issues to work on, as one new run."""

    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(min_length=1, description="Issue or epic keys; an epic expands to its children.")
    workflow: Literal["auto", "bugfix", "feature"] = "auto"
    dry_run: bool = Field(description="Required, so a real run is never started by omission.")


class ResumeRequest(BaseModel):
    """`POST /api/runs/{run_id}/resume`: continue a run from its checkpoints."""

    model_config = ConfigDict(extra="forbid")

    retry_failed: bool = Field(False, description="Also re-run FAILED tasks from the stage they failed in.")
    retry_blocked: bool = Field(False, description="Also re-run BLOCKED tasks from the stage they were in.")


def host_name(header: str) -> str:
    """The host part of a Host header: `[::1]:8765` -> `::1`, `localhost:8765` -> `localhost`."""
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def is_operator(authorization: str | None, token: str) -> bool:
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(value, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user_ok = hmac.compare_digest(user.encode(), OPERATOR.encode())
    password_ok = hmac.compare_digest(password.encode(), token.encode())
    return user_ok and password_ok


def create_app(
    cfg: Config,
    web: WebConfig,
    store: Store,
    redactor: Redactor,
    daemon: DaemonInfo,
    health: Health,
    token: str | None,
    runs: RunManager,
    request_shutdown: Callable[[str], None],
) -> FastAPI:
    """Build the app. `web` is the effective web config (command-line overrides applied).

    `request_shutdown(client)` stops the daemon as SIGTERM does.
    """
    app = FastAPI(title="orchestrator", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # a loopback daemon answers only to loopback names, which defeats DNS rebinding
        if web.loopback and host_name(request.headers.get("host", "")) not in LOOPBACK_NAMES:
            return JSONResponse({"detail": "unexpected Host header"}, status_code=400)
        if token is not None and not (web.public_read and request.method in READ_METHODS):
            if not is_operator(request.headers.get("authorization"), token):
                return JSONResponse(
                    {"detail": "operator token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="orchestrator", charset="UTF-8"'},
                )
        if request.method not in READ_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return JSONResponse({"detail": "control requests must be application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def redacted(payload: dict[str, Any]) -> JSONResponse:
        return JSONResponse(redactor.redact_obj(payload))

    def refused(e: ControlError) -> JSONResponse:
        return JSONResponse(redactor.redact_obj({"detail": str(e)}), status_code=e.status)

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def get_health() -> JSONResponse:
        return redacted(
            {
                "status": "ok",
                "daemon": daemon.model_dump(),
                "logins": {role: vars(state) for role, state in health.logins.items()},
                "tls": web.tls is not None,
                "loopback": web.loopback,
                "public_read": web.public_read,
            }
        )

    @app.get("/api/agents")
    def get_agents() -> JSONResponse:
        body = AgentsResponse(
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            daemon=daemon,
            agents=agent_rows(store, cfg),
        )
        return redacted(body.model_dump())

    @app.get("/api/runs")
    def get_runs() -> JSONResponse:
        body = RunsResponse(
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            runs=run_views(store, this_process(), set(runs.run_ids)),
        )
        return redacted(body.model_dump())

    @app.post("/api/runs")
    async def post_runs(body: RunRequest, request: Request) -> JSONResponse:
        try:
            submitted = await runs.submit(
                body.keys,
                workflow=None if body.workflow == "auto" else body.workflow,
                dry_run=body.dry_run,
                client=client_of(request),
            )
        except ControlError as e:
            return refused(e)
        return JSONResponse(redactor.redact_obj({**vars(submitted), "status": "queued"}), status_code=202)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request) -> JSONResponse:
        try:
            await runs.cancel(run_id, client=client_of(request))
        except ControlError as e:
            return refused(e)
        return JSONResponse({"run_id": run_id, "status": "cancelled"})

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str, body: ResumeRequest, request: Request) -> JSONResponse:
        try:
            resumed = await runs.resume(
                run_id,
                retry_failed=body.retry_failed,
                retry_blocked=body.retry_blocked,
                client=client_of(request),
            )
        except ControlError as e:
            return refused(e)
        return JSONResponse(redactor.redact_obj({**vars(resumed), "status": "queued"}), status_code=202)

    @app.post("/api/shutdown")
    async def shutdown(request: Request) -> JSONResponse:
        interrupted = runs.run_ids
        # the reply goes out before the server stops listening
        asyncio.get_running_loop().call_later(SHUTDOWN_DELAY, request_shutdown, client_of(request))
        return JSONResponse({"status": "stopping", "interrupted": interrupted}, status_code=202)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
