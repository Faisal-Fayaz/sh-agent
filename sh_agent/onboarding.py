"""Persisted credentials and phone pairing.

The token used to be generated, printed and thrown away, which meant closing the
terminal that started the agent invalidated whatever the phone had already saved
with no way to recover it. It is now written to a file the agent reads on the
next start, and the phone pairs by scanning a QR code rather than by someone
reading an IP off one line and typing it into a phone keyboard.
"""

from __future__ import annotations

import io
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

DEFAULT_CONFIG_DIR = Path(os.environ.get("SH_CONFIG_DIR") or Path.home() / ".config" / "sh")
SCHEME = "sh"


@dataclass
class Credentials:
    token: str
    write_token: str | None = None
    generated: bool = False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines, ignoring anything unparseable.

    This file is written by us, but a user editing it by hand should not get a
    stack trace because of a stray comment or a blank line.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip("'\"")
        if value:
            values[key] = value
    return values


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written literally, not percent-encoded: systemd parses EnvironmentFile as
    # KEY=value, so encoding here would corrupt the token it hands the agent.
    # token_urlsafe only ever emits [A-Za-z0-9_-], which needs no quoting.
    lines = []
    for key, value in values.items():
        if "\n" in value or "\r" in value or " " in value:
            raise ValueError(f"refusing to persist {key}: value is not safe for an env file")
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The token is a shell credential; 0600 so it is not world-readable, and a
    # directory that does not leak its contents.
    path.chmod(0o600)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def load_or_create(
    config_dir: Path | None = None,
    env_token: str | None = None,
    env_write_token: str | None = None,
) -> Credentials:
    """Resolve credentials from the environment, falling back to a stored file.

    Precedence is environment, then a previously stored token, then a freshly
    generated one. Only the generated token is ever written back: an environment
    variable is a per-run override, and persisting it would let a value that
    happened to be set for one invocation -- a test harness, a shell export --
    silently become permanent configuration.
    """
    directory = config_dir or DEFAULT_CONFIG_DIR
    path = directory / "agent.env"

    stored = _parse_env_file(path) if path.exists() else {}

    token = env_token or stored.get("SH_TOKEN") or ""
    write_token = env_write_token or stored.get("SH_WRITE_TOKEN") or None
    generated = False

    if not token:
        token = generate_token()
        generated = True
        stored["SH_TOKEN"] = token
        try:
            _write_env_file(path, stored)
        except OSError:
            # A read-only home should not stop the agent from running; the token
            # is printed in that case so the user can still pair.
            pass

    return Credentials(token=token, write_token=write_token, generated=generated)


def local_address() -> str | None:
    """The address this machine reaches the outside world from.

    Used to turn a wildcard bind into something a phone can actually dial. The
    UDP connect sends no packets; it just asks the routing table which local
    address would be used, which avoids enumerating interfaces.
    """
    import socket

    for probe in ("100.100.100.100", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.2)
                sock.connect((probe, 1))
                return str(sock.getsockname()[0])
        except OSError:
            continue
    return None


def resolve_pairing_host(bind: str, tailnet: str | None = None) -> tuple[str | None, str]:
    """Pick an address to put in the pairing code, and say why.

    A wildcard bind is what the agent listens on, not what a phone dials, so it
    never appears in a pairing code. The tailnet address is preferred over the
    LAN one because it is the only one that also works off the home network.
    """
    if tailnet and bind != "127.0.0.1":
        return tailnet, "tailnet"
    if bind not in ("0.0.0.0", "::", ""):
        if bind in ("127.0.0.1", "::1", "localhost"):
            return None, "loopback"
        return bind, "configured"
    address = local_address()
    if address:
        return address, "lan"
    return None, "unknown"


def pairing_uri(host: str, port: int, token: str, write_token: str | None = None) -> str:
    """A link the phone can open or scan.

    Percent-encoded rather than JSON so the QR payload stays a plain URI that a
    camera app, a browser and the app's own deep-link handler all understand.
    """
    parts = [f"host={quote(host, safe='')}", f"port={port}", f"token={quote(token, safe='')}"]
    if write_token:
        parts.append(f"wt={quote(write_token, safe='')}")
    return f"{SCHEME}://connect?{'&'.join(parts)}"


def render_qr(uri: str, border: int = 2) -> str:
    """A scannable QR in the terminal.

    segno renders to text with no image dependency, which matters for a package
    whose whole job is to run on whatever machine someone has. It writes to the
    stream it is given and returns None, so it is handed a buffer rather than
    being allowed to print behind the caller's back.

    compact=True draws each module as a half-block glyph instead of a coloured
    space. The default output depends on the terminal honouring ANSI colour and
    turns into unreadable escape codes the moment it is piped to a file, run
    under tmux, or read by a camera that cannot resolve the colour.
    """
    try:
        import segno
    except ImportError:  # pragma: no cover - segno is a declared dependency
        return ""
    buffer = io.StringIO()
    segno.make(uri, error="m").terminal(out=buffer, border=border, compact=True)
    return buffer.getvalue()
