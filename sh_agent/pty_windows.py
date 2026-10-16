from __future__ import annotations

import asyncio
import codecs
import functools
import os
import signal
import threading
from typing import TYPE_CHECKING

from .pty_backend import OnData, OnExit
from .pty_session import READ_SIZE

if TYPE_CHECKING:  # pragma: no cover - the machine running the tests has no winpty
    from winpty import PtyProcess


class IncrementalText:
    """Bytes in, text out, without splitting a character across the seam.

    The app sends the bytes xterm.js produced, and a multi-byte character can
    straddle two messages the same way it can straddle two WebSocket frames.
    pywinpty wants text, so decoding each message on its own would turn one
    character into two replacement characters at every boundary. This holds
    the start of a partial character back until the rest of it arrives.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, data: bytes) -> str:
        return self._decoder.decode(data, final=False)


class WindowsPtyBackend:
    """A pseudo-terminal through pywinpty, which wraps ConPTY on Windows 10 1809+.

    Two things shape this class. Reads block, and pywinpty's ``read()`` is a
    socket recv fed by a thread of its own, so turning it into a callback takes
    one more thread: started here, daemonised, and left to unwind on EOF.
    Writes are text rather than bytes, which ``IncrementalText`` handles.
    """

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
        self._proc: PtyProcess | None = None
        self._reader: threading.Thread | None = None
        self._text = IncrementalText()
        self._closing = False
        self.exit_code: int | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self, on_data: OnData, on_exit: OnExit) -> None:
        # Imported here rather than at module scope so this module can be
        # imported, and its text handling tested, where winpty does not exist.
        import winpty

        spawn = functools.partial(
            winpty.PtyProcess.spawn,
            self._argv,
            cwd=self._cwd,
            env=self._env,
            dimensions=(self._rows, self._cols),
        )
        proc = await self._loop.run_in_executor(None, spawn)
        self._proc = proc
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(proc, on_data, on_exit),
            name=f"sh-pty-{proc.pid}",
            daemon=True,
        )
        self._reader.start()

    def _read_loop(self, proc: PtyProcess, on_data: OnData, on_exit: OnExit) -> None:
        loop = self._loop
        while not self._closing:
            try:
                chunk = proc.read(READ_SIZE)
            except (EOFError, OSError, ValueError):
                break
            if not chunk:
                # pywinpty's keep-alive sentinel: nothing to read yet, not EOF.
                continue
            loop.call_soon_threadsafe(on_data, chunk.encode("utf-8", "replace"))
        loop.call_soon_threadsafe(on_exit)

    def write(self, data: bytes) -> None:
        proc = self._proc
        if proc is None or self._closing:
            return
        text = self._text.feed(data)
        if not text:
            return
        try:
            proc.write(text)
        except (EOFError, OSError, ValueError):
            pass

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        proc = self._proc
        if proc is None or self._closing:
            return
        try:
            proc.setwinsize(rows, cols)
        except (EOFError, OSError, ValueError):
            pass

    def kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.isalive():
                # os.kill on Windows does not deliver a signal: anything other
                # than the console events terminates the process outright,
                # which is what a Kill button means.
                os.kill(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    def release(self) -> None:
        self._closing = True
        self._dispose_in_thread()

    def close(self) -> None:
        self._closing = True
        self._dispose_in_thread()

    def _dispose_in_thread(self) -> None:
        proc = self._proc
        if proc is None:
            return

        def dispose() -> None:
            # pywinpty's close() sleeps while it waits for the process to die,
            # so it runs in a thread rather than stalling the event loop.
            try:
                proc.terminate(force=True)
            except Exception:
                pass
            try:
                proc.close()
            except Exception:
                pass

        self._loop.run_in_executor(None, dispose)

    async def wait(self) -> int | None:
        proc = self._proc
        if proc is None:
            return self.exit_code
        status = await self._loop.run_in_executor(None, proc.wait)
        self.exit_code = status
        return status
