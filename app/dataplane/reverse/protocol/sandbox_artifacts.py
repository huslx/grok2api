"""Extract media files produced by Grok code_execution / bash sandboxes.

Grok's code interpreter writes files under ``/home/workdir/artifacts/`` but the
SSE API does **not** expose a download URL.  Within the same turn the model can
still ``base64`` those files; we harvest that base64 (from tool stdout or the
final answer), persist bytes locally, and rewrite paths to signed proxy URLs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from typing import Any

from app.platform.auth.media_sign import build_signed_media_url
from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from app.platform.storage import save_local_image, save_local_video

_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".ogg")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_MEDIA_EXTS = _VIDEO_EXTS + _IMAGE_EXTS

# Absolute sandbox paths written by Grok code_execution / bash tools.
_SANDBOX_PATH_RE = re.compile(
    r"(?<![\w./-])(/home/workdir/artifacts/[^\s`\"'<>\]\)]+?(?:"
    + "|".join(re.escape(ext) for ext in _MEDIA_EXTS)
    + r"))",
    re.IGNORECASE,
)

# Explicit client-friendly markers we ask the model to emit.
_B64_FILE_RE = re.compile(
    r"B64_FILE:([^\s:]+):([A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)
_B64_INLINE_RE = re.compile(
    r"B64:([A-Za-z0-9+/=\s]{64,})",
    re.IGNORECASE,
)
# Model sometimes dumps bare base64 in fenced code blocks or long lines.
_B64_FENCE_RE = re.compile(
    r"```(?:base64|text|plain|bin|mp4|data)?\s*\n([A-Za-z0-9+/=\s]{64,})\n```",
    re.IGNORECASE,
)
_B64_LINE_RE = re.compile(r"(?m)^[A-Za-z0-9+/]{80,}={0,2}\s*$")
_DATA_VIDEO_RE = re.compile(
    r"data:video/[\w.+-]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
_DATA_IMAGE_RE = re.compile(
    r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)

# bash / shell writes that target artifact media paths
_WRITE_REDIRECT_RE = re.compile(
    r"(?:(?:>|>>)\s*|tee\s+(?:-a\s+)?)(['\"]?)(/home/workdir/artifacts/[^\s'\"|;&]+)\1",
    re.IGNORECASE,
)
_FFMPEG_OUT_RE = re.compile(
    r"ffmpeg\b[^\n]*?\s(['\"]?)(/home/workdir/artifacts/[^\s'\"|;&]+)\1\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_BASE64_CMD_RE = re.compile(
    r"\bbase64\b(?:\s+-w\s*0|\s+-w0)?\s+(['\"]?)(/home/workdir/artifacts/[^\s'\"]+)\1",
    re.IGNORECASE,
)
# cat/file | base64  /  base64 < file
_BASE64_PIPE_RE = re.compile(
    r"(?:cat|tee)\s+(['\"]?)(/home/workdir/artifacts/[^\s'\"]+)\1\s*\|\s*base64"
    r"|base64(?:\s+-w\s*0|\s+-w0)?\s*<\s*(['\"]?)(/home/workdir/artifacts/[^\s'\"]+)\3",
    re.IGNORECASE,
)

_ARTIFACT_HINT = (
    "When you create media under /home/workdir/artifacts, keep files SMALL "
    "(e.g. ffmpeg 160x120, 5fps, 1s, -crf 45 -movflags +faststart) so transfer works. "
    "Transfer with SEPARATE bash commands — one B64PART per command (max 800 chars each). "
    "First command:\n"
    "python3 -c \"import base64,pathlib;p=pathlib.Path('PATH');d=base64.b64encode(p.read_bytes()).decode();"
    "print(f'B64META:{p.name}:{len(d)}:{(len(d)+799)//800}')\"\n"
    "Then for each index i in 0..parts-1 run:\n"
    "python3 -c \"import base64,pathlib;d=base64.b64encode(pathlib.Path('PATH').read_bytes()).decode();"
    "print(f'B64PART:{i}:{d[i*800:(i+1)*800]}')\"\n"
    "Finally: python3 -c \"print('B64END')\"\n"
    "Final answer: only the sandbox path. Never paste long base64 into the final answer."
)

# Chunked transfer markers emitted by the artifact-transfer script.
_B64_META_RE = re.compile(r"B64META:([^\s:]+):(\d+)(?::(\d+))?")
# Allow whitespace inside part payload (platform may wrap long lines)
_B64_PART_RE = re.compile(
    r"B64PART:(\d+):([A-Za-z0-9+/=\s]+?)(?=\nB64PART:|\nB64END|\nB64META:|\Z)",
    re.IGNORECASE,
)
_B64_END_RE = re.compile(r"\bB64END\b")


def artifact_retrieval_hint() -> str:
    """Instruction merged into customPersonality so models emit base64."""
    return _ARTIFACT_HINT


def find_sandbox_media_paths(text: str) -> list[str]:
    if not text:
        return []
    # Models sometimes insert spaces: "/ home/workdir/..."
    normalized = re.sub(r"/\s+home\s*/\s*workdir\s*/\s*artifacts\s*/", "/home/workdir/artifacts/", text)
    seen: set[str] = set()
    out: list[str] = []
    for match in _SANDBOX_PATH_RE.finditer(normalized):
        path = match.group(1).rstrip(".,;:)")
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def paths_from_bash_command(command: str) -> list[str]:
    """Return artifact media paths referenced as outputs in a bash command."""
    if not command:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for rx in (_WRITE_REDIRECT_RE, _FFMPEG_OUT_RE):
        for match in rx.finditer(command):
            path = match.group(2)
            if _is_media_path(path) and path not in seen:
                seen.add(path)
                found.append(path)
    # also plain path mentions for ffmpeg -i input - output style last arg
    for path in find_sandbox_media_paths(command):
        if path not in seen:
            # only treat as output if appears after redirect-ish tokens or as trailing arg
            if re.search(
                rf"(?:>|>>|tee\s+|ffmpeg\b[^\n]*\s){re.escape(path)}",
                command,
                re.IGNORECASE,
            ):
                seen.add(path)
                found.append(path)
    return found


def base64_target_from_bash_command(command: str) -> str | None:
    if not command:
        return None
    match = _BASE64_CMD_RE.search(command)
    if match:
        path = match.group(2)
        return path if _is_media_path(path) else None
    match = _BASE64_PIPE_RE.search(command)
    if not match:
        return None
    path = match.group(2) or match.group(4) or ""
    return path if _is_media_path(path) else None


def looks_like_base64(value: str) -> bool:
    return extract_base64_payload(value) is not None


def extract_base64_payload(value: str) -> str | None:
    """Extract the longest base64 run from *value*.

    Tool stdout is often *mostly* base64 but may include log fragments
    (``[libx264 @ ...]``, paths, etc.). Requiring the entire string to be pure
    base64 caused complete video downloads to be dropped.
    """
    if not value:
        return None
    # Chunked transfer takes priority when present
    chunked = reassemble_b64_parts(value)
    if chunked:
        return chunked
    # Prefer contiguous pure runs after stripping whitespace
    compact = re.sub(r"\s+", "", value)
    runs = re.findall(r"[A-Za-z0-9+/]{64,}={0,3}", compact)
    if not runs:
        # Allow whitespace-separated base64 blocks (base64 without -w0)
        soft = re.findall(
            r"(?:[A-Za-z0-9+/]{4}\s*){16,}[A-Za-z0-9+/]{0,3}={0,3}",
            value,
        )
        runs = [re.sub(r"\s+", "", r) for r in soft]
    if not runs:
        return None
    # Longest first; require decodable length and base64 alphabet
    for run in sorted(runs, key=len, reverse=True):
        if not re.fullmatch(r"[A-Za-z0-9+/]+=*", run):
            continue
        if len(run) < 64:
            continue
        # pad to multiple of 4 for decode friendliness
        pad = (-len(run)) % 4
        if pad:
            run = run + ("=" * pad)
        return run
    return None


def reassemble_b64_parts(text: str) -> str | None:
    """Reassemble ``B64PART:n:...`` chunks; return full base64 or None.

    Requires a contiguous sequence of parts starting at 0. If any middle part
    is missing (platform truncation), returns None so we don't save a black mp4.
    """
    if not text or "B64PART:" not in text:
        return None
    parts: dict[int, str] = {}
    expected_len: int | None = None
    expected_parts: int | None = None
    name: str | None = None
    for match in _B64_META_RE.finditer(text):
        name = match.group(1)
        try:
            expected_len = int(match.group(2))
        except ValueError:
            expected_len = None
        if match.group(3):
            try:
                expected_parts = int(match.group(3))
            except ValueError:
                expected_parts = None
    for match in _B64_PART_RE.finditer(text):
        try:
            idx = int(match.group(1))
        except ValueError:
            continue
        payload = re.sub(r"\s+", "", match.group(2))
        if not payload:
            continue
        # keep longer payload if duplicate index
        if idx not in parts or len(payload) > len(parts[idx]):
            parts[idx] = payload
    if not parts:
        return None
    max_idx = max(parts)
    missing = [i for i in range(max_idx + 1) if i not in parts]
    if missing:
        logger.warning(
            "b64 parts incomplete: name={} have={} missing={} expected_parts={}",
            name,
            sorted(parts),
            missing,
            expected_parts,
        )
        return None  # refuse partial reassembly
    if expected_parts is not None and max_idx + 1 < expected_parts:
        logger.warning(
            "b64 parts count short: name={} got={} expected={}",
            name,
            max_idx + 1,
            expected_parts,
        )
        return None
    joined = "".join(parts[i] for i in range(max_idx + 1))
    if expected_len is not None and len(joined) < expected_len:
        logger.warning(
            "b64 parts shorter than meta: name={} got={} expected={}",
            name,
            len(joined),
            expected_len,
        )
        return None
    if len(joined) < 64:
        return None
    pad = (-len(joined)) % 4
    if pad:
        joined = joined + ("=" * pad)
    return joined


def decode_base64_media(value: str) -> tuple[bytes, str] | None:
    """Decode base64 and classify as video/image. Returns (raw, kind) or None."""
    s = extract_base64_payload(value)
    if not s:
        return None
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:
        return None
    if len(raw) < 16:
        return None
    kind = classify_media_bytes(raw)
    if kind is None:
        return None
    return raw, kind


def classify_media_bytes(raw: bytes) -> str | None:
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return "video"
    if raw.startswith(b"\x1aE\xdf\xa3"):  # EBML / webm
        return "video"
    if raw.startswith(b"\x00\x00\x00\x14ftypqt") or raw.startswith(b"\x00\x00\x00\x18ftyp"):
        return "video"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if raw.startswith(b"GIF8"):
        return "image"
    if raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
        return "image"
    # mp4 sometimes without checking offset 4
    if b"ftyp" in raw[:64]:
        return "video"
    return None


def is_complete_mp4(raw: bytes) -> bool:
    """Return True when *raw* looks like a complete MP4 (has ftyp + moov).

    Truncated base64 dumps often keep ``ftyp``/``mdat`` but lose trailing ``moov``,
    which browsers play as a black/empty video.
    """
    if len(raw) < 32 or b"ftyp" not in raw[:64]:
        return False
    if b"moov" not in raw:
        return False
    # Walk top-level boxes; require a full moov box within bounds
    i = 0
    n = len(raw)
    has_ftyp = False
    has_moov = False
    while i + 8 <= n:
        size = int.from_bytes(raw[i : i + 4], "big")
        typ = raw[i + 4 : i + 8]
        header = 8
        if size == 1:
            if i + 16 > n:
                return False
            size = int.from_bytes(raw[i + 8 : i + 16], "big")
            header = 16
        elif size == 0:
            size = n - i
        if size < header or i + size > n:
            return False
        if typ == b"ftyp":
            has_ftyp = True
        elif typ == b"moov":
            has_moov = True
        i += size
    return has_ftyp and has_moov


def is_playable_media(raw: bytes, kind: str) -> bool:
    """Reject truncated / empty media that would show as black or broken."""
    if kind == "image":
        return len(raw) >= 32 and classify_media_bytes(raw) == "image"
    if kind == "video":
        # Prefer strict MP4 completeness (ftyp+moov); allow webm with enough data
        if raw.startswith(b"\x1aE\xdf\xa3"):
            return len(raw) >= 1024
        return is_complete_mp4(raw)
    return False


def _prefer_longer_b64(dst: dict[str, str], key: str, b64: str) -> None:
    """Keep the longest base64 candidate for a key (tool stdout usually wins)."""
    prev = dst.get(key)
    if prev is None or len(b64) > len(prev):
        dst[key] = b64


def extract_b64_file_markers(text: str) -> dict[str, str]:
    """Parse ``B64_FILE:<name>:<b64>`` markers → {path_or_name: b64}.

    Values of ``ok`` / empty are ignored (path-only acknowledgements).
    """
    out: dict[str, str] = {}
    if not text:
        return out
    for match in _B64_FILE_RE.finditer(text):
        name = match.group(1).strip()
        b64 = re.sub(r"\s+", "", match.group(2))
        if not name:
            continue
        if b64.lower() in {"ok", "done", "true", "1"}:
            continue
        if not looks_like_base64(b64):
            continue
        path = name if name.startswith("/") else f"/home/workdir/artifacts/{name}"
        _prefer_longer_b64(out, path, b64)
        _prefer_longer_b64(out, name, b64)
    return out


def extract_inline_b64_blobs(text: str) -> list[str]:
    if not text:
        return []
    blobs: list[str] = []
    for rx in (_B64_INLINE_RE, _B64_FENCE_RE):
        for match in rx.finditer(text):
            b64 = re.sub(r"\s+", "", match.group(1))
            if looks_like_base64(b64):
                blobs.append(b64)
    for match in _B64_LINE_RE.finditer(text):
        b64 = re.sub(r"\s+", "", match.group(0))
        if looks_like_base64(b64):
            blobs.append(b64)
    # de-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for b in blobs:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def strip_b64_markers(text: str) -> str:
    """Remove bulky base64 markers / dumps from user-visible answer text."""
    if not text:
        return text
    text = _B64_FILE_RE.sub("", text)
    text = _B64_INLINE_RE.sub("", text)
    text = _B64_FENCE_RE.sub("", text)
    text = _B64_LINE_RE.sub("", text)
    text = _DATA_VIDEO_RE.sub("", text)
    # Keep data:image only when short (tiny icons); drop huge dumps
    def _drop_huge_data_image(match: re.Match[str]) -> str:
        return "" if len(match.group(0)) > 200 else match.group(0)

    text = _DATA_IMAGE_RE.sub(_drop_huge_data_image, text)
    # collapse leftover blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def media_kind_for_path(path: str) -> str | None:
    lower = path.lower()
    if any(lower.endswith(ext) for ext in _VIDEO_EXTS):
        return "video"
    if any(lower.endswith(ext) for ext in _IMAGE_EXTS):
        return "image"
    return None


def _is_media_path(path: str) -> bool:
    return media_kind_for_path(path) is not None


def _file_id_for(path: str, raw: bytes) -> str:
    stem = path.rsplit("/", 1)[-1]
    # drop extension — save_local_* appends its own suffix
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = re.sub(r"[^0-9a-zA-Z_-]+", "_", stem)[:32] or "artifact"
    digest = hashlib.sha1(raw).hexdigest()[:16]
    # media routes accept [0-9a-f\-]{16,36}; use pure hex id for proxy URLs
    return digest


def _render_video_embed(url: str) -> str:
    """Return markdown-style video link.

    Do **not** emit raw ``<video>`` HTML here: chat UIs escape HTML before
    rendering markdown, which would turn tags into unplayable plain text.
    Clients convert ``[video](url)`` into a real ``<video>`` element.
    """
    return f"[video]({url})"


def _render_image_embed(url: str) -> str:
    """Always return markdown image so clients render <img>, not bare <a> links."""
    return f"![image]({url})"


async def materialize_sandbox_media(
    text: str,
    *,
    known_paths: set[str] | None = None,
    known_b64: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Persist harvested sandbox media and rewrite *text*.

    Returns ``(rewritten_text, embeds)``:
      - *rewritten_text*: sandbox paths replaced with local embeds; B64 markers stripped
      - *embeds*: every materialised media snippet (for streaming append)
    """
    known_paths = set(known_paths or ())
    # Tool-harvested base64 first (usually complete); text markers second and
    # only win when strictly longer (truncated final-answer base64 must not replace).
    known_b64: dict[str, str] = dict(known_b64 or {})
    for key, b64 in extract_b64_file_markers(text).items():
        _prefer_longer_b64(known_b64, key, b64)

    paths = list(known_paths)
    for path in find_sandbox_media_paths(text):
        if path not in paths:
            paths.append(path)

    # associate unscoped inline B64: blobs with the first path lacking b64
    inline_blobs = extract_inline_b64_blobs(text)
    # Prefer longer blobs first when assigning
    inline_blobs.sort(key=len, reverse=True)
    orphan_paths = [
        p
        for p in paths
        if p not in known_b64
        and not any(k.endswith(p.rsplit("/", 1)[-1]) for k in known_b64)
    ]
    for path, blob in zip(orphan_paths, inline_blobs):
        _prefer_longer_b64(known_b64, path, blob)

    # if only blobs and no paths, invent artifact names from media type
    if not paths and inline_blobs:
        for i, blob in enumerate(inline_blobs):
            decoded = decode_base64_media(blob)
            if not decoded:
                continue
            raw, kind = decoded
            if not is_playable_media(raw, kind):
                continue
            ext = ".mp4" if kind == "video" else ".png"
            path = f"/home/workdir/artifacts/generated_{i}{ext}"
            paths.append(path)
            known_b64[path] = blob

    if not paths and not known_b64:
        return text, []

    cfg = get_config()
    app_url = cfg.get_str("app.app_url", "").rstrip("/")
    rewritten = text
    embeds: list[str] = []

    for path in paths:
        basename = path.rsplit("/", 1)[-1]
        # Collect all candidates for this path; pick longest playable
        candidates = [
            known_b64[k]
            for k in (path, basename)
            if k in known_b64
        ]
        # Also consider any inline blob that decodes to matching kind
        for blob in inline_blobs:
            if blob not in candidates:
                candidates.append(blob)
        if not candidates:
            continue

        best_raw: bytes | None = None
        best_kind: str | None = None
        for b64 in sorted(candidates, key=len, reverse=True):
            try:
                raw = base64.b64decode(re.sub(r"\s+", "", b64), validate=False)
            except Exception:
                continue
            kind = media_kind_for_path(path) or classify_media_bytes(raw)
            if kind is None or len(raw) < 16:
                continue
            if not is_playable_media(raw, kind):
                logger.warning(
                    "sandbox media rejected (incomplete/unplayable): path={} bytes={}",
                    path,
                    len(raw),
                )
                continue
            best_raw, best_kind = raw, kind
            break

        if best_raw is None or best_kind is None:
            continue
        raw, kind = best_raw, best_kind

        file_id = _file_id_for(path, raw)
        try:
            if kind == "video":
                await asyncio.to_thread(save_local_video, raw, file_id)
                url = build_signed_media_url("video", file_id, app_url=app_url)
                embed = _render_video_embed(url)
            else:
                mime = "image/png"
                if raw.startswith(b"\xff\xd8\xff"):
                    mime = "image/jpeg"
                elif raw.startswith(b"GIF8"):
                    mime = "image/gif"
                elif raw.startswith(b"RIFF"):
                    mime = "image/webp"
                await asyncio.to_thread(save_local_image, raw, mime, file_id)
                url = build_signed_media_url("image", file_id, app_url=app_url)
                embed = _render_image_embed(url)
        except Exception as exc:
            logger.warning(
                "sandbox media materialize failed: path={} error={}", path, exc
            )
            continue

        embeds.append(embed)
        # Prefer replacing fenced path `.../file.mp4` with bare embed (no nested ticks)
        rewritten = rewritten.replace(f"`{path}`", embed)
        if path in rewritten:
            rewritten = rewritten.replace(path, embed)
        basename = path.rsplit("/", 1)[-1]
        if basename:
            rewritten = rewritten.replace(f"`{basename}`", embed)
        # If model (or prior rewrite) left a bare proxy URL, upgrade to media markdown
        if url and url in rewritten and embed not in rewritten:
            rewritten = rewritten.replace(url, embed)

        logger.info(
            "sandbox media materialized: path={} kind={} bytes={} file_id={}",
            path,
            kind,
            len(raw),
            file_id,
        )

    rewritten = strip_b64_markers(rewritten)
    # Upgrade any leftover bare local media URLs to markdown embeds
    rewritten = _upgrade_bare_media_urls(rewritten)
    # Always append playable embeds at the end if missing (guarantees UI preview)
    for embed in embeds:
        if embed and embed not in rewritten:
            rewritten = (rewritten.rstrip() + "\n\n" + embed).strip()
    # If we saw sandbox media paths but failed to transfer a playable file, say so
    if paths and not embeds:
        note = (
            "\n\n_(沙箱里可能已生成媒体文件，但未能完整传回本地预览"
            "——平台会截断过长的 base64。请改用更小分辨率/码率后重试。)_"
        )
        if note.strip() not in rewritten:
            rewritten = rewritten.rstrip() + note
    return rewritten, embeds


