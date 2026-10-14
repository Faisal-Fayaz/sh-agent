"""The startup banner must survive a redirected stdout.

This exists because it shipped broken. serverctl.sh backgrounds the agent with
its output redirected to a log, and Python block-buffers stdout when it is not a
TTY. The banner sat in that buffer, the agent ran forever, and the pairing code
never appeared anywhere -- while uvicorn's own logging, which goes to stderr,
looked perfectly healthy. The log had INFO lines and no QR at all.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from shutil import which

import pytest

needs_tmux = pytest.mark.skipif(which("tmux") is None, reason="tmux is not installed")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_pairing_banner_reaches_a_redirected_stdout(tmp_path) -> None:
    """Run the real entry point with its stdout piped, the way serverctl does."""
    port = _free_port()
    env = {
        **os.environ,
        "SH_CONFIG_DIR": str(tmp_path),
        "SH_HOST": "0.0.0.0",
        "SH_PORT": str(port),
        "PYTHONUNBUFFERED": "",  # deliberately unset: the buffering is the thing under test
    }
    env.pop("PYTHONUNBUFFERED", None)

    result = subprocess.run(
        [sys.executable, "-m", "sh_agent", "--pairing"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "sh-agent listening on" in result.stdout, "the banner vanished into a buffer"
    assert "sh://connect?" in result.stdout, "no pairing link was printed"
    # Half-block glyphs, not coloured spaces: a camera cannot resolve ANSI colour.
    assert "\x1b[" not in result.stdout, "the code must not depend on terminal colour"


@needs_tmux
def test_the_startup_banner_is_flushed_while_the_server_keeps_running(tmp_path) -> None:
    """The hard case: a long-lived process whose buffer would never be flushed.

    Redirected to a file exactly as serverctl.sh does it, then polled rather than
    read, so a regression fails in seconds instead of blocking on a readline that
    never returns.
    """
    port = _free_port()
    log = tmp_path / "server.log"
    env = {
        **os.environ,
        "SH_CONFIG_DIR": str(tmp_path),
        "SH_HOST": "0.0.0.0",
        "SH_PORT": str(port),
    }
    env.pop("PYTHONUNBUFFERED", None)

    with log.open("w") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "sh_agent"],
            cwd=REPO_ROOT,
            env=env,
            stdout=sink,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    try:
        seen = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            seen = log.read_text(encoding="utf-8", errors="replace")
            if "sh://connect?" in seen:
                break
            time.sleep(0.2)
        assert "sh-agent listening on" in seen, "the banner never reached the log"
        assert "sh://connect?" in seen, "no pairing link was written while running"
    finally:
        process.terminate()
        process.wait(timeout=30)
