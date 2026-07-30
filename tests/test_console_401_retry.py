"""Console 401: expire dead SSO tokens and keep swapping accounts."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import orjson

from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.dataplane.account.selector import set_strategy
from app.dataplane.reverse.protocol.xai_usage import is_invalid_credentials_error
from app.platform.errors import UpstreamError
from app.products._account_selection import console_max_retries, selection_max_retries


class TestInvalidCredentials401(unittest.TestCase):
    def test_bare_401_is_invalid_credentials(self) -> None:
        exc = UpstreamError("Console API returned 401", status=401, body="")
        self.assertTrue(is_invalid_credentials_error(exc))
        self.assertEqual(feedback_kind_for_error(exc), FeedbackKind.UNAUTHORIZED)

    def test_401_with_body_still_invalid(self) -> None:
        exc = UpstreamError(
            "Console API returned 401",
            status=401,
            body='{"error":"invalid-credentials"}',
        )
        self.assertTrue(is_invalid_credentials_error(exc))

    def test_403_without_marker_is_not_invalid_credentials(self) -> None:
        exc = UpstreamError("forbidden", status=403, body="cloudflare challenge")
        self.assertFalse(is_invalid_credentials_error(exc))
        self.assertEqual(feedback_kind_for_error(exc), FeedbackKind.FORBIDDEN)


class TestConsoleMaxRetries(unittest.TestCase):
    def tearDown(self) -> None:
        set_strategy("random")

    def test_console_floor_overrides_low_quota_retries(self) -> None:
        set_strategy("quota")

        def fake_get(key, default=None):
            if key == "retry.max_retries":
                return 1
            if key == "chat.console_account_retries":
                return 8
            return default

        with patch("app.products._account_selection.get_config", side_effect=fake_get):
            self.assertEqual(selection_max_retries(), 1)
            self.assertEqual(console_max_retries(), 8)

    def test_console_uses_higher_configured_retry(self) -> None:
        set_strategy("quota")

        def fake_get(key, default=None):
            if key == "retry.max_retries":
                return 12
            if key == "chat.console_account_retries":
                return 8
            return default

        with patch("app.products._account_selection.get_config", side_effect=fake_get):
            self.assertEqual(console_max_retries(), 12)


class TestConsoleChat401Swap(unittest.TestCase):
    def test_stream_swaps_account_on_401_then_succeeds(self) -> None:
        tokens_tried: list[str] = []

        async def fake_stream_console_chat(token, payload, *, timeout_s=120.0):
            tokens_tried.append(token)
            if token == "dead-sso":
                raise UpstreamError("Console API returned 401", status=401, body="")
            yield "response.output_text.delta", orjson.dumps({"delta": "ok"}).decode()
            yield "response.completed", orjson.dumps({
                "response": {
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            }).decode()

        accounts = [
            SimpleNamespace(token="dead-sso"),
            SimpleNamespace(token="live-sso"),
        ]

        async def fake_reserve(directory, spec, *, now_s_override=None, exclude_tokens=None):
            excluded = set(exclude_tokens or [])
            for acct in accounts:
                if acct.token not in excluded:
                    return acct, 5
            return None, 5

        class _FakeDirectory:
            async def release(self, acct):
                return None

            async def feedback(self, *args, **kwargs):
                return None

        class _FakeConfig:
            def get_float(self, key, default=0.0):
                return float(default)

            def get(self, key, default=None):
                if key == "retry.on_codes":
                    return "429,401,503"
                return default

            def get_str(self, key, default=""):
                return str(default)

        async def run() -> list[str]:
            from app.products.openai import console_chat

            with (
                patch("app.dataplane.account._directory", _FakeDirectory()),
                patch.object(console_chat, "logger", SimpleNamespace(
                    info=lambda *a, **k: None,
                    warning=lambda *a, **k: None,
                )),
                patch.object(console_chat, "get_config", return_value=_FakeConfig()),
                patch.object(console_chat, "console_max_retries", return_value=3),
                patch.object(console_chat, "reserve_account", fake_reserve),
                patch.object(console_chat, "stream_console_chat", fake_stream_console_chat),
                patch.object(console_chat, "_quota_sync", AsyncMock()),
                patch.object(console_chat, "_fail_sync", AsyncMock()),
            ):
                gen = await console_chat.completions(
                    model="grok-4.5-console",
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                )
                return [frame async for frame in gen]

        frames = asyncio.run(run())
        self.assertEqual(tokens_tried, ["dead-sso", "live-sso"])
        self.assertTrue(any("ok" in f for f in frames))
        self.assertTrue(any("[DONE]" in f for f in frames))


if __name__ == "__main__":
    unittest.main()
