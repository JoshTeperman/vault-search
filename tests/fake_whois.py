"""Tiny Unix-socket LocalAPI WhoIs stand-in for tests."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SAMPLE_NODE = {
    "Node": {
        "Name": "fixture-device.example.ts.net.",
        "ComputedName": "fixture-device",
        "StableID": "stab-fixture",
        "Tags": ["tag:vault-reader"],
    },
    "UserProfile": {"LoginName": "fixture@example.com"},
}


def start_whois_socket(
    path: Path,
    responses: dict[str, dict] | None = None,
    default: dict | None = SAMPLE_NODE,
) -> threading.Event:
    """Serve GET /localapi/v0/whois?addr=… on a Unix socket. Returns a stop event."""
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(16)
    server.settimeout(0.3)
    stop = threading.Event()
    table = responses or {}

    def _loop() -> None:
        try:
            while not stop.is_set():
                try:
                    conn, _ignored = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    _handle(conn, table, default)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
        finally:
            try:
                server.close()
            except OSError:
                pass
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    thread = threading.Thread(target=_loop, name="fake-whois", daemon=True)
    thread.start()
    return stop


def _handle(conn: socket.socket, table: dict[str, dict], default: dict | None) -> None:
    conn.settimeout(1.0)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError:
        return
    try:
        request_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
    except Exception:
        _send(conn, 400, b"bad request")
        return
    parts = request_line.split()
    if len(parts) < 2:
        _send(conn, 400, b"bad request")
        return
    parsed = urlparse(parts[1])
    addr = (parse_qs(parsed.query).get("addr") or [""])[0]
    payload = table.get(addr) or table.get(addr.split(":")[0]) or default
    if payload is None:
        _send(conn, 404, b"not found")
        return
    body = json.dumps(payload).encode("utf-8")
    _send(conn, 200, body, content_type="application/json")


def _send(conn: socket.socket, status: int, body: bytes, content_type: str = "text/plain") -> None:
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "Error")
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        conn.sendall(header + body)
    except OSError:
        pass
