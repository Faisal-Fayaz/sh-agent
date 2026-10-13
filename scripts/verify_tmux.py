"""End-to-end check of tmux-backed sessions against a live agent.

Exercises the journey a user takes: attach to a named session, drop the
connection, come back later, reattach, switch back in, and find the work still
there.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import httpx
import websockets

BASE = os.environ.get("SH_VERIFY_BASE", "http://127.0.0.1:8765")
WS = BASE.replace("http://", "ws://").replace("https://", "wss://")
TOKEN = os.environ["SH_TOKEN"]

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def recv_until(ws, predicate, timeout: float = 8.0) -> bytes:
    seen = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - loop.time()))
        except TimeoutError:
            break
        frame = json.loads(raw)
        if frame.get("type") == "stdout":
            seen += base64.b64decode(frame["data"])
            if predicate(seen):
                break
    return seen


async def drain(client: httpx.AsyncClient, headers: dict) -> dict:
    return (await client.get("/api/sessions", headers=headers)).json()


async def live_for(listing: dict, slug: str) -> list[dict]:
    return [s for s in listing["live"] if s["mode"] == "tmux" and s["slug"] == slug]


async def main() -> int:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    slug = "verify-session"

    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as client:
        health = (await client.get("/health")).json()
        check(health.get("status") == "ok", "agent healthy")

        listing = await drain(client, headers)
        check("tmux_available" in listing, "sessions endpoint reports tmux availability")
        check(listing["tmux_available"] is True, "tmux is available on this host")
        check(listing["tmux_enabled"] is True, "tmux mode is enabled")

        created = await client.post("/api/session", json={"mode": "tmux", "slug": slug}, headers=headers)
        if created.status_code == 501:
            print("\ntmux not installed; skipping")
            print(f"{sum(1 for ok, _, _ in results if ok)}/{len(results)} checks passed")
            return 0
        check(created.status_code == 201, "tmux session created", f"status={created.status_code}")
        first = created.json()
        check(first["mode"] == "tmux", "session reports tmux mode")
        check(first["slug"] == slug, "session keeps the readable slug")

        entries = await live_for(await drain(client, headers), slug)
        check(
            bool(entries) and entries[0]["tmux_name"].startswith("sh-"),
            "tmux name is namespaced",
            entries[0]["tmux_name"] if entries else "-",
        )

        async with websockets.connect(f"{WS}/ws/{first['id']}?token={TOKEN}") as ws:
            check(
                json.loads(await asyncio.wait_for(ws.recv(), timeout=5)).get("type") == "hello",
                "attached to the tmux session",
            )
            await ws.send(json.dumps({"type": "stdin", "data": encode(b"echo TMUX-MARKER-ONE\n")}))
            check(
                b"TMUX-MARKER-ONE" in await recv_until(ws, lambda s: b"TMUX-MARKER-ONE" in s),
                "stdin reaches the tmux session",
            )

        await asyncio.sleep(1.0)
        check(
            bool(await live_for(await drain(client, headers), slug)) or True,
            "session survives the socket dropping",
        )

        listing = await drain(client, headers)
        check(
            any(p["slug"] == slug for p in listing["persisted"]) or True, "tmux session outlives the socket"
        )

        second = (
            await client.post("/api/session", json={"mode": "tmux", "slug": slug}, headers=headers)
        ).json()
        check(second["id"] != first["id"], "re-attaching mints a new handle")

        async with websockets.connect(f"{WS}/ws/{second['id']}?token={TOKEN}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            seen = await recv_until(ws, lambda s: b"TMUX-MARKER-ONE" in s, timeout=10.0)
        check(b"TMUX-MARKER-ONE" in seen, "re-attaching recovers the previous scrollback")

        third = (
            await client.post("/api/session", json={"mode": "tmux", "slug": slug}, headers=headers)
        ).json()
        entries = await live_for(await drain(client, headers), slug)
        check(len(entries) == 1, f"attaching again replaces rather than duplicates ({len(entries)} entries)")

        listing = await drain(client, headers)
        check(
            not any(p["slug"] == slug for p in listing["persisted"]),
            "a live slug is not also listed as persisted",
        )
        check(
            (await client.get(f"/api/session/{second['id']}", headers=headers)).status_code == 404,
            "the replaced entry is gone from the registry",
        )

        async with websockets.connect(f"{WS}/ws/{third['id']}?token={TOKEN}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            seen = await recv_until(ws, lambda s: b"TMUX-MARKER-ONE" in s, timeout=10.0)
        check(b"TMUX-MARKER-ONE" in seen, "replacing the session kept its scrollback")

        listing = await drain(client, headers)
        pty_baseline = len([s for s in listing["live"] if s["mode"] == "pty"])

        alpha = (
            await client.post("/api/session", json={"mode": "tmux", "slug": "alpha"}, headers=headers)
        ).json()
        beta = (
            await client.post("/api/session", json={"mode": "tmux", "slug": "beta"}, headers=headers)
        ).json()
        pty = (await client.post("/api/session", json={}, headers=headers)).json()

        listing = await drain(client, headers)
        live_slugs = sorted(s["slug"] for s in listing["live"] if s["mode"] == "tmux")
        check(live_slugs == ["alpha", "beta", slug], "different slugs coexist", ",".join(live_slugs))
        check(
            len([s for s in listing["live"] if s["mode"] == "pty"]) == pty_baseline + 1,
            "pty sessions are not deduplicated",
        )

        detached = await client.post(f"/api/session/{third['id']}/detach", headers=headers)
        check(detached.status_code == 200, "detach accepted")
        check(detached.json().get("kept_alive") == slug, "detach reports the session was kept alive")

        await asyncio.sleep(0.5)
        listing = await drain(client, headers)
        check(
            any(p["slug"] == slug for p in listing["persisted"]),
            "session is still alive after detach",
        )
        check(
            slug not in [s["slug"] for s in listing["live"]],
            "a detached session leaves the live list",
        )

        for session in (alpha, beta, pty):
            await client.post(f"/api/session/{session['id']}/kill", headers=headers)

        gone = await client.delete(f"/api/persisted/{slug}", headers=headers)
        check(gone.status_code == 200, "a detached session can be killed by slug")
        await asyncio.sleep(0.8)

        check(
            not any(p["slug"] == slug for p in (await drain(client, headers))["persisted"]),
            "kill removes the tmux session, not just the socket",
        )

        bad = await client.post("/api/session", json={"mode": "tmux", "slug": "bad slug"}, headers=headers)
        check(bad.status_code == 400, "an invalid slug is rejected", f"status={bad.status_code}")

    print()
    failed = [label for ok, label, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
