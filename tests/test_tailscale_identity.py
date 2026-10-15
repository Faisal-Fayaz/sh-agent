"""Tailscale identity enforcement, exercised against a fake tailscaled socket.

Tailscale is not installed in every environment, so the localapi `whois` endpoint is
stubbed with a unix-socket HTTP server. That lets the identity allowlist, the
`require` flag, and the degraded (socket missing) path all be tested for real.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from sh_agent.auth import AuthConfig, Authenticator, AuthError, tailnet_dns_name

TOKEN = "tok-1234567890"
ALLOWED = "me@example.com"
OTHER = "someone-else@example.com"


HOST_LOGIN = {"100.64.0.1": ALLOWED, "100.64.0.2": OTHER}

# The real daemon answers 403 to any Host it does not recognise. Spelled out here
# rather than imported from the agent, so a test cannot agree with a wrong value.
EXPECTED_HOST = "local-tailscaled.sock"


class _WhoisHandler(BaseHTTPRequestHandler):
    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Host") != EXPECTED_HOST:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"forbidden")
            return
        if self.path.startswith("/localapi/v0/status"):
            # Fully qualified with a trailing dot, exactly as tailscaled sends it.
            body = json.dumps(
                {
                    "BackendState": self.server.backend_state,  # type: ignore[attr-defined]
                    "Self": {"DNSName": "faisal.example.ts.net."},
                }
            ).encode()
            self._respond(200, body)
            return
        if "addr=" not in self.path:
            self.send_response(404)
            self.end_headers()
            return
        host = self.path.split("addr=")[-1].split(":")[0]
        login = HOST_LOGIN.get(host)
        if login is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        body = json.dumps(
            {
                "Node": {"Name": f"node-{host}.example.ts.net.", "ComputedName": f"node-{host}"},
                # A bare string, which is what the real localapi returns. A list
                # here let the agent read its first character and still pass.
                "UserProfile": {"LoginName": login},
            }
        ).encode()
        self._respond(200, body)

    def log_message(self, *args: object) -> None:
        return


class _UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, backend_state: str = "Running") -> None:
        self.unix_path = path
        self.backend_state = backend_state
        super().__init__(("localhost", 0), _WhoisHandler)

    def server_bind(self) -> None:
        self.socket.close()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(self.unix_path)

    def server_close(self) -> None:
        super().server_close()
        try:
            os.unlink(self.unix_path)
        except OSError:
            pass


class FakeTailscaled:
    def __init__(self, path: str, backend_state: str = "Running") -> None:
        self.path = path
        self.backend_state = backend_state

    def __enter__(self) -> FakeTailscaled:
        self._server = _UnixHTTPServer(self.path, self.backend_state)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def socket_path() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.join(tmp, "tailscaled.sock")


def make_auth(path: str, **kwargs) -> Authenticator:
    config = AuthConfig(token=TOKEN, write_token=None, tailscale_socket=path, **kwargs)
    return Authenticator(config)


def test_whois_resolves_login(socket_path: str) -> None:
    with FakeTailscaled(socket_path):
        auth = make_auth(socket_path)
        identity = auth.identify("100.64.0.1")
        assert identity.login == ALLOWED
        assert identity.verified_by == "tailscale"
        assert identity.node == "node-100.64.0.1"


def test_unknown_peer_is_not_on_the_tailnet(socket_path: str) -> None:
    with FakeTailscaled(socket_path):
        auth = make_auth(socket_path)
        identity = auth.identify("10.0.0.5")
        assert identity.login is None
        assert identity.verified_by == "token"


def test_missing_socket_degrades_to_token_only(socket_path: str) -> None:
    auth = make_auth(socket_path)
    assert auth.whois.available() is False
    identity = auth.identify("100.64.0.1")
    assert identity.verified_by == "token"


def test_allowlist_permits_listed_identity(socket_path: str) -> None:
    with FakeTailscaled(socket_path):
        auth = make_auth(socket_path, tailscale_allow=[ALLOWED])
        identity = auth.enforce(f"Bearer {TOKEN}", "100.64.0.1")
        assert identity.login == ALLOWED


def test_allowlist_rejects_other_identity(socket_path: str) -> None:
    with FakeTailscaled(socket_path):
        auth = make_auth(socket_path, tailscale_allow=[ALLOWED])
        with pytest.raises(AuthError) as excinfo:
            auth.enforce(f"Bearer {TOKEN}", "100.64.0.2")
        assert "not allowed" in str(excinfo.value)
        assert excinfo.value.status_code == 401


def test_require_flag_rejects_when_socket_absent(socket_path: str) -> None:
    auth = make_auth(socket_path, tailscale_require=True)
    with pytest.raises(AuthError) as excinfo:
        auth.enforce(f"Bearer {TOKEN}", "100.64.0.1")
    assert "tailscale identity required" in str(excinfo.value)


def test_require_flag_rejects_off_tailnet_peer(socket_path: str) -> None:
    with FakeTailscaled(socket_path):
        auth = make_auth(socket_path, tailscale_require=True)
        with pytest.raises(AuthError):
            auth.enforce(f"Bearer {TOKEN}", "10.0.0.5")


def test_token_still_checked_before_identity(socket_path: str) -> None:
    with FakeTailscaled(socket_path):
        auth = make_auth(socket_path, tailscale_allow=[ALLOWED])
        with pytest.raises(AuthError) as excinfo:
            auth.enforce("Bearer wrong", "100.64.0.1")
        assert "invalid token" in str(excinfo.value)


def test_write_token_gate_is_independent(socket_path: str) -> None:
    with FakeTailscaled(socket_path):
        auth = Authenticator(AuthConfig(token=TOKEN, write_token="", tailscale_socket=socket_path))
        with pytest.raises(AuthError) as excinfo:
            auth.enforce_write(f"Bearer {TOKEN}", "100.64.0.1")
        assert excinfo.value.status_code == 403


def test_magic_dns_name_is_read_without_the_root_dot(socket_path: str) -> None:
    """A pairing code wants a dialable name, not "faisal.example.ts.net."."""
    with FakeTailscaled(socket_path):
        assert tailnet_dns_name(socket_path) == "faisal.example.ts.net"


def test_magic_dns_name_is_absent_when_the_socket_is(socket_path: str) -> None:
    """No Tailscale is an ordinary state, not something to fail a pairing over."""
    assert tailnet_dns_name(socket_path) is None


def test_magic_dns_name_is_absent_when_the_backend_is_down(socket_path: str) -> None:
    """A logged-out daemon still has a socket and still answers, with no name to give."""
    with FakeTailscaled(socket_path, backend_state="Stopped"):
        assert tailnet_dns_name(socket_path) is None
