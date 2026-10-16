from __future__ import annotations

import asyncio
import os
import shlex

import pytest

from sh_agent.config import default_shell
from sh_agent.protocol import decode_bytes, encode_bytes, parse_client_message
from sh_agent.pty_posix import get_winsize
from sh_agent.pty_session import PtySession, RingBuffer, _unquote_windows, shell_argv
from sh_agent.pty_windows import IncrementalText

# Only the tests that need a real pty are POSIX-bound. The rest run everywhere,
# which is what lets a windows-latest job say something about the session core.
posix_only = pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pty")


def test_base64_roundtrip_preserves_multibyte_boundaries() -> None:
    payload = "héllo — 世界 🌍".encode()
    assert decode_bytes(encode_bytes(payload)) == payload


def test_parse_client_message_accepts_stdin_frame() -> None:
    message = parse_client_message('{"type":"stdin","data":"' + encode_bytes(b"ls\n") + '"}')
    assert message.type == "stdin"
    assert decode_bytes(message.data) == b"ls\n"


def test_parse_client_message_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_client_message("not json")
    with pytest.raises(ValueError):
        parse_client_message('["a"]')
    with pytest.raises(ValueError):
        parse_client_message('{"type":"resize","cols":0,"rows":24}')


def test_ring_buffer_replays_only_the_gap() -> None:
    buffer = RingBuffer(capacity=1024)
    buffer.append(b"hello ")
    buffer.append(b"world")
    assert buffer.end_offset == 11
    assert buffer.replay_from(0) == b"hello world"
    assert buffer.replay_from(6) == b"world"
    assert buffer.replay_from(11) == b""
    assert buffer.replay_from(999) == b""


def test_ring_buffer_evicts_oldest_and_clamps_offset() -> None:
    buffer = RingBuffer(capacity=8)
    for chunk in (b"aaa", b"bbb", b"ccc", b"ddd"):
        buffer.append(chunk)
    assert len(buffer) <= 8
    assert buffer.base_offset > 0
    replayed = buffer.replay_from(0)
    assert replayed == buffer.replay_from(buffer.base_offset)
    assert replayed.endswith(b"ddd")


def test_a_character_split_across_messages_is_not_split_in_the_output() -> None:
    """The Windows write path, reachable from any platform.

    An arrow is three bytes, and the app can send one byte at a time; winpty
    takes text, so decoding per message would produce two replacement
    characters and a wrong glyph on the user's screen.
    """
    text = IncrementalText()
    arrow = "→".encode()
    assert text.feed(arrow[:1]) == ""
    assert text.feed(arrow[1:2]) == ""
    assert text.feed(arrow[2:]) == "→"
    assert text.feed(b"ok") == "ok"


def test_a_quoted_windows_shell_keeps_its_backslashes() -> None:
    """C:\\Program Files must survive the split, quotes and all."""
    raw = r'"C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile'
    assert _unquote_windows(shlex.split(raw, posix=False)) == [
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        "-NoProfile",
    ]


def test_the_shell_default_follows_the_platform() -> None:
    assert shell_argv(None) == shell_argv(default_shell())


class FakeBackend:
    """A pty that is not one, so the session core can be exercised anywhere.

    This is the part of the Windows job that would otherwise be untested:
    the ring buffer, offsets, replay, attach/detach, the exit frame and
    `_finish` are all platform-free, and this is what says so.
    """

    def __init__(self) -> None:
        self.pid = 4321
        self.exit_code: int | None = None
        self.written: list[bytes] = []
        self.resized: list[tuple[int, int]] = []
        self.released = False
        self.backend_closed = False
        self._on_data = None
        self._on_exit = None

    async def start(self, on_data, on_exit) -> None:
        self._on_data = on_data
        self._on_exit = on_exit

    def emit(self, data: bytes) -> None:
        assert self._on_data is not None
        self._on_data(data)

    def end(self) -> None:
        assert self._on_exit is not None
        self._on_exit()

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def resize(self, cols: int, rows: int) -> None:
        self.resized.append((cols, rows))

    def kill(self) -> None:
        pass

    def release(self) -> None:
        self.released = True

    def close(self) -> None:
        self.backend_closed = True

    async def wait(self) -> int | None:
        return 7


