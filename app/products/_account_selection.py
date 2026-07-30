"""Shared account selection helpers for products-layer request handlers."""

from app.control.model.enums import ModeId
from app.control.model.spec import ModelSpec
from app.control.account.runtime import get_refresh_service
from app.dataplane.account.selector import current_strategy
from app.platform.config.snapshot import get_config

# Random strategy has no config key for retry count; it is pinned here so that
# every retry-driven call site (chat / images / video / anthropic) sees the same
# value without introducing scattered magic numbers.
_RANDOM_MAX_RETRIES = 5

# Console SSO pools often contain many expired tokens; a low quota-mode
# ``retry.max_retries`` (default 1) surfaces 401 to the client before a live
# account is found. Floor matches CLI's default account-swap budget.
_CONSOLE_DEFAULT_ACCOUNT_RETRIES = 8


def selection_max_retries() -> int:
    """Retry count for account-swap loops, aware of the active selection strategy.

    - ``random`` strategy: fixed at :data:`_RANDOM_MAX_RETRIES` (=5).
    - ``quota`` strategy:  reads ``retry.max_retries`` (default 1), preserving
      the historical behaviour.
    """
    if current_strategy() == "random":
        return _RANDOM_MAX_RETRIES
    return int(get_config("retry.max_retries", 1))


def console_max_retries() -> int:
    """Account-swap budget for console.x.ai paths (SSO, no OIDC).

    Takes the max of ``selection_max_retries()`` and
    ``chat.console_account_retries`` (default 8) so dense pools of expired
    SSO tokens can be skipped without failing the whole session.
    """
    base = selection_max_retries()
    floor = int(
        get_config("chat.console_account_retries", _CONSOLE_DEFAULT_ACCOUNT_RETRIES)
    )
    return max(base, max(0, floor))


def mode_candidates(spec: ModelSpec) -> tuple[int, ...]:
    """Return mode IDs to try for *spec* in priority order.

    Chat models using ``AUTO`` can optionally fall back to ``FAST`` and then
    ``EXPERT`` when the upstream ``auto`` quota window is exhausted but the
    account still has usable quota in the other chat windows.
    """
    primary = int(spec.mode_id)
    if (
        spec.is_chat()
        and spec.mode_id == ModeId.AUTO
        and get_config("features.auto_chat_mode_fallback", True)
    ):
        return (primary, int(ModeId.FAST), int(ModeId.EXPERT))
    return (primary,)


async def reserve_account(
    directory,
    spec: ModelSpec,
    *,
    exclude_tokens: list[str] | None = None,
    prefer_tokens: list[str] | None = None,
    now_s_override: int | None = None,
):
    """Reserve an account and return ``(lease, selected_mode_id)``.

    Returns ``(None, original_mode_id)`` when no account is available. Under the
    random strategy no on-demand refresh fallback is attempted — upstream quota
    data is never probed.

    *prefer_tokens* is a soft preference (e.g. OIDC-warm SSO tokens for CLI).
    When none of the preferred accounts are free, selection falls back to the
    full candidate pool.
    """
    original_mode_id = int(spec.mode_id)

    async def _try_reserve():
        for candidate_mode_id in mode_candidates(spec):
            lease = await directory.reserve(
                pool_candidates=spec.pool_candidates(),
                mode_id=candidate_mode_id,
                now_s_override=now_s_override,
                exclude_tokens=exclude_tokens,
                prefer_tokens=prefer_tokens,
            )
            if lease is not None:
                return lease, candidate_mode_id
        return None, original_mode_id

    lease, selected_mode_id = await _try_reserve()
    if lease is not None:
        return lease, selected_mode_id

    if current_strategy() == "random":
        return None, original_mode_id

    refresh_svc = get_refresh_service()
    if refresh_svc is not None:
        await refresh_svc.refresh_on_demand()
        lease, selected_mode_id = await _try_reserve()
        if lease is not None:
            return lease, selected_mode_id

    return None, original_mode_id

__all__ = [
    "selection_max_retries",
    "console_max_retries",
    "mode_candidates",
    "reserve_account",
]
