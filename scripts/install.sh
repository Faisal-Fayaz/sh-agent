#!/usr/bin/env bash
# Install sh-agent and get it running.
#
# Deliberately does one thing: put a working `sh-agent` on PATH and leave the
# machine able to run it at login. Everything else -- the token, the address, the
# phone pairing -- is printed by the agent itself on first run, because anything
# the user has to copy out of a script and into a phone keyboard is a step they
# will get wrong.
#
#   curl -fsSL https://raw.githubusercontent.com/Faisal-Fayaz/sh-agent/main/scripts/install.sh | bash
#
# For development, run this from a clone: ./scripts/install.sh

set -euo pipefail

REPO_URL="https://github.com/Faisal-Fayaz/sh-agent.git"
BIN_DIR="${SH_BIN_DIR:-$HOME/.local/bin}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

# --- platform -------------------------------------------------------------

case "$(uname -s)" in
  Linux)  PLATFORM="linux" ;;
  Darwin) PLATFORM="macos" ;;
  *)      die "unsupported platform $(uname -s). sh-agent supports macOS and Linux." ;;
esac
say "$PLATFORM detected"

# --- python ---------------------------------------------------------------

# Python 3.11 is the floor, and the version in the name matters: a system python
# called `python3` can easily be older than the one a user would install.
python_cmd() {
  for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON="$(python_cmd)" || die "Python 3.11 or newer is required. Install it and run this again."
say "using $PYTHON ($("$PYTHON" --version 2>&1))"

# --- install --------------------------------------------------------------

# uv is the nicest way to get an isolated install with a binary on PATH; pipx
# does the same thing and is more likely to already be around. Neither is
# required: the venv fallback always works.
install_with_uv() {
  command -v uv >/dev/null 2>&1 || return 1
  say "installing with uv"
  uv tool install --force . || uv tool install --force "sh-agent @ git+$REPO_URL" || return 1
}

install_with_pipx() {
  command -v pipx >/dev/null 2>&1 || return 1
  say "installing with pipx"
  pipx install --force . || pipx install --force "sh-agent @ git+$REPO_URL" || return 1
}

install_with_venv() {
  say "installing into a virtualenv at $HOME/.local/share/sh-agent"
  mkdir -p "$HOME/.local/share"
  "$PYTHON" -m venv "$HOME/.local/share/sh-agent"
  "$HOME/.local/share/sh-agent/bin/pip" install --quiet --upgrade pip
  "$HOME/.local/share/sh-agent/bin/pip" install --quiet . \
    || "$HOME/.local/share/sh-agent/bin/pip" install --quiet "sh-agent @ git+$REPO_URL"
  mkdir -p "$BIN_DIR"
  ln -sf "$HOME/.local/share/sh-agent/bin/sh-agent" "$BIN_DIR/sh-agent"
}

# Running from a clone is the happy path; the remote fallback is for the
# curl-piped case where there is no local source.
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$SOURCE_DIR/pyproject.toml" ]; then
  cd "$SOURCE_DIR"
else
  say "fetching source"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  git clone --depth 1 "$REPO_URL" "$TMP/sh-agent" 2>/dev/null \
    || die "could not fetch the source. Install git, or clone the repo and run ./scripts/install.sh from it."
  cd "$TMP/sh-agent"
fi

install_with_uv || install_with_pipx || install_with_venv \
  || die "installation failed"

# The systemd unit hardcodes ~/.local/bin/sh-agent, so anything installed
# elsewhere is made reachable from there too.
mkdir -p "$BIN_DIR"
AGENT_BIN="$(command -v sh-agent || echo "$HOME/.local/share/sh-agent/bin/sh-agent")"
if [ -x "$AGENT_BIN" ] && [ "$AGENT_BIN" != "$BIN_DIR/sh-agent" ]; then
  ln -sf "$AGENT_BIN" "$BIN_DIR/sh-agent" 2>/dev/null || true
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    warn "$BIN_DIR is not on your PATH"
    printf '  add this to your shell profile:\n    export PATH="$HOME/.local/bin:$PATH"\n' >&2
    ;;
esac

# --- optional pieces ------------------------------------------------------

# tmux powers the named-session mode. It is genuinely optional -- the default
# pty mode needs nothing -- but macOS does not ship it, so a Mac user would
# otherwise discover the feature is broken rather than simply absent.
if command -v tmux >/dev/null 2>&1; then
  say "tmux found, named sessions available"
else
  warn "tmux is not installed; named sessions will not work (plain shells still do)"
  case "$PLATFORM" in
    macos) printf '  install it with: brew install tmux\n' >&2 ;;
    linux) printf '  install it with your package manager, e.g. apt install tmux\n' >&2 ;;
  esac
fi

# Tailscale is what makes the agent reachable from outside the house, which is
# the whole point of carrying a terminal in a pocket. Without it the pairing code
# only works while the phone is on the same Wi-Fi, and nothing says so.
if command -v tailscale >/dev/null 2>&1; then
  say "Tailscale found; the agent will bind your tailnet address"
else
  warn "Tailscale is not installed; pairing will only work on your local network"
  printf '  install it and sign in on this machine and your phone:\n' >&2
  printf '    https://tailscale.com/download\n' >&2
fi

# --- background service ---------------------------------------------------

# Only offered on Linux, and only actually done if systemd is there. macOS
# would need a launchd plist, which is not written here rather than written
# wrongly: a broken service file is worse than none.
if [ "$PLATFORM" = "linux" ] && command -v systemctl >/dev/null 2>&1 && [ -d "$HOME/.config/systemd/user" -o -d "$HOME/.config/systemd" ]; then
  UNIT_SRC="$SOURCE_DIR/systemd/sh-agent.service"
  if [ -f "$UNIT_SRC" ]; then
    mkdir -p "$HOME/.config/systemd/user"
    if cp "$UNIT_SRC" "$HOME/.config/systemd/user/sh-agent.service" 2>/dev/null; then
      systemctl --user daemon-reload >/dev/null 2>&1 || true
      say "installed the systemd user unit"
      printf '  start it with:  systemctl --user enable --now sh-agent\n' >&2
    else
      warn "could not install the systemd unit; skip it if you prefer"
    fi
  fi
fi

# --- done -----------------------------------------------------------------

cat <<'EOF'

sh-agent is installed.

Start it with:

    sh-agent

It prints a QR code. With Tailscale running on this machine and on your phone
it uses your tailnet name, so the code keeps working from anywhere; otherwise it
uses your local network address, which works while both are on the same Wi-Fi.
Either way the first run creates a token and stores it in
~/.config/sh/agent.env, so restarting the agent never unpairs your phone.
Open the sh app, tap "Scan to pair", and point the camera at the code.

The token is the only key to this machine, so keep agent.env to yourself.
EOF