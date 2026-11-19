"""Caller details for the audit log. Access control is the tailnet's job, not the app's.

Forwarded headers are trusted only when the immediate TCP peer is loopback
(nginx on the same host). The only rejection is a request that carries
Tailscale's Funnel header, as defence in depth if Funnel is ever enabled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
AGENT_HEADER = "x-vault-agent-name"
REAL_IP_HEADER = "x-real-ip"
FORWARDED_FOR_HEADER = "x-forwarded-for"
FUNNEL_HEADER = "tailscale-funnel-request"


@dataclass(frozen=True)
class Caller:
    ip: str | None
    agent_name_unverified: str | None = None
    allowed: bool = True
    deny_reason: str | None = None


def header_map(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    value = host.strip().strip("[]").split("%")[0].lower()
    value = value.removeprefix("::ffff:")
    return value in LOOPBACK_HOSTS


def client_ip(headers: Mapping[str, str] | None, peer: str | None) -> str | None:
    """Return the caller IP: the forwarded value from a loopback peer, else the peer."""
    mapped = header_map(headers)
    if is_loopback(peer):
        for key in (REAL_IP_HEADER, FORWARDED_FOR_HEADER):
            ip = mapped.get(key, "").split(",")[0].strip()
            if ip:
                return ip
    return peer


def agent_name(headers: Mapping[str, str] | None) -> str | None:
    value = header_map(headers).get(AGENT_HEADER)
    if not value:
        return None
    return " ".join(value.split())[:120] or None


def funnel_requested(headers: Mapping[str, str] | None) -> bool:
    value = header_map(headers).get(FUNNEL_HEADER)
    return bool(value and value.strip())


def resolve_caller(*, headers: Mapping[str, str] | None, peer: str | None) -> Caller:
    caller = Caller(ip=client_ip(headers, peer), agent_name_unverified=agent_name(headers))
    if funnel_requested(headers):
        return Caller(caller.ip, caller.agent_name_unverified, allowed=False, deny_reason="funnel")
    return caller
