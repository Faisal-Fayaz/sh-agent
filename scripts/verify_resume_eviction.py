"""Resume from an offset the ring buffer has already evicted.

If the client was gone long enough to produce more output than SH_RING_CAPACITY,
the requested offset is no longer retained. The server clamps to the oldest byte
it still holds, and the client must be able to tell that it resynced rather than
silently believing it has a contiguous transcript.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
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


async def drain_until(ws, predicate, timeout: float) -> tuple[bytes, int]:
    seen = b""
    last_seq = 0
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
            last_seq = frame.get("seq", last_seq)
            if predicate(seen):
                break
        elif frame.get("type") == "exit":
            break
    return seen, last_seq


async def main() -> int:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    total_lines = 3000

    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as client:
        created = await client.post("/api/session", json={}, headers=headers)
        session_id = created.json()["id"]
        url = f"{WS}/ws/{session_id}?token={TOKEN}"

        async with websockets.connect(url) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            await ws.send(
                json.dumps(
                    {
                        "type": "stdin",
                        "data": encode(f"for i in $(seq 1 {total_lines}); do echo LINE-$i; done\n".encode()),
                    }
                )
            )
            _, last_seq = await drain_until(ws, lambda s: s.count(b"LINE-") >= 5, timeout=10.0)

        stale_offset = last_seq
        check(stale_offset > 0, "recorded a stale offset to resume from", f"seq={stale_offset}")

        async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as probe:
            before = (await probe.get(f"/api/session/{session_id}", headers=headers)).json()
            capacity = before["buffer_capacity"]
            check(capacity > 0, "server reports its ring buffer capacity", f"{capacity} bytes")

        await asyncio.sleep(3.0)

        async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as probe:
            after = (await probe.get(f"/api/session/{session_id}", headers=headers)).json()
            check(
                after["buffer_base"] > 0,
                "ring buffer evicted old bytes while detached",
                f"base={after['buffer_base']} end={after['buffer_end']} cap={capacity}",
            )
            check(
                after["buffer_base"] > stale_offset,
                "the stale offset was genuinely evicted",
                f"stale={stale_offset} base={after['buffer_base']}",
            )

            async with websockets.connect(f"{url}&resume={stale_offset}") as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                check(hello.get("type") == "hello", "reconnect accepted with an evicted offset")
                replay, replay_seq = await drain_until(ws, lambda s: s.count(b"LINE-") >= 200, timeout=15.0)

        check(len(replay) > 0, "server still replayed the retained tail", f"{len(replay)} bytes")
        check(replay_seq > stale_offset, "replay started after the retained window", f"seq={replay_seq}")

        numbers = [int(m) for m in re.findall(rb"LINE-(\d+)", replay)]
        check(len(numbers) > 0, "retained tail contains real output", f"{len(numbers)} numbered lines")
        check(numbers == sorted(numbers), "retained tail is in order")
        check(
            numbers[0] > 1,
            "tail starts after the evicted head, so the client can detect the gap",
            f"first={numbers[0]}",
        )
        check(
            len(set(numbers)) == len(numbers),
            "no duplication in the retained tail",
        )

    print()
    failed = [label for ok, label, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
