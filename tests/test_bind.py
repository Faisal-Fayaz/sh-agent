from __future__ import annotations

from sh_agent import config


def _no_tailnet(monkeypatch) -> None:
    monkeypatch.setattr(config, "tailnet_address", lambda: None)


def test_an_unconfigured_agent_binds_where_a_phone_can_reach_it(monkeypatch) -> None:
    """Loopback as a default left every user without Tailscale with nothing to scan.

    The agent exists so a phone can reach this machine. A default that no phone
    can dial fails at the only thing it was started to do, so the fallback has to
    be the local network -- an explicit SH_HOST is still the way to opt out.
    """
    monkeypatch.delenv("SH_HOST", raising=False)
    monkeypatch.delenv("SH_ALLOW_LAN", raising=False)
    _no_tailnet(monkeypatch)
    assert config.Config().public_bind == "0.0.0.0"


def test_an_explicit_host_is_honoured(monkeypatch) -> None:
    """Whatever the default is, setting SH_HOST must not be second-guessed."""
    monkeypatch.setenv("SH_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "tailnet_address", lambda: "100.64.0.9")
    assert config.Config().public_bind == "127.0.0.1"


def test_the_tailnet_is_preferred_when_nothing_is_configured(monkeypatch) -> None:
    """It is the only address that keeps working away from home."""
    monkeypatch.delenv("SH_HOST", raising=False)
    monkeypatch.delenv("SH_ALLOW_LAN", raising=False)
    monkeypatch.setattr(config, "tailnet_address", lambda: "100.64.0.9")
    assert config.Config().public_bind == "100.64.0.9"


def test_allowing_lan_without_a_host_binds_lan(monkeypatch) -> None:
    """SH_ALLOW_LAN=1 on its own used to bind loopback, which is the opposite.

    It appeared to opt in to network access and then produced no pairing code at
    all, because the host it fell back to was still 127.0.0.1.
    """
    monkeypatch.delenv("SH_HOST", raising=False)
    monkeypatch.setenv("SH_ALLOW_LAN", "1")
    monkeypatch.setattr(config, "tailnet_address", lambda: "100.64.0.9")
    assert config.Config().public_bind == "0.0.0.0"
