from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field

from .auth import AuthConfig

DEFAULT_PORT = 8765
DEFAULT_PALETTE_PATH = os.path.expanduser("~/.config/sh/palette.json")
DEFAULT_AUDIT_PATH = os.path.expanduser("~/.local/state/sh/audit.log")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def tailnet_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("100.100.100.100", 1))
            address = probe.getsockname()[0]
        return address if address.startswith("100.") else None
    except OSError:
        return None


@dataclass
class Config:
    host: str = field(default_factory=lambda: os.environ.get("SH_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int_env("SH_PORT", DEFAULT_PORT))
    allow_lan: bool = field(
        default_factory=lambda: os.environ.get("SH_ALLOW_LAN", "").lower() in {"1", "true", "yes"}
    )
    shell: str | None = field(default_factory=lambda: os.environ.get("SH_SHELL"))
    cwd: str | None = field(default_factory=lambda: os.environ.get("SH_CWD") or None)
    palette_path: str = field(default_factory=lambda: os.environ.get("SH_PALETTE", DEFAULT_PALETTE_PATH))
    audit_path: str | None = field(default_factory=lambda: os.environ.get("SH_AUDIT", DEFAULT_AUDIT_PATH))
    idle_ttl: float = field(default_factory=lambda: _float_env("SH_IDLE_TTL", 30 * 60.0))
    detach_grace: float = field(default_factory=lambda: _float_env("SH_DETACH_GRACE", 120.0))
    reap_interval: float = field(default_factory=lambda: _float_env("SH_REAP_INTERVAL", 15.0))
    max_sessions: int = field(default_factory=lambda: _int_env("SH_MAX_SESSIONS", 8))
    ring_capacity: int = field(default_factory=lambda: _int_env("SH_RING_CAPACITY", 256 * 1024))
    auth: AuthConfig = field(default_factory=AuthConfig.from_env)

    @property
    def public_bind(self) -> str:
        if self.allow_lan:
            return self.host
        return tailnet_address() or self.host
