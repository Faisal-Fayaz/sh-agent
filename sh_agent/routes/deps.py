from __future__ import annotations

from fastapi import Header, Request, WebSocket

from ..auth import Authenticator, AuthError, Identity
from ..config import Config
from ..palette import PaletteStore
from ..registry import AuditLog, SessionRegistry


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_registry(request: Request) -> SessionRegistry:
    return request.app.state.registry


def get_palette(request: Request) -> PaletteStore:
    return request.app.state.palette


def get_audit(request: Request) -> AuditLog:
    return request.app.state.audit


def get_authenticator(request: Request) -> Authenticator:
    return request.app.state.authenticator


def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Identity:
    authenticator = get_authenticator(request)
    peer = request.client.host if request.client else None
    identity = authenticator.enforce(authorization, peer)
    get_audit(request).record("auth.ok", login=identity.login, peer_ip=identity.peer_ip, node=identity.node)
    return identity


def require_write_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Identity:
    authenticator = get_authenticator(request)
    peer = request.client.host if request.client else None
    try:
        identity = authenticator.enforce_write(authorization, peer)
    except AuthError as exc:
        get_audit(request).record("auth.write_denied", peer_ip=peer, reason=str(exc))
        raise
    get_audit(request).record("auth.write_ok", login=identity.login, peer_ip=identity.peer_ip)
    return identity


def websocket_authorization(websocket: WebSocket, header: str | None, query_token: str | None) -> str | None:
    if header:
        return header
    if query_token:
        return f"Bearer {query_token}"
    return None
