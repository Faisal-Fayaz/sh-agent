"""Simulate a mobile network drop and verify the resume path loses nothing.

Drops the WebSocket mid-stream while a long command keeps producing output, then
reconnects with ?resume=<last seq> and checks the reassembled transcript.
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
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def drain_until(ws, predicate, timeout: float) -> tuple[bytes, int]:
    seen = b""
    last_seq = 0
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=max(0.1, deadline - asyncio.get_running_loop().time())
            )
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


async def new_session(client: httpx.AsyncClient, cols: int = 80, rows: int = 24) -> str:
    response = await client.post(
        "/api/session",
        json={"cols": cols, "rows": rows},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    response.raise_for_status()
    return response.json()["id"]


async def main() -> int:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    total_lines = 400

    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as client:
        session_id = await new_session(client)
        url = f"{WS}/ws/{session_id}?token={TOKEN}"

        transcript: bytes = b""
        last_seq = 0

        async with websockets.connect(url) as ws:
            greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            check(greeting.get("type") == "hello", "handshake on first connect", greeting.get("shell", ""))

            await ws.send(
                json.dumps(
                    {
                        "type": "stdin",
                        "data": encode(f"for i in $(seq 1 {total_lines}); do echo LINE-$i; done\n".encode()),
                    }
                )
            )
            # Drop the connection partway through the output.
            transcript, last_seq = await drain_until(ws, lambda s: s.count(b"LINE-") >= 50, timeout=10.0)
            partial_count = transcript.count(b"LINE-")

        check(partial_count >= 50, "captured partial output before the drop", f"{partial_count} lines")
        check(last_seq > 0, "tracked the byte offset", f"seq={last_seq}")

        # The shell keeps running while the socket is down.
        await asyncio.sleep(2.0)

        async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as probe:
            info = await probe.get(f"/api/session/{session_id}", headers=headers)
            body = info.json()
            check(body["closed"] is False, "shell survived the disconnect", f"pid={body['pid']}")
            buffered_end = body["buffer_end"]
            check(
                buffered_end > last_seq,
                "server buffered output produced while detached",
                f"{last_seq} -> {buffered_end}",
            )

            async with websockets.connect(f"{url}&resume={last_seq}") as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                check(
                    hello.get("type") == "hello",
                    "reconnect with resume accepted",
                    f"resumed_from={hello.get('seq')}",
                )

                replay, replay_seq = await drain_until(
                    ws, lambda s: s.count(b"LINE-") >= total_lines, timeout=20.0
                )
                transcript += replay
                last_seq = replay_seq or last_seq

        received = len(re.findall(rb"LINE-\d+", transcript))
        check(received == total_lines, "reassembled exactly the full line count", f"{received}/{total_lines}")

        # The echoed command line contains "LINE-$i", and it can share a \n-delimited
        # chunk with the first line of output, so match numbered tokens rather than lines.
        ordered = [int(m) for m in re.findall(rb"LINE-(\d+)", transcript)]
        check(ordered == sorted(ordered), "lines arrived in order")
        check(len(ordered) == len(set(ordered)), "no duplicated lines", f"unique={len(set(ordered))}")
        check(
            ordered == list(range(1, total_lines + 1)),
            "no missing lines",
            f"first={ordered[0] if ordered else None} last={ordered[-1] if ordered else None} count={len(ordered)}",
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
