from __future__ import annotations

import asyncio
import base64
import json
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from sh_agent import tmux
from sh_agent.pty_session import PtySession
from sh_agent.registry import SessionRegistry

TOKEN = "test-token-abcdefghijklmnop"
needs_tmux = pytest.mark.skipif(not tmux.available(), reason="tmux is not installed")


def _app(tmp_path, **auth_kwargs):
    from sh_agent.app import create_app
    from sh_agent.auth import AuthConfig
    from sh_agent.config import Config

    config = Config(
        host="127.0.0.1",
        port=0,
        palette_path=str(tmp_path / "palette.json"),
        audit_path=str(tmp_path / "audit.log"),
        max_sessions=4,
    )
    config.auth = AuthConfig(
        token=TOKEN,
        write_token=None,
        tailscale_socket=str(tmp_path / "none.sock"),
        **auth_kwargs,
    )
    return create_app(config)


@pytest.fixture()
def client(tmp_path):
    with TestClient(_app(tmp_path)) as test_client:
        yield test_client


@pytest.fixture()
def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# --- validation that does not need tmux -------------------------------------


def test_rejects_an_unknown_mode(client: TestClient, auth: dict) -> None:
    response = client.post("/api/session", json={"mode": "screen"}, headers=auth)
    assert response.status_code == 400
    assert "mode must be" in response.json()["detail"]


def test_rejects_a_malformed_slug(client: TestClient, auth: dict) -> None:
    response = client.post("/api/session", json={"mode": "tmux", "slug": "bad slug"}, headers=auth)
    assert response.status_code in (400, 501)


def test_session_info_reports_mode_and_slug(client: TestClient, auth: dict) -> None:
    body = client.post("/api/session", json={"mode": "pty"}, headers=auth).json()
    assert body["mode"] == "pty"
    assert body["slug"] is None


def test_listing_reports_pty_sessions_and_tmux_state(client: TestClient, auth: dict) -> None:
    client.post("/api/session", json={}, headers=auth)
    body = client.get("/api/sessions", headers=auth).json()
    assert len(body["live"]) == 1
    assert body["live"][0]["mode"] == "pty"
    assert "persisted" in body
    assert "tmux_available" in body


def test_listing_requires_auth(client: TestClient) -> None:
    assert client.get("/api/sessions").status_code == 401


def test_detach_on_a_missing_session_is_404(client: TestClient, auth: dict) -> None:
    assert client.post("/api/session/nope/detach", headers=auth).status_code == 404


def test_detach_closes_a_pty_session(client: TestClient, auth: dict) -> None:
    session = client.post("/api/session", json={}, headers=auth).json()
    response = client.post(f"/api/session/{session['id']}/detach", headers=auth)
    assert response.status_code == 200
    assert response.json()["kept_alive"] is None
    assert client.get(f"/api/session/{session['id']}", headers=auth).status_code == 404


# --- requires tmux -----------------------------------------------------------


@needs_tmux
def test_tmux_session_is_created_and_reported(client: TestClient, auth: dict, tmp_path) -> None:
    body = client.post("/api/session", json={"mode": "tmux", "slug": "api-check"}, headers=auth).json()
    assert body["mode"] == "tmux"
    assert body["slug"] == "api-check"

    listed = client.get("/api/sessions", headers=auth).json()
    names = [s["tmux_name"] for s in listed["live"] if s["mode"] == "tmux"]
    assert names and all(n.startswith("sh-") for n in names)
    namer = client.app.state.registry._namer
    assert [namer.slug_of(name) for name in names] == ["api-check"] * len(names)

    client.post(f"/api/session/{body['id']}/kill", headers=auth)


@needs_tmux
def test_tmux_limit_is_enforced(client: TestClient, auth: dict) -> None:
    registry = client.app.state.registry
    registry._tmux_max_sessions = 0
    response = client.post("/api/session", json={"mode": "tmux", "slug": "over-limit"}, headers=auth)
    assert response.status_code == 429
    assert "tmux session limit" in response.json()["detail"]


@needs_tmux
def test_tmux_detach_keeps_the_session_alive(client: TestClient, auth: dict) -> None:
    created = client.post("/api/session", json={"mode": "tmux", "slug": "keepalive"}, headers=auth).json()
    name = next(
        s["tmux_name"]
        for s in client.get("/api/sessions", headers=auth).json()["live"]
        if s["mode"] == "tmux"
    )

    for _ in range(60):
        if asyncio.run(tmux.has_session(name)):
            break
        import time

        time.sleep(0.1)

    response = client.post(f"/api/session/{created['id']}/detach", headers=auth)
    assert response.status_code == 200
    assert response.json()["kept_alive"] == "keepalive"

    import time

    time.sleep(0.5)
    assert asyncio.run(tmux.has_session(name)), "detach killed the tmux session"
    asyncio.run(tmux.kill_session(name))


