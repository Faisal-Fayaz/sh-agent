from __future__ import annotations

import hmac
import http.client
import json
import os
import socket
from dataclasses import dataclass, field

TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"
LOCALAPI_PREFIX = "/localapi/v0"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Identity:
    peer_ip: str | None = None
    login: str | None = None
    node: str | None = None
    verified_by: str = "token"

    def as_dict(self) -> dict[str, object]:
        return {
            "peer_ip": self.peer_ip,
            "login": self.login,
            "node": self.node,
            "verified_by": self.verified_by,
        }


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class AuthConfig:
    token: str | None = None
    write_token: str | None = None
    tailscale_require: bool = field(default_factory=lambda: _bool_env("SH_TAILSCALE_REQUIRE"))
    tailscale_allow: list[str] = field(default_factory=lambda: _list_env("SH_TAILSCALE_ALLOW"))
    tailscale_socket: str = field(
        default_factory=lambda: os.environ.get("SH_TAILSCALE_SOCKET", TAILSCALE_SOCKET)
    )

    @classmethod
    def from_env(cls) -> AuthConfig:
        return cls(
            token=os.environ.get("SH_TOKEN") or None,
            write_token=os.environ.get("SH_WRITE_TOKEN") or None,
        )


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 2.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class TailscaleWhois:
    def __init__(self, socket_path: str = TAILSCALE_SOCKET) -> None:
        self._socket_path = socket_path

    def available(self) -> bool:
        return os.path.exists(self._socket_path)

    def lookup(self, addr: str) -> dict[str, object]:
        conn = _UnixHTTPConnection(self._socket_path)
        try:
            conn.request("GET", f"{LOCALAPI_PREFIX}/whois?addr={addr}")
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        if response.status != 200:
            raise AuthError(f"whois failed with status {response.status}")
        return json.loads(body)


class Authenticator:
    def __init__(self, config: AuthConfig, whois: TailscaleWhois | None = None) -> None:
        self.config = config
        self.whois = whois or TailscaleWhois(config.tailscale_socket)

    def _constant_time_equals(self, provided: str | None, expected: str | None) -> bool:
        if not expected or provided is None:
            return False
        return hmac.compare_digest(provided, expected)

    def extract_bearer(self, authorization: str | None) -> str | None:
        if not authorization:
            return None
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return None
        return value.strip()

    def identify(self, peer_ip: str | None) -> Identity:
        identity = Identity(peer_ip=peer_ip)
        if peer_ip and self.whois.available():
            try:
                data = self.whois.lookup(f"{peer_ip}:0")
            except Exception:
                data = None
            if isinstance(data, dict):
                logins = (data.get("UserProfile") or {}).get("LoginName") or []
                if logins:
                    identity.login = str(logins[0])
                    identity.node = data.get("NodeName")
                    identity.verified_by = "tailscale"
        return identity

    def _enforce_tailnet(self, identity: Identity) -> Identity:
        if self.config.tailscale_require and identity.verified_by != "tailscale":
            raise AuthError("tailscale identity required but unavailable")
        if self.config.tailscale_allow:
            if identity.verified_by != "tailscale":
                raise AuthError("peer is not on the tailnet")
            if identity.login not in self.config.tailscale_allow:
                raise AuthError(f"identity not allowed: {identity.login}")
        return identity

    def enforce(self, authorization: str | None, peer_ip: str | None) -> Identity:
        if not self.config.token:
            raise AuthError("server has no SH_TOKEN configured")
        if not self._constant_time_equals(self.extract_bearer(authorization), self.config.token):
            raise AuthError("invalid token")
        return self._enforce_tailnet(self.identify(peer_ip))

    def enforce_write(self, authorization: str | None, peer_ip: str | None) -> Identity:
        provided = self.extract_bearer(authorization)
        if not self.config.write_token:
            raise AuthError("write access disabled: set SH_WRITE_TOKEN to enable", status_code=403)
        if self._constant_time_equals(provided, self.config.write_token):
            return self._enforce_tailnet(self.identify(peer_ip))
        if self.config.token and self._constant_time_equals(provided, self.config.token):
            raise AuthError("write token required", status_code=403)
        raise AuthError("invalid token")
