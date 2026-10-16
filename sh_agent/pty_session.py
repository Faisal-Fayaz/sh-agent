from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable

from .config import default_shell
from .protocol import ServerMessage, exit_frame, stdout_frame
from .pty_backend import PtyBackend

READ_SIZE = 65536
DEFAULT_RING_CAPACITY = 256 * 1024
DEFAULT_TERM = "xterm-256color"

Sender = Callable[[ServerMessage], Awaitable[None]]

BackendFactory = Callable[[dict[str, str]], PtyBackend]


def shell_argv(shell: str | None) -> list[str]:
    """Split a shell setting into an argv.

    shlex's POSIX rules eat backslashes, and a Windows executable path is
    mostly backslashes -- ``C:\\Program Files\\...`` would arrive as
    ``C:Program Files...``. On Windows the non-POSIX rules keep the
    backslashes but leave surrounding quotes attached, so those come off here.
    """
    if not shell:
        shell = default_shell()
    if os.name != "nt":
        return shlex.split(shell)
    return _unquote_windows(shlex.split(shell, posix=False))


def _unquote_windows(tokens: list[str]) -> list[str]:
    return [token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token for token in tokens]


def shell_join(argv: list[str]) -> str:
    """The inverse of shell_argv, for showing a session's shell."""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


class RingBuffer:
    def __init__(self, capacity: int = DEFAULT_RING_CAPACITY) -> None:
        self._chunks: deque[bytes] = deque()
        self._capacity = capacity
        self._size = 0
        self._base_offset = 0
        self._end = 0

    def __len__(self) -> int:
        return self._size

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def end_offset(self) -> int:
        return self._end

    @property
    def base_offset(self) -> int:
        return self._base_offset

    def append(self, chunk: bytes) -> int:
        self._chunks.append(chunk)
        self._size += len(chunk)
        self._end += len(chunk)
        while self._size > self._capacity and len(self._chunks) > 1:
            dropped = self._chunks.popleft()
            self._size -= len(dropped)
            self._base_offset += len(dropped)
        return self._end

    def replay_from(self, offset: int) -> bytes:
        if offset >= self._end:
            return b""
        offset = max(offset, self._base_offset)
        pos = self._base_offset
        out = bytearray()
        for chunk in self._chunks:
            end = pos + len(chunk)
            if end > offset:
                out += chunk[max(0, offset - pos) :]
            pos = end
        return bytes(out)


