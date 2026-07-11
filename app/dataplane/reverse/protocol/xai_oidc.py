"""xAI OIDC helpers for Grok CLI chat proxy.

SSO cookie (grok.com / accounts.x.ai) → device-flow OIDC access_token,
compatible with cli-chat-proxy.grok.com (same approach as HM2899/grokcli-2api).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger

OIDC_ISSUER = "https://auth.x.ai"
GROK_CLI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPES = (
    "openid profile email offline_access grok-cli:access api:access "
    "conversations:read conversations:write"
)
OIDC_DEVICE_URL = f"{OIDC_ISSUER}/oauth2/device/code"
OIDC_TOKEN_URL = f"{OIDC_ISSUER}/oauth2/token"

# Process-local cache: sso_token → oidc credential dict
_OIDC_CACHE: dict[str, dict[str, Any]] = {}
_REFRESH_SKEW_S = 120.0
_DISK_LOCK = threading.Lock()
_DISK_LOADED = False

def _normalize_sso(sso_token: str) -> str:
    tok = sso_token[4:] if sso_token.startswith("sso=") else sso_token
    return tok.strip()


# Default on-disk cache written by scripts/sso_to_oidc.py
def _default_disk_path() -> Path:
    env = os.getenv("GROK_OIDC_AUTH_FILE", "").strip()
    if env:
        return Path(env)
    # xai_oidc.py → protocol → reverse → dataplane → app → root
    root = Path(__file__).resolve().parents[4]
    return root / "data" / "oidc_auth.json"


def sso_key(sso_token: str) -> str:
    """Stable lookup key for disk cache (sha256 of normalized SSO)."""
    return hashlib.sha256(_normalize_sso(sso_token).encode("utf-8")).hexdigest()


def _b64url_json(segment: str) -> dict[str, Any]:
    try:
        pad = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    return _b64url_json(parts[1])

def _expires_at_from_token(access: str, expires_in: float | None = None) -> float:
    claims = decode_jwt_claims(access)
    exp = claims.get("exp")
    if exp is not None:
        try:
            return float(exp)
        except (TypeError, ValueError):
            pass
    if expires_in is not None:
        return time.time() + float(expires_in)
    return time.time() + 21_600.0


def _is_fresh(cred: dict[str, Any]) -> bool:
    access = cred.get("access_token") or ""
    if not access:
        return False
    exp = float(cred.get("expires_at") or 0)
    return exp > (time.time() + _REFRESH_SKEW_S)


def _request_device_code() -> dict[str, Any]:
    data = urllib.parse.urlencode(
        {"client_id": GROK_CLI_CLIENT_ID, "scope": OIDC_SCOPES}
    ).encode()
    req = urllib.request.Request(
        OIDC_DEVICE_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise UpstreamError(
            f"OIDC device/code failed: {exc.code} {body}",
            status=502,
            body=body,
        ) from exc


def _poll_token(device_code: str, interval: int, expires_in: int) -> dict[str, Any]:
    deadline = time.time() + min(int(expires_in or 1800), 90)
    wait = max(1, int(interval or 5))
    last_err = "authorization_pending"
    while time.time() < deadline:
        time.sleep(wait)
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": GROK_CLI_CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            OIDC_TOKEN_URL,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:
                err = {}
            last_err = str(err.get("error") or exc.code)
            if last_err == "authorization_pending":
                continue
            if last_err == "slow_down":
                wait += 2
                continue
            raise UpstreamError(
                f"OIDC token poll failed: {last_err}",
                status=502,
                body=str(err)[:300],
            ) from exc
    raise UpstreamError(f"OIDC token poll timeout: {last_err}", status=502)


def _refresh_token(refresh_token: str) -> dict[str, Any]:
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": GROK_CLI_CLIENT_ID,
            "refresh_token": refresh_token,
        }
    ).encode()
    req = urllib.request.Request(
        OIDC_TOKEN_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise UpstreamError(
            f"OIDC refresh failed: {exc.code} {body}",
            status=401,
            body=body,
        ) from exc


def _sso_device_approve(sso_token: str, device: dict[str, Any]) -> None:
    """Use SSO session cookie to approve a device code (headless)."""
    from curl_cffi import requests as crequests

    sso = _normalize_sso(sso_token)
    session = crequests.Session()
    session.cookies.set("sso", sso, domain=".x.ai")

    try:
        probe = session.get("https://accounts.x.ai/", impersonate="chrome", timeout=20)
    except Exception as exc:
        raise UpstreamError(f"OIDC SSO probe failed: {exc}", status=502) from exc
    if "sign-in" in (probe.url or "") or "sign-up" in (probe.url or ""):
        raise UpstreamError("SSO cookie invalid for OIDC conversion", status=401)

    user_code = device.get("user_code") or ""
    verify_uri = device.get("verification_uri_complete") or ""
    if not user_code or not verify_uri:
        raise UpstreamError("OIDC device payload incomplete", status=502)

    try:
        session.get(verify_uri, impersonate="chrome", timeout=20)
        verify = session.post(
            f"{OIDC_ISSUER}/oauth2/device/verify",
            data={"user_code": user_code},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=20,
            allow_redirects=True,
        )
        if "consent" not in (verify.url or ""):
            raise UpstreamError(
                f"OIDC device verify failed: {verify.url}",
                status=502,
            )
        approve = session.post(
            f"{OIDC_ISSUER}/oauth2/device/approve",
            data={
                "user_code": user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=20,
            allow_redirects=True,
        )
        if "done" not in (approve.url or ""):
            raise UpstreamError(
                f"OIDC device approve failed: {approve.url}",
                status=502,
            )
    except UpstreamError:
        raise
    except Exception as exc:
        raise UpstreamError(f"OIDC device approve transport failed: {exc}", status=502) from exc


def _token_response_to_cred(token_data: dict[str, Any], *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    access = token_data.get("access_token") or token_data.get("key")
    if not access or not isinstance(access, str):
        raise UpstreamError("OIDC response missing access_token", status=502)
    refresh = token_data.get("refresh_token") or (previous or {}).get("refresh_token") or ""
    expires_in = token_data.get("expires_in")
    try:
        expires_in_f = float(expires_in) if expires_in is not None else None
    except (TypeError, ValueError):
        expires_in_f = None
    claims = decode_jwt_claims(access)
    return {
        "access_token": access,
        "refresh_token": refresh if isinstance(refresh, str) else "",
        "expires_at": _expires_at_from_token(access, expires_in_f),
        "user_id": claims.get("principal_id") or claims.get("sub") or "",
        "team_id": claims.get("team_id") or "",
        "scope": claims.get("scope") or token_data.get("scope") or "",
    }


def sso_to_oidc(sso_token: str) -> dict[str, Any]:
    """Convert SSO cookie to OIDC access/refresh credentials (blocking)."""
    device = _request_device_code()
    _sso_device_approve(sso_token, device)
    token_data = _poll_token(
        str(device.get("device_code") or ""),
        int(device.get("interval") or 5),
        int(device.get("expires_in") or 1800),
    )
    cred = _token_response_to_cred(token_data)
    logger.info(
        "oidc converted from sso: user_id={} team_id={} expires_in≈{}",
        str(cred.get("user_id") or "")[:12],
        str(cred.get("team_id") or "")[:12],
        int(float(cred["expires_at"]) - time.time()),
    )
    return cred


def refresh_oidc(cred: dict[str, Any]) -> dict[str, Any]:
    refresh = cred.get("refresh_token") or ""
    if not refresh:
        raise UpstreamError("OIDC refresh_token missing", status=401)
    token_data = _refresh_token(refresh)
    return _token_response_to_cred(token_data, previous=cred)


def load_disk_cache(path: Path | None = None) -> dict[str, Any]:
    """Load oidc_auth.json → {"entries": {sso_sha256: cred}}."""
    p = path or _default_disk_path()
    if not p.is_file():
        return {"version": 1, "entries": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"version": 1, "entries": {}}
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            raw["entries"] = {}
        return raw
    except Exception:
        return {"version": 1, "entries": {}}


def save_disk_entry(
    sso_token: str,
    cred: dict[str, Any],
    *,
    path: Path | None = None,
) -> Path:
    """Upsert one OIDC credential into data/oidc_auth.json."""
    p = path or _default_disk_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _DISK_LOCK:
        data = load_disk_cache(p)
        entries = data.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            data["entries"] = entries
        sk = sso_key(sso_token)
        entries[sk] = {
            "access_token": cred.get("access_token"),
            "refresh_token": cred.get("refresh_token"),
            "expires_at": cred.get("expires_at"),
            "user_id": cred.get("user_id"),
            "team_id": cred.get("team_id"),
            "scope": cred.get("scope"),
            "sso_prefix": _normalize_sso(sso_token)[:16],
            "updated_at": time.time(),
        }
        data["version"] = 1
        data["updated_at"] = time.time()
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(p)
    return p


def _ensure_disk_loaded() -> None:
    """Hydrate process cache from disk once per process."""
    global _DISK_LOADED
    if _DISK_LOADED:
        return
    with _DISK_LOCK:
        if _DISK_LOADED:
            return
        data = load_disk_cache()
        entries = data.get("entries") or {}
        # Disk is keyed by sha256; runtime resolve needs SSO→cred.
        # We keep a reverse index by sso_prefix is insufficient; store
        # shadow map keyed by hash under a special prefix.
        for sk, cred in entries.items():
            if isinstance(cred, dict) and cred.get("access_token"):
                _OIDC_CACHE[f"hash:{sk}"] = cred
        _DISK_LOADED = True


def _lookup_disk_for_sso(sso_token: str) -> dict[str, Any] | None:
    _ensure_disk_loaded()
    sk = sso_key(sso_token)
    # Prefer hash-prefixed in-memory mirror
    cred = _OIDC_CACHE.get(f"hash:{sk}")
    if cred:
        return cred
    # Reload single entry from disk (handles concurrent script writes)
    data = load_disk_cache()
    entry = (data.get("entries") or {}).get(sk)
    if isinstance(entry, dict) and entry.get("access_token"):
        _OIDC_CACHE[f"hash:{sk}"] = entry
        return entry
    return None


def resolve_oidc_access_token(sso_token: str) -> str:
    """Return a usable OIDC access_token for *sso_token* (cache + refresh + convert)."""
    key = _normalize_sso(sso_token)
    cached = _OIDC_CACHE.get(key)
    if cached and _is_fresh(cached):
        return str(cached["access_token"])

    # Disk cache from scripts/sso_to_oidc.py
    disk_cred = _lookup_disk_for_sso(key)
    if disk_cred and _is_fresh(disk_cred):
        _OIDC_CACHE[key] = disk_cred
        return str(disk_cred["access_token"])

    base = cached or disk_cred
    if base and base.get("refresh_token"):
        try:
            refreshed = refresh_oidc(base)
            _OIDC_CACHE[key] = refreshed
            try:
                save_disk_entry(key, refreshed)
            except Exception as exc:
                logger.warning("oidc disk save after refresh failed: {}", exc)
            return str(refreshed["access_token"])
        except UpstreamError as exc:
            logger.warning("oidc refresh failed, falling back to sso convert: {}", exc)

    converted = sso_to_oidc(key)
    _OIDC_CACHE[key] = converted
    try:
        save_disk_entry(key, converted)
    except Exception as exc:
        logger.warning("oidc disk save after convert failed: {}", exc)
    return str(converted["access_token"])


def cache_put(sso_token: str, cred: dict[str, Any]) -> None:
    norm = _normalize_sso(sso_token)
    _OIDC_CACHE[norm] = cred
    _OIDC_CACHE[f"hash:{sso_key(norm)}"] = cred


def cache_get(sso_token: str) -> dict[str, Any] | None:
    return _OIDC_CACHE.get(_normalize_sso(sso_token))


__all__ = [
    "OIDC_ISSUER",
    "GROK_CLI_CLIENT_ID",
    "decode_jwt_claims",
    "sso_key",
    "sso_to_oidc",
    "refresh_oidc",
    "resolve_oidc_access_token",
    "load_disk_cache",
    "save_disk_entry",
    "cache_put",
    "cache_get",
]
