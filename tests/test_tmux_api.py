from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from sh_agent import tmux
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
