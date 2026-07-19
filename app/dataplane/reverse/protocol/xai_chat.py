"""XAI app-chat protocol — payload builder and SSE stream adapter."""

import re
from dataclasses import dataclass
from typing import Any

import orjson

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.control.model.enums import ModeId
from app.dataplane.reverse.protocol.sandbox_artifacts import (
    base64_target_from_bash_command,
    merge_personality_with_artifact_hint,
    paths_from_bash_command,
)
from app.dataplane.reverse.protocol.xai_chat_reasoning import ReasoningAggregator


def build_chat_payload(
    *,
    message:               str,
    mode_id:               ModeId,
    file_attachments:      list[str]        = (),
    tool_overrides:        dict[str, Any]   | None = None,
    model_config_override: dict[str, Any]   | None = None,
    request_overrides:     dict[str, Any]   | None = None,
) -> dict[str, Any]:
    """Build the JSON payload for POST /rest/app-chat/conversations/new."""
    cfg = get_config()

    payload: dict[str, Any] = {
        "collectionIds":               [],
        "connectors":                  [],
        "deviceEnvInfo": {
            "darkModeEnabled":  False,
            "devicePixelRatio": 2,
            "screenHeight":     1329,
            "screenWidth":      2056,
            "viewportHeight":   1083,
            "viewportWidth":    2056,
        },
        "disableMemory":               not cfg.get_bool("features.memory", False),
        "disableSearch":               False,
        "disableSelfHarmShortCircuit": False,
        "disableTextFollowUps":        False,
        "enableImageGeneration":       True,
        "enableImageStreaming":        True,
        "enableSideBySide":            True,
        "fileAttachments":             list(file_attachments),
        "forceConcise":                False,
        "forceSideBySide":             False,
        "imageAttachments":            [],
        "imageGenerationCount":        2,
        "isAsyncChat":                 False,
        "message":                     message,
        "modeId":                      mode_id.to_api_str(),
        "responseMetadata":            {},
        "returnImageBytes":            False,
        "returnRawGrokInXaiRequest":   False,
        "searchAllConnectors":         False,
        "sendFinalMetadata":           True,
        "temporary":                   cfg.get_bool("features.temporary", True),
        "toolOverrides": tool_overrides or {
            "gmailSearch":           False,
            "googleCalendarSearch":  False,
            "outlookSearch":         False,
            "outlookCalendarSearch": False,
            "googleDriveSearch":     False,
        },
    }

    custom = cfg.get_str("features.custom_instruction", "").strip()
    # Always attach artifact-retrieval hint so code_execution media can be
    # materialised locally (sandbox paths alone are not downloadable).
    personality = merge_personality_with_artifact_hint(custom)
    if personality:
        payload["customPersonality"] = personality

    if model_config_override:
        payload["responseMetadata"]["modelConfigOverride"] = model_config_override

    if request_overrides:
        payload.update({k: v for k, v in request_overrides.items() if v is not None})

    logger.debug(
        "chat payload built: mode={} message_len={} file_count={}",
        mode_id.to_api_str(), len(message), len(file_attachments),
    )
    return payload


# ---------------------------------------------------------------------------
# SSE line classification (unchanged)
# ---------------------------------------------------------------------------


