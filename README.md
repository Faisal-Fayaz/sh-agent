# sh-agent

Tailscale-reachable PTY daemon. Gives a phone (or any tailnet peer) an interactive shell plus a
gesture-friendly command palette API.

Status: **M1 complete** — agent, protocol, palette CRUD, auth, tests.

## Quick start

```bash
# uv (lockfile committed, reproducible)
uv venv && uv pip install -e ".[dev]"

# or plain pip
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

export SH_TOKEN="$(.venv/bin/python -m sh_agent --generate-token)"
export SH_WRITE_TOKEN="$(.venv/bin/python -m sh_agent --generate-token)"   # optional
./.venv/bin/python -m sh_agent
```

With Tailscale running and no `SH_HOST` set, the agent binds to your tailnet address (`100.x.y.z`)
rather than `0.0.0.0`, so it is never exposed off-tailnet. Set `SH_ALLOW_LAN=1` to override (dev only).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SH_TOKEN` | — | **Required.** Bearer token for all access |
| `SH_WRITE_TOKEN` | unset | Enables palette write endpoints. Unset ⇒ 403 |
| `SH_HOST` | `127.0.0.1` | Bind address when not on a tailnet |
| `SH_PORT` | `8765` | Listen port |
| `SH_ALLOW_LAN` | unset | Bind `0.0.0.0`. Dev only |
| `SH_SHELL` | `/bin/bash -l` | Shell to spawn |
| `SH_CWD` | — | Default working directory |
| `SH_PALETTE` | `~/.config/sh/palette.json` | Palette file |
| `SH_AUDIT` | `~/.local/state/sh/audit.log` | Audit log (`SH_AUDIT=""` disables) |
| `SH_IDLE_TTL` | `1800` | Seconds before an idle session is reaped |
| `SH_DETACH_GRACE` | `120` | Seconds a disconnected session survives for reconnect/resume |
| `SH_REAP_INTERVAL` | `15` | How often the reaper runs |
| `SH_MAX_SESSIONS` | `8` | Concurrent session cap |
| `SH_RING_CAPACITY` | `262144` | Replay buffer bytes per session |
| `SH_TAILSCALE_REQUIRE` | unset | Reject peers whose identity can't be verified |
| `SH_TAILSCALE_ALLOW` | unset | Comma-separated allowlist of tailnet logins |

## Protocol

WebSocket at `/ws/{session_id}`. Token via `Authorization: Bearer <token>` header or `?token=`.
All PTY bytes are **base64-encoded** — a UTF-8 sequence can straddle a frame boundary, and raw text
frames corrupt output.

### Client → server

```json
{"type": "stdin",  "data": "<base64>"}
{"type": "resize", "cols": 132, "rows": 43}
{"type": "kill"}
{"type": "ping"}
```

### Server → client

```json
{"type": "hello",  "session_id": "…", "shell": "/bin/bash -l", "cols": 80, "rows": 24}
{"type": "stdout", "seq": 4096, "data": "<base64>"}
{"type": "exit",   "code": 0}
{"type": "error",  "message": "…"}
{"type": "pong"}
```

`seq` is a monotonic **byte offset** into the session's output stream, not a frame counter.

### Reconnect

Disconnecting the WebSocket does not kill the shell. Reconnect with `?resume=<seq>` where `seq` is
the last byte offset you received; the agent replays exactly the missed range. If the requested
offset has already been evicted from the ring buffer, replay starts from the oldest retained byte
and the client must treat the terminal as resynced. This is the mechanism for surviving mobile
network flaps.

### Close codes

`4401` auth failed · `4404` unknown session

## HTTP API

| Method | Path | Auth |
|---|---|---|
| `GET` | `/health` | open |
| `GET` | `/whoami` | token |
| `GET` | `/audit/recent?limit=` | token |
| `POST` | `/api/session` | token |
| `GET` | `/api/session/{id}` | token |
| `POST` | `/api/session/{id}/kill` | token |
| `GET` | `/api/commands` | token |
| `POST` | `/api/commands` | **write token** |
| `PATCH` | `/api/commands/{id}` | **write token** |
| `DELETE` | `/api/commands/{id}` | **write token** |

## Security model

1. Reachable only over the tailnet unless `SH_ALLOW_LAN` is set.
2. Bearer token compared with `hmac.compare_digest`; never logged.
3. Tailscale identity resolution via the `tailscaled.sock` localapi `whois` endpoint, so the agent
   can allowlist by login even if the token leaks to another node on the tailnet.
4. Palette writes require a distinct token and are off by default.
5. Built-in palette entries are immutable and cannot be shadowed in place — only overridden by name.
6. Every auth event, session lifecycle event, and palette mutation is appended to the audit log.
7. Sessions are capped and reaped; children are killed via `killpg` so no shell outlives its
   session.

### Socket lifetime

Killing or reaping a session **closes its WebSocket** with code `4404`. Without that, the client
would sit on a socket to a dead shell and never learn; the close code is what lets it start a
replacement shell and tell the user it happened.

### Session lifetime

Two independent reapers, because "idle" means different things in the two states:

- **Attached but idle** (`SH_IDLE_TTL`, 30 min) — a user walked away from a live terminal.
- **Detached** (`SH_DETACH_GRACE`, 2 min) — the WebSocket dropped. This is deliberately short but
  long enough to survive a mobile network flap, because `?resume=` can only replay a session that
  still exists.

Without the detach grace, every app reload leaks a session until the 30-minute idle reaper runs, and
a hot-reload dev loop hits `SH_MAX_SESSIONS` within minutes.

## Development

```bash
.venv/bin/python -m pytest -q          # 43 unit + API tests, incl. a fake tailscaled socket
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .

./scripts/serverctl.sh start           # pidfile-managed dev server
./scripts/serverctl.sh stop

# live end-to-end checks (need a running agent)
export SH_TOKEN=$(.venv/bin/python -m sh_agent --generate-token)
./scripts/serverctl.sh start
.venv/bin/python scripts/verify_m1.py               # 23 checks: shell, resize, auth, resume
.venv/bin/python scripts/verify_resume.py           # 10 checks: drop mid-stream, zero loss
.venv/bin/python scripts/verify_resume_eviction.py  # 11 checks: resume from an evicted offset
```

## Roadmap

- **M2** — Expo app: xterm.js in a WebView, bidirectional keystrokes, ANSI rendering
- **M3** — Gesture layer: two-finger arrows, swipe `Ctrl-C`/`Ctrl-D`, sticky modifiers, haptics
- **M4** — Command palette UI: swipe-to-run, host-synced entries
- **M5** — Tailscale identity enforcement, systemd user unit, reconnect hardening
