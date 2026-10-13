from __future__ import annotations

import argparse
import os
import secrets
import sys

import uvicorn

from .app import create_app
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

    if not config.auth.token:
        token = os.environ.get("SH_TOKEN")
        if not token:
            print(
                "refusing to start: SH_TOKEN is not set.\n"
                "generate one with: python -m sh_agent --generate-token",
                file=sys.stderr,
            )
            return 2

    bind = config.public_bind
    tailnet = tailnet_address()
    print(f"sh-agent listening on {bind}:{config.port}")
    if tailnet and bind == tailnet:
        print(f"reachable from tailnet at ws://{tailnet}:{config.port}")
    if config.allow_lan:
        print("WARNING: LAN binding enabled; the daemon is reachable outside the tailnet")
    if config.auth.write_token:
        print("palette write access: ENABLED")
    else:
        print("palette write access: disabled (set SH_WRITE_TOKEN to enable)")

    uvicorn.run(create_app(config), host=bind, port=config.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
