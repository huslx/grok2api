"""Grok CLI chat-proxy protocol (cli-chat-proxy.grok.com).

Mirrors HM2899/grokcli-2api:
  - Authorization: Bearer <OIDC access_token>
  - X-XAI-Token-Auth: xai-grok-cli
  - x-grok-client-version / surface / identifier
  - x-grok-model-override: <model>
  - POST /v1/chat/completions (OpenAI-compatible SSE)
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import orjson

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger

CLI_VERSION = "0.2.93"
CLIENT_SURFACE = "grok-cli"
CLIENT_IDENTIFIER = "grok2api"

# Public API model name → cli-chat-proxy upstream model id.
# Clients may use friendly aliases (e.g. grok-4.5-console); upstream only knows grok-4.5.
CLI_MODELS: dict[str, str] = {
    "grok-4.5": "grok-4.5",
    "grok-4.5-console": "grok-4.5",
}

# Fields known to be rejected by cli-chat-proxy for current models.
_UPSTREAM_UNSUPPORTED = frozenset(
    {
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "n",
        "group",
    }
)


def resolve_cli_model(model: str) -> str:
    """Map public model name to cli-chat-proxy model id."""
    name = (model or "").strip()
    if not name:
        return name
    if name in CLI_MODELS:
        return CLI_MODELS[name]
    # Common alias pattern: strip trailing -console
    if name.endswith("-console"):
        base = name[: -len("-console")]
        return CLI_MODELS.get(base, base)
    return name


def build_cli_headers(access_token: str, model: str) -> dict[str, str]:
    upstream_model = resolve_cli_model(model)
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-model-override": upstream_model,
        "x-grok-client-version": CLI_VERSION,
        "x-grok-client-surface": CLIENT_SURFACE,
        "x-grok-client-identifier": CLIENT_IDENTIFIER,
        "User-Agent": f"grok-cli/{CLI_VERSION}",
        "Accept": "text/event-stream, application/json",
    }


# cli-chat-proxy only accepts these tool.type values (others → HTTP 422).
_CLI_TOOL_TYPES = frozenset({"function", "live_search"})


def _flatten_message_content(content: Any) -> Any:
    """Collapse OpenAI multi-part content lists into a plain string when possible.

    Codex / Responses clients often send content as
    ``[{"type":"text","text":"..."}]`` or ``input_text`` parts. cli-chat-proxy
    is happier with plain strings for text-only turns.
    """
    if not isinstance(content, list):
        return content
    texts: list[str] = []
    only_text = True
    for part in content:
        if not isinstance(part, dict):
            only_text = False
            break
        ptype = str(part.get("type") or "")
        if ptype in ("text", "input_text", "output_text"):
            texts.append(str(part.get("text") or ""))
        else:
            only_text = False
            break
    if only_text:
        return "".join(texts)
    return content


def _normalize_cli_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize chat messages for cli-chat-proxy."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = dict(msg)
        role = str(m.get("role") or "user")
        # Responses API "developer" → system for chat-completions upstream.
        if role == "developer":
            m["role"] = "system"
            role = "system"
        if "content" in m:
            m["content"] = _flatten_message_content(m.get("content"))
        # Drop empty system/developer noise.
        if role in ("system", "user", "assistant") and m.get("content") == "":
            if role == "system":
                continue
        out.append(m)
    return out


def _to_cli_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Keep only tool types cli-chat-proxy accepts; normalize Responses flat tools.

    Codex ships built-ins (web_search, local_shell, computer_use, …) that the
    Grok CLI proxy rejects with 422 ``unknown variant``. Strip them so function
    tools still work.
    """
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    dropped: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        ttype = str(tool.get("type") or "function")
        # Responses API flat function tools: {type, name, description, parameters}
        if ttype == "function" and "function" not in tool and "name" in tool:
            fn: dict[str, Any] = {
                "name": tool.get("name", ""),
                "description": tool.get("description") or "",
            }
            if tool.get("parameters") is not None:
                fn["parameters"] = tool["parameters"]
            elif tool.get("parameters") is None:
                fn["parameters"] = {"type": "object", "properties": {}}
            # strict is optional; keep if present
            if "strict" in tool:
                fn["strict"] = tool["strict"]
            out.append({"type": "function", "function": fn})
            continue
        if ttype not in _CLI_TOOL_TYPES:
            dropped.append(ttype)
            continue
        # Nested function tools — ensure parameters default
        if ttype == "function":
            nested = tool.get("function") if isinstance(tool.get("function"), dict) else None
            if nested is not None:
                fn = dict(nested)
                if fn.get("parameters") is None:
                    fn["parameters"] = {"type": "object", "properties": {}}
                cleaned = dict(tool)
                cleaned["function"] = fn
                out.append(cleaned)
                continue
        out.append(tool)
    if dropped:
        # De-dupe while preserving order for the log line
        seen: set[str] = set()
        uniq = [t for t in dropped if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
        logger.info(
            "cli tools filtered: dropped_types={} kept={}",
            uniq,
            len(out),
        )
    return out or None


def _normalize_tool_choice(tool_choice: Any, tools: list[dict[str, Any]] | None) -> Any:
    """Map Responses-style tool_choice to chat-completions form when needed."""
    if tool_choice is None or not tools:
        return tool_choice
    if not isinstance(tool_choice, dict):
        return tool_choice
    # Responses: {"type":"function","name":"foo"} → chat {"type":"function","function":{"name":"foo"}}
    if tool_choice.get("type") == "function" and "name" in tool_choice and "function" not in tool_choice:
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return tool_choice


def build_cli_payload(
    *,
    messages: list[dict[str, Any]],
    model: str,
    stream: bool = True,
    temperature: float | None = None,
    top_p: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Build OpenAI Chat Completions body for cli-chat-proxy."""
    upstream_model = resolve_cli_model(model)
    norm_messages = _normalize_cli_messages(messages)
    body: dict[str, Any] = {
        "model": upstream_model,
        "messages": norm_messages,
        "stream": bool(stream),
    }
    if temperature is not None:
        try:
            body["temperature"] = max(0.0, min(2.0, float(temperature)))
        except (TypeError, ValueError):
            pass
    if top_p is not None:
        try:
            body["top_p"] = max(0.0, min(1.0, float(top_p)))
        except (TypeError, ValueError):
            pass
    if max_tokens is not None:
        try:
            mt = int(max_tokens)
            if mt >= 1:
                body["max_tokens"] = mt
        except (TypeError, ValueError):
            pass
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort

    cli_tools = _to_cli_tools(tools)
    if cli_tools:
        body["tools"] = cli_tools
        choice = _normalize_tool_choice(tool_choice, cli_tools)
        if choice is not None:
            body["tool_choice"] = choice

    if body.get("stream"):
        body["stream_options"] = {"include_usage": True}

    for key in list(body.keys()):
        if key in _UPSTREAM_UNSUPPORTED:
            body.pop(key, None)

    logger.debug(
        "cli payload built: model={} upstream={} messages={} tools={} stream={}",
        model,
        upstream_model,
        len(norm_messages),
        len(cli_tools or []),
        body.get("stream"),
    )
    return body


class CliStreamAdapter:
    """Parse OpenAI-compatible SSE from cli-chat-proxy into text / tool deltas."""

    __slots__ = (
        "text_buf",
        "reasoning_buf",
        "usage",
        "finish_reason",
        "tool_calls",
        "_done",
        "_tool_acc",
    )

    def __init__(self) -> None:
        self.text_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.tool_calls: list[dict[str, Any]] = []
        self._done = False
        self._tool_acc: dict[int, dict[str, Any]] = {}

    def feed_data_obj(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a list of outbound events: {kind: text|reasoning|tool_calls, ...}."""
        if self._done:
            return []

        events: list[dict[str, Any]] = []
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]

        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            return events

        choice0 = choices[0] if isinstance(choices[0], dict) else {}
        finish = choice0.get("finish_reason")
        if finish:
            self.finish_reason = str(finish)

        delta = choice0.get("delta") if isinstance(choice0.get("delta"), dict) else {}
        # non-stream message shape
        message = choice0.get("message") if isinstance(choice0.get("message"), dict) else {}

        content = delta.get("content")
        if content is None and message:
            content = message.get("content")
        if isinstance(content, str) and content:
            self.text_buf.append(content)
            events.append({"kind": "text", "text": content})

        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning is None and message:
            reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            self.reasoning_buf.append(reasoning)
            events.append({"kind": "reasoning", "text": reasoning})

        tool_deltas = delta.get("tool_calls")
        if tool_deltas is None and message:
            tool_deltas = message.get("tool_calls")
        if isinstance(tool_deltas, list):
            for td in tool_deltas:
                if not isinstance(td, dict):
                    continue
                idx = int(td.get("index") or 0)
                acc = self._tool_acc.setdefault(
                    idx,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if td.get("id"):
                    acc["id"] = str(td["id"])
                if td.get("type"):
                    acc["type"] = str(td["type"])
                fn = td.get("function") if isinstance(td.get("function"), dict) else {}
                if fn.get("name"):
                    acc["function"]["name"] = str(fn["name"])
                if fn.get("arguments"):
                    acc["function"]["arguments"] = (
                        str(acc["function"].get("arguments") or "")
                        + str(fn["arguments"])
                    )
                events.append({"kind": "tool_calls", "tool_call": td, "index": idx})

        return events

    def finalize_tools(self) -> list[dict[str, Any]]:
        if not self._tool_acc:
            return []
        ordered = [self._tool_acc[i] for i in sorted(self._tool_acc)]
        self.tool_calls = ordered
        return ordered

    @property
    def full_text(self) -> str:
        return "".join(self.text_buf)

    @property
    def full_reasoning(self) -> str:
        return "".join(self.reasoning_buf)

    def mark_done(self) -> None:
        self._done = True
        self.finalize_tools()


async def stream_cli_chat(
    access_token: str,
    payload: dict[str, Any],
    *,
    timeout_s: float = 600.0,
) -> AsyncGenerator[tuple[str, str], None]:
    """POST cli-chat-proxy and yield (event_kind, raw_data).

    event_kind:
      - "data": JSON payload string of one SSE data frame
      - "done": stream finished
    """
    from app.dataplane.proxy import get_proxy_runtime
    from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
    from app.dataplane.reverse.runtime.endpoint_table import CLI_CHAT

    # payload.model is already resolved upstream id; override header uses it as-is.
    model = str(payload.get("model") or "")
    headers = build_cli_headers(access_token, model)
    payload_bytes = orjson.dumps(payload)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    session_kwargs = build_session_kwargs(lease=lease)

    async with ResettableSession(**session_kwargs) as session:
        try:
            response = await session.post(
                CLI_CHAT,
                headers=headers,
                data=payload_bytes,
                timeout=timeout_s,
                stream=True,
            )
        except Exception as exc:
            await proxy.feedback(lease, _transport_error_feedback())
            raise UpstreamError(f"CLI chat transport failed: {exc}", status=502) from exc

        if response.status_code != 200:
            body = ""
            try:
                # Prefer aiter for streamed error bodies; fall back to .content.
                chunks: list[bytes] = []
                async for part in response.aiter_content():
                    if isinstance(part, str):
                        part = part.encode("utf-8", "replace")
                    chunks.append(part)
                    if sum(len(c) for c in chunks) > 2000:
                        break
                if chunks:
                    body = b"".join(chunks).decode("utf-8", "replace")[:500]
                elif getattr(response, "content", None):
                    raw = response.content
                    if isinstance(raw, (bytes, bytearray)):
                        body = bytes(raw).decode("utf-8", "replace")[:500]
                    else:
                        body = str(raw)[:500]
            except Exception:
                body = ""
            await proxy.feedback(lease, _status_feedback(response.status_code))
            # Surface billing/permission hints in the public error message.
            if response.status_code == 403:
                msg = (
                    "CLI chat API returned 403 permission-denied "
                    "(account likely has billing limit=0 / no cli-chat chat entitlement; "
                    "use accounts with Grok CLI credits like grokcli-2api device-login). "
                    f"Upstream: {body[:240] or '(empty body)'}"
                )
            else:
                msg = f"CLI chat API returned {response.status_code}: {body[:280] or '(empty body)'}"
            raise UpstreamError(msg, status=response.status_code, body=body)

        await proxy.feedback(lease, _success_feedback())

        try:
            async for raw_line in response.aiter_lines():
                if isinstance(raw_line, bytes):
                    try:
                        raw_line = raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        raw_line = raw_line.decode("utf-8", errors="replace")
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith(":"):
                    # SSE comment / keepalive
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    yield "done", ""
                    return
                yield "data", data
        except Exception as exc:
            raise UpstreamError(f"CLI chat stream read failed: {exc}", status=502) from exc


def _success_feedback():
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    return ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200)


def _transport_error_feedback():
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    return ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR)


def _status_feedback(status: int):
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    if status == 403:
        kind = ProxyFeedbackKind.FORBIDDEN
    elif status == 429:
        kind = ProxyFeedbackKind.RATE_LIMITED
    elif status >= 500:
        kind = ProxyFeedbackKind.UPSTREAM_5XX
    else:
        kind = ProxyFeedbackKind.FORBIDDEN
    return ProxyFeedback(kind=kind, status_code=status)


__all__ = [
    "CLI_VERSION",
    "CLI_MODELS",
    "resolve_cli_model",
    "build_cli_headers",
    "build_cli_payload",
    "CliStreamAdapter",
    "stream_cli_chat",
]
