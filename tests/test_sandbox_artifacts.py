"""Sandbox code_execution media harvest + local materialisation."""

from __future__ import annotations

import base64
import unittest
from unittest.mock import AsyncMock, patch

from app.dataplane.reverse.protocol.sandbox_artifacts import (
    base64_target_from_bash_command,
    classify_media_bytes,
    extract_base64_payload,
    extract_b64_file_markers,
    find_sandbox_media_paths,
    is_complete_mp4,
    is_playable_media,
    looks_like_base64,
    materialize_sandbox_media,
    merge_personality_with_artifact_hint,
    paths_from_bash_command,
    strip_b64_markers,
)


# Minimal incomplete MP4 (ftyp only — no moov → unplayable / "black")
_TRUNCATED_MP4 = (
    b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
    + b"\x00\x00\x00\x08free"
    + b"\x00\x00\x00\x40mdat"
    + b"\x00" * 56
)

# Complete-enough MP4: ftyp + free + mdat + moov (empty moov box is enough for gate)
def _box(typ: bytes, payload: bytes) -> bytes:
    size = 8 + len(payload)
    return size.to_bytes(4, "big") + typ + payload


_MINI_MP4 = (
    _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    + _box(b"free", b"")
    + _box(b"mdat", b"\x00" * 64)
    + _box(b"moov", b"\x00" * 32)
)


class PathAndBashParsingTests(unittest.TestCase):
    def test_find_sandbox_paths(self) -> None:
        text = "saved to `/home/workdir/artifacts/cute_kitten_1s.mp4` done"
        paths = find_sandbox_media_paths(text)
        self.assertEqual(paths, ["/home/workdir/artifacts/cute_kitten_1s.mp4"])

    def test_paths_from_ffmpeg_and_redirect(self) -> None:
        cmd = 'ffmpeg -y -i in.mp4 /home/workdir/artifacts/out.mp4'
        self.assertIn(
            "/home/workdir/artifacts/out.mp4", paths_from_bash_command(cmd)
        )
        cmd2 = 'echo x > /home/workdir/artifacts/a.webm'
        self.assertIn(
            "/home/workdir/artifacts/a.webm", paths_from_bash_command(cmd2)
        )

    def test_base64_target(self) -> None:
        cmd = "base64 -w0 /home/workdir/artifacts/demo.mp4"
        self.assertEqual(
            base64_target_from_bash_command(cmd),
            "/home/workdir/artifacts/demo.mp4",
        )

    def test_b64_markers(self) -> None:
        b64 = base64.b64encode(_MINI_MP4).decode()
        text = f"file ready\nB64_FILE:demo.mp4:{b64}\n"
        markers = extract_b64_file_markers(text)
        self.assertIn("demo.mp4", markers)
        self.assertTrue(looks_like_base64(markers["demo.mp4"]))
        self.assertEqual(classify_media_bytes(_MINI_MP4), "video")
        self.assertTrue(is_complete_mp4(_MINI_MP4))
        self.assertFalse(is_complete_mp4(_TRUNCATED_MP4))
        self.assertTrue(is_playable_media(_MINI_MP4, "video"))
        self.assertFalse(is_playable_media(_TRUNCATED_MP4, "video"))
        stripped = strip_b64_markers(text)
        self.assertNotIn("B64_FILE", stripped)


