from __future__ import annotations

import json
import os
import shlex
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

MAX_NAME_LEN = 64
MAX_COMMAND_PARTS = 32
MAX_PART_LEN = 512
MAX_ENTRIES = 500
WRITE_RATE_LIMIT = 30
RATE_WINDOW = 60.0


class PaletteError(Exception):
    pass


@dataclass
class PaletteEntry:
    id: str
    name: str
    command: list[str]
    group: str = "general"
    builtin: bool = False
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "command": list(self.command),
            "group": self.group,
            "builtin": self.builtin,
            "description": self.description,
        }


BUILTIN_ENTRIES: list[PaletteEntry] = [
    PaletteEntry(id="builtin:uptime", name="uptime", command=["uptime"], group="system", builtin=True),
    PaletteEntry(id="builtin:disk", name="disk", command=["df", "-h"], group="system", builtin=True),
    PaletteEntry(
        id="builtin:docker", name="docker ps", command=["docker", "ps"], group="docker", builtin=True
    ),
    PaletteEntry(
        id="builtin:git-status",
        name="git status",
        command=["git", "status", "--short", "--branch"],
        group="git",
        builtin=True,
    ),
]


@dataclass
class _RateWindow:
    stamps: list[float] = field(default_factory=list)

    def allow(self, now: float) -> bool:
        self.stamps = [t for t in self.stamps if now - t < RATE_WINDOW]
        if len(self.stamps) >= WRITE_RATE_LIMIT:
            return False
        self.stamps.append(now)
        return True


def validate_command(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            raise PaletteError(f"command does not parse: {exc}") from exc
    elif isinstance(raw, list):
        parts = [str(p) for p in raw]
    else:
        raise PaletteError("command must be a string or a list of strings")
    if not parts:
        raise PaletteError("command must not be empty")
    if len(parts) > MAX_COMMAND_PARTS:
        raise PaletteError("command has too many parts")
    for part in parts:
        if not part.strip():
            raise PaletteError("command parts must not be blank")
        if len(part) > MAX_PART_LEN:
            raise PaletteError("command part too long")
        if "\x00" in part:
            raise PaletteError("command parts must not contain NUL")
    return parts


def validate_name(raw: Any) -> str:
    name = str(raw).strip()
    if not name:
        raise PaletteError("name must not be empty")
    if len(name) > MAX_NAME_LEN:
        raise PaletteError("name too long")
    return name


class PaletteStore:
    def __init__(self, path: str, include_builtin: bool = True) -> None:
        self.path = path
        self.include_builtin = include_builtin
        self._rate = _RateWindow()

    def load(self) -> list[PaletteEntry]:
        entries: list[PaletteEntry] = []
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise PaletteError(f"palette file unreadable: {exc}") from exc
            if not isinstance(raw, list):
                raise PaletteError("palette file must contain a list")
            for item in raw:
                try:
                    entries.append(
                        PaletteEntry(
                            id=str(item.get("id") or uuid.uuid4().hex),
                            name=validate_name(item.get("name")),
                            command=validate_command(item.get("command")),
                            group=str(item.get("group") or "general"),
                            description=item.get("description"),
                        )
                    )
                except PaletteError:
                    continue
        if self.include_builtin:
            shadowed = {e.name for e in entries}
            entries.extend(e for e in BUILTIN_ENTRIES if e.name not in shadowed)
        return entries

    def _save(self, entries: list[PaletteEntry]) -> None:
        if len(entries) > MAX_ENTRIES:
            raise PaletteError("palette entry limit reached")
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".palette-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump([e.to_dict() for e in entries], handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _rate_limit(self) -> None:
        if not self._rate.allow(time.monotonic()):
            raise PaletteError("write rate limit exceeded")

    def create(
        self, name: str, command: Any, group: str = "general", description: str | None = None
    ) -> PaletteEntry:
        self._rate_limit()
        entry = PaletteEntry(
            id=uuid.uuid4().hex,
            name=validate_name(name),
            command=validate_command(command),
            group=str(group or "general")[:MAX_NAME_LEN],
            description=description,
        )
        entries = self.load()
        if any(e.name == entry.name and e.group == entry.group for e in entries):
            raise PaletteError("an entry with that name and group already exists")
        self._save([e for e in entries if not e.builtin] + [entry])
        return entry

    def update(self, entry_id: str, **changes: Any) -> PaletteEntry:
        self._rate_limit()
        entries = self.load()
        target = next((e for e in entries if e.id == entry_id), None)
        if target is None:
            raise PaletteError("entry not found")
        if target.builtin:
            raise PaletteError("built-in entries cannot be edited")
        updated = PaletteEntry(
            id=target.id,
            name=validate_name(changes.get("name", target.name)),
            command=validate_command(changes.get("command", target.command)),
            group=str(changes.get("group", target.group) or "general")[:MAX_NAME_LEN],
            description=changes.get("description", target.description),
        )
        self._save([updated if e.id == entry_id else e for e in entries if not e.builtin])
        return updated

    def delete(self, entry_id: str) -> PaletteEntry:
        self._rate_limit()
        entries = self.load()
        target = next((e for e in entries if e.id == entry_id), None)
        if target is None:
            raise PaletteError("entry not found")
        if target.builtin:
            raise PaletteError("built-in entries cannot be deleted")
        self._save([e for e in entries if not e.builtin and e.id != entry_id])
        return target
