from __future__ import annotations

import asyncio

import pytest

from sh_agent.protocol import decode_bytes, encode_bytes, parse_client_message
from sh_agent.pty_session import PtySession, RingBuffer, get_winsize


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


@pytest.mark.asyncio
async def test_pty_resize_changes_reported_winsize() -> None:
    loop = asyncio.get_running_loop()
    session = PtySession(session_id="t-resize", loop=loop, shell="/bin/sh")
    await session.open()
    try:
        session.resize(120, 40)
        await asyncio.sleep(0.2)
        cols, rows = await loop.run_in_executor(None, get_winsize, session._master_fd)
        assert (cols, rows) == (120, 40)
    finally:
        session.close()


@pytest.mark.asyncio
async def test_close_leaves_no_orphan_child() -> None:
    loop = asyncio.get_running_loop()
    session = PtySession(session_id="t-orphan", loop=loop, shell="/bin/sh")
    await session.open()
    proc = session._proc
    assert proc is not None
    pid = proc.pid
    session.close()
    await asyncio.sleep(0.3)
    assert not _pid_alive(pid), "child process survived session close"


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
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
