from __future__ import annotations

import asyncio
import os
import shlex
import time
import uuid

from . import tmux
from .pty_session import DEFAULT_RING_CAPACITY, PtySession

DEFAULT_IDLE_TTL = 30 * 60.0
DEFAULT_REAP_INTERVAL = 15.0
DEFAULT_MAX_SESSIONS = 8
DEFAULT_DETACH_GRACE = 120.0
DEFAULT_TMUX_MAX = 12
DEFAULT_TMUX_IDLE_TTL = 3600.0


class SessionRegistry:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        shell: str | None = None,
        cwd: str | None = None,
        idle_ttl: float = DEFAULT_IDLE_TTL,
        reap_interval: float = DEFAULT_REAP_INTERVAL,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        detach_grace: float = DEFAULT_DETACH_GRACE,
        ring_capacity: int = DEFAULT_RING_CAPACITY,
        namer: tmux.SlugNamer | None = None,
        tmux_max_sessions: int = DEFAULT_TMUX_MAX,
        tmux_idle_ttl: float = DEFAULT_TMUX_IDLE_TTL,
        tmux_enabled: bool = True,
    ) -> None:
        self._loop = loop
        self._shell = shell
        self._cwd = cwd
        self._idle_ttl = idle_ttl
        self._reap_interval = reap_interval
        self._max_sessions = max_sessions
        self._detach_grace = detach_grace
        self._ring_capacity = ring_capacity
        self._namer = namer or tmux.SlugNamer("sh-default")
        self._tmux_max_sessions = tmux_max_sessions
        self._tmux_idle_ttl = tmux_idle_ttl
        self._tmux_enabled = tmux_enabled
        self._sessions: dict[str, PtySession] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def count(self) -> int:
        return len(self._sessions)

    def all(self) -> list[PtySession]:
        return list(self._sessions.values())

    def get(self, session_id: str) -> PtySession | None:
        return self._sessions.get(session_id)

    async def create(
        self,
        cols: int = 80,
        rows: int = 24,
        cwd: str | None = None,
        mode: str = "pty",
        slug: str | None = None,
    ) -> PtySession:
        async with self._lock:
            self._reap_locked()
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError(f"session limit reached ({self._max_sessions})")

            resolved_cwd = cwd or self._cwd
            env_overrides = {}
            tmux_name = None

            if mode == "tmux":
                # The flag exists to let an operator turn the feature off. Checking it
                # here rather than in the route covers every caller.
                if not self._tmux_enabled:
                    raise tmux.TmuxUnavailable("tmux sessions are disabled (SH_TMUX_ENABLED=0)")
                if not tmux.available():
                    raise tmux.TmuxUnavailable("tmux is not installed on this host")
                clean = tmux.validate_slug(slug or "")
                tmux_name = self._namer.qualify(clean)
                # Re-attaching to a session we already hold does not add a tmux session,
                # so it must not be blocked once the cap is reached.
                if not self._tracks(tmux_name) and await self._tmux_at_limit():
                    raise RuntimeError(f"tmux session limit reached ({self._tmux_max_sessions})")
                await self._supersede(tmux_name)
                env_overrides = {"SH_SLUG": clean, "SH_TMUX_NAME": tmux_name}

            session_id = uuid.uuid4().hex
            session = PtySession(
                session_id=session_id,
                loop=self._loop,
                shell=shlex.join(tmux.attach_argv(tmux_name, resolved_cwd)) if tmux_name else self._shell,
                cwd=resolved_cwd,
                cols=cols,
                rows=rows,
                env_overrides={**env_overrides, "SH_SESSION_ID": session_id},
                ring_capacity=self._ring_capacity,
                mode=mode,
                slug=slug if tmux_name else None,
                tmux_name=tmux_name,
            )
            await session.open()
            self._sessions[session_id] = session
            return session

    def _tracks(self, tmux_name: str) -> bool:
        return any(session.tmux_name == tmux_name for session in self._sessions.values())

    async def _supersede(self, tmux_name: str) -> list[str]:
        """Replace any session we already track for this tmux name.

        tmux happily attaches several clients to one session, so without this every
        tap on a session in the switcher would add another PTY and another registry
        row pointing at the same tmux session. Attaching is a switch, not a clone:
        the old socket is closed and its client detached before the new one attaches.
        The tmux session itself, and its scrollback, is untouched.
        """
        replaced: list[str] = []
        for session_id, session in list(self._sessions.items()):
            if session.tmux_name != tmux_name:
                continue
            self._sessions.pop(session_id, None)
            session.close()
            replaced.append(session_id)
        if replaced:
            try:
                await tmux.detach_session(tmux_name)
            except tmux.TmuxUnavailable:
                pass
        return replaced

    async def _tmux_at_limit(self) -> bool:
        try:
            ours = [s for s in await tmux.list_sessions() if self._namer.owns(s.name)]
        except tmux.TmuxUnavailable:
            return False
        return len(ours) >= self._tmux_max_sessions

    async def list_tmux(self) -> list[tmux.TmuxSession]:
        try:
            sessions = await tmux.list_sessions()
        except tmux.TmuxUnavailable:
            return []
        result = []
        for session in sessions:
            slug = self._namer.slug_of(session.name)
            if slug is None:
                continue
            result.append(tmux.TmuxSession(name=session.name, attached=session.attached, slug=slug))
        return result

    async def kill_persisted(self, slug: str) -> bool:
        """Kill a tmux session that is no longer in the registry.

        Once a session is detached it leaves the registry, so the per-session kill
        endpoint 404s and the session can only be removed by the sweeper or tmux
        itself. This is the explicit path the app needs.
        """
        clean = tmux.validate_slug(slug)
        name = self._namer.qualify(clean)
        for session_id, session in list(self._sessions.items()):
            if session.tmux_name == name:
                await self.kill(session_id)
                return True
        if not self._namer.owns(name):
            return False
        try:
            if not await tmux.has_session(name):
                return False
        except tmux.TmuxUnavailable:
            return False
        await tmux.kill_session(name)
        return True

    async def reap_tmux(self) -> list[str]:
        """Kill tmux sessions of ours that nobody is attached to and that outlived the TTL."""
        try:
            sessions = await tmux.list_sessions()
        except tmux.TmuxUnavailable:
            return []
        candidates = [s for s in sessions if self._namer.owns(s.name)]
        if not candidates:
            return []

        # tmux reports activity as Unix epoch, so compare against wall clock rather
        # than the event loop's monotonic clock.
        now = time.time()
        reaped: list[str] = []

        # Age first. SH_TMUX_IDLE_TTL exists so unattached sessions do not pile up
        # forever on a machine that rarely reaches the cap.
        for session in candidates:
            if session.attached > 0:
                continue
            if self._tmux_idle_ttl <= 0:
                continue
            if session.activity is None:
                continue
            if now - session.activity >= self._tmux_idle_ttl:
                await tmux.kill_session(session.name)
                reaped.append(session.name)

        if reaped:
            candidates = [s for s in candidates if s.name not in set(reaped)]

        # Then the count cap, oldest first, and never anything currently attached.
        if len(candidates) > self._tmux_max_sessions:
            candidates.sort(key=lambda s: (s.activity is None, s.activity or 0, s.name))
            for session in candidates:
                if session.attached > 0:
                    continue
                if len(candidates) - len(reaped) <= self._tmux_max_sessions:
                    break
                await tmux.kill_session(session.name)
                reaped.append(session.name)
        return reaped

    def close(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        return True

    async def kill(self, session_id: str) -> bool:
        """Close the session and, for tmux mode, the tmux session it was attached to.

        Closing only the PTY leaves the tmux session running, which is what detach
        wants and the opposite of what an explicit kill wants.
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        if session.is_tmux and session.tmux_name:
            try:
                await tmux.kill_session(session.tmux_name)
            except tmux.TmuxUnavailable:
                pass
        session.close()
        return True

    def close_all(self) -> None:
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()

    def idle_seconds(self, session: PtySession) -> float:
        return self._loop.time() - session.last_activity

    def detached_seconds(self, session: PtySession) -> float | None:
        if session.detached_at is None:
            return None
        return self._loop.time() - session.detached_at

    def _is_expired(self, session: PtySession) -> bool:
        if session.closed:
            return True
        detached_for = self.detached_seconds(session)
        if detached_for is not None and detached_for > self._detach_grace:
            return True
        return self.idle_seconds(session) > self._idle_ttl

    def _reap_locked(self) -> list[str]:
        reaped = [sid for sid, session in self._sessions.items() if self._is_expired(session)]
        for sid in reaped:
            session = self._sessions.pop(sid)
            if session is not None:
                session.close()
        return reaped

    def reap(self) -> list[str]:
        return self._reap_locked()

    def start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = self._loop.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._reap_interval)
                async with self._lock:
                    self._reap_locked()
                try:
                    for name in await self.reap_tmux():
                        audit = getattr(self, "_audit", None)
                        if audit is not None:
                            audit.record("tmux.reaped", tmux_name=name)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper = None
        self.close_all()


class AuditLog:
    def __init__(self, path: str | None) -> None:
        self._path = path
        self.enabled = bool(path)
        self.error: str | None = None
        if not path:
            return
        directory = os.path.dirname(os.path.abspath(path))
        try:
            created = not os.path.isdir(directory)
            os.makedirs(directory, exist_ok=True)
            if created:
                # Only when we made it. A directory the user pointed us at by
                # hand is theirs to set the permissions on.
                os.chmod(directory, 0o700)
        except OSError as exc:
            self.enabled = False
            self.error = f"cannot create audit directory {directory}: {exc}"
            return
        self._restrict(path)
        if not os.access(directory, os.W_OK):
            self.enabled = False
            self.error = f"audit directory is not writable: {directory}"

    @staticmethod
    def _restrict(path: str) -> None:
        """Keep the log as private as the token file.

        Peer addresses and tailnet logins are written here, and the file was
        created world-readable by the ambient umask -- 0664 on the machine this
        was written on. The directory it lives in is normally 0700 already, but
        the file itself should not depend on that. Not being able to tighten it
        is not a reason to stop logging, so failures are ignored.
        """
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def record(self, event: str, **fields: object) -> None:
        if not self._path or not self.enabled:
            return
        import json

        entry = {
            "ts": time.time(),
            "pid": os.getpid(),
            "event": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        line = json.dumps(entry, sort_keys=True)
        try:
            # O_CREAT's mode applies only at creation, so a first run gets the
            # right permissions rather than only the run after a restart.
            descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            self.enabled = False
            self.error = f"audit log write failed: {exc}"
