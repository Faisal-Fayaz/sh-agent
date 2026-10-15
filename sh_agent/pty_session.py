from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import shlex
import signal
import struct
import termios
from collections import deque
from collections.abc import Awaitable, Callable

from .config import DEFAULT_SHELL
from .protocol import ServerMessage, stdout_frame

READ_SIZE = 65536
DEFAULT_RING_CAPACITY = 256 * 1024
DEFAULT_TERM = "xterm-256color"

Sender = Callable[[ServerMessage], Awaitable[None]]


def _claim_controlling_tty(slave_fd: int) -> Callable[[], None]:
    """Make the slave pty the child's controlling terminal.

    ``start_new_session=True`` calls setsid(), which leaves the child as a session
    leader with no controlling terminal. Without one the kernel has no foreground
    process group, so TIOCSWINSZ updates the stored window size but never delivers
    SIGWINCH. Programs that poll for it simply never learn about a resize: tmux and
    every other full-screen app keep rendering for the size they saw at startup.

    This targets the slave fd rather than stdin because CPython runs preexec_fn
    before it dup2's the standard streams into place, so fd 0 is not yet the pty.
    """

    def setup() -> None:
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    return setup


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


def set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def get_winsize(fd: int) -> tuple[int, int]:
    packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    return cols, rows


class PtySession:
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
    ) -> None:
        self.id = session_id
        self._loop = loop
        self._shell_argv = shlex.split(shell) if shell else shlex.split(DEFAULT_SHELL)
        self.mode = mode
        self.slug = slug
        self.tmux_name = tmux_name
        self._cwd = cwd
        self._env_overrides = env_overrides or {}
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._cols = cols
        self._rows = rows
        self._sender: Sender | None = None
        self._pending: set[asyncio.Task[None]] = set()
        self._close_listeners: list[Callable[[], None]] = []
        self._reader_registered = False
        self._closed = False
        self.exit_code: int | None = None
        self.created_at = loop.time()
        self.last_activity = loop.time()
        self.detached_at: float | None = None
        self.buffer = RingBuffer(ring_capacity)

    @property
    def shell(self) -> str:
        return shlex.join(self._shell_argv)

    @property
    def is_tmux(self) -> bool:
        return self.mode == "tmux"

    @property
    def cwd(self) -> str | None:
        return self._cwd

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

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
        master_fd, slave_fd = os.openpty()
        env = {
            **os.environ,
            "TERM": DEFAULT_TERM,
            "COLORTERM": "truecolor",
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            **self._env_overrides,
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._shell_argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self._cwd,
                env=env,
                preexec_fn=_claim_controlling_tty(slave_fd),
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)
        set_winsize(master_fd, self._cols, self._rows)
        self._master_fd = master_fd
        self._proc = proc
        self._loop.add_reader(master_fd, self._on_readable)
        self._reader_registered = True

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
        if self._master_fd is None or self._closed:
            return
        view = memoryview(data)
        while view:
            try:
                written = os.write(self._master_fd, view)
            except BlockingIOError:
                self.touch()
                return
            except OSError:
                return
            if written <= 0:
                return
            view = view[written:]
        self.touch()

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        if self._master_fd is not None and not self._closed:
            try:
                set_winsize(self._master_fd, cols, rows)
            except OSError:
                pass

    def kill(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._notify_closed()
        self._deregister_reader()
        self.kill()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        self._sender = None

    def _deregister_reader(self) -> None:
        if self._reader_registered and self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except (OSError, ValueError):
                pass
            self._reader_registered = False

    def _on_readable(self) -> None:
        fd = self._master_fd
        if fd is None:
            return
        try:
            chunk = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                self._loop.create_task(self._finish())
            return

        if not chunk:
            self._loop.create_task(self._finish())
            return

        seq = self.buffer.append(chunk)
        self.touch()
        sender = self._sender
        if sender is not None:
            task = self._loop.create_task(self._send(sender, stdout_frame(seq, chunk)))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _send(self, sender: Sender, message: ServerMessage) -> None:
        try:
            await sender(message)
        except Exception:
            self.detach()

    async def _finish(self) -> None:
        self._deregister_reader()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                await proc.wait()
            except Exception:
                pass
            code = proc.returncode
            self.exit_code = os.waitstatus_to_exitcode(code) if code is not None else None
        self._closed = True
        self._notify_closed()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        sender = self._sender
        if sender is not None:
            from .protocol import exit_frame

            task = self._loop.create_task(
                self._send(sender, exit_frame(self.exit_code if self.exit_code is not None else 0))
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
