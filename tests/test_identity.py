"""Forwarded-header trust, Funnel rejection, public host config, and audit log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_search.audit import append_event, masked_hit_count, summarize_args
from vault_search.config import ConfigError, Settings, assert_bind_host
from vault_search.identity import client_ip, resolve_caller


def test_forwarded_headers_trusted_only_from_loopback() -> None:
    headers = {"X-Real-IP": "100.64.1.20", "X-Forwarded-For": "100.64.1.20, 127.0.0.1"}
    assert client_ip(headers, "127.0.0.1") == "100.64.1.20"
    assert client_ip(headers, "::1") == "100.64.1.20"
    # A remote peer cannot spoof the client by sending those headers.
    assert client_ip(headers, "100.64.9.9") == "100.64.9.9"
    assert client_ip({}, "127.0.0.1") == "127.0.0.1"


def test_resolve_caller_allows_tailnet_and_rejects_funnel() -> None:
    caller = resolve_caller(
        headers={"X-Real-IP": "100.64.1.20", "X-Vault-Agent-Name": "forge"},
        peer="127.0.0.1",
    )
    assert caller.allowed
    assert caller.ip == "100.64.1.20"
    assert caller.agent_name_unverified == "forge"

    funnel = resolve_caller(
        headers={"Tailscale-Funnel-Request": "1", "X-Vault-Agent-Name": "forge"},
        peer="127.0.0.1",
    )
    assert not funnel.allowed
    assert funnel.deny_reason == "funnel"
    assert funnel.agent_name_unverified == "forge"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://box.example.ts.net", "box.example.ts.net"),
        ("https://box.example.ts.net:8443/", "box.example.ts.net"),
        ("https://<vps>.<tailnet>.ts.net", None),
        ("", None),
    ],
)
def test_public_host_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str | None
) -> None:
    (tmp_path / "vault").mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("PUBLIC_BASE_URL", raw)
    assert Settings.from_env().public_host == expected


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
        headers={"X-Vault-Agent-Name": "unit-test", "X-Real-IP": "100.64.1.20"},
        peer="127.0.0.1",
    )
    text = f"hello [REDACTED:github_pat] and {token}"
    # The logged event never includes the raw token; summarize_args masks it.
    args = summarize_args({"query": f"see {token}", "limit": 8})
    assert token not in json.dumps(args)
    assert "REDACTED" in args["query"]
    hit = "- **Secret note** — `wiki/concepts/secret-note.md`\n  hello [REDACTED:github_pat]"
    event = append_event(
        log,
        caller=caller,
        tool="search",
        args={"query": f"see {token}", "limit": 8},
        result_text=hit,
    )
    assert event["client_ip"] == "100.64.1.20"
    assert event["agent_name_unverified"] == "unit-test"
    assert event["masked_hits"] == 1
    assert event["result_count"] == 1
    assert event["zero_hits"] is False
    assert masked_hit_count(hit) == 1
    recorded = log.read_text(encoding="utf-8")
    assert token not in recorded
    assert "unit-test" in recorded


def test_audit_marks_zero_hit_searches(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    caller = resolve_caller(
        headers={"X-Vault-Agent-Name": "unit-test", "X-Real-IP": "100.64.1.20"},
        peer="127.0.0.1",
    )
    event = append_event(
        log,
        caller=caller,
        tool="search",
        args={"query": "no such term"},
        result_text="No results.",
    )
    assert event["result_count"] == 0
    assert event["zero_hits"] is True
    recorded = log.read_text(encoding="utf-8")
    assert '"zero_hits": true' in recorded
    assert "no such term" in recorded
    denied = append_event(
        log,
        caller=caller,
        tool="search",
        args={"query": "no such term"},
        result_text="not authorized",
        denied=True,
    )
    assert denied["result_count"] == 0
    assert denied["zero_hits"] is False
