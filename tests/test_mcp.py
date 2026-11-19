"""MCP server: bind policy, Host check, audit, and verify-deploy e2e on loopback."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vault_search.client import Client
from vault_search.config import Settings

REPO = Path(__file__).resolve().parents[1]
VERIFY = REPO / "scripts" / "verify-deploy"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_port(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"server did not listen on 127.0.0.1:{port}")


def _token() -> str:
    return "gh" + "p_" + ("C" * 36)


def test_refuse_nonlocal_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "VAULT_ROOT": str(vault),
            "INDEX_PATH": str(tmp_path / "index.db"),
            "LISTEN_HOST": "0.0.0.0",
            "LISTEN_PORT": "18787",
            "PYTHONPATH": str(REPO),
        }
    )
    env.pop("LISTEN_ALLOW_NONLOCAL", None)
    proc = subprocess.run(
        [sys.executable, "-m", "vault_search.mcp"],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "LISTEN_ALLOW_NONLOCAL" in (proc.stderr or "")


def test_verify_deploy_against_loopback_server(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    # Copy synthetic fixtures and add a page carrying a runtime-assembled token.
    for src in FIXTURES.rglob("*.md"):
        dest = vault / src.relative_to(FIXTURES)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    secret = _token()
    fixture_path = "wiki/concepts/secret-note.md"
    (vault / fixture_path).parent.mkdir(parents=True, exist_ok=True)
    (vault / fixture_path).write_text(
        "---\ntitle: Secret note\ntype: concept\n---\n\nToken " + secret + " stays in the fixture only.\n",
        encoding="utf-8",
    )

    index_path = tmp_path / "index.db"
    settings = Settings(vault_root=vault, index_path=index_path)
    with Client(settings, autoload=False) as client:
        client.reindex()
        assert secret not in client.read_note(fixture_path)
        assert "[REDACTED:" in client.read_note(fixture_path)

    audit = tmp_path / "audit.jsonl"
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "VAULT_ROOT": str(vault),
            "INDEX_PATH": str(index_path),
            "LISTEN_HOST": "127.0.0.1",
            "LISTEN_PORT": str(port),
            "MCP_PATH": "/vault/mcp",
            "PUBLIC_BASE_URL": "https://box.example.ts.net",
            "AUDIT_LOG_PATH": str(audit),
            "PYTHONPATH": str(REPO),
        }
    )
    env.pop("LISTEN_ALLOW_NONLOCAL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "vault_search.mcp"],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        try:
            _wait_port(port, timeout=12)
        except AssertionError:
            leftover = proc.poll()
            out, err = ("", "")
            if leftover is not None:
                out, err = proc.communicate(timeout=5)
            raise AssertionError(
                f"server did not listen (exit={leftover}):\n{out}\n{err}"
            ) from None
        verify = subprocess.run(
            [sys.executable, str(VERIFY)],
            cwd=str(REPO),
            env={
                **os.environ,
                "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
                "MCP_PATH": "/vault/mcp",
                "VERIFY_FIXTURE_PATH": fixture_path,
                "VERIFY_SYNTHETIC_SECRET": secret,
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert verify.returncode == 0, verify.stdout + "\n" + verify.stderr
        assert "PASS  initialize" in verify.stdout
        assert "PASS  tools/list" in verify.stdout
        assert "PASS  masked fixture" in verify.stdout
        assert secret not in verify.stdout
        assert secret not in verify.stderr
        log = audit.read_text(encoding="utf-8")
        assert "read_note" in log
        assert '"client_ip": "127.0.0.1"' in log
        # The proxied MagicDNS Host is accepted; an unknown Host still gets 421.
        assert _initialize_status(port, "box.example.ts.net") == 200
        assert _initialize_status(port, "attacker.example") == 421
        assert secret not in log
        assert "[REDACTED:" not in log or "masked_hits" in log
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _initialize_status(port: int, host: str) -> int:
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
        b'"capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/vault/mcp",
        data=body,
        headers={
            "Host": host,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
