from __future__ import annotations

import base64
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1

MsgType = Literal["hello", "stdin", "stdout", "resize", "exit", "error", "ping", "pong"]


def encode_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_bytes(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"), validate=True)


class ClientMessage(BaseModel):
    type: Literal["stdin", "resize", "kill", "ping"] = "stdin"
    data: str | None = None
    cols: int | None = Field(default=None, ge=1, le=1000)
    rows: int | None = Field(default=None, ge=1, le=1000)


class ServerMessage(BaseModel):
    type: MsgType
    seq: int | None = None
    data: str | None = None
    cols: int | None = None
    rows: int | None = None
    shell: str | None = None
    session_id: str | None = None
    code: int | None = None
    message: str | None = None


def hello(
    session_id: str, shell: str, cols: int, rows: int, resumed_from: int | None = None
) -> ServerMessage:
    return ServerMessage(
        type="hello",
        session_id=session_id,
        shell=shell,
        cols=cols,
        rows=rows,
        seq=resumed_from,
    )


def stdout_frame(seq: int, payload: bytes) -> ServerMessage:
    return ServerMessage(type="stdout", seq=seq, data=encode_bytes(payload))


def exit_frame(code: int) -> ServerMessage:
    return ServerMessage(type="exit", code=code)


def error_frame(message: str) -> ServerMessage:
    return ServerMessage(type="error", message=message)


def parse_client_message(raw: str) -> ClientMessage:
    import json

    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed json: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("expected a json object")
    try:
        return ClientMessage(**parsed)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
