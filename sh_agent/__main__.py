from __future__ import annotations

import argparse
import os
import secrets

import uvicorn

from . import onboarding
from .app import create_app
from .auth import AuthConfig
from .config import Config, tailnet_address


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
    args = parser.parse_args()

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
    tailnet = tailnet_address()
    print(f"sh-agent listening on {bind}:{config.port}")
    if tailnet and bind == tailnet:
        print(f"reachable from tailnet at {bind}:{config.port}")
    if config.allow_lan:
        print("WARNING: LAN binding enabled; the daemon is reachable outside the tailnet")

    # A wildcard bind is what the agent listens on, not what a phone dials, so
    # the pairing code carries a resolved address or none at all. Printing a code
    # for 0.0.0.0 would send the user to an address that cannot work.
    host, source = onboarding.resolve_pairing_host(bind, tailnet)
    if host:
        uri = onboarding.pairing_uri(host, config.port, credentials.token, credentials.write_token)
        if credentials.generated:
            print(f"generated a new token, stored in {onboarding.DEFAULT_CONFIG_DIR / 'agent.env'}")
        if source == "lan":
            print(f"pairing over the local network ({host}); run Tailscale to pair from anywhere")
        print("\nPair your phone by scanning this, or open the link:\n")
        print(onboarding.render_qr(uri))
        print(f"\n{uri}\n")
    else:
        print(
            f"not printing a pairing code: listening on {bind}, which a phone cannot reach.\n"
            "start with SH_HOST=0.0.0.0 (shared LAN) or run Tailscale for tailnet access."
        )
    print()

    if config.auth.write_token:
        print("palette write access: ENABLED")
    else:
        print("palette write access: disabled (set SH_WRITE_TOKEN to enable)")

    uvicorn.run(create_app(config), host=bind, port=config.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
