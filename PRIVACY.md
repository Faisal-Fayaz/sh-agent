# Privacy policy — sh-agent

Last updated: 15 October 2026.

`sh-agent` runs on a machine you own and gives your phone a shell on it. It does
not collect anything, and it has no way to: there is no server belonging to the
developer for it to report to.

The phone app is covered by the [app's privacy
policy](https://github.com/Faisal-Fayaz/sh-app/blob/main/PRIVACY.md), which
covers the same ground from the other end of the connection.

## What it does with your data

Nothing leaves your machine. The agent listens on an address you choose, accepts
connections that present the token it generated, and passes bytes between those
connections and a shell running as your user. It makes no outbound connections
of its own except the one described below, and it contains no analytics,
telemetry, or crash reporting.

If Tailscale is present, the agent asks the **local** Tailscale daemon, over a
unix socket on the same machine, who is connecting. That question is answered
locally and never leaves your computer. It is the only identity check available
to it.

## What it stores

- `~/.config/sh/agent.env` — the access token, mode 0600
- `~/.config/sh/palette.json` — the command palette, if you use one
- `~/.local/state/sh/audit.log` — session and authentication events, including
  the connecting device's address and, where Tailscale confirms it, its
  Tailscale login. Mode 0600, created that way, and tightened on start if an
  older version made it readable by others.

Deleting any of these is safe. Deleting the token file and restarting the agent
revokes every paired phone.

## Contact

Open an issue at
[github.com/Faisal-Fayaz/sh-agent/issues](https://github.com/Faisal-Fayaz/sh-agent/issues)
or email <fai504438@gmail.com>.