_LOCAL_VIDEO_URL_RE = re.compile(
    r"(?<!\()((?:https?://[^\s)\]>\"']+)?/v1/files/video\?[^\s)\]>\"']+)",
    re.IGNORECASE,
)
_LOCAL_IMAGE_URL_RE = re.compile(
    r"(?<!\()((?:https?://[^\s)\]>\"']+)?/v1/files/image\?[^\s)\]>\"']+)",
    re.IGNORECASE,
)


def _upgrade_bare_media_urls(text: str) -> str:
    """Turn bare /v1/files/{video,image} URLs into markdown media embeds."""
    if not text:
        return text

    def _vid(m: re.Match[str]) -> str:
        url = m.group(1).rstrip(".,;:。，；：")
        # already inside markdown? (lookbehind only checks '(')
        return f"[video]({url})"

    def _img(m: re.Match[str]) -> str:
        url = m.group(1).rstrip(".,;:。，；：")
        return f"![image]({url})"

    # Skip URLs already wrapped as [video](...) or ![image](...)
    # Process line by line to avoid double-wrapping
    lines: list[str] = []
    for line in text.split("\n"):
        if "[video](" in line or "![image](" in line:
            lines.append(line)
            continue
        line = _LOCAL_VIDEO_URL_RE.sub(_vid, line)
        line = _LOCAL_IMAGE_URL_RE.sub(_img, line)
        lines.append(line)
    return "\n".join(lines)


def merge_personality_with_artifact_hint(custom: str) -> str:
    custom = (custom or "").strip()
    hint = artifact_retrieval_hint()
    if not custom:
        return hint
    if "B64_FILE:" in custom or "base64 -w0" in custom:
        return custom
    return f"{custom}\n\n{hint}"


__all__ = [
    "artifact_retrieval_hint",
    "find_sandbox_media_paths",
    "paths_from_bash_command",
    "base64_target_from_bash_command",
    "looks_like_base64",
    "extract_base64_payload",
    "decode_base64_media",
    "classify_media_bytes",
    "is_complete_mp4",
    "is_playable_media",
    "extract_b64_file_markers",
    "extract_inline_b64_blobs",
    "strip_b64_markers",
    "materialize_sandbox_media",
    "merge_personality_with_artifact_hint",
    "media_kind_for_path",
]
