from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from sh_agent.app import create_app
from sh_agent.auth import AuthConfig
from sh_agent.config import Config

TOKEN = "test-token-abcdefghijklmnop"
WRITE_TOKEN = "test-write-token-zyxwvutsrqponm"


@pytest.fixture()
def client(tmp_path) -> TestClient:
    config = Config(
        host="127.0.0.1",
        port=0,
        palette_path=str(tmp_path / "palette.json"),
        audit_path=str(tmp_path / "audit.log"),
        max_sessions=2,
        ring_capacity=32768,
    )
    config.auth = AuthConfig(
        token=TOKEN,
        write_token=None,
        tailscale_require=False,
        tailscale_allow=[],
        tailscale_socket=str(tmp_path / "nonexistent.sock"),
    )
    with TestClient(create_app(config)) as test_client:
        yield test_client


@pytest.fixture()
def write_client(tmp_path) -> TestClient:
    config = Config(
        host="127.0.0.1",
        port=0,
        palette_path=str(tmp_path / "palette.json"),
        audit_path=str(tmp_path / "audit.log"),
        max_sessions=2,
    )
    config.auth = AuthConfig(
        token=TOKEN,
        write_token=WRITE_TOKEN,
        tailscale_require=False,
        tailscale_allow=[],
        tailscale_socket=str(tmp_path / "nonexistent.sock"),
    )
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_health_is_open_and_reports_state(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["protocol"] == 1
    assert body["write_enabled"] is False
    assert body["tailscale_required"] is False


def test_health_reports_the_version_that_was_actually_installed(client: TestClient) -> None:
    """The version was written out in two more places by hand and had already drifted.

    Anything that reads it back out of the package has to agree with the metadata,
    or a released build reports a number that is not the one people installed.
    """
    from sh_agent import __version__

    assert client.get("/health").json()["version"] == __version__
    assert __version__ != "0.0.0+unknown", "the test suite runs against an installed package"


def test_missing_token_is_rejected(client: TestClient) -> None:
    assert client.get("/whoami").status_code == 401


def test_wrong_token_is_rejected(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_valid_token_returns_identity(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["verified_by"] == "token"


def test_session_create_requires_auth(client: TestClient) -> None:
    assert client.post("/api/session", json={"cols": 80, "rows": 24}).status_code == 401


def test_session_create_and_fetch(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    created = client.post("/api/session", json={"cols": 100, "rows": 30}, headers=headers)
    assert created.status_code == 201
    session = created.json()
    assert session["cols"] == 100 and session["rows"] == 30
    assert session["closed"] is False
    fetched = client.get(f"/api/session/{session['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == session["id"]


def test_session_limit_is_enforced(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/api/session", json={}, headers=headers).status_code == 201
    assert client.post("/api/session", json={}, headers=headers).status_code == 201
    assert client.post("/api/session", json={}, headers=headers).status_code == 429


def test_kill_unknown_session_is_404(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/api/session/deadbeef/kill", headers=headers).status_code == 404


def test_commands_list_requires_auth_but_not_write_token(client: TestClient) -> None:
    assert client.get("/api/commands").status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    body = client.get("/api/commands", headers=headers).json()
    assert body["count"] >= 4
    assert all(entry["builtin"] for entry in body["entries"])


def test_write_endpoints_disabled_without_write_token(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = client.post(
        "/api/commands",
        json={"name": "test", "command": ["echo", "hi"]},
        headers=headers,
    )
    assert response.status_code == 403


def test_write_requires_write_token_not_read_token(write_client: TestClient) -> None:
    read_only = {"Authorization": f"Bearer {TOKEN}"}
    response = write_client.post(
        "/api/commands",
        json={"name": "test", "command": ["echo", "hi"]},
        headers=read_only,
    )
    assert response.status_code == 403


def test_write_with_write_token_persists_entry(write_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {WRITE_TOKEN}"}
    created = write_client.post(
        "/api/commands",
        json={"name": "hello", "command": "echo hi", "group": "test"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["command"] == ["echo", "hi"]
    assert entry["builtin"] is False

    listed = write_client.get("/api/commands", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert any(e["name"] == "hello" for e in listed["entries"])

    patched = write_client.patch(f"/api/commands/{entry['id']}", json={"name": "hello2"}, headers=headers)
    assert patched.status_code == 200
    assert patched.json()["name"] == "hello2"

    deleted = write_client.delete(f"/api/commands/{entry['id']}", headers=headers)
    assert deleted.status_code == 204


def test_builtin_entries_cannot_be_deleted(write_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {WRITE_TOKEN}"}
    response = write_client.delete("/api/commands/builtin:uptime", headers=headers)
    assert response.status_code == 400


def test_invalid_command_is_rejected(write_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {WRITE_TOKEN}"}
    response = write_client.post(
        "/api/commands",
        json={"name": "broken", "command": "echo 'unterminated"},
        headers=headers,
    )
    assert response.status_code == 400


def test_audit_log_records_auth_events(client: TestClient, tmp_path) -> None:
    client.get("/whoami", headers={"Authorization": f"Bearer {TOKEN}"})
    client.post(
        "/api/commands", json={"name": "x", "command": "echo"}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    audit_file = tmp_path / "audit.log"
    assert audit_file.exists()
    contents = audit_file.read_text()
    assert "auth.ok" in contents
    assert "auth.write_denied" in contents


def test_websocket_rejects_bad_token(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    headers = {"Authorization": f"Bearer {TOKEN}"}
    session = client.post("/api/session", json={}, headers=headers).json()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/{session['id']}?token=wrong") as ws:
            ws.receive_text()
    assert session["id"] not in {}


def test_websocket_streams_pty_output_and_accepts_stdin(client: TestClient) -> None:
    import json

    from sh_agent.protocol import decode_bytes, encode_bytes

    headers = {"Authorization": f"Bearer {TOKEN}"}
    session = client.post("/api/session", json={"cols": 80, "rows": 24}, headers=headers).json()

    with client.websocket_connect(f"/ws/{session['id']}?token={TOKEN}") as ws:
        greeting = ws.receive_json()
        assert greeting["type"] == "hello"
        assert greeting["session_id"] == session["id"]

        ws.send_text(json.dumps({"type": "stdin", "data": encode_bytes(b"echo ws-marker-99\n")}))
        seen = b""
        for _ in range(60):
            frame = ws.receive_json()
            if frame.get("type") == "stdout":
                seen += decode_bytes(frame["data"])
                if b"ws-marker-99" in seen:
                    break
        assert b"ws-marker-99" in seen

        ws.send_text(json.dumps({"type": "resize", "cols": 132, "rows": 43}))
        info = client.get(f"/api/session/{session['id']}", headers=headers).json()
        assert info["cols"] == 132 and info["rows"] == 43


def test_websocket_replays_gap_on_resume(client: TestClient) -> None:
    import json

    from sh_agent.protocol import decode_bytes, encode_bytes

    headers = {"Authorization": f"Bearer {TOKEN}"}
    session = client.post("/api/session", json={}, headers=headers).json()
    url = f"/ws/{session['id']}?token={TOKEN}"

    with client.websocket_connect(url) as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "stdin", "data": encode_bytes(b"echo phase-one\n")}))
        last_seq = 0
        while True:
            frame = ws.receive_json()
            if frame.get("type") == "stdout":
                last_seq = frame["seq"]
                if b"phase-one" in decode_bytes(frame["data"]):
                    break

    with client.websocket_connect(f"{url}&resume={last_seq}") as ws:
        greeting = ws.receive_json()
        assert greeting["type"] == "hello"
        ws.send_text(json.dumps({"type": "stdin", "data": encode_bytes(b"echo phase-two\n")}))
        seen = b""
        for _ in range(60):
            frame = ws.receive_json()
            if frame.get("type") == "stdout":
                seen += decode_bytes(frame["data"])
                if b"phase-two" in seen:
                    break
        assert b"phase-two" in seen
        assert b"phase-one" not in seen


def test_websocket_unknown_session_closes(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/nosuchsession?token={TOKEN}") as ws:
            ws.receive_text()


def test_detached_session_is_reaped_after_grace(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    created = client.post("/api/session", json={}, headers=headers)
    assert created.status_code == 201
    session_id = created.json()["id"]

    registry = client.app.state.registry
    session = registry.get(session_id)
    assert session is not None

    session.detach()
    assert registry.detached_seconds(session) is not None
    registry._detach_grace = -1.0
    assert session_id in registry.reap()
    assert client.get(f"/api/session/{session_id}", headers=headers).status_code == 404


def test_attached_session_is_not_reaped_for_idle(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    created = client.post("/api/session", json={}, headers=headers)
    session_id = created.json()["id"]
    registry = client.app.state.registry
    session = registry.get(session_id)
    assert session is not None

    session.touch()
    registry._detach_grace = 0.0
    assert registry.reap() == []
    assert registry.get(session_id) is not None


def test_ring_capacity_is_configurable(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    created = client.post("/api/session", json={}, headers=headers)
    assert created.status_code == 201
    info = created.json()
    assert info["buffer_capacity"] == 32768


def test_registry_passes_ring_capacity_to_sessions() -> None:
    import asyncio

    from sh_agent.registry import SessionRegistry

    async def build() -> SessionRegistry:
        loop = asyncio.get_running_loop()
        return SessionRegistry(loop=loop, shell="/bin/sh", ring_capacity=1234)

    registry = asyncio.run(build())
    assert registry._ring_capacity == 1234


def test_killing_a_session_closes_its_websocket(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    headers = {"Authorization": f"Bearer {TOKEN}"}
    session = client.post("/api/session", json={}, headers=headers).json()

    with client.websocket_connect(f"/ws/{session['id']}?token={TOKEN}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"

        killed = client.post(f"/api/session/{session['id']}/kill", headers=headers)
        assert killed.status_code == 200

        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4404


def test_reaping_a_session_closes_its_websocket(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    headers = {"Authorization": f"Bearer {TOKEN}"}
    session = client.post("/api/session", json={}, headers=headers).json()

    with client.websocket_connect(f"/ws/{session['id']}?token={TOKEN}") as ws:
        assert ws.receive_json()["type"] == "hello"

        registry = client.app.state.registry
        registry._detach_grace = -1.0
        session_obj = registry.get(session["id"])
        assert session_obj is not None
        session_obj.detach()
        assert session["id"] in registry.reap()

        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4404


def test_audit_log_creates_missing_directory(tmp_path) -> None:
    from sh_agent.registry import AuditLog

    nested = tmp_path / "does" / "not" / "exist" / "audit.log"
    audit = AuditLog(str(nested))
    assert audit.error is None
    assert audit.enabled is True

    audit.record("probe", detail="hello")
    assert nested.exists()
    assert "probe" in nested.read_text()


def test_the_audit_log_is_private_even_under_a_loose_umask(tmp_path) -> None:
    """It records peer addresses and tailnet logins, so 0664 is not acceptable.

    The file was being created with whatever umask the agent inherited, which is
    002 on plenty of systems, and nothing about an audit log looks sensitive
    enough to check until you read what is in it.
    """
    from sh_agent.registry import AuditLog

    previous = os.umask(0o002)
    try:
        nested = tmp_path / "state" / "audit.log"
        audit = AuditLog(str(nested))
        audit.record("auth.ws_denied", peer_ip="100.64.0.9", login="someone@example.com")
    finally:
        os.umask(previous)

    assert nested.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o700


def test_an_existing_loose_audit_log_is_tightened(tmp_path) -> None:
    """A log written by an older version is still a log of who connected."""
    from sh_agent.registry import AuditLog

    path = tmp_path / "audit.log"
    path.write_text('{"event": "agent.start"}\n', encoding="utf-8")
    path.chmod(0o664)

    AuditLog(str(path))

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8") == '{"event": "agent.start"}\n'


def test_a_directory_the_user_chose_is_not_rechmodded(tmp_path) -> None:
    """SH_AUDIT can point anywhere, and that directory is not ours to alter."""
    from sh_agent.registry import AuditLog

    chosen = tmp_path / "logs"
    chosen.mkdir()
    chosen.chmod(0o755)

    AuditLog(str(chosen / "audit.log"))

    assert chosen.stat().st_mode & 0o777 == 0o755


def test_audit_log_reports_unwritable_directory(tmp_path) -> None:
    from sh_agent.registry import AuditLog

    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        audit = AuditLog(str(locked / "audit.log"))
        assert audit.enabled is False
        assert audit.error is not None
        audit.record("probe")
        assert not (locked / "audit.log").exists()
    finally:
        locked.chmod(0o700)


def test_audit_log_disabled_when_path_empty() -> None:
    from sh_agent.registry import AuditLog

    audit = AuditLog(None)
    assert audit.enabled is False
    audit.record("probe")


def test_events_are_actually_recorded(client: TestClient, tmp_path) -> None:
    audit_file = tmp_path / "fresh" / "audit.log"
    client.app.state.audit = type(client.app.state.audit)(str(audit_file))
    client.get("/whoami", headers={"Authorization": f"Bearer {TOKEN}"})
    contents = audit_file.read_text()
    assert "auth.ok" in contents


def test_reconnecting_to_a_session_does_not_accumulate_close_listeners(client: TestClient) -> None:
    """Switching back and forth is the normal path now; it must not leak sockets."""
    from sh_agent.pty_session import PtySession

    headers = {"Authorization": f"Bearer {TOKEN}"}
    session_id = client.post("/api/session", json={}, headers=headers).json()["id"]
    registry = client.app.state.registry
    session = registry.get(session_id)
    assert isinstance(session, PtySession)
    baseline = len(session._close_listeners)

    for _ in range(5):
        with client.websocket_connect(f"/ws/{session_id}?token={TOKEN}") as ws:
            ws.receive_json()

    assert len(session._close_listeners) == baseline, (
        f"listeners grew from {baseline} to {len(session._close_listeners)} across reconnects"
    )
