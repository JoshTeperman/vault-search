"""Tests for scripts/verify-deploy. No live VPS; local mocks and runtime-built secrets only."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from vault_search.secrets import RULE_GITHUB

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-deploy"


def _load_script():
    loader = importlib.machinery.SourceFileLoader("verify_deploy_script", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


vd = _load_script()


def _github_token() -> str:
    return "gh" + "p_" + ("A" * 36)


def test_parse_rpc_json_and_sse() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert vd.parse_rpc_body(json.dumps(payload))["result"]["ok"] is True
    sse = "event: message\ndata: " + json.dumps(payload) + "\n\n"
    assert vd.parse_rpc_body(sse, "text/event-stream")["id"] == 1


def test_tool_names_and_masking_helpers() -> None:
    payload = {
        "result": {
            "tools": [
                {"name": "search"},
                {"name": "read_note"},
                {"name": "list_notes"},
                {"name": "list_projects"},
            ]
        }
    }
    assert tuple(vd.tool_names(payload)) == vd.EXPECTED_TOOLS
    token = _github_token()
    placeholder = "[REDACTED:" + RULE_GITHUB + "]"
    ok, reason = vd.masking_ok(placeholder + " and a snippet", token)
    assert ok, reason
    leaked, leaked_reason = vd.masking_ok(f"leaked {token}", token)
    assert not leaked
    assert "unmasked" in leaked_reason
    assert token not in leaked_reason
    missing, missing_reason = vd.masking_ok("just a note, nothing redacted", token)
    assert not missing
    assert "REDACTED" in missing_reason


def test_text_from_tool_result() -> None:
    payload = {"result": {"content": [{"type": "text", "text": "hello"}]}}
    assert vd.text_from_tool_result(payload) == "hello"


def test_urls() -> None:
    assert vd.mcp_url("https://<vps>.<tailnet>.ts.net", "/vault/mcp") == (
        "https://<vps>.<tailnet>.ts.net/vault/mcp"
    )
    assert vd.public_ip_url("203.0.113.10", "/vault/mcp") == "https://203.0.113.10/vault/mcp"
    assert vd.public_ip_url("2001:db8::1", "/vault/mcp") == "https://[2001:db8::1]/vault/mcp"


class _Handler(BaseHTTPRequestHandler):
    secret = ""
    fixture_path = "wiki/fixture.md"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _write(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "session-test")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        message = json.loads(raw.decode("utf-8"))
        method = message.get("method")
        if method == "initialize":
            self._write({"jsonrpc": "2.0", "id": message.get("id"), "result": {"protocolVersion": "2026-07-28"}})
            return
        if method == "tools/list":
            tools = [{"name": name} for name in vd.EXPECTED_TOOLS]
            self._write({"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": tools}})
            return
        if method == "tools/call":
            placeholder = "[REDACTED:" + RULE_GITHUB + "]"
            text = f"title\n{placeholder}\nsnippet"
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            )
            return
        self._write({"jsonrpc": "2.0", "id": message.get("id"), "error": {"message": "unknown"}}, 400)

    def do_GET(self) -> None:  # noqa: N802
        self._write({"error": "nope"}, 200)


def _serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_script_initialize_tools_and_mask() -> None:
    token = _github_token()
    server, base = _serve()
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--base-url",
                base,
                "--mcp-path",
                "/vault/mcp",
                "--fixture-path",
                "wiki/fixture.md",
                "--synthetic-secret",
                token,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "PASS  initialize" in proc.stdout
        assert "PASS  tools/list" in proc.stdout
        assert "PASS  masked fixture" in proc.stdout
        assert token not in proc.stdout
        assert token not in proc.stderr
    finally:
        server.shutdown()
        server.server_close()


def test_script_usage_without_base_url() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": __import__("os").environ.get("PATH", "")},
    )
    assert proc.returncode == 2
    assert "PUBLIC_BASE_URL" in proc.stderr


def test_public_ip_closed_port_counts_as_pass() -> None:
    # 127.0.0.1 with HTTPS should not serve MCP; the check must pass (failed as expected).
    result = vd.check_public_ip_unreachable("127.0.0.1", "/vault/mcp", timeout=2)
    assert result.ok, result.detail
    assert "UNEXPECTED SUCCESS" not in result.detail
