"""Imagine Public video proxy: host detection + format-aware local rehosting."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.products.openai.video import (
    _is_imagine_public_url,
    _resolve_video_output,
)


class ImaginePublicUrlTests(unittest.TestCase):
    def test_matches_imagine_public_hosts(self) -> None:
        self.assertTrue(
            _is_imagine_public_url("https://imagine-public.x.ai/users/a/b.mp4")
        )
        self.assertTrue(
            _is_imagine_public_url("https://imagine-public-cdn.example.com/v.mp4")
        )

    def test_rejects_non_public_hosts(self) -> None:
        self.assertFalse(
            _is_imagine_public_url("https://assets.grok.com/users/a/content")
        )
        self.assertFalse(_is_imagine_public_url(""))
        self.assertFalse(_is_imagine_public_url("not-a-url"))


class ResolveVideoOutputProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_grok_url_returns_upstream_when_proxy_disabled(self) -> None:
        public_url = "https://imagine-public.x.ai/vid/abc.mp4"
        cfg = MagicMock()
        cfg.get_str.return_value = "grok_url"
        cfg.get_bool.return_value = False
        with patch("app.products.openai.video.get_config", return_value=cfg):
            result = await _resolve_video_output(
                token="tok",
                url=public_url,
                file_id="fileid123",
            )
        self.assertEqual(result, public_url)

    async def test_grok_url_proxies_imagine_public_when_enabled(self) -> None:
        public_url = "https://imagine-public.x.ai/vid/abc.mp4"
        cfg = MagicMock()
        cfg.get_str.side_effect = lambda key, default="": {
            "features.video_format": "grok_url",
            "app.app_url": "http://localhost:8000",
        }.get(key, default)
        cfg.get_bool.return_value = True

        with (
            patch("app.products.openai.video.get_config", return_value=cfg),
            patch(
                "app.products.openai.video._download_video_bytes",
                new=AsyncMock(return_value=(b"\x00\x00fake-mp4", "video/mp4")),
            ) as download,
            patch(
                "app.products.openai.video._save_video_bytes",
                return_value=MagicMock(),
            ) as save,
            patch(
                "app.products.openai.video._local_video_url",
                return_value="http://localhost:8000/v1/files/video?id=fileid123",
            ),
        ):
            result = await _resolve_video_output(
                token="tok",
                url=public_url,
                file_id="fileid123",
            )

        download.assert_awaited_once_with("tok", public_url)
        save.assert_called_once()
        self.assertEqual(
            result, "http://localhost:8000/v1/files/video?id=fileid123"
        )

    async def test_grok_html_proxies_imagine_public_when_enabled(self) -> None:
        public_url = "https://imagine-public.x.ai/vid/abc.mp4"
        local = "http://localhost:8000/v1/files/video?id=fileid123"
        cfg = MagicMock()
        cfg.get_str.side_effect = lambda key, default="": {
            "features.video_format": "grok_html",
            "app.app_url": "http://localhost:8000",
        }.get(key, default)
        cfg.get_bool.return_value = True

        with (
            patch("app.products.openai.video.get_config", return_value=cfg),
            patch(
                "app.products.openai.video._download_video_bytes",
                new=AsyncMock(return_value=(b"\x00\x00fake-mp4", "video/mp4")),
            ),
            patch(
                "app.products.openai.video._save_video_bytes",
                return_value=MagicMock(),
            ),
            patch(
                "app.products.openai.video._local_video_url",
                return_value=local,
            ),
        ):
            result = await _resolve_video_output(
                token="tok",
                url=public_url,
                file_id="fileid123",
            )

        self.assertIn("<video controls", result)
        self.assertIn(local, result)
        self.assertNotIn(public_url, result)

    async def test_assets_grok_not_forced_by_imagine_public_flag(self) -> None:
        assets_url = "https://assets.grok.com/users/a/content"
        cfg = MagicMock()
        cfg.get_str.return_value = "grok_url"
        cfg.get_bool.return_value = True  # flag on, but host is not imagine-public
        with (
            patch("app.products.openai.video.get_config", return_value=cfg),
            patch(
                "app.products.openai.video._download_video_bytes",
                new=AsyncMock(),
            ) as download,
        ):
            result = await _resolve_video_output(
                token="tok",
                url=assets_url,
                file_id="fileid123",
            )
        download.assert_not_awaited()
        self.assertEqual(result, assets_url)


if __name__ == "__main__":
    unittest.main()
