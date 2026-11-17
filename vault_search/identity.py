"""Resolve the calling device via Tailscale WhoIs. Headers are untrusted off loopback."""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})
AGENT_HEADER = "x-vault-agent-name"
REAL_IP_HEADER = "x-real-ip"
FORWARDED_FOR_HEADER = "x-forwarded-for"
FUNNEL_HEADER = "tailscale-funnel-request"

WhoIsFn = Callable[[str], "WhoIs"]


class WhoIsError(RuntimeError):
    """LocalAPI WhoIs failed or returned no node."""


@dataclass(frozen=True)
class WhoIs:
    node: str
    device: str
    tags: tuple[str, ...] = ()
    login: str | None = None
    stable_id: str | None = None

    def identities(self) -> tuple[str, ...]:
        values = [self.device, self.node, self.login, self.stable_id, *self.tags]
        return tuple(v for v in values if v)


@dataclass(frozen=True)
class Caller:
    ip: str | None
    node: str | None = None
    device: str | None = None
    tags: tuple[str, ...] = ()
    login: str | None = None
    agent_name_unverified: str | None = None
    allowed: bool = False
    deny_reason: str | None = None
    identities: tuple[str, ...] = field(default_factory=tuple)


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over a Unix domain socket (tailscaled LocalAPI)."""

    def __init__(self, socket_path: Path, timeout: float = 2.0) -> None:
        super().__init__("local-tailscaled.sock", timeout=timeout)
        self._socket_path = str(socket_path)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def header_map(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    value = host.strip().strip("[]").split("%")[0].lower()
    if value.startswith("::ffff:"):
        value = value.split(":")[-1] if value.count(":") == 3 and "." in value else value
        # ::ffff:127.0.0.1
        if value.startswith("::ffff:"):
            mapped = value[len("::ffff:") :]
            return mapped in LOOPBACK_HOSTS or mapped == "127.0.0.1"
    return value in LOOPBACK_HOSTS


def _first_forwarded(value: str) -> str:
    # Left-most hop is the original client when nginx appends correctly.
    return value.split(",")[0].strip()


def client_ip(
    headers: Mapping[str, str] | None,
    peer: str | None,
) -> str | None:
    """Return the caller IP.

    `X-Real-IP` / `X-Forwarded-For` are trusted only when the immediate TCP
    peer is loopback (nginx on the same host). Otherwise the peer itself is used.
    """
    mapped = header_map(headers)
    if is_loopback(peer):
        for key in (REAL_IP_HEADER, FORWARDED_FOR_HEADER):
            raw = mapped.get(key)
            if raw:
                ip = _first_forwarded(raw)
                if ip:
                    return ip
    return peer


def agent_name(headers: Mapping[str, str] | None) -> str | None:
    value = header_map(headers).get(AGENT_HEADER)
    if not value:
        return None
    cleaned = " ".join(value.split())
    return cleaned[:120] or None


def funnel_requested(headers: Mapping[str, str] | None) -> bool:
    value = header_map(headers).get(FUNNEL_HEADER)
    return bool(value and value.strip())


def _node_label(name: str | None, computed: str | None) -> tuple[str, str]:
    node = (name or "").rstrip(".")
    device = (computed or "").strip()
    if not device and node:
        device = node.split(".")[0]
    if not node:
        node = device
    return node, device


def parse_whois(payload: dict) -> WhoIs:
    node = payload.get("Node") or payload.get("node") or {}
    if not isinstance(node, dict):
        node = {}
    profile = payload.get("UserProfile") or payload.get("userProfile") or {}
    if not isinstance(profile, dict):
        profile = {}
    tags_raw = node.get("Tags") or node.get("tags") or ()
    tags: list[str] = []
    if isinstance(tags_raw, (list, tuple)):
        for tag in tags_raw:
            text = str(tag).strip()
            if text:
                tags.append(text)
    node_name, device = _node_label(
        node.get("Name") or node.get("name"),
        node.get("ComputedName") or node.get("computedName"),
    )
    if not node_name and not device:
        raise WhoIsError("whois response had no node name")
    login = profile.get("LoginName") or profile.get("loginName")
    login_s = str(login).strip() if login else None
    stable = node.get("StableID") or node.get("stableID")
    stable_s = str(stable).strip() if stable else None
    return WhoIs(
        node=node_name,
        device=device or node_name,
        tags=tuple(tags),
        login=login_s or None,
        stable_id=stable_s or None,
    )


def whois_lookup(addr: str, socket_path: Path, timeout: float = 2.0) -> WhoIs:
    """GET /localapi/v0/whois?addr=… on the tailscaled LocalAPI socket."""
    if not addr:
        raise WhoIsError("missing address")
    path = Path(socket_path)
    if not path.exists():
        raise WhoIsError(f"tailscaled socket not found: {path}")
    # WhoIs accepts IP or IP:port. Strip a URL scheme if a caller passed one.
    parsed = urlparse(addr)
    target = parsed.hostname or addr
    if parsed.port:
        target = f"{target}:{parsed.port}"
    request = f"/localapi/v0/whois?addr={quote(target)}"
    conn = UnixHTTPConnection(path, timeout=timeout)
    try:
        conn.request("GET", request, headers={"Host": "local-tailscaled.sock"})
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise WhoIsError(f"whois HTTP {response.status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WhoIsError("whois returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise WhoIsError("whois returned a non-object")
        return parse_whois(payload)
    except WhoIsError:
        raise
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise WhoIsError(f"whois failed: {exc}") from exc
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _normalize_identity(value: str) -> str:
    text = value.strip().casefold().rstrip(".")
    if text.startswith("tag:"):
        return text
    return text


def is_denied(identities: tuple[str, ...], denylist: tuple[str, ...]) -> bool:
    if not denylist or not identities:
        return False
    blocked = {_normalize_identity(item) for item in denylist}
    # Also allow denylist entries written as tags without the tag: prefix.
    blocked |= {item.removeprefix("tag:") for item in blocked}
    for identity in identities:
        key = _normalize_identity(identity)
        if key in blocked or key.removeprefix("tag:") in blocked:
            return True
    return False


def resolve_caller(
    *,
    headers: Mapping[str, str] | None,
    peer: str | None,
    denylist: tuple[str, ...] = (),
    whois: WhoIsFn | None = None,
    socket_path: Path | None = None,
) -> Caller:
    unverified = agent_name(headers)
    if funnel_requested(headers):
        return Caller(
            ip=client_ip(headers, peer),
            agent_name_unverified=unverified,
            allowed=False,
            deny_reason="funnel",
        )
    ip = client_ip(headers, peer)
    if not ip:
        return Caller(
            ip=None,
            agent_name_unverified=unverified,
            allowed=False,
            deny_reason="missing_client_ip",
        )
    lookup = whois or (lambda addr: whois_lookup(addr, socket_path or Path("/var/run/tailscale/tailscaled.sock")))
    try:
        info = lookup(ip)
    except WhoIsError:
        return Caller(
            ip=ip,
            agent_name_unverified=unverified,
            allowed=False,
            deny_reason="whois_failed",
        )
    identities = info.identities()
    if is_denied(identities, denylist):
        return Caller(
            ip=ip,
            node=info.node,
            device=info.device,
            tags=info.tags,
            login=info.login,
            agent_name_unverified=unverified,
            allowed=False,
            deny_reason="denied",
            identities=identities,
        )
    return Caller(
        ip=ip,
        node=info.node,
        device=info.device,
        tags=info.tags,
        login=info.login,
        agent_name_unverified=unverified,
        allowed=True,
        identities=identities,
    )
