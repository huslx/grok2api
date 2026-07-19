"""Browser login session cookie for local media access.

Admin / WebUI auth is stored in localStorage and sent as Bearer on XHR.
``<img src>`` / address-bar navigation cannot attach Authorization headers,
so a short signed HttpOnly cookie is set on successful login/verify and
accepted by ``/v1/files/*`` as proof of an authenticated browser session.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Literal

from fastapi import Response

from app.platform.auth.media_sign import media_signing_secret, media_url_ttl_sec

SessionRole = Literal["admin", "webui"]

SESSION_COOKIE_NAME = "grok2api_session"
_VALID_ROLES = frozenset({"admin", "webui"})


def issue_session_value(role: SessionRole, *, ttl_sec: int | None = None) -> str:
    """Return ``{role}.{exp}.{sig}`` for the session cookie value."""
    if role not in _VALID_ROLES:
        raise ValueError(f"invalid session role: {role}")
    exp = int(time.time()) + int(ttl_sec if ttl_sec is not None else media_url_ttl_sec())
    return f"{role}.{exp}.{_signature(role, exp)}"


def verify_session_value(value: str | None, *, now: int | None = None) -> bool:
    """Return True when *value* is a non-expired, well-formed session cookie."""
    if not value:
        return False
    parts = str(value).split(".")
    if len(parts) != 3:
        return False
    role, exp_s, sig = parts
    if role not in _VALID_ROLES or not sig:
        return False
    try:
        exp = int(exp_s)
    except (TypeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if exp < current:
        return False
    expected = _signature(role, exp)
    return hmac.compare_digest(expected, sig)


def attach_session_cookie(
    response: Response,
    role: SessionRole,
    *,
    ttl_sec: int | None = None,
) -> None:
    """Set the HttpOnly session cookie on *response* (path=/ so /v1/files sees it)."""
    ttl = int(ttl_sec if ttl_sec is not None else media_url_ttl_sec())
    ttl = max(60, ttl)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=issue_session_value(role, ttl_sec=ttl),
        max_age=ttl,
        httponly=True,
        samesite="lax",
        path="/",
        # localhost is http in dev; secure=False keeps cookie usable there.
        secure=False,
    )


def clear_session_cookie(response: Response) -> None:
    """Expire the session cookie."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
    )


def _signature(role: str, exp: int) -> str:
    msg = f"session:{role}:{exp}".encode("utf-8")
    digest = hmac.new(media_signing_secret(), msg, hashlib.sha256).hexdigest()
    return digest[:32]


__all__ = [
    "SESSION_COOKIE_NAME",
    "SessionRole",
    "issue_session_value",
    "verify_session_value",
    "attach_session_cookie",
    "clear_session_cookie",
]
