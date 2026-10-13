from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import Authenticator, AuthError
from .config import Config
from .palette import PaletteStore
from .registry import AuditLog, SessionRegistry
from .routes import commands, health, session


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: Config = app.state.config
    audit: AuditLog = app.state.audit
    loop = asyncio.get_running_loop()

    registry = SessionRegistry(
        loop=loop,
        shell=config.shell,
        cwd=config.cwd,
        idle_ttl=config.idle_ttl,
        max_sessions=config.max_sessions,
        detach_grace=config.detach_grace,
        reap_interval=config.reap_interval,
        ring_capacity=config.ring_capacity,
    )
    app.state.registry = registry
    registry.start_reaper()

    audit.record(
        "agent.start",
        bind=f"{config.public_bind}:{config.port}",
        allow_lan=config.allow_lan,
        write_enabled=bool(config.auth.write_token),
        tailscale_required=config.auth.tailscale_require,
    )
    if audit.error:
        print(
            f"WARNING: audit log disabled -- {audit.error}\n"
            f"         set SH_AUDIT to a writable path to record auth and session events",
            file=sys.stderr,
        )
    try:
        yield
    finally:
        audit.record("agent.stop")
        await registry.shutdown()


def create_app(config: Config | None = None) -> FastAPI:
    resolved = config or Config()
    app = FastAPI(
        title="sh agent",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.config = resolved
    app.state.audit = AuditLog(resolved.audit_path)
    app.state.palette = PaletteStore(resolved.palette_path)
    app.state.authenticator = Authenticator(resolved.auth)

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        resolved_audit: AuditLog = request.app.state.audit
        resolved_audit.record(
            "auth.rejected",
            peer_ip=request.client.host if request.client else None,
            reason=str(exc),
            status=exc.status_code,
        )
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
            headers=headers,
        )

    app.include_router(health.router)
    app.include_router(session.router)
    app.include_router(commands.router)
    return app


app = create_app()