@pytest.mark.asyncio
async def test_the_session_core_needs_no_pty() -> None:
    loop = asyncio.get_running_loop()
    backend = FakeBackend()
    session = PtySession("t-fake", loop, shell="/bin/sh", backend_factory=lambda env: backend)
    sent = []

    async def sender(message) -> None:
        sent.append(message)

    await session.open()
    session.attach(sender)

    session.write(b"echo hi\n")
    session.resize(100, 30)
    assert backend.written == [b"echo hi\n"]
    assert backend.resized == [(100, 30)]
    assert session.pid == 4321

    backend.emit(b"prompt$ ")
    await asyncio.sleep(0.05)
    assert [message.type for message in sent] == ["stdout"]
    assert decode_bytes(sent[0].data) == b"prompt$ "
    assert sent[0].seq == len(b"prompt$ ")
    end, payload = session.replay_from(0)
    assert (end, payload) == (8, b"prompt$ ")

    session.detach()
    assert session.detached_at is not None
    backend.emit(b"while away")
    await asyncio.sleep(0.05)
    assert len(sent) == 1, "a detached session must not keep sending"
    assert session.replay_from(0)[1] == b"prompt$ while away"

    # The app reconnects to the same session id, so the exit frame still has
    # somewhere to go once it is attached again.
    session.attach(sender)
    backend.end()
    await asyncio.sleep(0.05)
    assert session.closed
    assert session.exit_code == 7
    assert backend.released, "finishing must release the pty"
    assert [message.type for message in sent] == ["stdout", "exit"]
    assert sent[1].code == 7

    session.close()
    assert session.closed


@posix_only
@pytest.mark.asyncio
async def test_pty_echoes_stdin_and_reports_exit_code() -> None:
    loop = asyncio.get_running_loop()
    session = PtySession(session_id="t-echo", loop=loop, shell="/bin/sh", ring_capacity=65536)
    await session.open()
    try:
        session.write(b"echo marker-$((6*7))\n")
        output = await _drain(session, expect_contains=b"marker-42", timeout=6.0)
        assert b"marker-42" in output
    finally:
        session.close()
    assert session.closed


@posix_only
@pytest.mark.asyncio
async def test_pty_resize_changes_reported_winsize() -> None:
    loop = asyncio.get_running_loop()
    session = PtySession(session_id="t-resize", loop=loop, shell="/bin/sh")
    await session.open()
    try:
        session.resize(120, 40)
        await asyncio.sleep(0.2)
        master_fd = session._backend.master_fd
        assert master_fd is not None
        cols, rows = await loop.run_in_executor(None, get_winsize, master_fd)
        assert (cols, rows) == (120, 40)
    finally:
        session.close()


@posix_only
@pytest.mark.asyncio
async def test_close_leaves_no_orphan_child() -> None:
    loop = asyncio.get_running_loop()
    session = PtySession(session_id="t-orphan", loop=loop, shell="/bin/sh")
    await session.open()
    pid = session.pid
    assert pid is not None
    session.close()
    await asyncio.sleep(0.3)
    assert not _pid_alive(pid), "child process survived session close"


@posix_only
@pytest.mark.asyncio
async def test_detached_output_is_replayable_by_offset() -> None:
    loop = asyncio.get_running_loop()
    session = PtySession(session_id="t-replay", loop=loop, shell="/bin/sh")
    await session.open()
    try:
        session.write(b"echo replay-check\n")
        await asyncio.sleep(0.8)
        end, payload = session.replay_from(0)
        assert end == session.buffer.end_offset
        assert b"replay-check" in payload
        mid = max(0, end - 5)
        _, tail = session.replay_from(mid)
        assert len(tail) == 5
    finally:
        session.close()


async def _drain(session: PtySession, expect_contains: bytes, timeout: float) -> bytes:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        if expect_contains in session.buffer.replay_from(0):
            return session.buffer.replay_from(0)
    return session.buffer.replay_from(0)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
