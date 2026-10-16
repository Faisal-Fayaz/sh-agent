from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import signal
import struct
import termios
from collections.abc import Callable

from .pty_backend import OnData, OnExit
from .pty_session import READ_SIZE

# EIO is how Linux reports that the child closed the pty; EBADF is how it looks
# when a close raced the reader.
DEAD_MASTER_ERRORS = (errno.EIO, errno.EBADF)


def set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def get_winsize(fd: int) -> tuple[int, int]:
    packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    return cols, rows


def _claim_controlling_tty(slave_fd: int) -> Callable[[], None]:
    """Make the slave pty the child's controlling terminal.

    ``start_new_session=True`` calls setsid(), which leaves the child as a session
    leader with no controlling terminal. Without one the kernel has no foreground
    process group, so TIOCSWINSZ updates the stored window size but never delivers
    SIGWINCH. Programs that poll for it simply never learn about a resize: tmux and
    every other full-screen app keep rendering for the size they saw at startup.

    This targets the slave fd rather than stdin because CPython runs preexec_fn
    before it dup2's the standard streams into place, so fd 0 is not yet the pty.

    bash does not need this: it claims a controlling terminal for itself at
    startup, so a test that watches a bash trap fire passes whether or not the
    ioctl is here. tmux is the one that proves it -- remove the ioctl and the
    tmux resize test fails, with tmux still rendering for the size it started
    with. It is also the case that matters, since full-screen programs are the
    reason the signal exists.
    """

    def setup() -> None:
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    return setup


class PosixPtyBackend:
    """openpty(3), a non-blocking master fd, and the loop's own reader."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        argv: list[str],
        cwd: str | None,
        env: dict[str, str],
        cols: int,
        rows: int,
    ) -> None:
        self._loop = loop
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._cols = cols
        self._rows = rows
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._on_data: OnData | None = None
        self._on_exit: OnExit | None = None
        self._reader_registered = False
        self.exit_code: int | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def master_fd(self) -> int | None:
        return self._master_fd

    async def start(self, on_data: OnData, on_exit: OnExit) -> None:
        self._on_data = on_data
        self._on_exit = on_exit
        master_fd, slave_fd = os.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self._cwd,
                env=self._env,
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

    def write(self, data: bytes) -> None:
        fd = self._master_fd
        if fd is None:
            return
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                # The pty's buffer is full; the rest of this write is dropped
                # rather than queued. Typing outruns a pty far less often than
                # it seems it might.
                return
            except OSError:
                return
            if written <= 0:
                return
            view = view[written:]

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        fd = self._master_fd
        if fd is not None:
            try:
                set_winsize(fd, cols, rows)
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

    def release(self) -> None:
        self._deregister_reader()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def close(self) -> None:
        self.kill()
        self.release()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def wait(self) -> int | None:
        proc = self._proc
        if proc is None:
            return self.exit_code
        if proc.returncode is None:
            try:
                await proc.wait()
            except Exception:
                return self.exit_code
        code = proc.returncode
        self.exit_code = os.waitstatus_to_exitcode(code) if code is not None else None
        return self.exit_code

    def _deregister_reader(self) -> None:
        if self._reader_registered and self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except (OSError, ValueError):
                pass
            self._reader_registered = False

    def _on_readable(self) -> None:
        fd = self._master_fd
        if fd is None or self._on_data is None or self._on_exit is None:
            return
        try:
            chunk = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in DEAD_MASTER_ERRORS:
                self._on_exit()
            return

        if not chunk:
            self._on_exit()
            return

        self._on_data(chunk)
