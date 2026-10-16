# Install sh-agent on Windows and get it running.
#
# The same one thing the POSIX script does: put a working `sh-agent` on PATH.
# The token, the address and the pairing code come from the agent itself on
# first run, because anything a user has to copy by hand is a step they get
# wrong.
#
#   irm https://raw.githubusercontent.com/Faisal-Fayaz/sh-agent/main/scripts/install.ps1 | iex
#
# For development, run it from a clone: .\scripts\install.ps1

$ErrorActionPreference = 'Stop'

$RepoUrl = 'https://github.com/Faisal-Fayaz/sh-agent.git'

function Say($message)  { Write-Host "==> " -ForegroundColor Cyan -NoNewline; Write-Host $message }
function Warn($message) { Write-Host "warning: " -ForegroundColor Yellow -NoNewline; Write-Host $message }
function Die($message)  { Write-Host "error: " -ForegroundColor Red -NoNewline; Write-Host $message; exit 1 }

Say "Windows detected"

# --- python ---------------------------------------------------------------

# 3.11 is the floor. `py -3` is the launcher Windows ships with every official
# installer, and it finds versions a bare `python` on PATH often misses.
function Get-Python {
    foreach ($candidate in @(@('py', @('-3.13')), @('py', @('-3.12')), @('py', @('-3.11')), @('python', @()))) {
        $exe, $exeArgs = $candidate
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        & $exe @exeArgs -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return , @($exe, $exeArgs) }
    }
    return $null
}

$python = Get-Python
if (-not $python) {
    Die "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again."
}
$pythonExe = $python[0]
$pythonArgs = $python[1]
$version = & $pythonExe @pythonArgs --version
Say "using $pythonExe ($version)"

# --- source ---------------------------------------------------------------

# Running from a clone is the happy path; the remote fallback is for the
# irm-piped case, where $PSScriptRoot is empty because there is no script file.
$sourceDir = $null
$temp = $null
if ($PSScriptRoot) {
    $candidate = Resolve-Path (Join-Path $PSScriptRoot '..') -ErrorAction SilentlyContinue
    if ($candidate -and (Test-Path (Join-Path $candidate 'pyproject.toml'))) { $sourceDir = $candidate }
}
if (-not $sourceDir) {
    Say "fetching source"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Die "could not fetch the source: git is not installed. Install git, or clone the repo and run .\scripts\install.ps1 from it."
    }
    $temp = Join-Path ([System.IO.Path]::GetTempPath()) ("sh-agent-" + [System.Guid]::NewGuid().ToString('N'))
    git clone --depth 1 $RepoUrl $temp
    if ($LASTEXITCODE -ne 0) { Die "could not clone $RepoUrl" }
    $sourceDir = $temp
}

try {
    Push-Location $sourceDir

    # --- install ----------------------------------------------------------

    # Same order as the POSIX script: uv if it is there, then pipx, then a venv
    # that always works.
    $installed = $false
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Say "installing with uv"
        uv tool install --force .
        if ($LASTEXITCODE -ne 0) { uv tool install --force "sh-agent @ git+$RepoUrl" }
        $installed = $LASTEXITCODE -eq 0
    }
    if (-not $installed -and (Get-Command pipx -ErrorAction SilentlyContinue)) {
        Say "installing with pipx"
        pipx install --force .
        if ($LASTEXITCODE -ne 0) { pipx install --force "sh-agent @ git+$RepoUrl" }
        $installed = $LASTEXITCODE -eq 0
    }
    if (-not $installed) {
        $venv = Join-Path $env:LOCALAPPDATA 'sh-agent'
        Say "installing into a virtualenv at $venv"
        & $pythonExe @pythonArgs -m venv $venv
        $pip = Join-Path $venv 'Scripts\pip.exe'
        & $pip install --quiet --upgrade pip
        & $pip install --quiet .
        if ($LASTEXITCODE -ne 0) { & $pip install --quiet "sh-agent @ git+$RepoUrl" }
        $installed = $LASTEXITCODE -eq 0
    }
    if (-not $installed) { Die "installation failed" }
}
finally {
    Pop-Location
    if ($temp) { Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue }
}

# uv and pipx both land here, and both leave it off the user PATH on a fresh
# machine until a shell is restarted.
$binDir = Join-Path $env:USERPROFILE '.local\bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$binDir*") {
    Warn "$binDir is not on your PATH"
    $newPath = if ($userPath) { "$userPath;$binDir" } else { $binDir }
    Write-Host "  add it to your user PATH with:"
    Write-Host "    [Environment]::SetEnvironmentVariable('Path', '$newPath', 'User')"
    Write-Host "  then open a new terminal."
}

# --- optional pieces ------------------------------------------------------

# The named-session mode is tmux, and tmux does not run natively on Windows:
# the default pty mode needs nothing, and this is the one feature that stays
# behind.
Warn "tmux does not run natively on Windows; named sessions will not work here"
Write-Host "  plain shells are unaffected: that is the default mode."

# Tailscale is what makes the agent reachable from outside the house. It runs
# on Windows, but its local API is a named pipe rather than the unix socket the
# agent reads, so the address works and the identity check does not.
if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    Say "Tailscale found; the agent will bind your tailnet address"
    Warn "Tailscale identity checks need the tailscaled socket, which is a named pipe on Windows"
    Write-Host "  the agent falls back to token-only authentication; see the README."
} else {
    Warn "Tailscale is not installed; pairing will only work on your local network"
    Write-Host "  install it and sign in on this machine and your phone:"
    Write-Host "    https://tailscale.com/download"
}

# --- done -----------------------------------------------------------------

Write-Host @"

sh-agent is installed.

Start it with:

    sh-agent

It prints a QR code. With Tailscale running on this machine and on your phone
it uses your tailnet name, so the code keeps working from anywhere; otherwise it
uses your local network address, which works while both are on the same Wi-Fi.
Either way the first run creates a token and stores it in
%USERPROFILE%\.config\sh\agent.env, so restarting the agent never unpairs your
phone. Open the sh app, tap "Scan to pair", and point the camera at the code.

The token is the only key to this machine, so keep agent.env to yourself.
"@