def classify_line(line: str | bytes) -> tuple[str, str]:
    """Return (event_type, data) for a raw SSE line.

    event_type: 'data' | 'done' | 'skip'

    Handles both standard SSE ``data: {...}`` lines and raw JSON lines
    (upstream sometimes omits the ``data:`` prefix).
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return "skip", ""
    if line.startswith("data:"):
        data = line[5:].strip()
        if data == "[DONE]":
            return "done", ""
        return "data", data
    if line.startswith("event:"):
        return "skip", ""
    # Raw JSON line (no "data:" prefix) — treat as data.
    if line.startswith("{"):
        return "data", line
    return "skip", ""


def stream_error_from_payload(obj: dict[str, Any]) -> UpstreamError | None:
    """Convert upstream in-band stream error payloads to retryable errors."""
    error = obj.get("error")
    if not isinstance(error, dict):
        return None

    raw_message = error.get("message") or error.get("error") or "Upstream stream error"
    message = str(raw_message)
    code = error.get("code")
    text = message.lower()
    status = 429 if code == 8 or "too many requests" in text or "rate limit" in text else 502

    try:
        body = orjson.dumps(obj).decode()
    except (TypeError, ValueError):
        body = str(obj)

    return UpstreamError(
        f"Upstream stream error: {message}",
        status=status,
        body=body[:400],
    )


def raise_for_stream_error(data: str | bytes | dict[str, Any]) -> None:
    """Raise :class:`UpstreamError` for raw or decoded in-band stream errors."""
    if isinstance(data, dict):
        obj = data
    else:
        try:
            obj = orjson.loads(data)
        except (orjson.JSONDecodeError, ValueError, TypeError):
            return
    if not isinstance(obj, dict):
        return
    exc = stream_error_from_payload(obj)
    if exc is not None:
        raise exc


# ---------------------------------------------------------------------------
# FrameEvent — single output event from StreamAdapter.feed()
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FrameEvent:
    """One parsed event produced by StreamAdapter."""

    kind: str
    """Event kind:
    - ``text``      — cleaned final text token  (content = token string)
    - ``thinking``  — Grok main-model thinking   (content = raw token)
    - ``image``     — generated image final URL   (content = full URL, image_id = upstream UUID)
    - ``image_progress`` — generated image progress (content = percent string, image_id = upstream UUID)
    - ``annotation`` — url citation annotation   (annotation_data = annotation dict)
    - ``soft_stop`` — stream end signal
    - ``skip``      — filtered frame, do nothing
    """
    content: str = ""
    image_id: str = ""
    rollout_id: str = ""
    message_tag: str = ""
    message_step_id: int | None = None
    annotation_data: dict | None = None


# ---------------------------------------------------------------------------
# StreamAdapter — stateful SSE frame parser
# ---------------------------------------------------------------------------

_GROK_RENDER_RE = re.compile(
    r'<grok:render\s+card_id="([^"]+)"\s+card_type="([^"]+)"\s+type="([^"]+)"'
    r'[^>]*>.*?</grok:render>',
    re.DOTALL,
)

_IMAGE_BASE = "https://assets.grok.com/"

# 工具使用卡片 → emoji 单行格式化映射（详细模式专用）
# 格式: tool_name → (emoji, (可展示的参数 key 列表))
_TOOL_FMT: dict[str, tuple[str, tuple[str, ...]]] = {
    "web_search":          ("🔍", ("query", "q")),
    "x_search":            ("🔍", ("query",)),
    "x_keyword_search":    ("🔍", ("query",)),
    "x_semantic_search":   ("🔍", ("query",)),
    "browse_page":         ("🌐", ("url",)),
    "search_images":       ("🖼️", ("image_description", "imageDescription")),
    "image_search":        ("🖼️", ("image_description", "imageDescription")),
    "chatroom_send":       ("📋", ("message",)),
    "code_execution":      ("💻", ()),
}


class StreamAdapter:
    """Parse upstream SSE frames and emit :class:`FrameEvent` objects.

    One instance per HTTP request.  Call :meth:`feed` for every ``data:``
    line; iterate over the returned list of events.
    """

    __slots__ = (
        "_card_cache",
        "_citation_order",
        "_citation_map",
        "_last_citation_index",
        "_pending_citations",
        "_annotations",
        "_text_offset",
        "_emitted_reasoning_keys",
        "_reasoning",
        "_summary_mode",
        "_last_rollout",
        "_content_started",
        "_web_search_results",
        "_web_search_urls_seen",
        "thinking_buf",
        "text_buf",
        "image_urls",
        "sandbox_media_paths",
        "sandbox_media_b64",
        "used_code_execution",
        "_pending_base64_path",
        "_last_bash_command",
        "_b64_part_buf",
    )

    def __init__(self) -> None:
        self._card_cache: dict[str, dict] = {}
        self._citation_order: list[str] = []
        self._citation_map: dict[str, int] = {}
        self._last_citation_index: int = -1
        self._pending_citations: list[dict] = []       # _render_replace 产出的待定位引用
        self._annotations: list[dict] = []             # 已定位的完整 annotations（绝对位置）
        self._text_offset: int = 0                     # 累计文本长度（仅 text 事件）
        self._emitted_reasoning_keys: set[str] = set()
        # 思维链模式：精简摘要 / 详细原始流
        self._summary_mode: bool = get_config().get_bool("features.thinking_summary", False)
        self._last_rollout: str = ""
        self._content_started: bool = False
        self._reasoning = ReasoningAggregator() if self._summary_mode else None
        self._web_search_results: list[dict] = []
        self._web_search_urls_seen: set[str] = set()
        self.thinking_buf: list[str] = []
        self.text_buf: list[str] = []
        self.image_urls: list[tuple[str, str]] = []   # [(url, imageUuid), ...]
        # code_execution / bash sandbox media harvest
        self.sandbox_media_paths: set[str] = set()
        self.sandbox_media_b64: dict[str, str] = {}
        self.used_code_execution: bool = False
        self._pending_base64_path: str | None = None
        self._last_bash_command: str = ""
        # Accumulate all tool stdout for chunked B64PART reassembly across steps
        self._b64_part_buf: list[str] = []

    # 搜索信源追加：当配置启用且有 webSearchResults 时，格式化为 ## Sources 段落
    # 标记行 [grok2api-sources]: # 是 markdown link reference definition，渲染器不显示，
    # 用于 _extract_message() 在多轮对话中精确识别并剥离前轮的 Sources 段落
    def references_suffix(self) -> str:
        """当有搜索信源且配置启用时，格式化为 ## Sources markdown 段落。"""
        if not self._web_search_results:
            return ""
        if not get_config().get_bool("features.show_search_sources", False):
            return ""
        lines = ["\n\n## Sources", "[grok2api-sources]: #"]
        for item in self._web_search_results:
            title = item.get("title") or item.get("url", "")
            # 转义 Markdown 链接文本中的特殊字符，防止 []\ 打坏语法
            title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            lines.append(f"- [{title}]({item['url']})")
        return "\n".join(lines) + "\n"

    # 内联引用 annotations：生成时同步构建，含绝对位置
    def annotations_list(self) -> list[dict]:
        """已收集的 url_citation annotations（扁平格式，绝对位置）。无引用时返回 []。"""
        return list(self._annotations)

    # 结构化搜索信源：始终输出（不受配置开关控制），供 search_sources 字段使用
    def search_sources_list(self) -> list[dict] | None:
        """当有搜索信源时，返回结构化列表；无则返回 None。"""
        if not self._web_search_results:
            return None
        return [
            {
                "url": item["url"],
                "title": item.get("title") or item.get("url", ""),
                "type": item.get("type", "web"),
            }
            for item in self._web_search_results
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, data: str) -> list[FrameEvent]:
        """Parse one JSON ``data:`` payload; return 0-N events."""
        try:
            obj = orjson.loads(data)
        except (orjson.JSONDecodeError, ValueError, TypeError):
            return []
        raise_for_stream_error(obj)

        result = obj.get("result")
        if not result:
            return []
        resp = result.get("response")
        if not resp:
            return []

        events: list[FrameEvent] = []

        # ── cache every cardAttachment first ──────────────────────
        card_raw = resp.get("cardAttachment")
        if card_raw:
            events.extend(self._handle_card(card_raw))

        # Late/fallback image harvest from modelResponse (fileAttachments etc.)
        model_response = resp.get("modelResponse")
        if isinstance(model_response, dict):
            self._harvest_model_response_images(model_response)

        # ── 采集 webSearchResults（搜索信源，多帧累积去重）───────
        wsr = resp.get("webSearchResults")
        if wsr and isinstance(wsr, dict):
            for item in wsr.get("results", []):
                if isinstance(item, dict) and item.get("url"):
                    url = item["url"]
                    if url not in self._web_search_urls_seen:
                        self._web_search_urls_seen.add(url)
                        self._web_search_results.append({**item, "type": "web"})

        # ── 采集 xSearchResults（X/Twitter 帖子信源，多帧累积去重）──
        xsr = resp.get("xSearchResults")
        if xsr and isinstance(xsr, dict):
            for item in xsr.get("results", []):
                if isinstance(item, dict) and item.get("postId") and item.get("username"):
                    url = f"https://x.com/{item['username']}/status/{item['postId']}"
                    if url not in self._web_search_urls_seen:
                        self._web_search_urls_seen.add(url)
                        # 构造 title：归一化空白，text 为空退回 @username
                        # Markdown 转义统一在 references_suffix() 中处理
                        raw = re.sub(r"\s+", " ", (item.get("text") or "")).strip()
                        if raw:
                            title = f"𝕏/@{item['username']}: {raw[:50]}{'...' if len(raw) > 50 else ''}"
                        else:
                            title = f"𝕏/@{item['username']}"
                        self._web_search_results.append({"url": url, "title": title, "type": "x_post"})

        token   = resp.get("token")
        think   = resp.get("isThinking")
        tag     = resp.get("messageTag")
        rollout = resp.get("rolloutId")
        step_id = resp.get("messageStepId")

        if tag == "tool_usage_card":
            self._track_sandbox_tool_card(resp)
            # 正文已开始后的迟到 tool card：静默丢弃
            if self._content_started:
                return events
            if self._summary_mode:
                # 精简模式：走 ReasoningAggregator 提炼摘要
                for line in self._summarize_tool_usage_summary(
                    resp, rollout=rollout, step_id=step_id,
                ):
                    self._append_reasoning(
                        events, line,
                        rollout=rollout, tag=tag, step_id=step_id,
                    )
            else:
                # 详细模式：格式化为 emoji 单行（含 Agent 身份）
                line = self._format_tool_card(resp, rollout=rollout)
                if line:
                    # 同步 Agent 标识，确保后续 Grok summary 能正确插前缀
                    if rollout:
                        self._last_rollout = rollout
                    self._append_reasoning(
                        events, line,
                        rollout=rollout, tag=tag, step_id=step_id,
                    )
            return events   # card events (if any) already added

        # ── raw_function_result ───────────────────────────────────
        if tag == "raw_function_result":
            self._harvest_code_execution_result(resp)
            return events

        # ── toolUsageCardId-only follow-up frame ──────────────────
        if resp.get("toolUsageCardId") and not resp.get("webSearchResults") and not resp.get("codeExecutionResult"):
            return events

        # Harvest codeExecutionResult on any frame that carries it
        if resp.get("codeExecutionResult") is not None:
            self._harvest_code_execution_result(resp)

        # ── 思维链 token 处理 ──────────────────────────────────────
        if token is not None and think is True:
            # 正文已开始后的迟到 thinking：写入 buf（非流式可用）但不发事件（流式不显示）
            if self._content_started:
                raw = str(token).strip()
                if raw:
                    formatted = raw if raw.endswith("\n") else raw + "\n"
                    self.thinking_buf.append(formatted)
                return events
            if self._summary_mode:
                # 精简模式：走 ReasoningAggregator 提炼摘要
                for line in self._reasoning.on_thinking(
                    str(token), tag=tag, rollout=rollout,
                    step_id=step_id if isinstance(step_id, int) else None,
                ):
                    self._append_reasoning(
                        events, line,
                        rollout=rollout, tag=tag, step_id=step_id,
                    )
            else:
                # 详细模式：Agent 切换时插入身份前缀，原始 token 直接透传
                raw = str(token)
                # 去掉 Grok summary 自带的 "- " 前缀，避免触发 markdown 列表缩进
                if raw.startswith("- "):
                    raw = raw[2:]
                if not raw:
                    return events
                agent = rollout or ""
                if agent and agent != self._last_rollout:
                    self._last_rollout = agent
                    # Agent 切换标识：绕过去重，直接写 buf + 发 event（同一 Agent 可多次出现）
                    header = f"\n[{agent}]\n"
                    self.thinking_buf.append(header)
                    events.append(FrameEvent(
                        "thinking", header, rollout_id=agent,
                    ))
                self._append_reasoning(
                    events, raw,
                    rollout=rollout, tag=tag, step_id=step_id,
                )
            return events

        # ── final text token (needs cleaning) ─────────────────────
        if token is not None and think is not True and tag == "final":
            self._content_started = True
            cleaned, local_anns = self._clean_token(token)
            if cleaned:
                # 先发 text 事件（OpenAI 顺序：text.delta 先，annotation.added 后）
                self.text_buf.append(cleaned)
                events.append(FrameEvent("text", cleaned))
                # 再发 annotation 事件：局部位置 → 绝对位置
                for ann in local_anns:
                    ann["start_index"] = self._text_offset + ann.pop("local_start")
                    ann["end_index"] = self._text_offset + ann.pop("local_end")
                    self._annotations.append(ann)
                    events.append(FrameEvent("annotation", annotation_data=ann))
                self._text_offset += len(cleaned)
            return events

        # ── end signals ───────────────────────────────────────────
        if resp.get("isSoftStop"):
            self._flush_pending_reasoning(events)
            events.append(FrameEvent("soft_stop"))
            return events

        if resp.get("finalMetadata"):
            self._flush_pending_reasoning(events)
            events.append(FrameEvent("soft_stop"))
            return events

        return events

    # ------------------------------------------------------------------
    # Card attachment handling
    # ------------------------------------------------------------------

    def _handle_card(self, card_raw: dict) -> list[FrameEvent]:
        """Cache card data; emit image event on progress=100."""
        try:
            jd = orjson.loads(card_raw["jsonData"])
        except (orjson.JSONDecodeError, ValueError, TypeError, KeyError):
            return []

        card_id = jd.get("id", "")
        self._card_cache[card_id] = jd

        chunk = jd.get("image_chunk")
        if chunk:
            progress = chunk.get("progress")
            uuid = str(chunk.get("imageUuid") or "")
            events: list[FrameEvent] = []
            try:
                if progress is not None:
                    events.append(FrameEvent("image_progress", str(int(progress)), uuid))
            except (TypeError, ValueError):
                pass
            # Accept progress>=100 (some payloads use 100.0 / true completion)
            try:
                progress_i = int(progress) if progress is not None else -1
            except (TypeError, ValueError):
                progress_i = -1
            if progress_i >= 100 and not chunk.get("moderated"):
                raw_url = chunk.get("imageUrl") or ""
                if isinstance(raw_url, str) and raw_url.strip():
                    url = raw_url if raw_url.startswith("http") else _IMAGE_BASE + raw_url.lstrip("/")
                    # de-dupe by uuid / url
                    if not any(u == url or i == uuid for u, i in self.image_urls):
                        self.image_urls.append((url, uuid or url))
                        events.append(FrameEvent("image", url, uuid or url))
            return events

        return []

    def _harvest_model_response_images(self, model_response: dict[str, Any]) -> None:
        """Fallback: pull finished images from modelResponse when card stream is incomplete."""
        existing_ids = {img_id for _, img_id in self.image_urls}
        existing_urls = {url for url, _ in self.image_urls}

        for item in model_response.get("generatedImageUrls") or []:
            if not isinstance(item, str) or not item.strip():
                continue
            url = item if item.startswith("http") else _IMAGE_BASE + item.lstrip("/")
            if url in existing_urls:
                continue
            img_id = url.rstrip("/").rsplit("/", 1)[-1].split(".", 1)[0] or url
            self.image_urls.append((url, img_id))
            existing_urls.add(url)
            existing_ids.add(img_id)

        # fileAttachments often hold asset UUIDs for generated images
        user_id = None
        for url, _ in self.image_urls:
            # users/<userId>/generated/<uuid>/...
            m = re.search(r"/users/([0-9a-fA-F-]{16,})/", url)
            if m:
                user_id = m.group(1)
                break

        for att in model_response.get("fileAttachments") or []:
            if not isinstance(att, str) or not att.strip():
                continue
            fid = att.strip()
            if fid in existing_ids:
                continue
            if fid.startswith("http") or fid.startswith("users/"):
                url = fid if fid.startswith("http") else _IMAGE_BASE + fid.lstrip("/")
            elif user_id:
                url = f"{_IMAGE_BASE}users/{user_id}/generated/{fid}/image.jpg"
            else:
                # last resort — may still download via assets CDN path
                url = f"{_IMAGE_BASE}{fid}/content"
            if url in existing_urls:
                continue
            self.image_urls.append((url, fid))
            existing_urls.add(url)
            existing_ids.add(fid)

    # ------------------------------------------------------------------
    # Token cleaning — <grok:render> → markdown
    # ------------------------------------------------------------------

    # 返回 (cleaned_text, local_annotations)，annotations 含局部 start/end
    def _clean_token(self, token: str) -> tuple[str, list[dict]]:
        if "<grok:render" not in token:
            return token, []
        cleaned = _GROK_RENDER_RE.sub(self._render_replace, token)
        # 去除引用标签替换后残留的独占空白行（如 "\n [[1]](...)" → " [[1]](...)"）
        cleaned = cleaned.lstrip("\n") if cleaned.startswith("\n") and "[[" in cleaned else cleaned

        # 从 cleaned 中定位 pending citations 的局部位置（游标递进防碰撞）
        local_annotations: list[dict] = []
        if self._pending_citations:
            search_start = 0
            for cite in self._pending_citations:
                pos = cleaned.find(cite["needle"], search_start)
                if pos != -1:
                    local_annotations.append({
                        "type": "url_citation",
                        "url": cite["url"],
                        "title": cite["title"],
                        "local_start": pos,
                        "local_end": pos + len(cite["needle"]),
                    })
                    search_start = pos + len(cite["needle"])
                # 找不到 → fail closed，跳过此 annotation
            self._pending_citations.clear()
        return cleaned, local_annotations

    def _render_replace(self, m: re.Match) -> str:
        card_id     = m.group(1)
        render_type = m.group(3)
        card = self._card_cache.get(card_id)
        if not card:
            return ""

        if render_type == "render_searched_image":
            img   = card.get("image", {})
            title = img.get("title", "image")
            thumb = img.get("thumbnail") or img.get("original", "")
            link  = img.get("link", "")
            if link:
                return f"[![{title}]({thumb})]({link})"
            return f"![{title}]({thumb})"

        if render_type == "render_generated_image":
            return ""   # actual URL emitted by progress=100 card frame

        if render_type == "render_inline_citation":
            url = card.get("url", "")
            if not url:
                return ""
            index = self._citation_map.get(url)
            if index is None:
                self._citation_order.append(url)
                index = len(self._citation_order)
                self._citation_map[url] = index
            # 连续相同引用去重
            if index == self._last_citation_index:
                return ""
            self._last_citation_index = index
            citation_text = f" [[{index}]]({url})"
            # 解析标题：card → webSearchResults → URL fallback
            # Grok citation card 仅含 [id, type, cardType, url]，无 title 字段
            title = card.get("title", "")
            if not title:
                for item in self._web_search_results:
                    if item.get("url") == url:
                        title = item.get("title", "")
                        break
            # 记录引用元数据，位置在 _clean_token 返回后定位
            self._pending_citations.append({
                "url": url,
                "title": title or url,
                "needle": citation_text,
            })
            return citation_text

        return ""

    def _append_reasoning(
        self,
        events: list[FrameEvent],
        line: str,
        *,
        rollout: str | None,
        tag: str | None,
        step_id: Any,
    ) -> None:
        """将思维链文本追加到 thinking_buf 和事件列表（双模式去重）"""
        if self._summary_mode:
            # 精简模式：激进去重（移除标点/空格后比较）
            text = line.strip()
            if not text:
                return
            key = self._normalize_key(text)
        else:
            # 详细模式：精确去重（rollout + 原文）
            text = line
            if not text:
                return
            key = f"{rollout or ''}:{text}"

        if key in self._emitted_reasoning_keys:
            return
        self._emitted_reasoning_keys.add(key)

        # 统一用 \n 换行（去掉 "- " 前缀后不再有列表上下文，普通 \n 即可）
        formatted = text if text.endswith("\n") else text + "\n"
        self.thinking_buf.append(formatted)
        events.append(FrameEvent(
            "thinking",
            formatted,
            rollout_id=rollout or "",
            message_tag=tag or "",
            message_step_id=step_id if isinstance(step_id, int) else None,
        ))

    def _flush_pending_reasoning(self, events: list[FrameEvent]) -> None:
        """flush ReasoningAggregator 缓冲事件（仅精简模式有效）"""
        if self._summary_mode and self._reasoning is not None:
            for line in self._reasoning.finalize():
                self._append_reasoning(events, line, rollout="", tag="summary", step_id=None)

    @staticmethod
    def _extract_tool_info(resp: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """从 toolUsageCard 提取工具名（snake_case）和参数"""
        card = resp.get("toolUsageCard")
        if not isinstance(card, dict):
            return "", {}
        for key, value in card.items():
            if key == "toolUsageCardId" or not isinstance(value, dict):
                continue
            # camelCase → snake_case
            tool_name = re.sub(r"(?<!^)([A-Z])", r"_\1", key).lower()
            raw_args = value.get("args")
            return tool_name, (raw_args if isinstance(raw_args, dict) else {})
        return "", {}

    # 精简模式：走 ReasoningAggregator 提炼摘要
    def _summarize_tool_usage_summary(self, resp: dict[str, Any], *, rollout: str | None, step_id: int | None) -> list[str]:
        tool_name, args = self._extract_tool_info(resp)
        if not tool_name:
            return []
        return self._reasoning.on_tool_usage(tool_name, args, rollout=rollout, step_id=step_id)

    # 详细模式：格式化为 emoji 单行（含 Agent 身份）
    def _format_tool_card(self, resp: dict[str, Any], *, rollout: str | None) -> str:
        tool_name, args = self._extract_tool_info(resp)
        if not tool_name:
            return ""
        emoji, arg_keys = _TOOL_FMT.get(tool_name, ("🔧", ()))
        # 提取要展示的参数值
        display_arg = ""
        for ak in arg_keys:
            val = args.get(ak)
            if val:
                display_arg = str(val).strip()
                break
        # 构造 Agent 前缀（不加前导 \n，由 _append_reasoning 统一处理换行）
        prefix = f"[{rollout}] " if rollout else ""
        if display_arg:
            return f"{prefix}{emoji} {tool_name}: {display_arg}"
        return f"{prefix}{emoji} {tool_name}"

    def _normalize_key(self, text: str) -> str:
        lowered = text.lower()
        lowered = re.sub(r"https?://\S+", "", lowered)
        lowered = re.sub(r"[^\w\u4e00-\u9fff]+", "", lowered)
        return lowered

    # ------------------------------------------------------------------
    # Sandbox media harvest (code_execution / bash artifacts)
    # ------------------------------------------------------------------

    def _track_sandbox_tool_card(self, resp: dict[str, Any]) -> None:
        """Record artifact media paths / pending base64 targets from tool cards."""
        card = resp.get("toolUsageCard")
        if not isinstance(card, dict):
            return
        command = ""
        bash = card.get("bash")
        if isinstance(bash, dict):
            self.used_code_execution = True
            args = bash.get("args")
            if isinstance(args, dict):
                command = str(args.get("command") or "")
        if not command:
            # other shapes: tool name key with args.command
            for key, value in card.items():
                if key in {"toolUsageCardId", "intent"} or not isinstance(value, dict):
                    continue
                # bash / code_execution / python etc.
                key_l = str(key).lower()
                if any(x in key_l for x in ("bash", "code", "python", "shell", "terminal")):
                    self.used_code_execution = True
                args = value.get("args")
                if isinstance(args, dict) and args.get("command"):
                    command = str(args.get("command") or "")
                    break
        if not command:
            return
        self.used_code_execution = True
        self._last_bash_command = command
        for path in paths_from_bash_command(command):
            self.sandbox_media_paths.add(path)
        target = base64_target_from_bash_command(command)
        if target:
            self._pending_base64_path = target
            self.sandbox_media_paths.add(target)

    @property
    def should_buffer_final_text(self) -> bool:
        """Buffer final answer when code_execution may dump base64 media."""
        return bool(
            self.used_code_execution
            or self.sandbox_media_paths
            or self.sandbox_media_b64
        )

    def _harvest_code_execution_result(self, resp: dict[str, Any]) -> None:
        """Capture base64 stdout produced for a pending sandbox media path."""
        from app.dataplane.reverse.protocol.sandbox_artifacts import (
            extract_base64_payload,
            reassemble_b64_parts,
        )

        cer = resp.get("codeExecutionResult")
        if not isinstance(cer, dict):
            return
        stdout = str(cer.get("stdout") or "")
        stderr = str(cer.get("stderr") or "")
        combined = stdout + "\n" + stderr
        if combined.strip():
            self._b64_part_buf.append(combined)

        # Prefer reassembly across all tool stdout so far (chunked transfer)
        all_out = "\n".join(self._b64_part_buf)
        b64 = reassemble_b64_parts(all_out)
        if not b64:
            # Only fall back to monolithic base64 when not in chunked mode
            if "B64META:" in all_out or "B64PART:" in all_out:
                return  # wait for remaining parts
            b64 = extract_base64_payload(combined)
        if not b64:
            return

        path = self._pending_base64_path
        # B64META:<name>:<len> may name the file
        meta = re.search(r"B64META:([^\s:]+):(\d+)", all_out)
        if meta:
            name = meta.group(1)
            if not name.startswith("/"):
                path = f"/home/workdir/artifacts/{name}"
            else:
                path = name
            self.sandbox_media_paths.add(path)

        if path is None and self.sandbox_media_paths:
            path = next(reversed(list(self.sandbox_media_paths)))
        if path is None:
            path = f"/home/workdir/artifacts/stdout_{len(self.sandbox_media_b64)}.bin"
            self.sandbox_media_paths.add(path)

        prev = self.sandbox_media_b64.get(path)
        if prev is None or len(b64) > len(prev):
            self.sandbox_media_b64[path] = b64
            logger.info(
                "sandbox base64 harvested: path={} b64_len={} parts_buf={}",
                path,
                len(b64),
                len(self._b64_part_buf),
            )
        # Don't clear pending on partial single dumps — only after we have something
        if "B64END" in combined or reassemble_b64_parts(all_out):
            self._pending_base64_path = None


__all__ = [
    "build_chat_payload",
    "classify_line",
    "FrameEvent",
    "StreamAdapter",
]
