from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import shutil
from dataclasses import dataclass

TMUX_BIN = "tmux"
NAME_PREFIX = "sh"
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TmuxUnavailable(RuntimeError):
    pass


class SlugError(ValueError):
    pass


def available() -> bool:
    return shutil.which(TMUX_BIN) is not None


def require() -> str:
    path = shutil.which(TMUX_BIN)
    if path is None:
        raise TmuxUnavailable(f"{TMUX_BIN} is not installed on PATH")
    return path


def validate_slug(slug: str) -> str:
    cleaned = (slug or "").strip()
    if not SLUG_RE.match(cleaned):
        raise SlugError(
            "slug must be 1-64 characters of letters, digits, dot, underscore or dash, "
            "starting with a letter or digit"
        )
    return cleaned


class SlugNamer:
    """Maps short user slugs onto tmux session names that other clients cannot guess.

    tmux attaches to any existing session whose name matches, so an un-namespaced
    slug would let anyone holding a token attach to another holder's session.
    Namespacing with an HMAC of a per-install secret keeps the readable part for
    humans while making the addressable part unguessable.
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode() if isinstance(secret, str) else secret

    def _digest(self, slug: str) -> str:
        return hmac.new(self._secret, slug.encode(), hashlib.sha256).hexdigest()[:8]

    def qualify(self, slug: str) -> str:
        clean = validate_slug(slug)
        return f"{NAME_PREFIX}-{self._digest(clean)}-{clean}"

    def owns(self, name: str) -> bool:
        return name.startswith(f"{NAME_PREFIX}-") and self.split(name) is not None

    def split(self, name: str) -> str | None:
        parts = name.split("-", 2)
        if len(parts) != 3:
            return None
        prefix, digest, slug = parts
        if prefix != NAME_PREFIX or len(digest) != 8:
            return None
        if not hmac.compare_digest(self._digest(slug), digest):
            return None
        return slug

    def slug_of(self, name: str) -> str | None:
        return self.split(name) if name.startswith(f"{NAME_PREFIX}-") else None


@dataclass
class TmuxSession:
    name: str
    attached: int
    slug: str | None = None


def attach_argv(name: str, cwd: str | None = None) -> list[str]:
    argv = [TMUX_BIN, "new-session", "-A", "-s", name]
    if cwd:
        argv += ["-c", cwd]
    return argv


async def _run(*argv: str, check: bool = False) -> tuple[int, str]:
    require()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    out, err = await proc.communicate()
    text = (out or b"").decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise TmuxUnavailable((err or b"").decode("utf-8", "replace").strip() or "tmux command failed")
    return proc.returncode or 0, text


async def list_sessions() -> list[TmuxSession]:
    try:
        code, out = await _run(TMUX_BIN, "list-sessions", "-F", "#{session_name} #{session_attached}")
    except TmuxUnavailable:
        return []
    if code != 0:
        return []
    sessions: list[TmuxSession] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, attached = line.rpartition(" ")
        if not name:
            continue
        try:
            count = int(attached)
        except ValueError:
            count = 0
        sessions.append(TmuxSession(name=name, attached=count))
    return sessions


async def kill_session(name: str) -> None:
    await _run(TMUX_BIN, "kill-session", "-t", name)


async def detach_session(name: str) -> None:
    await _run(TMUX_BIN, "detach-client", "-s", name)


async def has_session(name: str) -> bool:
    code, _ = await _run(TMUX_BIN, "has-session", "-t", name)
    return code == 0
