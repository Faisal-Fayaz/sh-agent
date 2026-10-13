#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
RUN_DIR="/tmp/sh-agent-run"
PIDFILE="$RUN_DIR/pid"
LOGFILE="$RUN_DIR/server.log"

mkdir -p "$RUN_DIR"

stop_server() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      for _ in $(seq 1 30); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
      done
      kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PIDFILE"
  fi
}

start_server() {
  stop_server
  rm -f "$LOGFILE"
  "$ROOT/.venv/bin/python" -m sh_agent >"$LOGFILE" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 50); do
    if curl -fsS -m 1 "http://127.0.0.1:${SH_PORT:-8765}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "server failed to become healthy; log:" >&2
  cat "$LOGFILE" >&2
  return 1
}

case "${1:-}" in
  start) start_server ;;
  stop) stop_server; echo "stopped" ;;
  log) cat "$LOGFILE" ;;
  *) echo "usage: $0 {start|stop|log}" >&2; exit 2 ;;
esac