@needs_tmux
def test_sweeper_kills_sessions_over_the_limit(tmp_path) -> None:
    """Detached tmux sessions outlive everything, so the sweeper is the only cap."""

    async def run() -> list[str]:
        namer = tmux.SlugNamer("sweeper-secret")
        registry = SessionRegistry(
            loop=asyncio.get_running_loop(),
            namer=namer,
            tmux_max_sessions=1,
        )
        names = [namer.qualify(f"sweep-{i}") for i in range(3)]
        sessions_created = []
        for name in names:
            await tmux.kill_session(name)
            from sh_agent.pty_session import PtySession

            session = PtySession(
                session_id=f"s-{name}",
                loop=asyncio.get_running_loop(),
                shell=f"tmux new-session -A -s {name}",
                mode="tmux",
                slug=name,
                tmux_name=name,
            )
            await session.open()
            sessions_created.append(session)
        for _ in range(60):
            states = [await tmux.has_session(name) for name in names]
            if all(states):
                break
            await asyncio.sleep(0.1)

        for session in sessions_created:
            session.close()
        await asyncio.sleep(0.6)

        attached = [await tmux.list_sessions()]
        still_attached = [s for s in attached[0] if s.name in names and s.attached > 0]

        reaped = await registry.reap_tmux()
        for name in names:
            await tmux.kill_session(name)
        return [n for n in reaped if n in names], len(still_attached)

    reaped, still_attached = asyncio.run(run())
    assert still_attached == 0, f"test setup left {still_attached} sessions attached"
    assert len(reaped) == 2, f"expected 2 reaped, got {reaped}"


@needs_tmux
def test_sweeper_leaves_attached_sessions_alone(tmp_path) -> None:
    async def run() -> tuple[int, bool]:
        namer = tmux.SlugNamer("attached-secret")
        registry = SessionRegistry(
            loop=asyncio.get_running_loop(),
            namer=namer,
            tmux_max_sessions=0,
        )
        name = namer.qualify("busy")
        await tmux.kill_session(name)
        from sh_agent.pty_session import PtySession

        session = PtySession(
            session_id="s-busy",
            loop=asyncio.get_running_loop(),
            shell=f"tmux new-session -A -s {name}",
            mode="tmux",
            slug="busy",
            tmux_name=name,
        )
        await session.open()
        for _ in range(60):
            if await tmux.has_session(name):
                break
            await asyncio.sleep(0.1)

        reaped = await registry.reap_tmux()
        still = await tmux.has_session(name)
        session.close()
        await tmux.kill_session(name)
        return len(reaped), still

    count, still_there = asyncio.run(run())
    assert count == 0, "sweeper killed an attached session"
    assert still_there is True


def test_audit_records_detach(client: TestClient, auth: dict, tmp_path) -> None:
    session = client.post("/api/session", json={}, headers=auth).json()
    client.post(f"/api/session/{session['id']}/detach", headers=auth)
    lines = (tmp_path / "audit.log").read_text().splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert "session.detached" in events


@needs_tmux
def test_kill_also_kills_the_tmux_session(client: TestClient, auth: dict) -> None:
    """Closing only the PTY would leave the tmux session running."""
    import time

    created = client.post("/api/session", json={"mode": "tmux", "slug": "killme"}, headers=auth).json()
    name = next(
        s["tmux_name"]
        for s in client.get("/api/sessions", headers=auth).json()["live"]
        if s["mode"] == "tmux"
    )
    for _ in range(60):
        if asyncio.run(tmux.has_session(name)):
            break
        time.sleep(0.1)
    assert asyncio.run(tmux.has_session(name))

    assert client.post(f"/api/session/{created['id']}/kill", headers=auth).status_code == 200
    time.sleep(0.4)
    assert not asyncio.run(tmux.has_session(name)), "tmux session outlived an explicit kill"


@needs_tmux
def test_attaching_twice_replaces_rather_than_duplicates(client: TestClient, auth: dict) -> None:
    """Tapping a session in the switcher must switch to it, not clone it."""
    import time

    first = client.post("/api/session", json={"mode": "tmux", "slug": "twice"}, headers=auth).json()

    second = client.post("/api/session", json={"mode": "tmux", "slug": "twice"}, headers=auth).json()
    assert second["id"] != first["id"]

    live = client.get("/api/sessions", headers=auth).json()["live"]
    ours = [s for s in live if s["mode"] == "tmux" and s["slug"] == "twice"]
    assert len(ours) == 1, f"expected 1 live entry for 'twice', got {len(ours)}"

    assert client.get(f"/api/session/{first['id']}", headers=auth).status_code == 404

    client.post(f"/api/session/{second['id']}/kill", headers=auth)
    time.sleep(0.3)


