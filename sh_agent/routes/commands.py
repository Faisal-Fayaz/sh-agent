from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..auth import Identity
from ..palette import PaletteError
from .deps import get_audit, get_palette, require_auth, require_write_auth

router = APIRouter(prefix="/api/commands", tags=["palette"])


class CommandCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    command: list[str] | str
    group: str = "general"
    description: str | None = None


class CommandPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    command: list[str] | str | None = None
    group: str | None = None
    description: str | None = None


def _fail(exc: PaletteError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
async def list_commands(request: Request, _: Identity = Depends(require_auth)) -> dict[str, Any]:
    palette = get_palette(request)
    try:
        entries = palette.load()
    except PaletteError as exc:
        raise _fail(exc) from exc
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(entry.group, []).append(entry.to_dict())
    return {"count": len(entries), "entries": [e.to_dict() for e in entries], "groups": groups}


@router.post("", status_code=201)
async def create_command(
    body: CommandCreate,
    request: Request,
    identity: Identity = Depends(require_write_auth),
) -> dict[str, Any]:
    palette = get_palette(request)
    try:
        entry = palette.create(body.name, body.command, body.group, body.description)
    except PaletteError as exc:
        get_audit(request).record("palette.create_denied", login=identity.login, reason=str(exc))
        raise _fail(exc) from exc
    get_audit(request).record("palette.created", entry_id=entry.id, name=entry.name, login=identity.login)
    return entry.to_dict()


@router.patch("/{entry_id}")
async def patch_command(
    entry_id: str,
    body: CommandPatch,
    request: Request,
    identity: Identity = Depends(require_write_auth),
) -> dict[str, Any]:
    palette = get_palette(request)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        entry = palette.update(entry_id, **changes)
    except PaletteError as exc:
        get_audit(request).record(
            "palette.update_denied", entry_id=entry_id, login=identity.login, reason=str(exc)
        )
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    get_audit(request).record("palette.updated", entry_id=entry.id, name=entry.name, login=identity.login)
    return entry.to_dict()


@router.delete("/{entry_id}")
async def delete_command(
    entry_id: str,
    request: Request,
    identity: Identity = Depends(require_write_auth),
) -> Response:
    palette = get_palette(request)
    try:
        palette.delete(entry_id)
    except PaletteError as exc:
        get_audit(request).record(
            "palette.delete_denied", entry_id=entry_id, login=identity.login, reason=str(exc)
        )
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    get_audit(request).record("palette.deleted", entry_id=entry_id, login=identity.login)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
