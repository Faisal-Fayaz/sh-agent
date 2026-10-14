from __future__ import annotations

import stat

from sh_agent import onboarding


def test_a_token_is_generated_and_persisted(tmp_path) -> None:
    """The whole point: a token that is not stored cannot be recovered."""
    first = onboarding.load_or_create(config_dir=tmp_path)
    assert first.generated is True
    assert first.token

    stored = tmp_path / "agent.env"
    assert stored.exists()
    assert f"SH_TOKEN={first.token}" in stored.read_text(encoding="utf-8")


def test_a_stored_token_is_reused(tmp_path) -> None:
    """Restarting must not invalidate whatever the phone already saved."""
    first = onboarding.load_or_create(config_dir=tmp_path)
    second = onboarding.load_or_create(config_dir=tmp_path)
    assert second.token == first.token
    assert second.generated is False


def test_the_environment_overrides_a_stored_token(tmp_path) -> None:
    stored = onboarding.load_or_create(config_dir=tmp_path)
    override = onboarding.load_or_create(config_dir=tmp_path, env_token="from-env")
    assert override.token == "from-env"
    assert override.generated is False
    # The file must not be clobbered by an override that only exists in a shell.
    assert stored.token in (tmp_path / "agent.env").read_text(encoding="utf-8")


def test_the_write_token_persists_too(tmp_path) -> None:
    creds = onboarding.load_or_create(config_dir=tmp_path, env_write_token="write-me")
    assert creds.write_token == "write-me"
    again = onboarding.load_or_create(config_dir=tmp_path)
    assert again.write_token == "write-me"


def test_the_token_file_is_not_world_readable(tmp_path) -> None:
    onboarding.load_or_create(config_dir=tmp_path)
    mode = (tmp_path / "agent.env").stat().st_mode
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH
    assert not mode & stat.S_IWGRP


def test_a_malformed_env_file_does_not_crash_the_agent(tmp_path) -> None:
    """A user editing this by hand should get a new token, not a stack trace."""
    (tmp_path / "agent.env").write_text(
        "# a comment\n\nnot a pair\nSH_TOKEN=still-good\n=novalue\n", encoding="utf-8"
    )
    creds = onboarding.load_or_create(config_dir=tmp_path)
    assert creds.token == "still-good"


def test_pairing_uri_encodes_the_token() -> None:
    uri = onboarding.pairing_uri("100.1.2.3", 8765, "a+b/c=d")
    assert uri.startswith("sh://connect?")
    assert "host=100.1.2.3" in uri
    assert "port=8765" in uri
    assert "a%2Bb%2Fc%3Dd" in uri


def test_pairing_uri_omits_an_absent_write_token() -> None:
    uri = onboarding.pairing_uri("10.0.0.1", 8765, "tok")
    assert "wt=" not in uri


def test_a_wildcard_bind_is_never_offered_as_a_host() -> None:
    """0.0.0.0 is what we listen on, not something a phone can dial."""
    host, source = onboarding.resolve_pairing_host("0.0.0.0", tailnet=None)
    assert host is None or host != "0.0.0.0"
    assert source in ("lan", "unknown")


def test_a_loopback_bind_offers_nothing() -> None:
    host, source = onboarding.resolve_pairing_host("127.0.0.1", tailnet=None)
    assert host is None
    assert source == "loopback"


def test_the_tailnet_address_is_preferred_over_a_wildcard_bind() -> None:
    """It is the only host that also works away from home."""
    host, source = onboarding.resolve_pairing_host("0.0.0.0", tailnet="100.64.0.9")
    assert host == "100.64.0.9"
    assert source == "tailnet"


def test_a_configured_address_is_used_as_is() -> None:
    host, source = onboarding.resolve_pairing_host("192.168.1.50", tailnet=None)
    assert host == "192.168.1.50"
    assert source == "configured"


def test_the_qr_renders_something_scannable() -> None:
    qr = onboarding.render_qr(onboarding.pairing_uri("10.0.0.5", 8765, "tok"))
    assert isinstance(qr, str)
    assert qr.strip(), "render_qr must return the code, not print it and return None"
    lines = [line for line in qr.splitlines() if line.strip()]
    assert len(lines) > 15


def test_the_qr_does_not_depend_on_terminal_colour() -> None:
    """A camera reading the screen cannot resolve ANSI colour, and a pipe mangles it."""
    qr = onboarding.render_qr(onboarding.pairing_uri("10.0.0.5", 8765, "tok"))
    assert "\x1b[" not in qr
    assert any(char in qr for char in "\u2580\u2584")
