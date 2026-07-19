"""HMAC-signed local media URLs.

Local image/video proxy endpoints are often embedded in Markdown / HTML
without an Authorization header.  Signed query params keep those embeds
working while preventing unauthenticated enumeration of cached files.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from app.platform.auth.middleware import get_admin_key, get_api_keys
from app.platform.config.snapshot import get_config

_DEFAULT_TTL_SEC = 7 * 24 * 3600  # 7 days


def media_signing_secret() -> bytes:
    """Derive signing secret from admin key (stable) or API key.

    Prefer ``app.app_key`` so rotating ``api_key`` does not invalidate
    already-issued Markdown/HTML media embeds.
    """
    admin = get_admin_key()
    if admin:
        return admin.encode("utf-8")
    keys = get_api_keys()
    if keys:
        return keys[0].encode("utf-8")
    # Last resort — still signs, but weak if defaults are unchanged.
    return b"grok2api-media"


def media_url_ttl_sec() -> int:
    cfg = get_config()
    try:
        ttl = int(cfg.get("cache.local.signed_url_ttl_sec", _DEFAULT_TTL_SEC))
    except (TypeError, ValueError):
        ttl = _DEFAULT_TTL_SEC
    return max(60, ttl)


def sign_media_query(media_kind: str, file_id: str, *, ttl_sec: int | None = None) -> dict[str, str]:
    """Return query params ``id``, ``exp``, ``sig`` for a local media URL."""
    exp = int(time.time()) + int(ttl_sec if ttl_sec is not None else media_url_ttl_sec())
    sig = _signature(media_kind, file_id, exp)
    return {"id": file_id, "exp": str(exp), "sig": sig}


def build_signed_media_path(media_kind: str, file_id: str, *, ttl_sec: int | None = None) -> str:
    """Return path+query such as ``/v1/files/image?id=...&exp=...&sig=...``."""
    params = sign_media_query(media_kind, file_id, ttl_sec=ttl_sec)
    return f"/v1/files/{media_kind}?{urlencode(params)}"


def build_signed_media_url(
    media_kind: str,
    file_id: str,
    *,
    app_url: str = "",
    ttl_sec: int | None = None,
) -> str:
    path = build_signed_media_path(media_kind, file_id, ttl_sec=ttl_sec)
    base = (app_url or "").rstrip("/")
    return f"{base}{path}" if base else path


def verify_media_signature(
    media_kind: str,
    file_id: str,
    exp: str | int | None,
    sig: str | None,
    *,
    now: int | None = None,
) -> bool:
    """Return True when *sig*/*exp* form a valid, non-expired signature."""
    if not file_id or exp is None or not sig:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if exp_i < current:
        return False
    expected = _signature(media_kind, file_id, exp_i)
    return hmac.compare_digest(expected, str(sig))


def _signature(media_kind: str, file_id: str, exp: int) -> str:
    msg = f"{media_kind}:{file_id}:{exp}".encode("utf-8")
    digest = hmac.new(media_signing_secret(), msg, hashlib.sha256).hexdigest()
    return digest[:32]


__all__ = [
    "media_signing_secret",
    "media_url_ttl_sec",
    "sign_media_query",
    "build_signed_media_path",
    "build_signed_media_url",
    "verify_media_signature",
]
