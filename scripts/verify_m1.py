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

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


def enc(data: bytes) -> str:
    return base64.b64encode(data).decode()


def dec(data: str) -> bytes:
    return base64.b64decode(data)


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def drain_until(ws, needle: bytes, timeout: float = 8.0) -> bytes:
    seen = b""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(
            ws.recv(), timeout=max(0.1, deadline - asyncio.get_running_loop().time())
        )
        frame = json.loads(raw)
        if frame.get("type") == "stdout":
            seen += dec(frame["data"])
            if needle in seen:
                break
        elif frame.get("type") == "exit":
            break
    return seen


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}

        health = await client.get("/health")
        check(health.status_code == 200, "GET /health returns 200", f"status={health.status_code}")
        check(health.json().get("status") == "ok", "health payload reports ok")

        denied = await client.get("/whoami")
        check(denied.status_code == 401, "no token -> 401", f"status={denied.status_code}")

        bad = await client.get("/whoami", headers={"Authorization": "Bearer wrong"})
        check(bad.status_code == 401, "wrong token -> 401", f"status={bad.status_code}")

        me = await client.get("/whoami", headers=headers)
        check(me.status_code == 200, "valid token -> 200", f"identity={me.json().get('verified_by')}")

        write_denied = await client.post(
            "/api/commands", json={"name": "x", "command": "echo hi"}, headers=headers
        )
        check(
            write_denied.status_code == 403,
            "palette write -> 403 when SH_WRITE_TOKEN unset",
            f"status={write_denied.status_code}",
        )

        created = await client.post("/api/session", json={"cols": 80, "rows": 24}, headers=headers)
        check(created.status_code == 201, "POST /api/session -> 201", f"status={created.status_code}")
        session = created.json()
        sid = session["id"]

        url = f"{WS}/ws/{sid}?token={TOKEN}"

        async with websockets.connect(url) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            check(hello.get("type") == "hello", "WS handshake sends hello", f"shell={hello.get('shell')}")

            await ws.send(json.dumps({"type": "stdin", "data": enc(b"echo m1-marker-$((21*2))\n")}))
            out = await drain_until(ws, b"m1-marker-42")
            check(b"m1-marker-42" in out, "stdin reaches the PTY and stdout streams back")

            await ws.send(json.dumps({"type": "stdin", "data": enc(b"stty size\n")}))
            out = await drain_until(ws, b"24 80")
            check(b"24 80" in out, "stty size reports the initial 80x24 window")

            await ws.send(json.dumps({"type": "resize", "cols": 132, "rows": 43}))
            await asyncio.sleep(0.4)
            await ws.send(json.dumps({"type": "stdin", "data": enc(b"stty size\n")}))
            out = await drain_until(ws, b"43 132")
            check(b"43 132" in out, "resize message changes the PTY window (43 132)")

            info = await client.get(f"/api/session/{sid}", headers=headers)
            check(info.json()["cols"] == 132, "session state tracks new size")

            await ws.send(
                json.dumps({"type": "stdin", "data": enc(b"printf '\\x1b[31mCOLORCHECK\\x1b[0m\\n'\n")})
            )
            out = await drain_until(ws, b"\x1b[31mCOLORCHECK")
            check(
                b"\x1b[31mCOLORCHECK\x1b[0m" in out,
                "ANSI escape sequences pass through base64 framing intact",
                f"tail={out[-40:]!r}",
            )

            await ws.send(json.dumps({"type": "ping"}))
            pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            check(pong.get("type") == "pong", "ping -> pong")

            await ws.send(json.dumps({"type": "stdin", "data": enc(b"printf 'resume-phase-1\\n'\n")}))
            out = await drain_until(ws, b"resume-phase-1")
            check(b"resume-phase-1" in out, "phase-1 output received before drop")

        info = await client.get(f"/api/session/{sid}", headers=headers)
        last_seq = info.json()["buffer_end"]
        child_pid = info.json()["pid"]

        await asyncio.sleep(0.5)
        alive = _pid_alive(child_pid)
        check(alive, "child shell still alive while session is detached (resume window)", f"pid={child_pid}")

        async with websockets.connect(f"{url}&resume={last_seq}") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            check(hello.get("type") == "hello", "reconnect with resume accepted")
            await ws.send(json.dumps({"type": "stdin", "data": enc(b"echo resume-phase-2\n")}))
            out = await drain_until(ws, b"resume-phase-2")
            check(b"resume-phase-2" in out, "session survives drop and accepts new input")
            check(b"resume-phase-1" not in out, "resume offset does not replay already-seen bytes")

        killed = await client.post(f"/api/session/{sid}/kill", headers=headers)
        check(killed.status_code == 200, "kill endpoint closes the session")

        await asyncio.sleep(0.6)
        still_alive = _pid_alive(child_pid)
        check(not still_alive, "no orphaned child process after kill", f"pid={child_pid} alive={still_alive}")

        gone = await client.get(f"/api/session/{sid}", headers=headers)
        check(gone.status_code == 404, "killed session no longer resolvable")

        try:
            async with websockets.connect(f"{url}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
            check(False, "WS to a dead session is refused")
        except Exception:
            check(True, "WS to a dead session is refused")

    print()
    failed = [label for ok, label in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
