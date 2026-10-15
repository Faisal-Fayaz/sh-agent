# Changelog

Notable changes to `sh-agent`, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.2 — 2026-10-15

### Fixed

- **Tailscale identity never actually worked against a real `tailscaled`.**
  Three separate bugs, all hidden by a fake socket that was too forgiving:

  - The LocalAPI request sent `Host: localhost`, which `tailscaled` refuses
    with a 403 — its DNS-rebinding guard. It now sends `Host:
    local-tailscaled.sock`, and the fake answers 403 for anything else, so the
    same mistake cannot pass again.
  - The node name was read from a top-level `NodeName` that does not exist; it
    lives under `Node`, as `ComputedName`.
  - `LoginName` was indexed as a list when it is a bare string, so
    `Faisal-Fayaz@github` came back as `F`. Every entry in
    `SH_TAILSCALE_ALLOW` was therefore unmatchable.

  The failure was closed rather than open — with `SH_TAILSCALE_REQUIRE=1` every
  peer is refused — but it made the remote path this is for unusable.

- A tailnet address was put in the pairing code even when the agent was not
  bound to it, so `SH_HOST=192.168.1.50` produced a code naming an address
  nothing was listening on. The tailnet is now offered only when actually bound.

### Added

- The pairing code carries the MagicDNS name rather than a raw address, with the
  `100.x` address printed underneath, because the name survives an address
  change and the address is what you type if resolution fails.

### Changed

- Stored settings in `~/.config/sh/agent.env` apply however the agent was
  started, not only when it was started by the systemd unit. Starting it by hand
  used to silently ignore them.
- Credentials are read from that file by the agent itself rather than being
  injected into its environment by the unit, so a shell the agent spawns can no
  longer inherit the token.

## 0.1.1 — 2026-10-15 · yanked

Yanked from PyPI. It was built and uploaded before the Tailscale fixes above
landed, so it verifies no peer correctly. Install 0.1.2 instead.

### Added

- An agent with no Tailscale and no `SH_HOST` binds every interface and prints a
  pairing code, rather than binding loopback and printing nothing a phone can
  use. It says loudly that it is listening on every interface. `SH_HOST` remains
  the way to opt out.

### Fixed

- `/health` and the OpenAPI schema reported a version string written out by
  hand, which had already drifted from the package version. It is now read from
  the installed metadata.

## 0.1.0 — 2026-10-14

First release.

- Interactive shell sessions over WebSocket, one pty per session, with a ring
  buffer so a phone that drops off a train can reconnect and replay what it
  missed.
- Pairing by scanning a QR code instead of retyping a token.
- Token authentication, with an optional separate `SH_WRITE_TOKEN` gating the
  palette endpoints.
- Tailscale peer verification through the `tailscaled` LocalAPI, with
  `SH_TAILSCALE_REQUIRE` to refuse anything that cannot be verified and
  `SH_TAILSCALE_ALLOW` to restrict it to named logins.
- tmux-backed named sessions that outlive the app being closed.
- An audit log of session and authentication events.