class PtySession:
    """A shell in a pty, with replay, attachment, and a platform underneath.

    Everything here is the same on every platform; ``PosixPtyBackend`` and
    ``WindowsPtyBackend`` own the parts that are not. The backend calls the
    two callbacks below from whatever thread it reads on, so they marshal
    onto the loop before touching session state.
    """

    def __init__(
        self,
        session_id: str,
        loop: asyncio.AbstractEventLoop,
        shell: str | None = None,
        cwd: str | None = None,
        cols: int = 80,
        rows: int = 24,
        env_overrides: dict[str, str] | None = None,
        ring_capacity: int = DEFAULT_RING_CAPACITY,
        mode: str = "pty",
        slug: str | None = None,
        tmux_name: str | None = None,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        self.id = session_id
        self._loop = loop
        self._shell_argv = shell_argv(shell)
        self.mode = mode
        self.slug = slug
        self.tmux_name = tmux_name
        self._cwd = cwd
        self._env_overrides = env_overrides or {}
        self._backend: PtyBackend | None = None
        self._backend_factory = backend_factory
        self._cols = cols
        self._rows = rows
        self._sender: Sender | None = None
        self._pending: set[asyncio.Task[None]] = set()
        self._close_listeners: list[Callable[[], None]] = []
        self._closed = False
        self._finishing = False
        self.exit_code: int | None = None
        self.created_at = loop.time()
        self.last_activity = loop.time()
        self.detached_at: float | None = None
        self.buffer = RingBuffer(ring_capacity)

    @property
    def shell(self) -> str:
        return shell_join(self._shell_argv)

    @property
    def is_tmux(self) -> bool:
        return self.mode == "tmux"

    @property
    def cwd(self) -> str | None:
        return self._cwd

    @property
    def pid(self) -> int | None:
        return self._backend.pid if self._backend is not None else None

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def closed(self) -> bool:
        return self._closed

    async def open(self) -> None:
        env = {
            **os.environ,
            "TERM": DEFAULT_TERM,
            "COLORTERM": "truecolor",
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            **self._env_overrides,
        }
        backend = self._make_backend(env)
        self._backend = backend
        await backend.start(self._data_from_backend, self._exit_from_backend)

    def _make_backend(self, env: dict[str, str]) -> PtyBackend:
        if self._backend_factory is not None:
            return self._backend_factory(env)
        kwargs = {
            "loop": self._loop,
            "argv": self._shell_argv,
            "cwd": self._cwd,
            "env": env,
            "cols": self._cols,
            "rows": self._rows,
        }
        # Imported here so a POSIX machine never needs winpty installed, and
        # a Windows one never imports termios.
        if os.name == "nt":
            from .pty_windows import WindowsPtyBackend

            return WindowsPtyBackend(**kwargs)
        from .pty_posix import PosixPtyBackend

        return PosixPtyBackend(**kwargs)

    def on_close(self, callback: Callable[[], None]) -> None:
        """Register a synchronous callback fired when the session is torn down."""
        self._close_listeners.append(callback)

    def forget_close_listener(self, callback: Callable[[], None]) -> None:
        """Drop a callback registered by a socket that has already gone away.

        The app reconnects to the same session id every time it switches back, so
        without this a long-lived session accumulates one dead socket per visit.
        """
        try:
            self._close_listeners.remove(callback)
        except ValueError:
            pass

    def _notify_closed(self) -> None:
        for callback in list(self._close_listeners):
            try:
                callback()
            except Exception:
                pass
        self._close_listeners.clear()

    def attach(self, sender: Sender) -> None:
        self._sender = sender
        self.detached_at = None

    def detach(self) -> None:
        self._sender = None
        self.detached_at = self._loop.time()

    def replay_from(self, offset: int) -> tuple[int, bytes]:
        return self.buffer.end_offset, self.buffer.replay_from(offset)

    def touch(self) -> None:
        self.last_activity = self._loop.time()

    def write(self, data: bytes) -> None:
        if self._backend is None or self._closed:
            return
        self._backend.write(data)
        self.touch()

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        if self._backend is not None and not self._closed:
            self._backend.resize(cols, rows)

    def kill(self) -> None:
        if self._backend is not None:
            self._backend.kill()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._notify_closed()
        if self._backend is not None:
            self._backend.close()
        self._sender = None

    def _data_from_backend(self, chunk: bytes) -> None:
        # Callable from the backend's reader thread: hand it to the loop.
        self._loop.call_soon_threadsafe(self._deliver, chunk)

    def _exit_from_backend(self) -> None:
        self._loop.call_soon_threadsafe(self._begin_finish)

    def _deliver(self, chunk: bytes) -> None:
        seq = self.buffer.append(chunk)
        self.touch()
        sender = self._sender
        if sender is not None:
            task = self._loop.create_task(self._send(sender, stdout_frame(seq, chunk)))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    def _begin_finish(self) -> None:
        # EOF can arrive twice -- a read of zero bytes and then EIO -- and the
        # finish path closes things, so it runs once.
        if self._finishing:
            return
        self._finishing = True
        task = self._loop.create_task(self._finish())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, sender: Sender, message: ServerMessage) -> None:
        try:
            await sender(message)
        except Exception:
            self.detach()

    async def _finish(self) -> None:
        code = None
        backend = self._backend
        if backend is not None:
            backend.release()
            code = await backend.wait()
        self.exit_code = code
        self._closed = True
        self._notify_closed()
        sender = self._sender
        if sender is not None:
            task = self._loop.create_task(self._send(sender, exit_frame(code if code is not None else 0)))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
