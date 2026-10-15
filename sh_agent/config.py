from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field

from .auth import AuthConfig
from .env import bool_env as _bool_env
from .env import float_env as _float_env
from .env import int_env as _int_env

DEFAULT_PORT = 8765
DEFAULT_PALETTE_PATH = os.path.expanduser("~/.config/sh/palette.json")
DEFAULT_AUDIT_PATH = os.path.expanduser("~/.local/state/sh/audit.log")


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
    host: str = field(default_factory=lambda: os.environ.get("SH_HOST") or "")
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
    tmux_enabled: bool = field(default_factory=lambda: _bool_env("SH_TMUX_ENABLED", True))
    tmux_max_sessions: int = field(default_factory=lambda: _int_env("SH_TMUX_MAX_SESSIONS", 12))
    tmux_idle_ttl: float = field(default_factory=lambda: _float_env("SH_TMUX_IDLE_TTL", 3600.0))

    @property
    def tmux_secret(self) -> str:
        configured = os.environ.get("SH_TMUX_SECRET")
        if configured:
            return configured
        return self.auth.token or "sh-default"

    @property
    def public_bind(self) -> str:
        """The address to actually listen on.

        An explicit SH_HOST is always honoured, so nothing here surprises anyone
        who set one. Otherwise Tailscale wins, because a tailnet address is the
        only one that also works away from home. With neither, the fallback is
        every interface: loopback would leave the phone with nothing to dial,
        and an agent started for a phone that no phone can reach has failed at
        the only thing it was started to do.
        """
        if self.host:
            return self.host
        if self.allow_lan:
            return "0.0.0.0"
        return tailnet_address() or "0.0.0.0"
