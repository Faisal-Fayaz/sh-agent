from __future__ import annotations

import asyncio
import shlex

import pytest

from sh_agent import tmux
from sh_agent.registry import SessionRegistry
from sh_agent.tmux import SlugError, SlugNamer

needs_tmux = pytest.mark.skipif(not tmux.available(), reason="tmux is not installed")


# --- slug naming ------------------------------------------------------------


def test_qualify_is_stable_for_the_same_secret() -> None:
    namer = SlugNamer("secret-a")
    assert namer.qualify("deploy") == namer.qualify("deploy")


def test_qualify_differs_per_secret() -> None:
    assert SlugNamer("secret-a").qualify("deploy") != SlugNamer("secret-b").qualify("deploy")


def test_slug_survives_a_round_trip() -> None:
    namer = SlugNamer("secret-a")
    for slug in ("deploy", "logs.1", "a_b-c", "x"):
        assert namer.slug_of(namer.qualify(slug)) == slug


def test_a_different_secret_cannot_read_our_names() -> None:
    ours = SlugNamer("secret-a").qualify("deploy")
    assert SlugNamer("secret-b").split(ours) is None
    assert SlugNamer("secret-b").owns(ours) is False


def test_names_from_other_software_are_not_claimed() -> None:
    namer = SlugNamer("secret-a")
    for foreign in ("my-editor-session", "sh-notours", "sh-zzzzzzzz-deploy", "random"):
        assert namer.owns(foreign) is False


def test_the_hmac_digest_is_the_expected_length() -> None:
    name = SlugNamer("s").qualify("deploy")
    prefix, digest, slug = name.split("-", 2)
    assert (prefix, len(digest), slug) == ("sh", 8, "deploy")


@pytest.mark.parametrize("bad", ["", "-lead", "has space", "semi;colon", "a" * 65, "ünïcode"])
def test_invalid_slugs_are_rejected(bad: str) -> None:
    with pytest.raises(SlugError):
        tmux.validate_slug(bad)


@pytest.mark.parametrize("good", ["deploy", "logs.1", "a_b-c", "x9"])
def test_valid_slugs_are_accepted(good: str) -> None:
    assert tmux.validate_slug(good) == good


# --- argv construction ------------------------------------------------------


def test_attach_argv_is_attach_or_create() -> None:
    argv = tmux.attach_argv("sh-abcdef12-deploy")
    assert argv[:5] == ["tmux", "new-session", "-A", "-s", "sh-abcdef12-deploy"]


def test_attach_argv_carries_cwd() -> None:
    argv = tmux.attach_argv("s", "/tmp")
    assert argv[-2:] == ["-c", "/tmp"]


# --- availability -----------------------------------------------------------


def test_unavailable_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: None)
    with pytest.raises(tmux.TmuxUnavailable) as excinfo:
        tmux.require()
    assert "not installed" in str(excinfo.value)


def test_list_sessions_returns_empty_without_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: None)
    import asyncio

    assert asyncio.run(tmux.list_sessions()) == []


# --- against a real tmux ----------------------------------------------------


@needs_tmux
async def _session_via_pty(name: str, cwd: str | None = None):
    """Drive tmux through the real PtySession path, the way the agent does."""
    import asyncio

    from sh_agent.pty_session import PtySession

    loop = asyncio.get_running_loop()
    session = PtySession(
        session_id=f"t-{name}",
        loop=loop,
        shell=shlex.join(tmux.attach_argv(name, cwd)),
        mode="tmux",
        slug=name,
        tmux_name=name,
    )
    await session.open()
    return session


@pytest.mark.asyncio
@needs_tmux
async def test_session_survives_the_pty_closing() -> None:
    """The whole point of tmux mode: dropping the socket must not kill the work."""
    name = SlugNamer("test-secret").qualify("persist-check")
    await tmux.kill_session(name)
    session = await _session_via_pty(name)
    try:
        for _ in range(60):
            if await tmux.has_session(name):
                break
            await asyncio.sleep(0.1)
        assert await tmux.has_session(name)

        session.close()
        await asyncio.sleep(0.5)

        assert await tmux.has_session(name), "session died with the PTY"
    finally:
        await tmux.kill_session(name)


@pytest.mark.asyncio
@needs_tmux
async def test_reattach_picks_up_the_existing_session() -> None:
    name = SlugNamer("test-secret").qualify("reattach-check")
    await tmux.kill_session(name)

    first = await _session_via_pty(name)
    try:
        for _ in range(60):
            if await tmux.has_session(name):
                break
            await asyncio.sleep(0.1)
        first.write(b"echo REATTACH-MARKER\n")
        await asyncio.sleep(1.0)
    finally:
        first.close()
    await asyncio.sleep(0.3)

    second = await _session_via_pty(name)
    try:
        await asyncio.sleep(1.0)
        transcript = second.buffer.replay_from(0).decode("utf-8", "replace")
        assert "REATTACH-MARKER" in transcript, "reattaching lost the previous scrollback"
    finally:
        second.close()
        await tmux.kill_session(name)


@pytest.mark.asyncio
@needs_tmux
async def test_kill_removes_the_session() -> None:
    name = SlugNamer("test-secret").qualify("kill-check")
    await tmux.kill_session(name)
    session = await _session_via_pty(name)
    try:
        for _ in range(60):
            if await tmux.has_session(name):
                break
            await asyncio.sleep(0.1)
        assert await tmux.has_session(name)
        await tmux.kill_session(name)
        await asyncio.sleep(0.4)
        assert not await tmux.has_session(name)
    finally:
        session.close()
        await tmux.kill_session(name)


@pytest.mark.asyncio
@needs_tmux
async def test_listing_filters_to_our_namespace() -> None:
    ours = SlugNamer("test-secret")
    theirs = SlugNamer("other-secret")
    mine = ours.qualify("mine")
    foreign = theirs.qualify("mine")
    for name in (mine, foreign):
        await tmux.kill_session(name)

    a = await _session_via_pty(mine)
    b = await _session_via_pty(foreign)
    try:
        for _ in range(60):
            if await tmux.has_session(mine) and await tmux.has_session(foreign):
                break
            await asyncio.sleep(0.1)

        registry = SessionRegistry(loop=asyncio.get_running_loop(), namer=ours)
        visible = await registry.list_tmux()
        assert sorted(s.slug for s in visible) == ["mine"]
        assert all(s.name.startswith("sh-") for s in visible)

        foreign_registry = SessionRegistry(loop=asyncio.get_running_loop(), namer=theirs)
        assert [s.slug for s in await foreign_registry.list_tmux()] == ["mine"]
    finally:
        a.close()
        b.close()
        for name in (mine, foreign):
            await tmux.kill_session(name)
