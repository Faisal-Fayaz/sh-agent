from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

OnData = Callable[[bytes], None]
OnExit = Callable[[], None]


class PtyBackend(Protocol):
    """One running pseudo-terminal, and everything about it the platform decides.

    The session core owns what is the same everywhere -- the ring buffer and
    its offsets, replay, attach and detach, the sender and its teardown. A
    backend owns what is not: opening a pty, reading it, writing it, resizing
    it, killing it, and knowing why it ended. POSIX and Windows disagree about
    those and about nothing else.

    ``on_data`` and ``on_exit`` may be called from any thread, because the
    Windows backend reads on one it starts itself. The core marshals them back
    onto the event loop before touching session state.
    """

    @property
    def pid(self) -> int | None: ...

    @property
    def exit_code(self) -> int | None: ...

    async def start(self, on_data: OnData, on_exit: OnExit) -> None: ...

    def write(self, data: bytes) -> None: ...

    def resize(self, cols: int, rows: int) -> None: ...

    def kill(self) -> None: ...

    def release(self) -> None:
        """Let go of the pty without killing anything.

        Only called after the pty has reported EOF, so the child is already
        gone and this is about closing a descriptor or a socket.
        """
        ...

    def close(self) -> None:
        """Kill the child if it is still there, then release."""
        ...

    async def wait(self) -> int | None:
        """The child's exit code, once it has exited."""
        ...