@needs_tmux
def test_replacing_keeps_the_tmux_session_and_its_scrollback(client: TestClient, auth: dict) -> None:
    import base64
    import time

    created = client.post("/api/session", json={"mode": "tmux", "slug": "keepmine"}, headers=auth).json()
    name = next(
        s["tmux_name"]
        for s in client.get("/api/sessions", headers=auth).json()["live"]
        if s["mode"] == "tmux"
    )
    for _ in range(60):
        if asyncio.run(tmux.has_session(name)):
            break
        time.sleep(0.1)

    with client.websocket_connect(f"/ws/{created['id']}?token={TOKEN}") as ws:
        ws.receive_json()
        ws.send_text(
            json.dumps({"type": "stdin", "data": base64.b64encode(b"echo SURVIVES-SWAP\n").decode()})
        )
        seen = b""
        for _ in range(60):
            frame = ws.receive_json()
            if frame.get("type") == "stdout":
                seen += base64.b64decode(frame["data"])
                if b"SURVIVES-SWAP" in seen:
                    break
    assert b"SURVIVES-SWAP" in seen

    replacement = client.post("/api/session", json={"mode": "tmux", "slug": "keepmine"}, headers=auth).json()
    with client.websocket_connect(f"/ws/{replacement['id']}?token={TOKEN}") as ws:
        ws.receive_json()
        transcript = b""
        deadline = time.time() + 8
        while time.time() < deadline and b"SURVIVES-SWAP" not in transcript:
            frame = ws.receive_json()
            if frame.get("type") == "stdout":
                transcript += base64.b64decode(frame["data"])

    assert b"SURVIVES-SWAP" in transcript, "replacing the session lost the tmux scrollback"
    client.post(f"/api/session/{replacement['id']}/kill", headers=auth)


@needs_tmux
def test_different_slugs_still_coexist(client: TestClient, auth: dict) -> None:
    import time

    for slug in ("alpha", "beta"):
        client.post("/api/session", json={"mode": "tmux", "slug": slug}, headers=auth)
    live = client.get("/api/sessions", headers=auth).json()["live"]
    ours = sorted(s["slug"] for s in live if s["mode"] == "tmux")
    assert ours == ["alpha", "beta"]
    for session in live:
        client.post(f"/api/session/{session['id']}/kill", headers=auth)
    time.sleep(0.2)


def test_multiple_pty_sessions_still_coexist(client: TestClient, auth: dict) -> None:
    """Dedupe is scoped to tmux names; ephemeral shells are not unique."""
    client.post("/api/session", json={}, headers=auth)
    client.post("/api/session", json={}, headers=auth)
    live = client.get("/api/sessions", headers=auth).json()["live"]
    assert len([s for s in live if s["mode"] == "pty"]) == 2


@needs_tmux
def test_persisted_listing_hides_live_slugs(client: TestClient, auth: dict) -> None:
    import time

    client.post("/api/session", json={"mode": "tmux", "slug": "hiding"}, headers=auth)
    for _ in range(60):
        listing = client.get("/api/sessions", headers=auth).json()
        if listing["live"]:
            break
        time.sleep(0.1)

    slugs = [s["slug"] for s in listing["live"] if s["mode"] == "tmux"]
    persisted = [p["slug"] for p in listing["persisted"]]
    check_set = set(slugs)
    assert check_set, "expected a live tmux session"
    assert not (check_set & set(persisted)), f"slug listed twice: {check_set & set(persisted)}"

    session = next(s for s in listing["live"] if s["mode"] == "tmux")
    client.post(f"/api/session/{session['id']}/kill", headers=auth)


@needs_tmux
def test_a_detached_session_can_still_be_killed(client: TestClient, auth: dict) -> None:
    """Once detached it has no session id, so it needs a slug-level kill."""
    import time

    created = client.post("/api/session", json={"mode": "tmux", "slug": "ghost"}, headers=auth).json()
    name = next(
        s["tmux_name"]
        for s in client.get("/api/sessions", headers=auth).json()["live"]
        if s["mode"] == "tmux"
    )
    for _ in range(60):
        if asyncio.run(tmux.has_session(name)):
            break
        time.sleep(0.1)

    assert client.post(f"/api/session/{created['id']}/detach", headers=auth).status_code == 200
    assert client.get(f"/api/session/{created['id']}", headers=auth).status_code == 404
    time.sleep(0.3)
    assert asyncio.run(tmux.has_session(name)), "detach should have kept it alive"

    removed = client.delete("/api/persisted/ghost", headers=auth)
    assert removed.status_code == 200
    time.sleep(0.4)
    assert not asyncio.run(tmux.has_session(name)), "persisted kill did not remove the session"

    assert client.delete("/api/persisted/ghost", headers=auth).status_code == 404


