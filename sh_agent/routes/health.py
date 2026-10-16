from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Depends, Request

from ..auth import Authenticator, Identity
from ..config import Config, default_shell
from ..protocol import PROTOCOL_VERSION
from ..registry import SessionRegistry
from ..version import __version__
from .deps import get_audit, get_authenticator, get_config, get_registry, require_auth

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    config: Config = get_config(request)
    registry: SessionRegistry = get_registry(request)
    authenticator: Authenticator = get_authenticator(request)
    return {
        "status": "ok",
        "protocol": PROTOCOL_VERSION,
        "version": __version__,
        "sessions": registry.count(),
        "max_sessions": registry.max_sessions,
        "shell": config.shell or default_shell(),
        "write_enabled": bool(config.auth.write_token),
        "tailscale_available": authenticator.whois.available(),
        "tailscale_required": config.auth.tailscale_require,
        "bind": f"{config.public_bind}:{config.port}",
    }


@router.get("/whoami")
async def whoami(identity: Identity = Depends(require_auth)) -> dict[str, object]:
    return identity.as_dict()


@router.get("/audit/recent")
async def recent_audit(request: Request, limit: int = 50) -> dict[str, object]:
    get_audit(request).record("audit.read", limit=limit)
    config = get_config(request)
    path = config.audit_path
    if not path:
        return {"entries": []}

    def _read() -> list[dict[str, object]]:
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        entries: list[dict[str, object]] = []
        for line in lines[-max(1, min(limit, 500)) :]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    return {"entries": await asyncio.to_thread(_read)}
