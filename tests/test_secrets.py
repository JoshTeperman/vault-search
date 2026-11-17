"""Unit tests for the secret detector.

Synthetic fixtures only. Token-like strings are assembled at runtime so the
committed source does not contain a complete secret that gitleaks or this
detector would flag.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from vault_search.secrets import (
    RULE_ANTHROPIC,
    RULE_AWS_ACCESS_KEY,
    RULE_GENERIC,
    RULE_GITHUB,
    RULE_GOOGLE,
    RULE_JWT,
    RULE_OPENAI,
    RULE_PRIVATE_KEY,
    RULE_SLACK,
    mask,
    scan,
    shannon_entropy,
)


def _join(*parts: str) -> str:
    return "".join(parts)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _github_classic(kind: str = "p") -> str:
    return _join("gh", kind, "_", "A" * 36)


def _github_fine_grained() -> str:
    return _join("github", "_pat_", "B" * 22, "_", "C" * 59)


def _openai_key() -> str:
    return _join("sk", "-", "D" * 48)


def _openai_project_key() -> str:
    return _join("sk", "-proj-", "E" * 48)


def _anthropic_key() -> str:
    return _join("sk", "-ant-", "F" * 40)


def _aws_key() -> str:
    return _join("AKIA", "G" * 16)


def _slack_bot() -> str:
    return _join("xoxb", "-", "1" * 12, "-", "2" * 12, "-", "Hh" * 12)


def _google_api_key() -> str:
    return _join("AIza", "J" * 35)


def _google_client_secret() -> str:
    return _join("GOCSPX", "-", "K" * 24)


def _jwt() -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"sub": "fixture"}, separators=(",", ":")).encode())
    sig = "L" * 32
    return _join(header, ".", payload, ".", sig)


def _private_key_pem() -> str:
    begin = _join("-----BEGIN ", "RSA PRIVATE ", "KEY-----")
    end = _join("-----END ", "RSA PRIVATE ", "KEY-----")
    body = "\n".join(["M" * 64] * 4)
    return f"{begin}\n{body}\n{end}"


def _generic_value() -> str:
    # High-entropy, mixed charset, long enough for the generic rule.
    return _join("n7Q", "w2p", "Lx9", "vR4", "tZ8", "bK1", "sH6", "cY3")


def _rules(text: str) -> list[str]:
    return [finding.rule for finding in scan(text)]


def test_clean_prose_has_no_findings() -> None:
    text = "Search returns a compact markdown list of paths, titles and snippets."
    assert scan(text) == []
    assert mask(text) == text


def test_github_classic_and_fine_grained() -> None:
    classic = _github_classic()
    fine = _github_fine_grained()
    text = f"export TOKEN={classic}\n# also {fine}"
    assert RULE_GITHUB in _rules(text)
    assert classic in text
    masked = mask(text)
    assert classic not in masked
    assert fine not in masked
    assert "REDACTED" in masked


@pytest.mark.parametrize("kind", list("pours"))
def test_github_prefix_variants(kind: str) -> None:
    token = _github_classic(kind)
    assert RULE_GITHUB in _rules(f"key={token}")


def test_openai_and_anthropic_are_distinct() -> None:
    openai = _openai_key()
    project = _openai_project_key()
    anthropic = _anthropic_key()
    text = f"{openai}\n{project}\n{anthropic}"
    rules = _rules(text)
    assert rules.count(RULE_OPENAI) == 2
    assert RULE_ANTHROPIC in rules
    assert RULE_OPENAI in rules
    masked = mask(text)
    assert openai not in masked
    assert project not in masked
    assert anthropic not in masked


def test_aws_slack_google() -> None:
    aws = _aws_key()
    slack = _slack_bot()
    google = _google_api_key()
    client = _google_client_secret()
    text = f"{aws} {slack} {google} {client}"
    rules = set(_rules(text))
    assert {RULE_AWS_ACCESS_KEY, RULE_SLACK, RULE_GOOGLE} <= rules
    masked = mask(text)
    for token in (aws, slack, google, client):
        assert token not in masked


def test_jwt_requires_valid_header() -> None:
    token = _jwt()
    assert RULE_JWT in _rules(f"Authorization: Bearer {token}")
    # Same shape, but the first segment is not a JSON header.
    fake = _join("eyJ", "xxxxYYYY", ".", "zzzzzzzzzz", ".", "wwwwwwww")
    assert RULE_JWT not in _rules(fake)
    assert token not in mask(token)


def test_private_key_block() -> None:
    pem = _private_key_pem()
    findings = scan(f"config:\n{pem}\n")
    assert any(f.rule == RULE_PRIVATE_KEY for f in findings)
    masked = mask(pem)
    assert "BEGIN" not in masked or "PRIVATE" not in masked
    assert "MMMM" not in masked
    assert "REDACTED" in masked


def test_generic_high_entropy_assignment() -> None:
    value = _generic_value()
    text = f"api_token = {value}"
    assert RULE_GENERIC in _rules(text)
    assert value not in mask(text)


def test_generic_low_entropy_or_placeholder_not_flagged() -> None:
    samples = [
        "token=hello",
        "token=aaaaaaaaaaaaaaaaaaaa",
        "api_key=$VAULT_ROOT",
        "api_key=${VAULT_ROOT}",
        "secret=<your-token-here>",
        "password=changeme",
        "token=example",
        "key=name",
    ]
    for text in samples:
        assert scan(text) == [], text


def test_mask_merges_overlapping_spans() -> None:
    token = _github_classic()
    # The same span can also look like a generic assignment.
    text = f"token={token}"
    masked = mask(text)
    assert token not in masked
    assert masked.count("REDACTED") >= 1
    assert "ghp" not in masked or "A" * 36 not in masked


def test_finding_does_not_expose_secret_attributes() -> None:
    token = _openai_key()
    findings = scan(token)
    assert findings
    dumped = " ".join(
        list(findings[0].__dict__.keys()) + [repr(item) for item in findings]
    )
    assert token not in dumped
    assert not hasattr(findings[0], "value")
    assert not hasattr(findings[0], "secret")
    assert not hasattr(findings[0], "match")


def test_shannon_entropy_bounds() -> None:
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy("ab") == 1.0
    assert shannon_entropy(_generic_value()) >= 4.0


def test_line_and_column() -> None:
    token = _github_classic()
    text = f"first line\n  {token}"
    findings = scan(text)
    assert findings[0].line == 2
    assert findings[0].column == 3


def test_cli_scan_exits_nonzero_on_hit(tmp_path: Path) -> None:
    token = _anthropic_key()
    clean = tmp_path / "clean.txt"
    dirty = tmp_path / "dirty.txt"
    clean.write_text("no secrets here, only documentation.\n", encoding="utf-8")
    dirty.write_text(f"credential: {token}\n", encoding="utf-8")

    clean_proc = subprocess.run(
        [sys.executable, "-m", "vault_search.secrets", "scan", str(clean)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert clean_proc.returncode == 0, clean_proc.stderr
    assert clean_proc.stdout == ""

    dirty_proc = subprocess.run(
        [sys.executable, "-m", "vault_search.secrets", "scan", str(dirty)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert dirty_proc.returncode == 1, dirty_proc.stderr
    assert "anthropic_api_key" in dirty_proc.stdout
    assert token not in dirty_proc.stdout
    assert token not in dirty_proc.stderr


def test_cli_scan_directory_and_missing_path(tmp_path: Path) -> None:
    nested = tmp_path / "notes"
    nested.mkdir()
    (nested / "ok.md").write_text("a heading and some prose\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "vault_search.secrets", "scan", str(nested)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    missing = subprocess.run(
        [sys.executable, "-m", "vault_search.secrets", "scan", str(tmp_path / "nope")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    assert "path not found" in missing.stderr


def test_cli_mask_strips_secret_from_stdin() -> None:
    token = _google_api_key()
    proc = subprocess.run(
        [sys.executable, "-m", "vault_search.secrets", "mask"],
        check=False,
        capture_output=True,
        text=True,
        input=f"key={token}\n",
    )
    assert proc.returncode == 0
    assert token not in proc.stdout
    assert "REDACTED" in proc.stdout
