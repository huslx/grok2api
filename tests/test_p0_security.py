"""P0 security guards: media signatures, SSRF URL safety, SSE tickets, admin auth."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from app.platform.auth.media_sign import (
    build_signed_media_path,
    sign_media_query,
    verify_media_signature,
)
from app.platform.auth.sse_ticket import issue_sse_ticket, validate_sse_ticket
from app.platform.errors import ValidationError
from app.platform.net.url_safety import assert_safe_fetch_url


class MediaSignatureTests(unittest.TestCase):
    def test_roundtrip_valid(self) -> None:
        with patch(
            "app.platform.auth.media_sign.get_admin_key",
            return_value="test-admin-secret",
        ):
            params = sign_media_query("image", "abc123def4567890")
            self.assertTrue(
                verify_media_signature(
                    "image",
                    "abc123def4567890",
                    params["exp"],
                    params["sig"],
                )
            )

    def test_rejects_tampered_sig(self) -> None:
        with patch(
            "app.platform.auth.media_sign.get_admin_key",
            return_value="test-admin-secret",
        ):
            params = sign_media_query("image", "abc123def4567890")
            self.assertFalse(
                verify_media_signature(
                    "image",
                    "abc123def4567890",
                    params["exp"],
                    "0" * 32,
                )
            )

    def test_rejects_expired(self) -> None:
        with patch(
            "app.platform.auth.media_sign.get_admin_key",
            return_value="test-admin-secret",
        ):
            params = sign_media_query("video", "abc123def4567890", ttl_sec=60)
            past = int(time.time()) + 120
            self.assertFalse(
                verify_media_signature(
                    "video",
                    "abc123def4567890",
                    params["exp"],
                    params["sig"],
                    now=past,
                )
            )

    def test_path_contains_sig(self) -> None:
        with patch(
            "app.platform.auth.media_sign.get_admin_key",
            return_value="test-admin-secret",
        ):
            path = build_signed_media_path("image", "abc123def4567890")
            self.assertIn("/v1/files/image?", path)
            self.assertIn("sig=", path)
            self.assertIn("exp=", path)


class UrlSafetyTests(unittest.TestCase):
    def test_blocks_localhost(self) -> None:
        with self.assertRaises(ValidationError):
            assert_safe_fetch_url("http://localhost/secret")

    def test_blocks_loopback_ip(self) -> None:
        with self.assertRaises(ValidationError):
            assert_safe_fetch_url("http://127.0.0.1/secret")

    def test_blocks_metadata_host(self) -> None:
        with self.assertRaises(ValidationError):
            assert_safe_fetch_url("http://metadata.google.internal/computeMetadata/v1/")

    def test_blocks_private_ip(self) -> None:
        with self.assertRaises(ValidationError):
            assert_safe_fetch_url("http://192.168.1.1/")

    def test_blocks_link_local(self) -> None:
        with self.assertRaises(ValidationError):
            assert_safe_fetch_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_file_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            assert_safe_fetch_url("file:///etc/passwd")

    def test_blocks_embedded_credentials(self) -> None:
        with self.assertRaises(ValidationError):
            assert_safe_fetch_url("https://user:pass@example.com/a")

    def test_allows_public_https(self) -> None:
        # example.com is public; DNS resolution required — skip if offline.
        try:
            url = assert_safe_fetch_url("https://example.com/path.png")
        except ValidationError as exc:
            if "Cannot resolve" in str(exc):
                self.skipTest("DNS unavailable")
            raise
        self.assertEqual(url, "https://example.com/path.png")


class SessionCookieTests(unittest.TestCase):
    def test_issue_and_validate(self) -> None:
        from app.platform.auth.session_cookie import (
            issue_session_value,
            verify_session_value,
        )

        with patch(
            "app.platform.auth.media_sign.get_admin_key",
            return_value="test-admin-secret",
        ):
            value = issue_session_value("admin", ttl_sec=3600)
            self.assertTrue(verify_session_value(value))
            self.assertFalse(verify_session_value(value + "x"))
            self.assertFalse(verify_session_value(None))

    def test_rejects_expired(self) -> None:
        from app.platform.auth.session_cookie import (
            issue_session_value,
            verify_session_value,
        )

        with patch(
            "app.platform.auth.media_sign.get_admin_key",
            return_value="test-admin-secret",
        ):
            value = issue_session_value("webui", ttl_sec=60)
            past = int(time.time()) + 120
            self.assertFalse(verify_session_value(value, now=past))


class SseTicketTests(unittest.TestCase):
    def test_issue_and_validate(self) -> None:
        ticket = issue_sse_ticket(ttl_sec=60)
        self.assertTrue(validate_sse_ticket(ticket))

    def test_rejects_unknown(self) -> None:
        self.assertFalse(validate_sse_ticket("not-a-real-ticket"))

    def test_rejects_empty(self) -> None:
        self.assertFalse(validate_sse_ticket(""))
        self.assertFalse(validate_sse_ticket(None))


if __name__ == "__main__":
    unittest.main()
