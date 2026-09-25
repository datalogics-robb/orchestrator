"""The daemon's HTTP surface: the static page and its JSON endpoints.

One operator controls the daemon with one token, sent as HTTP Basic auth (user `operator`). With
`web.public_read`, GET requests are served without it. Every response passes through the Redactor,
because stored task state is not redacted.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from orchestrator.config.schema import Config, WebConfig
from orchestrator.reporting.audit import Redactor
from orchestrator.state.store import Store
from orchestrator.web.status import AgentsResponse, DaemonInfo, agent_rows

STATIC_DIR = Path(__file__).parent / "static"
OPERATOR = "operator"
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
READ_METHODS = frozenset({"GET", "HEAD"})


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
) -> FastAPI:
    """Build the app. `web` is the effective web config (command-line overrides applied)."""
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
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def redacted(payload: dict[str, Any]) -> JSONResponse:
        return JSONResponse(redactor.redact_obj(payload))

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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
