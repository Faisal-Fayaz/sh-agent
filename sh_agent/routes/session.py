from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from ..auth import Authenticator, AuthError, Identity
from ..protocol import (
    ServerMessage,
    decode_bytes,
    encode_bytes,
    error_frame,
    exit_frame,
    hello,
    parse_client_message,
)
from ..registry import AuditLog, SessionRegistry
from .deps import (
    get_audit,
    get_registry,
    require_auth,
    websocket_authorization,
)

router = APIRouter(tags=["sessions"])


class CreateSessionRequest(BaseModel):
    cols: int = Field(default=80, ge=1, le=1000)
    rows: int = Field(default=24, ge=1, le=1000)
    cwd: str | None = None


class SessionInfo(BaseModel):
    id: str
    shell: str
    cols: int
    rows: int
    closed: bool
    exit_code: int | None
    pid: int | None
    buffer_end: int
    buffer_base: int
    buffer_size: int
    buffer_capacity: int


@router.post("/api/session", status_code=201)
async def create_session(
    request: Request,
    body: CreateSessionRequest | None = None,
    identity: Identity = Depends(require_auth),
) -> SessionInfo:
    payload = body or CreateSessionRequest()
    registry = get_registry(request)
    try:
        session = await registry.create(cols=payload.cols, rows=payload.rows, cwd=payload.cwd)
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    get_audit(request).record(
        "session.created",
        session_id=session.id,
        login=identity.login,
        peer_ip=identity.peer_ip,
        cwd=session.cwd,
    )
    return _info(session)


@router.get("/api/session/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str, request: Request, _: Identity = Depends(require_auth)) -> SessionInfo:
    registry = get_registry(request)
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _info(session)


@router.post("/api/session/{session_id}/kill")
async def kill_session(
    session_id: str, request: Request, identity: Identity = Depends(require_auth)
) -> dict[str, object]:
    registry = get_registry(request)
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    get_audit(request).record("session.kill", session_id=session_id, login=identity.login)
    registry.close(session_id)
    return {"status": "closed", "session_id": session_id}


@router.websocket("/ws/{session_id}")
async def terminal_socket(
    websocket: WebSocket,
    session_id: str,
    token: str | None = Query(default=None),
    resume: int | None = Query(default=None, ge=0),
) -> None:
    app = websocket.app
    registry: SessionRegistry = app.state.registry
    authenticator: Authenticator = app.state.authenticator
    audit: AuditLog = app.state.audit

    header = websocket.headers.get("authorization")
    authorization = websocket_authorization(websocket, header, token)
    peer = websocket.client.host if websocket.client else None

    try:
        identity = authenticator.enforce(authorization, peer)
    except AuthError as exc:
        audit.record("auth.ws_denied", peer_ip=peer, reason=str(exc))
        await websocket.close(code=4401, reason=str(exc)[:120])
        return

    session = registry.get(session_id)
    if session is None:
        audit.record("ws.no_session", session_id=session_id, peer_ip=peer)
        await websocket.close(code=4404, reason="session not found")
        return

    await websocket.accept()

    # Capture the loop now: a session can be closed later from any thread (the reaper,
    # a kill request), and get_running_loop() only works on the loop thread.
    loop = asyncio.get_running_loop()

    def close_socket() -> None:
        async def _close() -> None:
            try:
                await websocket.close(code=4404, reason="session closed")
            except RuntimeError:
                pass

        loop.create_task(_close())

    session.on_close(close_socket)

    audit.record(
        "ws.open",
        session_id=session_id,
        login=identity.login,
        peer_ip=identity.peer_ip,
        resume=resume,
    )

    send_lock = asyncio.Lock()

    async def sender(message: ServerMessage) -> None:
        async with send_lock:
            await websocket.send_json(message.model_dump(exclude_none=True))

    session.attach(sender)
    try:
        await sender(
            hello(
                session_id=session.id,
                shell=session.shell,
                cols=session.cols,
                rows=session.rows,
                resumed_from=resume,
            )
        )
        if resume is not None:
            end, payload = session.replay_from(resume)
            if payload:
                await sender(ServerMessage(type="stdout", seq=end, data=encode_bytes(payload)))
            audit.record("ws.replayed", session_id=session_id, bytes=len(payload), to=end)

        while True:
            raw = await websocket.receive_text()
            try:
                message = parse_client_message(raw)
            except ValueError as exc:
                await sender(error_frame(str(exc)))
                continue

            if message.type == "stdin":
                if message.data:
                    try:
                        session.write(decode_bytes(message.data))
                    except Exception as exc:
                        await sender(error_frame(f"stdin decode failed: {exc}"))
            elif message.type == "resize":
                if message.cols and message.rows:
                    session.resize(message.cols, message.rows)
                    audit.record("ws.resize", session_id=session_id, cols=message.cols, rows=message.rows)
            elif message.type == "kill":
                session.kill()
                await sender(exit_frame(session.exit_code or 0))
                break
            elif message.type == "ping":
                await sender(ServerMessage(type="pong"))
            session.touch()

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        audit.record("ws.error", session_id=session_id, error=str(exc))
    finally:
        session.detach()
        audit.record("ws.close", session_id=session_id)
        try:
            await websocket.close()
        except RuntimeError:
            pass


def _info(session) -> SessionInfo:
    return SessionInfo(
        id=session.id,
        shell=session.shell,
        cols=session.cols,
        rows=session.rows,
        closed=session.closed,
        exit_code=session.exit_code,
        pid=session.pid,
        buffer_end=session.buffer.end_offset,
        buffer_base=session.buffer.base_offset,
        buffer_size=len(session.buffer),
        buffer_capacity=session.buffer.capacity,
    )
