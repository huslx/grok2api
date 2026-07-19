"""Outbound URL safety checks (SSRF guard).

Used when the gateway fetches user-controlled HTTP(S) URLs (e.g. vision
attachments, image-edit inputs).  Blocks:

* non-http(s) schemes
* localhost / metadata hostnames
* private, loopback, link-local, reserved, and multicast resolved IPs
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.platform.errors import ValidationError

# Hostnames that must never be fetched, even if DNS resolves to a public IP.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
})

_BLOCKED_HOSTNAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
)


def _hostname_blocked(hostname: str) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES:
        return True
    return any(host.endswith(suffix) for suffix in _BLOCKED_HOSTNAME_SUFFIXES)


def _ip_is_non_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
            getattr(ip, "is_site_local", False),
        )
    )


def _literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    host = hostname.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _resolve_host_ips(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve *hostname* and return unique IP addresses."""
    literal = _literal_ip(hostname)
    if literal is not None:
        return [literal]

    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValidationError(
            f"Cannot resolve fetch URL host: {hostname}",
            param="content",
            code="unsafe_fetch_url",
        ) from exc

    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr in seen:
            continue
        seen.add(addr)
        try:
            ips.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    if not ips:
        raise ValidationError(
            f"Cannot resolve fetch URL host: {hostname}",
            param="content",
            code="unsafe_fetch_url",
        )
    return ips


def assert_safe_fetch_url(url: str, *, param: str = "content") -> str:
    """Validate that *url* is safe to fetch from the gateway process.

    Returns the stripped URL on success; raises ``ValidationError`` otherwise.
    """
    value = (url or "").strip()
    if not value:
        raise ValidationError("Fetch URL cannot be empty", param=param, code="unsafe_fetch_url")

    try:
        parsed = urlparse(value)
    except Exception as exc:
        raise ValidationError(
            "Malformed fetch URL",
            param=param,
            code="unsafe_fetch_url",
        ) from exc

    if parsed.scheme not in {"http", "https"}:
        raise ValidationError(
            "Fetch URL scheme must be http or https",
            param=param,
            code="unsafe_fetch_url",
        )
    if parsed.username or parsed.password:
        raise ValidationError(
            "Fetch URL must not contain embedded credentials",
            param=param,
            code="unsafe_fetch_url",
        )

    hostname = parsed.hostname or ""
    if _hostname_blocked(hostname):
        raise ValidationError(
            "Fetch URL host is not allowed",
            param=param,
            code="unsafe_fetch_url",
        )

    # Port sanity (optional explicit ports only).
    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise ValidationError(
            "Fetch URL port is invalid",
            param=param,
            code="unsafe_fetch_url",
        )

    for ip in _resolve_host_ips(hostname):
        if _ip_is_non_public(ip):
            raise ValidationError(
                "Fetch URL resolves to a non-public address",
                param=param,
                code="unsafe_fetch_url",
            )

    return value


__all__ = ["assert_safe_fetch_url"]
