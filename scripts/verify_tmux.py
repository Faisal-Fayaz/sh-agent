"""End-to-end check of tmux-backed sessions against a live agent.

Exercises the journey a user takes: attach to a named session, drop the
connection, come back later, and find the work still there.
"""

from __future__ import annotations

import asyncio
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
    import base64

    return base64.b64encode(data).decode()


async def recv_until(ws, predicate, timeout: float = 8.0) -> bytes:
    seen = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - loop.time()))
        frame = json.loads(raw)
        if frame.get("type") == "stdout":
            seen += __import__("base64").b64decode(frame["data"])
            if predicate(seen):
                break
    return seen


async def main() -> int:
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as client:
        health = (await client.get("/health")).json()
        check(health.get("status") == "ok", "agent healthy")

        listing = (await client.get("/api/sessions", headers=headers)).json()
        check("tmux_available" in listing, "sessions endpoint reports tmux availability",
              str(listing.get("tmux_available")))
        check(listing["tmux_available"] is True, "tmux is available on this host")
        check(listing["tmux_enabled"] is True, "tmux mode is enabled")

        slug = "verify-session"
        created = await client.post(
            "/api/session",
            json={"mode": "tmux", "slug": slug, "cols": 80, "rows": 24},
            headers=headers,
        )
        if created.status_code == 501:
            print("\ntmux not installed; skipping")
            print(f"{sum(1 for ok, _, _ in results if ok)}/{len(results)} checks passed")
            return 0
        check(created.status_code == 201, "tmux session created", f"status={created.status_code}")
        body = created.json()
        check(body["mode"] == "tmux", "session reports tmux mode")
        check(body["slug"] == slug, "session keeps the readable slug")

        listing = (await client.get("/api/sessions", headers=headers)).json()
        names = [s["tmux_name"] for s in listing["live"] if s["mode"] == "tmux"]
        check(bool(names) and names[0].startswith("sh-"), "tmux name is namespaced", names[0] if names else "-")

        async with websockets.connect(f"{WS}/ws/{body['id']}?token={TOKEN}") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            check(hello.get("type") == "hello", "attached to the tmux session")
            await ws.send(json.dumps({"type": "stdin", "data": encode(b"echo TMUX-MARKER-ONE\n")}))
            seen = await recv_until(ws, lambda s: b"TMUX-MARKER-ONE" in s)
            check(b"TMUX-MARKER-ONE" in seen, "stdin reaches the tmux session")

        await asyncio.sleep(1.0)

        persisted = (await client.get("/api/sessions", headers=headers)).json()
        mine = [p for p in persisted["persisted"] if p["slug"] == slug]
        check(bool(mine), "session survives the socket dropping")

        again = await client.post(
            "/api/session", json={"mode": "tmux", "slug": slug}, headers=headers
        )
        check(again.status_code == 201, "re-attaching to the same slug succeeds")
        second = again.json()

        async with websockets.connect(f"{WS}/ws/{second['id']}?token={TOKEN}") as ws:
            json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            seen = await recv_until(ws, lambda s: b"TMUX-MARKER-ONE" in s, timeout=10.0)
            check(
                b"TMUX-MARKER-ONE" in seen,
                "re-attaching recovers the previous scrollback",
            )

        detached = await client.post(f"/api/session/{second['id']}/detach", headers=headers)
        check(detached.status_code == 200, "detach accepted")
        check(detached.json()["kept_alive"] == slug, "detach reports the session was kept alive")

        await asyncio.sleep(0.5)
        after = (await client.get("/api/sessions", headers=headers)).json()
        check(
            any(p["slug"] == slug for p in after["persisted"]),
            "session is still alive after detach",
        )

        third = await client.post("/api/session", json={"mode": "tmux", "slug": slug}, headers=headers)
        third_body = third.json()
        await client.post(f"/api/session/{third_body['id']}/kill", headers=headers)
        await asyncio.sleep(0.6)

        final = (await client.get("/api/sessions", headers=headers)).json()
        check(
            not any(p["slug"] == slug for p in final["persisted"]),
            "kill removes the tmux session, not just the socket",
        )

        bad = await client.post(
            "/api/session", json={"mode": "tmux", "slug": "bad slug"}, headers=headers
        )
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