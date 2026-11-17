"""WhoIs identity, forwarded-header trust, denylist, and audit log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_search.audit import append_event, masked_hit_count, summarize_args
from vault_search.config import ConfigError, Settings, assert_bind_host, load_denylist
from vault_search.identity import (
    WhoIs,
    WhoIsError,
    client_ip,
    is_denied,
    parse_whois,
    resolve_caller,
    whois_lookup,
)
from tests.fake_whois import SAMPLE_NODE, start_whois_socket


def test_forwarded_headers_trusted_only_from_loopback() -> None:
    headers = {"X-Real-IP": "100.64.1.20", "X-Forwarded-For": "100.64.1.20, 127.0.0.1"}
    assert client_ip(headers, "127.0.0.1") == "100.64.1.20"
    assert client_ip(headers, "::1") == "100.64.1.20"
    # A remote peer cannot spoof the client by sending those headers.
    assert client_ip(headers, "100.64.9.9") == "100.64.9.9"
    assert client_ip({}, "127.0.0.1") == "127.0.0.1"


def test_parse_whois_and_denylist() -> None:
    info = parse_whois(SAMPLE_NODE)
    assert info.device == "fixture-device"
    assert info.node.endswith("example.ts.net")
    assert "tag:vault-reader" in info.tags
    assert is_denied(info.identities(), ("fixture-device",))
    assert is_denied(info.identities(), ("tag:vault-reader",))
    assert is_denied(info.identities(), ("vault-reader",))
    assert not is_denied(info.identities(), ("other-device",))


def test_resolve_caller_funnel_and_whois_failure() -> None:
    def boom(_addr: str) -> WhoIs:
        raise WhoIsError("down")

    funnel = resolve_caller(
        headers={"Tailscale-Funnel-Request": "1", "X-Vault-Agent-Name": "forge"},
        peer="127.0.0.1",
        whois=boom,
    )
    assert not funnel.allowed
    assert funnel.deny_reason == "funnel"
    assert funnel.agent_name_unverified == "forge"

    failed = resolve_caller(headers={}, peer="127.0.0.1", whois=boom)
    assert not failed.allowed
    assert failed.deny_reason == "whois_failed"


def test_whois_over_unix_socket(tmp_path: Path) -> None:
    sock = tmp_path / "tailscaled.sock"
    stop = start_whois_socket(sock)
    try:
        info = whois_lookup("100.64.1.20", sock)
        assert info.device == "fixture-device"
        assert "tag:vault-reader" in info.tags
    finally:
        stop.set()


def test_denylist_from_env_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "denylist"
    path.write_text("# comment\nbeta-device\n", encoding="utf-8")
    loaded = load_denylist(path, ("alpha-device", "beta-device"))
    assert loaded == ("alpha-device", "beta-device")
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    monkeypatch.setenv("DENYLIST", "gamma")
    monkeypatch.setenv("DENYLIST_PATH", str(path))
    settings = Settings.from_env()
    assert "gamma" in settings.denylist
    assert "beta-device" in settings.denylist


def test_assert_bind_host_refuses_wildcard() -> None:
    assert assert_bind_host("127.0.0.1", False) == "127.0.0.1"
    with pytest.raises(ConfigError, match="LISTEN_ALLOW_NONLOCAL"):
        assert_bind_host("0.0.0.0", False)
    with pytest.raises(ConfigError, match="LISTEN_ALLOW_NONLOCAL"):
        assert_bind_host("::", False)
    assert assert_bind_host("0.0.0.0", True) == "0.0.0.0"


def test_audit_masks_args_and_counts_redactions(tmp_path: Path) -> None:
    log = tmp_path / "audit" / "log.jsonl"
    token = "gh" + "p_" + ("B" * 36)
    caller = resolve_caller(
        headers={"X-Vault-Agent-Name": "unit-test"},
        peer="127.0.0.1",
        whois=lambda _addr: parse_whois(SAMPLE_NODE),
    )
    text = f"hello [REDACTED:github_pat] and {token}"
    # The logged event never includes the raw token; summarize_args masks it.
    args = summarize_args({"query": f"see {token}", "limit": 8})
    assert token not in json.dumps(args)
    assert "REDACTED" in args["query"]
    event = append_event(
        log,
        caller=caller,
        tool="search",
        args={"query": f"see {token}", "limit": 8},
        result_text="hello [REDACTED:github_pat]",
    )
    assert event["device"] == "fixture-device"
    assert event["agent_name_unverified"] == "unit-test"
    assert event["masked_hits"] == 1
    assert masked_hit_count("hello [REDACTED:github_pat]") == 1
    recorded = log.read_text(encoding="utf-8")
    assert token not in recorded
    assert "unit-test" in recorded
