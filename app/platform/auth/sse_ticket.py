"""Short-lived admin SSE tickets.

EventSource cannot set Authorization headers, so the admin UI previously
passed ``?app_key=...`` on the stream URL (leaking the password into logs /
history).  Tickets are single-purpose, time-bounded substitutes.
"""

from __future__ import annotations

import secrets
import time
from threading import Lock

_DEFAULT_TTL_SEC = 120
_MAX_TICKETS = 4096

_lock = Lock()
# ticket → expiry epoch seconds
_tickets: dict[str, float] = {}


def issue_sse_ticket(*, ttl_sec: int = _DEFAULT_TTL_SEC) -> str:
    """Mint a new ticket valid for *ttl_sec* seconds."""
    ttl = max(15, int(ttl_sec))
    token = secrets.token_urlsafe(32)
    exp = time.time() + ttl
    with _lock:
        _purge_locked(now=time.time())
        if len(_tickets) >= _MAX_TICKETS:
            # Drop oldest half when flooded.
            for key in list(_tickets.keys())[: len(_tickets) // 2]:
                _tickets.pop(key, None)
        _tickets[token] = exp
    return token


def validate_sse_ticket(ticket: str | None) -> bool:
    """Return True if *ticket* exists and has not expired."""
    if not ticket:
        return False
    now = time.time()
    with _lock:
        exp = _tickets.get(ticket)
        if exp is None:
            return False
        if exp < now:
            _tickets.pop(ticket, None)
            return False
        return True


def _purge_locked(*, now: float) -> None:
    stale = [k for k, exp in _tickets.items() if exp < now]
    for k in stale:
        _tickets.pop(k, None)


__all__ = ["issue_sse_ticket", "validate_sse_ticket"]
