from __future__ import annotations

import argparse
import os
import secrets

import uvicorn

from . import onboarding
from .app import create_app
from .auth import AuthConfig, tailnet_dns_name
from .config import Config, tailnet_address


def _pairing_banner(
    bind: str,
    port: int,
    token: str,
    write_token: str | None,
    generated: bool = False,
) -> None:
    """Print the address, a scannable code, and a copy-pasteable link.

    Every line is flushed as it is produced. The agent runs forever afterwards, so
    anything left in a buffer would never reach the user -- and the QR is the one
    piece of output somebody cannot wait for.
    """
    tailnet = tailnet_address()
    name = tailnet_dns_name() if tailnet else None
    print(f"sh-agent listening on {bind}:{port}", flush=True)
    if tailnet and bind == tailnet:
        print(f"reachable from tailnet at {name or bind}:{port}", flush=True)

    host, source = onboarding.resolve_pairing_host(bind, tailnet, name)
    if not host:
        print(
            f"not printing a pairing code: bound to {bind}, which a phone cannot reach.\n"
            "\n"
            "Drop SH_HOST and the agent picks an address your phone can dial:\n"
            "    sh-agent\n"
            "\n"
            "Or bind every interface yourself:\n"
            "    SH_HOST=0.0.0.0 sh-agent",
            flush=True,
        )
        return

    uri = onboarding.pairing_uri(host, port, token, write_token)
    if generated:
        print(
            f"generated a new token, stored in {onboarding.DEFAULT_CONFIG_DIR / 'agent.env'}",
            flush=True,
        )
    if source == "lan":
        print(
            f"pairing over the local network ({host}); run Tailscale to pair from anywhere",
            flush=True,
        )
    print("\nPair your phone by scanning this, or open the link:\n", flush=True)
    print(onboarding.render_qr(uri), flush=True)
    print(f"\n{uri}\n", flush=True)
    if tailnet and host != tailnet:
        print(
            f"the code uses your tailnet name; if {host} does not resolve on the phone,\n"
            f"enter {tailnet}:{port} by hand instead.",
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(prog="sh-agent")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--allow-lan", action="store_true")
    parser.add_argument("--shell", default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--palette", default=None)
    parser.add_argument("--audit", default=None)
    parser.add_argument("--generate-token", action="store_true")
    parser.add_argument("--print-bind", action="store_true")
    parser.add_argument(
        "--pairing",
        action="store_true",
        help="print the pairing code for the current token and exit",
    )
    args = parser.parse_args()

    # Before Config() reads the environment: stored settings have to apply the
    # same way however the agent was started.
    onboarding.apply_stored_env()

    if args.generate_token:
        print(secrets.token_urlsafe(32))
        return 0

    config = Config()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.allow_lan:
        config.allow_lan = True
    if args.shell:
        config.shell = args.shell
    if args.cwd:
        config.cwd = args.cwd
    if args.palette:
        config.palette_path = args.palette
    if args.audit:
        config.audit_path = args.audit
    config.auth = Config().auth

    if args.print_bind:
        print(config.public_bind)
        return 0

    # Resolve credentials before building the auth config, so a token generated on
    # this run is the one the agent actually serves. Environment wins, then a
    # previously stored token, then a freshly generated one that gets persisted --
    # a generated token that is not stored leaves the phone's saved copy invalid
    # the moment this terminal closes, with no way to recover it.
    credentials = onboarding.load_or_create(
        env_token=os.environ.get("SH_TOKEN"),
        env_write_token=os.environ.get("SH_WRITE_TOKEN"),
    )
    config.auth = AuthConfig(
        token=credentials.token,
        write_token=credentials.write_token,
        tailscale_require=config.auth.tailscale_require,
        tailscale_allow=config.auth.tailscale_allow,
        tailscale_socket=config.auth.tailscale_socket,
    )

    bind = config.public_bind

    # --pairing reprints the code without starting a server, so a second phone can
    # be added, or a lost one replaced, without touching the host.
    if args.pairing:
        _pairing_banner(bind, config.port, credentials.token, credentials.write_token, credentials.generated)
        return 0

    if bind in ("0.0.0.0", "::"):
        print(
            "WARNING: listening on every interface, so anything on this network can reach the agent.\n"
            "         the pairing token is still required. set SH_HOST=127.0.0.1 to stay on this machine.",
            flush=True,
        )

    _pairing_banner(bind, config.port, credentials.token, credentials.write_token, credentials.generated)

    if config.auth.write_token:
        print("palette write access: ENABLED", flush=True)
    else:
        print("palette write access: disabled (set SH_WRITE_TOKEN to enable)", flush=True)

    uvicorn.run(create_app(config), host=bind, port=config.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
