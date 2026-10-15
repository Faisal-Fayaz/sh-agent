# sh-agent

A real interactive shell on your own machine, driven from your phone. Run the agent on the
machine you want a shell into, install the app on your phone, scan a code, and you have a
terminal that keeps its scrollback across a dropped connection.

The client is [`sh-app`](https://github.com/Faisal-Fayaz/sh-app), an Expo app for Android and iOS.

## Who this is for

Someone already uses a terminal, wants their phone to be a genuinely usable one, and is
comfortable installing a small program on their own machine and a VPN on two devices.

## What you get

- **Two-finger gestures** for the keys a phone keyboard hides: arrows, `Ctrl-C`, `Ctrl-D`,
  `Esc`, `Ctrl-Z`, plus a sticky modifier.
- **Pinch to resize the text**, so a narrow phone screen is not a cramped one.
- **Named tmux sessions** that survive the app being closed, so a long build keeps running.
- **Reconnect that waits.** A host that is asleep or out of cell range is retried
  indefinitely rather than written off after 40 seconds.
- **A swipe-to-run command palette** for the commands you actually type often.

## What this is not, yet

- **No Windows.** The agent is POSIX-only (`openpty`, `TIOCSCTTY`, `killpg`). Windows needs a
  ConPTY layer and does not have one.
- **The host needs Python 3.11+**, macOS or Linux.
- **It does not wake your machine.** A sleeping host serves nothing. Tailscale gets you
  reachability from anywhere; it does not get you a powered-on laptop.
- **iOS builds need an Apple account.** Expo Go works for trying it; a standalone build needs
  the Apple Developer Program.

Everything above is a deliberate boundary, not a bug in disguise.

## Try it in five minutes

You need Python 3.11+ on the host, the app on your phone, and either Tailscale (works from
anywhere) or being on the same Wi-Fi (works at home).

Install the app first. [Expo Go](https://expo.dev/go) is quickest on both platforms — run
`npx expo start` in the `sh-app` repo and scan its QR with the app. If you would rather not run a
dev server, the client's README has a standalone Android APK and says what an iOS build needs.

```bash
uv tool install sh-agent          # or: pipx install sh-agent

sh-agent                          # prints a pairing code; Ctrl-C to stop
```

It prints a code your phone scans. With Tailscale running on this machine and your phone it uses
your tailnet address, so the code keeps working away from home; otherwise it uses your local
network address, which works while both are on the same Wi-Fi. It says which one it picked.

Both come from [PyPI](https://pypi.org/project/sh-agent) and need only Python 3.11+. If you would
rather not have a package manager involved:

```bash
curl -fsSL https://raw.githubusercontent.com/Faisal-Fayaz/sh-agent/main/scripts/install.sh | bash
```

That script picks `uv` or `pipx` if you have either, and falls back to a virtualenv if you do not.
Running `./scripts/install.sh` from a clone does the same thing.

There is no token to generate and nothing to copy into your shell. The agent creates one on
first run, stores it in `~/.config/sh/agent.env` (mode 0600), and prints a QR code. Open the app,
tap **Scan to pair**, and point your phone at it:

```
sh-agent listening on 0.0.0.0:8765
pairing over the local network (172.20.10.3); run Tailscale to pair from anywhere

Pair your phone by scanning this, or open the link:

█████████████████████████████████████████████
██ ▄▄▄▄▄ █ ▀ ▄▄▄▀▄ ▀█▀▀▄▀ ▀██▀▀▀█ ▄▄▄▄▄ ██
...

sh://connect?host=172.20.10.3&port=8765&token=...
```

The token persists, so restarting the agent never unpairs a phone. To get the code again later --
to add a second phone, or after deleting `agent.env` -- without restarting anything:

```bash
sh-agent --pairing
```

If you start the agent through `scripts/serverctl.sh` the code is echoed to your terminal after
the health check, since that script backgrounds the agent with its output redirected to a log.

With Tailscale running the same command needs no `SH_HOST` at all: the agent binds your `100.x`
tailnet address and the pairing code carries that instead, which is the only version that also
works once you leave the house.

Then scan the code with the app — tap **Scan to pair** and point the camera at the terminal. If
the camera is unavailable, **Enter manually** takes the same host, port and token by hand.

Verify it worked:

```bash
curl -s http://<host>:8765/health | python3 -m json.tool
```

## Running from a clone

For development, where you want the tests and a local checkout:

```bash
# uv (lockfile committed, reproducible)
uv venv && uv pip install -e ".[dev]"

# or plain pip
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

./.venv/bin/python -m sh_agent
```

There is no `SH_TOKEN` to set. To pin one for a single run instead, export `SH_TOKEN`; it
overrides the stored value without replacing it.

With Tailscale running and no `SH_HOST` set, the agent binds to your tailnet address (`100.x.y.z`)
rather than `0.0.0.0`, so it is never exposed off-tailnet. Set `SH_ALLOW_LAN=1` to override (dev only).

## Running it always

A phone client is only useful if the agent is up when you reach for it, and a shell
started by hand dies with the terminal. A **user** unit needs no root and survives
reboots once you log in or enable lingering:

```bash
uv tool install sh-agent          # or: uv tool install --force .
install -Dm755 systemd/sh-agent.service ~/.config/systemd/user/sh-agent.service
systemctl --user daemon-reload && systemctl --user enable --now sh-agent
loginctl enable-linger "$USER"    # optional: keep it running without a login session
```

`uv tool install` puts the `sh-agent` binary on `PATH` at `~/.local/bin`, which is what the unit
runs. The token is not in the unit at all: run the agent once and it writes
`~/.config/sh/agent.env` (mode 0600), which the unit then loads. That is what stops restarting
the agent from invalidating a phone that is already paired.

**One ordering caveat.** `tailscaled` is a system unit, so a user unit cannot order
itself against it. If the agent wins that race on boot it binds `127.0.0.1` instead of
the tailnet address, which is silently unreachable from the phone. The agent prints
the address it bound to, so check that line after a reboot; if it says `127.0.0.1`
while you are on a tailnet, run `systemctl --user restart sh-agent`.

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
| `SH_TMUX_ENABLED` | `1` | Offer tmux-backed named sessions |
| `SH_TMUX_SECRET` | derived from `SH_TOKEN` | Secret for namespacing tmux session names |
| `SH_TMUX_MAX_SESSIONS` | `12` | Cap on persisted tmux sessions |
| `SH_TMUX_IDLE_TTL` | `3600` | Seconds an unattached tmux session may linger (see below) |

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

## Two session modes

| | `pty` (default) | `tmux` |
|---|---|---|
| Persistence | Survives a disconnect for `SH_DETACH_GRACE` | Survives indefinitely |
| Identity | Anonymous, per connection | Named, stable |
| Reconnect | Byte-offset replay into a ring buffer | `tmux new-session -A` reattaches |
| Scrollback | Replayed since the last disconnect | tmux's own, including across app restarts |
| Best for | Ad-hoc commands | Long-running work you come back to |

tmux mode is the same `PtySession` with its command set to `tmux new-session -A -s <name>`, so it
reuses the entire streaming path rather than introducing a second one.

**Namespacing.** `tmux new-session -A` attaches to *any* session with a matching name, so an
un-namespaced slug would let anyone holding a token attach to another holder's session. Slugs are
therefore mapped to `sh-<hmac8(secret, slug)>-<slug>`: readable in `tmux ls`, unguessable to a
client that does not hold the secret. The default secret derives from `SH_TOKEN`, so everyone
sharing a token shares a namespace — set `SH_TMUX_SECRET` for isolation.

**Lifecycle.** tmux sessions outlive everything, which is the point and the hazard. The sweeper caps
them at `SH_TMUX_MAX_SESSIONS` and only ever kills sessions with **no attached client** — an
explicit `kill` removes the tmux session itself, while `detach` deliberately leaves it running.

**Attaching is a switch, not a clone.** tmux will happily attach several clients to one session, so
tapping a session repeatedly would otherwise add a PTY and a registry row every time. Creating a
tmux session now *replaces* any session the agent already tracks under that name: the old socket is
closed, its tmux clients are detached, and the new one attaches. The tmux session and its scrollback
are untouched, so reconnecting mid-task is seamless. `/api/sessions` also omits any slug that is
already listed as live, so one session can never read as two rows.

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
.venv/bin/python -m pytest -q          # 99 unit + API tests, incl. a fake tailscaled socket
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
.venv/bin/python scripts/verify_tmux.py                # 27 checks: named sessions end to end
```

New API:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sessions` | List live PTY sessions and persisted tmux sessions |
| `POST` | `/api/session/{id}/detach` | Drop the socket but keep a tmux session running |
| `DELETE` | `/api/persisted/{slug}` | Remove a detached tmux session (it has no session id left to kill by) |

## Terminal size

Sessions are created at 80x24 and resized as the client measures itself. That resize only
reaches full-screen programs if the spawned shell owns its controlling terminal: the agent
uses `os.openpty()` plus `setsid()`, and without `TIOCSCTTY` there is no foreground process
group, so `TIOCSWINSZ` updates the stored size but never delivers `SIGWINCH`. A plain shell
reads the size when asked and so never notices; tmux, vim and other full-screen programs only
re-read the terminal when signalled, and would keep rendering for their startup size. With
the controlling terminal in place, tmux follows the client and its `window-size latest`
option does the right thing on its own.

`preexec_fn` targets the slave fd rather than stdin because CPython runs it before it dup2's
the standard streams, so fd 0 is not yet the pty. The agent is single-threaded, which is the
one thing that would make `preexec_fn` unsafe.

## Roadmap

The features are built and verified on hardware, the agent is on PyPI, and the app has a
standalone Android build. What remains is reach.

- **Windows.** The one real gap. The agent is POSIX-only -- `os.openpty`, `TIOCSCTTY` and
  `killpg` have no Windows equivalent and there is no ConPTY layer, so this is an
  abstraction to write rather than a branch to patch. macOS and Linux are supported today.
- **Store distribution.** The Android build is sideloaded, so Play Protect warns about it, and
  iOS needs the Apple Developer Program. Both are accounts and review queues, not code.
- **Wake on demand.** Not implemented, and not a small thing. Wake-on-LAN is a Layer 2
  magic packet and Tailscale is Layer 3, so it cannot be sent over the tunnel at all --
  it needs a second always-on machine whose only job is sending the packet. That machine
  can just as well run the agent.

Earlier milestones -- the PTY agent, the Expo client, the gesture layer, the command palette,
Tailscale identity, reconnect hardening, and the pairing bootstrap -- are all done.