def test_persisted_kill_requires_auth(client: TestClient) -> None:
    assert client.delete("/api/persisted/anything").status_code == 401


def test_persisted_kill_rejects_a_bad_slug(client: TestClient, auth: dict) -> None:
    response = client.delete("/api/persisted/bad%20slug", headers=auth)
    assert response.status_code == 400


@needs_tmux
def test_the_cap_blocks_new_names_but_not_reopening() -> None:
    """The cap limits extra tmux sessions; reconnecting to one you hold is not an extra.

    The cap probe is stubbed so this checks the guard rather than the host's tmux
    server, which other tests in this file also mutate.
    """

    async def run() -> tuple[str, str, bool, int]:
        registry = SessionRegistry(
            loop=asyncio.get_running_loop(),
            namer=tmux.SlugNamer("cap-secret"),
            tmux_max_sessions=1,
        )
        created = 0

        async def probe() -> bool:
            return created >= 1

        registry._tmux_at_limit = probe  # type: ignore[method-assign]

        first = await registry.create(mode="tmux", slug="capped")
        created += 1
        again = await registry.create(mode="tmux", slug="capped")
        created += 1
        blocked = False
        try:
            await registry.create(mode="tmux", slug="other")
        except RuntimeError:
            blocked = True
        held = len([s for s in registry.all() if s.tmux_name == first.tmux_name])
        registry.close_all()
        return first.tmux_name or "", again.tmux_name or "", blocked, held

    name, again, blocked, held = asyncio.run(run())
    assert name == again, "reopening the same slug must reuse the tmux session"
    assert blocked, "a genuinely new slug must still hit the cap"
    assert held == 1, f"reopening should leave one live entry, not {held}"


def _tmux_window_size(name: str) -> tuple[int, int]:
    """Ask tmux directly, rather than trusting the size the session believes it has."""
    out = subprocess.run(
        ["tmux", "list-windows", "-t", name, "-F", "#{window_width}x#{window_height}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    cols, rows = out.split("x")
    return int(cols), int(rows)


@needs_tmux
def test_resizing_a_tmux_session_resizes_the_tmux_window(tmp_path) -> None:
    """tmux learns the new size only if the child owns a controlling terminal."""

    async def run() -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
        namer = tmux.SlugNamer("resize-secret")
        name = namer.qualify("resize")
        await tmux.kill_session(name)
        registry = SessionRegistry(
            loop=asyncio.get_running_loop(),
            namer=namer,
            tmux_max_sessions=4,
        )
        session = await registry.create(cols=30, rows=10, mode="tmux", slug="resize")
        for _ in range(60):
            if await tmux.has_session(name):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        spawned = _tmux_window_size(name)

        session.resize(48, 21)
        for _ in range(60):
            if _tmux_window_size(name) != spawned:
                break
            await asyncio.sleep(0.1)

        resized = _tmux_window_size(name)
        believed = (session.cols, session.rows)
        await registry.kill(session.id)
        await tmux.kill_session(name)
        return spawned, resized, believed

    spawned, resized, believed = asyncio.run(run())
    assert believed == (48, 21), f"the session should record the resize, got {believed}"
    assert resized == (48, 20), (
        f"tmux ignored the resize: window was {spawned} and stayed there instead of "
        f"becoming 48x20 (one row is the status line)"
    )


def test_resizing_a_pty_delivers_sigwinch_to_the_shell() -> None:
    """A plain shell must observe the new size too, which rides on the same signal."""

    async def run() -> str:
        loop = asyncio.get_running_loop()
        chunks: list[str] = []

        async def sender(message):
            data = message.model_dump(exclude_none=True).get("data")
            if data:
                chunks.append(data)

        session = PtySession(session_id="winsize", loop=loop, shell="/bin/bash", cols=30, rows=10)
        await session.open()
        session.attach(sender)
        # bash services traps between foreground commands, so the sleep is short enough
        # that it returns and the trap runs while we are still reading.
        session.write(b"trap 'stty size' WINCH; stty size; sleep 1\n")
        await asyncio.sleep(0.5)
        session.resize(48, 21)

        text = ""
        for _ in range(100):
            text = "".join(base64.b64decode(c).decode(errors="replace") for c in chunks)
            if re.search(r"\b21 48\b", text):
                break
            await asyncio.sleep(0.1)
        session.close()
        return text

    text = asyncio.run(run())
    assert re.search(r"\b21 48\b", text), f"shell never saw the resize; output was: {text!r}"