class MaterializeTests(unittest.IsolatedAsyncioTestCase):
    async def test_materialize_rewrites_path_to_local_video(self) -> None:
        b64 = base64.b64encode(_MINI_MP4).decode()
        path = "/home/workdir/artifacts/cute_kitten_1s.mp4"
        text = f"Video at {path}\nB64:{b64}\n"

        with (
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.save_local_video",
                return_value=None,
            ) as save,
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.build_signed_media_url",
                return_value="http://localhost:8000/v1/files/video?id=abc&exp=1&sig=s",
            ),
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.get_config",
            ) as cfg,
        ):
            cfg.return_value.get_str.side_effect = lambda key, default="": {
                "app.app_url": "http://localhost:8000",
                "features.video_format": "local_url",
                "features.image_format": "local_url",
            }.get(key, default)

            rewritten, embeds = await materialize_sandbox_media(
                text,
                known_paths={path},
                known_b64={path: b64},
            )

        save.assert_called_once()
        self.assertIn("/v1/files/video?", rewritten)
        self.assertIn("[video](", rewritten)
        self.assertNotIn("<video", rewritten)  # raw HTML would be escaped by chat UI
        self.assertNotIn(path, rewritten)
        self.assertNotIn("B64:", rewritten)
        self.assertTrue(embeds)
        self.assertIn("/v1/files/video?", embeds[0])
        self.assertIn("[video](", embeds[0])

    async def test_strips_fenced_and_line_base64(self) -> None:
        b64 = base64.b64encode(_MINI_MP4).decode()
        path = "/home/workdir/artifacts/clip.mp4"
        text = (
            f"done {path}\n"
            f"```base64\n{b64}\n```\n"
            f"{b64}\n"
        )
        with (
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.save_local_video",
                return_value=None,
            ),
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.build_signed_media_url",
                return_value="http://localhost:8000/v1/files/video?id=x&exp=1&sig=s",
            ),
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.get_config",
            ) as cfg,
        ):
            cfg.return_value.get_str.side_effect = lambda key, default="": {
                "app.app_url": "http://localhost:8000",
                "features.video_format": "local_url",
                "features.image_format": "local_url",
            }.get(key, default)
            rewritten, embeds = await materialize_sandbox_media(text)

        self.assertNotIn(b64[:40], rewritten)
        self.assertIn("[video](", rewritten)
        self.assertTrue(embeds)

    async def test_rejects_truncated_mp4(self) -> None:
        b64 = base64.b64encode(_TRUNCATED_MP4).decode()
        path = "/home/workdir/artifacts/bad.mp4"
        text = f"path {path}\nB64_FILE:bad.mp4:{b64}\n"
        with (
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.save_local_video",
                return_value=None,
            ) as save,
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.get_config",
            ) as cfg,
        ):
            cfg.return_value.get_str.side_effect = lambda key, default="": {
                "app.app_url": "http://localhost:8000",
            }.get(key, default)
            rewritten, embeds = await materialize_sandbox_media(
                text, known_paths={path}, known_b64={path: b64}
            )
        save.assert_not_called()
        self.assertEqual(embeds, [])
        self.assertNotIn("[video](", rewritten)

    async def test_prefers_longer_tool_b64_over_truncated_text(self) -> None:
        good = base64.b64encode(_MINI_MP4).decode()
        bad = base64.b64encode(_TRUNCATED_MP4).decode()
        path = "/home/workdir/artifacts/clip.mp4"
        text = f"{path}\nB64_FILE:clip.mp4:{bad}\n"
        with (
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.save_local_video",
                return_value=None,
            ) as save,
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.build_signed_media_url",
                return_value="http://localhost:8000/v1/files/video?id=ok&exp=1&sig=s",
            ),
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.get_config",
            ) as cfg,
        ):
            cfg.return_value.get_str.side_effect = lambda key, default="": {
                "app.app_url": "http://localhost:8000",
            }.get(key, default)
            rewritten, embeds = await materialize_sandbox_media(
                text,
                known_paths={path},
                known_b64={path: good},  # tool stdout (complete)
            )
        save.assert_called_once()
        raw_saved = save.call_args[0][0]
        self.assertTrue(is_complete_mp4(raw_saved))
        self.assertIn("[video](", rewritten)
        self.assertTrue(embeds)

    async def test_upgrades_bare_local_video_url(self) -> None:
        b64 = base64.b64encode(_MINI_MP4).decode()
        path = "/home/workdir/artifacts/x.mp4"
        # Simulate rewritten bare URL left in text (e.g. model said 文件路径：url)
        bare = "http://localhost:8000/v1/files/video?id=deadbeefdeadbeef&exp=1&sig=s"
        text = f"文件路径：{bare}\nB64_FILE:x.mp4:{b64}\n"
        with (
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.save_local_video",
                return_value=None,
            ),
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.build_signed_media_url",
                return_value=bare,
            ),
            patch(
                "app.dataplane.reverse.protocol.sandbox_artifacts.get_config",
            ) as cfg,
        ):
            cfg.return_value.get_str.side_effect = lambda key, default="": {
                "app.app_url": "http://localhost:8000",
            }.get(key, default)
            rewritten, embeds = await materialize_sandbox_media(
                text, known_paths={path}, known_b64={path: b64}
            )
        self.assertIn("[video](", rewritten)
        self.assertNotIn("B64_FILE", rewritten)
        # bare URL should be wrapped, not left alone after 文件路径：
        self.assertNotRegex(rewritten, r"文件路径：http://")

    def test_extract_base64_from_noisy_stdout(self) -> None:
        pure = base64.b64encode(_MINI_MP4).decode()
        noisy = f"[libx264 @ 0x123] frame I:1\n{pure}\n"
        extracted = extract_base64_payload(noisy)
        self.assertIsNotNone(extracted)
        self.assertGreaterEqual(len(extracted or ""), len(pure))
        self.assertTrue(looks_like_base64(noisy))

    def test_reassemble_chunked_b64_parts(self) -> None:
        from app.dataplane.reverse.protocol.sandbox_artifacts import reassemble_b64_parts

        pure = base64.b64encode(_MINI_MP4).decode()
        # split into 40-char chunks
        chunks = [pure[i : i + 40] for i in range(0, len(pure), 40)]
        text = f"B64META:clip.mp4:{len(pure)}:{len(chunks)}\n"
        for i, c in enumerate(chunks):
            text += f"B64PART:{i}:{c}\n"
        text += "B64END\n"
        joined = reassemble_b64_parts(text)
        self.assertIsNotNone(joined)
        self.assertEqual((joined or "")[: len(pure)], pure)
        raw = base64.b64decode(joined or "")
        self.assertTrue(is_complete_mp4(raw))

    def test_reassemble_rejects_missing_middle_parts(self) -> None:
        from app.dataplane.reverse.protocol.sandbox_artifacts import reassemble_b64_parts

        text = (
            "B64META:x.mp4:100:3\n"
            "B64PART:0:AAAA\n"
            "B64PART:2:ZZZZ\n"  # missing part 1
            "B64END\n"
        )
        self.assertIsNone(reassemble_b64_parts(text))

    def test_personality_hint_merged(self) -> None:
        merged = merge_personality_with_artifact_hint("be concise")
        self.assertIn("be concise", merged)
        self.assertIn("B64PART", merged)
        self.assertIn("B64META", merged)


if __name__ == "__main__":
    unittest.main()
