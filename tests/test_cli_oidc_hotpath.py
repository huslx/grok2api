"""Tests for CLI OIDC hot-path optimizations (no full device convert on miss)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.dataplane.reverse.protocol import xai_oidc as oidc
from app.platform.errors import UpstreamError
from app.products.openai.cli_chat import _reasoning_effort


def setup_function() -> None:
    oidc._OIDC_CACHE.clear()
    oidc._DISK_LOADED = False


def test_resolve_uses_fresh_cache_without_convert() -> None:
    sso = "test-sso-token-abc"
    oidc.cache_put(
        sso,
        {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_at": time.time() + 3600,
        },
    )
    with patch.object(oidc, "sso_to_oidc") as convert, patch.object(
        oidc, "refresh_oidc"
    ) as refresh:
        token = oidc.resolve_oidc_access_token(sso, allow_convert=False)
        assert token == "access-1"
        convert.assert_not_called()
        refresh.assert_not_called()


def test_resolve_refreshes_without_convert() -> None:
    sso = "test-sso-token-def"
    oidc.cache_put(
        sso,
        {
            "access_token": "old",
            "refresh_token": "refresh-2",
            "expires_at": time.time() - 10,  # expired
        },
    )
    with patch.object(
        oidc,
        "refresh_oidc",
        return_value={
            "access_token": "new-access",
            "refresh_token": "refresh-2",
            "expires_at": time.time() + 3600,
        },
    ) as refresh, patch.object(oidc, "sso_to_oidc") as convert, patch.object(
        oidc, "save_disk_entry"
    ):
        token = oidc.resolve_oidc_access_token(sso, allow_convert=False)
        assert token == "new-access"
        refresh.assert_called_once()
        convert.assert_not_called()


def test_resolve_no_convert_schedules_repair_and_raises() -> None:
    sso = "test-sso-token-ghi"
    oidc.cache_put(
        sso,
        {
            "access_token": "old",
            "refresh_token": "bad-refresh",
            "expires_at": time.time() - 10,
        },
    )
    with patch.object(
        oidc,
        "refresh_oidc",
        side_effect=UpstreamError("OIDC refresh failed: 400", status=401),
    ), patch.object(oidc, "sso_to_oidc") as convert, patch.object(
        oidc, "schedule_oidc_repair", return_value=True
    ) as repair, patch.object(oidc, "_drop_cred"):
        with pytest.raises(UpstreamError) as ei:
            oidc.resolve_oidc_access_token(
                sso, allow_convert=False, schedule_repair=True
            )
        # Must NOT be 401 — dataplane maps 401 → account EXPIRED.
        assert ei.value.status == 503
        assert "oidc_unavailable" in str(ei.value.details.get("body", ""))
        convert.assert_not_called()
        repair.assert_called_once()


def test_resolve_allow_convert_true_falls_back() -> None:
    sso = "test-sso-token-jkl"
    with patch.object(
        oidc,
        "sso_to_oidc",
        return_value={
            "access_token": "converted",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
        },
    ) as convert, patch.object(oidc, "save_disk_entry"):
        token = oidc.resolve_oidc_access_token(sso, allow_convert=True)
        assert token == "converted"
        convert.assert_called_once()


def test_reasoning_effort_default_medium() -> None:
    assert _reasoning_effort(None, None, default_effort="medium") == "medium"
    assert _reasoning_effort(True, None, default_effort="medium") == "medium"
    assert _reasoning_effort(False, None, default_effort="medium") is None
    assert _reasoning_effort(None, "high", default_effort="medium") == "high"
    assert _reasoning_effort(None, "none", default_effort="medium") is None
    assert _reasoning_effort(None, "minimal", default_effort="high") == "low"
    assert _reasoning_effort(None, "xhigh", default_effort="medium") == "high"
    assert _reasoning_effort(None, None, default_effort="") == "medium"


def test_warm_index_and_list_warm_sso() -> None:
    sso = "warm-sso-token-xyz"
    oidc.cache_put(
        sso,
        {
            "access_token": "access-w",
            "refresh_token": "refresh-w",
            "expires_at": time.time() + 3600,
        },
    )
    assert oidc.has_fresh_oidc(sso)
    assert oidc.any_warm_oidc()
    assert sso in oidc.list_warm_sso_tokens()


def test_convert_oidc_one_skips_when_fresh() -> None:
    sso = "convert-skip-sso"
    oidc.cache_put(
        sso,
        {
            "access_token": "access-c",
            "refresh_token": "refresh-c",
            "expires_at": time.time() + 3600,
        },
    )
    with patch.object(oidc, "sso_to_oidc") as convert:
        status, err = oidc.convert_oidc_one(sso)
        assert status == "skipped"
        assert err is None
        convert.assert_not_called()


def test_should_hot_convert_only_on_last_attempt() -> None:
    from app.products.openai.cli_chat import _should_hot_convert

    oidc.cache_put(
        "pool-warm-sso",
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
        },
    )
    assert _should_hot_convert(last_resort=True, attempt=8, max_retries=8) is True
    assert _should_hot_convert(last_resort=True, attempt=3, max_retries=8) is False
    assert _should_hot_convert(last_resort=False, attempt=8, max_retries=8) is False


def test_poll_token_does_not_sleep_before_first_success() -> None:
    sleeps: list[float] = []

    def fake_post_form(url, data):  # noqa: ARG001
        return 200, {"access_token": "t", "refresh_token": "r", "expires_in": 3600}, ""

    with patch.object(oidc.time, "sleep", side_effect=lambda s: sleeps.append(s)), patch.object(
        oidc, "_post_form", side_effect=fake_post_form
    ):
        data = oidc._poll_token("device", interval=5, expires_in=1800)
        assert data["access_token"] == "t"
        assert sleeps == []  # approved path: first poll succeeds, no wait
