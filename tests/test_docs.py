from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from sh_agent import config
from sh_agent.onboarding import DEFAULT_CONFIG_DIR

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
PACKAGE = REPO / "sh_agent"

# An environment read is either os.environ["X"], os.environ.get("X"), or one of
# the typed helpers in sh_agent/env.py, which are all named _int_env("X", ...).
_ENV_READ = re.compile(r"""(?:os\.environ\s*(?:\.get\(\s*|\[\s*)|(?:_[a-z]+_env)\(\s*)["']?(SH_[A-Z_]+)""")


def _documented_table() -> dict[str, str]:
    """The Variable and Default columns of the configuration table."""
    rows: dict[str, str] = {}
    in_table = False
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Variable | Default |"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 3 or set(cells[0]) <= set("-: "):
            continue
        # Backticks are stripped rather than trimmed: a default like
        # "derived from `SH_TOKEN`" carries them internally too.
        rows[cells[0].replace("`", "").strip()] = cells[1].replace("`", "").strip()
    return rows


def _knobs_read_in_code() -> set[str]:
    found: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        found.update(_ENV_READ.findall(path.read_text(encoding="utf-8")))
    return found


def _render(value: object) -> str:
    """How the README writes a default value down.

    Separators are normalised because the README writes a user's home the way
    a shell shows it: on Windows the same directory is ``C:\\Users\\x``, and
    the table still says ``~/.config/sh``.
    """
    if isinstance(value, bool):
        return "1" if value else "unset"
    if value is None or value == "" or value == []:
        return "unset"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace(str(Path.home()), "~").replace(os.sep, "/")


@pytest.fixture
def pristine(monkeypatch) -> config.Config:
    """A Config built as a first run would build it, with nothing set."""
    for key in [key for key in os.environ if key.startswith("SH_")]:
        monkeypatch.delenv(key, raising=False)
    return config.Config()


def _expected_defaults(cfg: config.Config) -> dict[str, str]:
    return {
        "SH_HOST": _render(cfg.host),
        "SH_PORT": _render(cfg.port),
        "SH_ALLOW_LAN": _render(cfg.allow_lan),
        "SH_SHELL": _render(config.DEFAULT_SHELL),
        "SH_CWD": "—",
        "SH_PALETTE": _render(cfg.palette_path),
        "SH_AUDIT": _render(cfg.audit_path),
        "SH_CONFIG_DIR": _render(DEFAULT_CONFIG_DIR),
        "SH_IDLE_TTL": _render(cfg.idle_ttl),
        "SH_DETACH_GRACE": _render(cfg.detach_grace),
        "SH_REAP_INTERVAL": _render(cfg.reap_interval),
        "SH_MAX_SESSIONS": _render(cfg.max_sessions),
        "SH_RING_CAPACITY": _render(cfg.ring_capacity),
        "SH_TAILSCALE_REQUIRE": _render(cfg.auth.tailscale_require),
        "SH_TAILSCALE_ALLOW": _render(cfg.auth.tailscale_allow),
        "SH_TAILSCALE_SOCKET": _render(cfg.auth.tailscale_socket),
        "SH_TMUX_ENABLED": _render(cfg.tmux_enabled),
        "SH_TMUX_MAX_SESSIONS": _render(cfg.tmux_max_sessions),
        "SH_TMUX_IDLE_TTL": _render(cfg.tmux_idle_ttl),
        "SH_TMUX_SECRET": "derived from SH_TOKEN",
        "SH_TOKEN": "—",
        "SH_WRITE_TOKEN": "unset",
    }


def test_every_knob_the_code_reads_is_documented() -> None:
    """An undocumented variable is one nobody can discover, only trip over."""
    undocumented = _knobs_read_in_code() - set(_documented_table())
    assert not undocumented, f"these are read but not in the README table: {sorted(undocumented)}"


def test_every_documented_knob_is_still_read() -> None:
    """A table row for a variable nothing reads sends people chasing nothing."""
    ghosts = set(_documented_table()) - _knobs_read_in_code()
    assert not ghosts, f"documented but never read: {sorted(ghosts)}"


def test_the_documented_defaults_are_the_real_ones(pristine: config.Config) -> None:
    """The table said SH_HOST defaults to 127.0.0.1 long after it did not.

    Nobody noticed, because a wrong default in prose looks exactly like a right
    one. The column is compared against a Config built with nothing set, so the
    code is the only place a default is written down.
    """
    documented = _documented_table()
    wrong = {
        name: (documented.get(name), expected)
        for name, expected in _expected_defaults(pristine).items()
        if documented.get(name) != expected
    }
    assert not wrong, f"documented default (got, want): {wrong}"


def test_the_readme_claims_the_real_number_of_tests(request: pytest.FixtureRequest) -> None:
    """The count had drifted to understating the suite by a third.

    A number in prose is a promise that has to be maintained or dropped, and it
    is worth keeping: it is how a reader judges how much is actually checked.

    Only checked on a full run. Invoking pytest on a single file collects a
    handful of tests, and comparing the README against that would fail for a
    reason that has nothing to do with the README.

    The command line is read from invocation_params rather than config.args:
    the latter is backfilled from testpaths, so it says "tests" even when
    nothing was passed.
    """
    passed_paths = [
        arg
        for arg in request.session.config.invocation_params.args
        if isinstance(arg, str) and not arg.startswith("-")
    ]
    if passed_paths:
        pytest.skip("comparing against a partial run would be meaningless")

    claimed = re.search(r"(\d+) unit \+ API tests", README.read_text(encoding="utf-8"))
    assert claimed, "the README no longer states how many tests there are"
    collected = request.session.testscollected
    assert int(claimed.group(1)) == collected, (
        f"the README says {claimed.group(1)} tests but the suite collects {collected}; "
        f"update the count in README.md"
    )
