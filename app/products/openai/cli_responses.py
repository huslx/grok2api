"""CLI Responses API handler — /v1/responses for cli-chat-proxy models.

``grok-4.5`` / ``grok-4.5-console`` only work on cli-chat-proxy.grok.com.
``/v1/chat/completions`` already routes there; this module does the same for
the Responses API surface that Codex uses (wire_api = "responses").
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncGenerator

import orjson

from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_s
from app.platform.tokens import estimate_prompt_tokens, estimate_tokens, estimate_tool_call_tokens
from app.control.model.registry import resolve as resolve_model
from app.dataplane.reverse.protocol.xai_cli_chat import (
    CliStreamAdapter,
    build_cli_payload,
    stream_cli_chat,
)
from app.products._account_selection import reserve_account
from app.products.openai.chat import _configured_retry_codes, _should_retry_upstream
from app.products.openai.cli_chat import (
    _cli_max_retries,
    _prefer_warm_tokens,
    _reasoning_effort,
    _release_and_feedback,
    _resolve_access_token,
    _should_hot_convert,
    _usage_from_adapter,
)
from ._format import (
    build_resp_usage,
    format_sse,
    make_resp_id,
    make_resp_object,
)


def _to_chat_tools(tools: list[dict] | None) -> list[dict] | None:
    """Responses/Codex tools → CLI-safe Chat Completions function tools.

    Built-in OpenAI tools (web_search, local_shell, …) are dropped here as well
    as in ``build_cli_payload`` so ``client_function_tools`` reflects only
    tools the upstream actually receives.
    """
    from app.dataplane.reverse.protocol.xai_cli_chat import _to_cli_tools

    return _to_cli_tools(tools)


def _message_added(message_id: str) -> str:
    return format_sse("response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "status": "in_progress",
        },
    })


def _content_part_added(message_id: str) -> str:
    return format_sse("response.content_part.added", {
        "type": "response.content_part.added",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })


def _fc_items_from_adapter(adapter: CliStreamAdapter) -> list[dict]:
    items: list[dict] = []
    for i, tc in enumerate(adapter.tool_calls):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        items.append({
            "id": make_resp_id("fc"),
            "type": "function_call",
            "call_id": str(tc.get("id") or f"call_{i}"),
            "name": str(fn.get("name") or ""),
            "arguments": str(fn.get("arguments") or "{}"),
            "status": "completed",
        })
    return items


def _emit_fc_events(items: list[dict]) -> list[str]:
    events: list[str] = []
    for output_index, item in enumerate(items):
        item_id = item["id"]
        arguments = item.get("arguments") or "{}"
        events.append(format_sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {
                "id": item_id,
                "type": "function_call",
                "call_id": item["call_id"],
                "name": item["name"],
                "arguments": "",
                "status": "in_progress",
            },
        }))
        events.append(format_sse("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": output_index,
            "delta": arguments,
        }))
        events.append(format_sse("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "output_index": output_index,
            "arguments": arguments,
        }))
        events.append(format_sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": item,
        }))
    return events


async def create(
    *,
    model: str,
    messages: list[dict],
    stream: bool,
    emit_think: bool,
    temperature: float,
    top_p: float,
    response_id: str,
    reasoning_id: str,
    message_id: str,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    reasoning_effort: str | None = None,
) -> dict | AsyncGenerator[str, None]:
    """CLI models /v1/responses handler (cli-chat-proxy)."""
    cfg = get_config()
    spec = resolve_model(model)
    default_effort = cfg.get_str("chat.cli_reasoning_effort", "medium")
    effort = _reasoning_effort(
        emit_think, reasoning_effort, default_effort=default_effort
    )
    timeout_s = cfg.get_float("chat.timeout", 120.0)
    if timeout_s < 300:
        timeout_s = 600.0
    max_retries = _cli_max_retries(cfg)
    last_resort_convert = cfg.get_bool("chat.cli_oidc_hot_convert_last_resort", True)
    retry_codes = _configured_retry_codes(cfg)
    chat_tools = _to_chat_tools(tools)
    client_function_tools = bool(chat_tools)

    logger.info(
        "cli responses request: model={} stream={} messages={} tools={} effort={}",
        model,
        stream,
        len(messages),
        len(chat_tools or []),
        effort,
    )

    from app.dataplane.account import _directory as _acct_dir
    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    async def _one_attempt(
        attempt: int,
        excluded: list[str],
        *,
        stream_out: bool,
    ) -> AsyncGenerator[Any, None]:
        prefer = _prefer_warm_tokens()
        acct, selected_mode_id = await reserve_account(
            directory,
            spec,
            now_s_override=now_s(),
            exclude_tokens=excluded or None,
            prefer_tokens=prefer,
        )
        if acct is None:
            raise RateLimitError("No available accounts for this model tier")

        token = acct.token
        success = False
        fail_exc: BaseException | None = None
        adapter = CliStreamAdapter()
        allow_convert = _should_hot_convert(
            last_resort=last_resort_convert,
            attempt=attempt,
            max_retries=max_retries,
        )
        t_req = time.perf_counter()
        oidc_ms = 0

        try:
            try:
                t_oidc = time.perf_counter()
                access = await asyncio.to_thread(
                    _resolve_access_token, token, allow_convert=allow_convert
                )
                oidc_ms = int((time.perf_counter() - t_oidc) * 1000)
            except UpstreamError as exc:
                fail_exc = exc
                if attempt < max_retries:
                    logger.warning(
                        "cli responses oidc miss swap: attempt={}/{} status={} "
                        "token={}... oidc_ms={} hot_convert={}",
                        attempt + 1,
                        max_retries,
                        exc.status,
                        token[:8],
                        oidc_ms,
                        allow_convert,
                    )
                    yield ("retry", token, exc)
                    return
                raise

            payload = build_cli_payload(
                messages=messages,
                model=model,
                stream=True,
                temperature=temperature,
                top_p=top_p,
                tools=chat_tools,
                tool_choice=tool_choice,
                reasoning_effort=effort,
            )

            try:
                t_up = time.perf_counter()
                if stream_out:
                    # Delay response.created until upstream accepts the request.
                    # Otherwise a 422 after created leaves Codex waiting forever
                    # for response.completed ("stream closed before response.completed").
                    reasoning_started = False
                    reasoning_closed = False
                    message_started = False
                    text_buf: list[str] = []
                    think_buf: list[str] = []
                    opened = False

                    def _open_stream():
                        nonlocal opened, message_started
                        events = [
                            format_sse("response.created", {
                                "type": "response.created",
                                "response": make_resp_object(
                                    response_id, model, "in_progress", []
                                ),
                            }),
                            format_sse("response.in_progress", {
                                "type": "response.in_progress",
                                "response": make_resp_object(
                                    response_id, model, "in_progress", []
                                ),
                            }),
                        ]
                        # With tools, delay message events until we know there is no tool call.
                        if not client_function_tools:
                            events.append(_message_added(message_id))
                            events.append(_content_part_added(message_id))
                            message_started = True
                        events.append(": heartbeat\n\n")
                        opened = True
                        return events

                    async for kind, data in stream_cli_chat(
                        access, payload, timeout_s=timeout_s
                    ):
                        if kind == "done":
                            break
                        if kind != "data":
                            continue
                        try:
                            obj = orjson.loads(data)
                        except (orjson.JSONDecodeError, ValueError):
                            continue
                        if not isinstance(obj, dict):
                            continue

                        if not opened:
                            for frame in _open_stream():
                                yield frame

                        for ev in adapter.feed_data_obj(obj):
                            if (
                                ev["kind"] == "reasoning"
                                and emit_think is not False
                            ):
                                if not reasoning_started:
                                    reasoning_started = True
                                    yield format_sse("response.output_item.added", {
                                        "type": "response.output_item.added",
                                        "output_index": 0,
                                        "item": {
                                            "id": reasoning_id,
                                            "type": "reasoning",
                                            "summary": [],
                                            "status": "in_progress",
                                        },
                                    })
                                    yield format_sse(
                                        "response.reasoning_summary_part.added",
                                        {
                                            "type": "response.reasoning_summary_part.added",
                                            "item_id": reasoning_id,
                                            "output_index": 0,
                                            "summary_index": 0,
                                            "part": {
                                                "type": "summary_text",
                                                "text": "",
                                            },
                                        },
                                    )
                                think_buf.append(ev["text"])
                                yield format_sse(
                                    "response.reasoning_summary_text.delta",
                                    {
                                        "type": "response.reasoning_summary_text.delta",
                                        "item_id": reasoning_id,
                                        "output_index": 0,
                                        "summary_index": 0,
                                        "delta": ev["text"],
                                    },
                                )
                            elif ev["kind"] == "text":
                                if reasoning_started and not reasoning_closed:
                                    reasoning_closed = True
                                    full_think = "".join(think_buf)
                                    yield format_sse(
                                        "response.reasoning_summary_text.done",
                                        {
                                            "type": "response.reasoning_summary_text.done",
                                            "item_id": reasoning_id,
                                            "output_index": 0,
                                            "summary_index": 0,
                                            "text": full_think,
                                        },
                                    )
                                    yield format_sse(
                                        "response.reasoning_summary_part.done",
                                        {
                                            "type": "response.reasoning_summary_part.done",
                                            "item_id": reasoning_id,
                                            "output_index": 0,
                                            "summary_index": 0,
                                            "part": {
                                                "type": "summary_text",
                                                "text": full_think,
                                            },
                                        },
                                    )
                                    yield format_sse("response.output_item.done", {
                                        "type": "response.output_item.done",
                                        "output_index": 0,
                                        "item": {
                                            "id": reasoning_id,
                                            "type": "reasoning",
                                            "summary": [{
                                                "type": "summary_text",
                                                "text": full_think,
                                            }],
                                            "status": "completed",
                                        },
                                    })

                                # Buffer text when tools may still produce function_call.
                                if client_function_tools:
                                    text_buf.append(ev["text"])
                                    continue

                                msg_idx = 1 if reasoning_started else 0
                                if not message_started:
                                    message_started = True
                                    # Re-emit with correct index if reasoning preceded.
                                    yield format_sse("response.output_item.added", {
                                        "type": "response.output_item.added",
                                        "output_index": msg_idx,
                                        "item": {
                                            "id": message_id,
                                            "type": "message",
                                            "role": "assistant",
                                            "content": [],
                                            "status": "in_progress",
                                        },
                                    })
                                    yield format_sse(
                                        "response.content_part.added",
                                        {
                                            "type": "response.content_part.added",
                                            "item_id": message_id,
                                            "output_index": msg_idx,
                                            "content_index": 0,
                                            "part": {
                                                "type": "output_text",
                                                "text": "",
                                                "annotations": [],
                                            },
                                        },
                                    )
                                text_buf.append(ev["text"])
                                yield format_sse("response.output_text.delta", {
                                    "type": "response.output_text.delta",
                                    "item_id": message_id,
                                    "output_index": msg_idx,
                                    "content_index": 0,
                                    "delta": ev["text"],
                                })
                            elif ev["kind"] == "tool_calls":
                                # Accumulated in adapter; finalize after stream.
                                yield ": heartbeat\n\n"

                    # Upstream accepted but sent no data frames — still open SSE.
                    if not opened:
                        for frame in _open_stream():
                            yield frame

                    adapter.mark_done()
                    usage_data, usage = _usage_from_adapter(adapter, messages)
                    upstream_ms = int((time.perf_counter() - t_up) * 1000)
                    fc_items = _fc_items_from_adapter(adapter)

                    if fc_items:
                        for event in _emit_fc_events(fc_items):
                            yield event
                        input_tokens = usage.get("prompt_tokens") or estimate_prompt_tokens(
                            messages
                        )
                        output_tokens = usage.get(
                            "completion_tokens"
                        ) or estimate_tool_call_tokens([
                            type("T", (), {
                                "name": it["name"],
                                "arguments": it["arguments"],
                            })()
                            for it in fc_items
                        ])
                        yield format_sse("response.completed", {
                            "type": "response.completed",
                            "response": make_resp_object(
                                response_id,
                                model,
                                "completed",
                                fc_items,
                                usage=build_resp_usage(input_tokens, output_tokens),
                            ),
                        })
                        yield "data: [DONE]\n\n"
                        success = True
                        logger.info(
                            "cli responses stream function_call: model={} calls={} "
                            "attempt={}/{} oidc_ms={} upstream_ms={}",
                            model,
                            len(fc_items),
                            attempt + 1,
                            max_retries + 1,
                            oidc_ms,
                            upstream_ms,
                        )
                        return

                    # Close open reasoning if stream ended without text.
                    if reasoning_started and not reasoning_closed:
                        reasoning_closed = True
                        full_think = "".join(think_buf)
                        yield format_sse("response.reasoning_summary_text.done", {
                            "type": "response.reasoning_summary_text.done",
                            "item_id": reasoning_id,
                            "output_index": 0,
                            "summary_index": 0,
                            "text": full_think,
                        })
                        yield format_sse("response.reasoning_summary_part.done", {
                            "type": "response.reasoning_summary_part.done",
                            "item_id": reasoning_id,
                            "output_index": 0,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": full_think},
                        })
                        yield format_sse("response.output_item.done", {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {
                                "id": reasoning_id,
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": full_think}],
                                "status": "completed",
                            },
                        })

                    full_text = "".join(text_buf) if text_buf else adapter.full_text
                    msg_idx = 1 if reasoning_started else 0
                    if not message_started:
                        yield format_sse("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": msg_idx,
                            "item": {
                                "id": message_id,
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "status": "in_progress",
                            },
                        })
                        yield format_sse("response.content_part.added", {
                            "type": "response.content_part.added",
                            "item_id": message_id,
                            "output_index": msg_idx,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        })
                        message_started = True
                        # Flush buffered tool-path text as deltas.
                        if client_function_tools and full_text:
                            yield format_sse("response.output_text.delta", {
                                "type": "response.output_text.delta",
                                "item_id": message_id,
                                "output_index": msg_idx,
                                "content_index": 0,
                                "delta": full_text,
                            })

                    yield format_sse("response.output_text.done", {
                        "type": "response.output_text.done",
                        "item_id": message_id,
                        "output_index": msg_idx,
                        "content_index": 0,
                        "text": full_text,
                    })
                    yield format_sse("response.content_part.done", {
                        "type": "response.content_part.done",
                        "item_id": message_id,
                        "output_index": msg_idx,
                        "content_index": 0,
                        "part": {
                            "type": "output_text",
                            "text": full_text,
                            "annotations": [],
                        },
                    })
                    msg_item = {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{
                            "type": "output_text",
                            "text": full_text,
                            "annotations": [],
                        }],
                    }
                    yield format_sse("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": msg_idx,
                        "item": msg_item,
                    })

                    output_items: list[dict] = []
                    if reasoning_started:
                        output_items.append({
                            "id": reasoning_id,
                            "type": "reasoning",
                            "summary": [{
                                "type": "summary_text",
                                "text": "".join(think_buf),
                            }],
                            "status": "completed",
                        })
                    output_items.append(msg_item)

                    input_tokens = usage.get("prompt_tokens") or estimate_prompt_tokens(
                        messages
                    )
                    output_tokens = usage.get("completion_tokens") or estimate_tokens(
                        full_text
                    )
                    yield format_sse("response.completed", {
                        "type": "response.completed",
                        "response": make_resp_object(
                            response_id,
                            model,
                            "completed",
                            output_items,
                            usage=build_resp_usage(input_tokens, output_tokens),
                        ),
                    })
                    yield "data: [DONE]\n\n"
                    success = True
                    total_ms = int((time.perf_counter() - t_req) * 1000)
                    logger.info(
                        "cli responses stream completed: attempt={}/{} model={} "
                        "text_len={} oidc_ms={} upstream_ms={} total_ms={}",
                        attempt + 1,
                        max_retries + 1,
                        model,
                        len(full_text),
                        oidc_ms,
                        upstream_ms,
                        total_ms,
                    )
                    return

                # non-stream
                async for kind, data in stream_cli_chat(
                    access, payload, timeout_s=timeout_s
                ):
                    if kind == "done":
                        break
                    if kind != "data":
                        continue
                    try:
                        obj = orjson.loads(data)
                    except (orjson.JSONDecodeError, ValueError):
                        continue
                    if isinstance(obj, dict):
                        adapter.feed_data_obj(obj)

                adapter.mark_done()
                usage_data, usage = _usage_from_adapter(adapter, messages)
                upstream_ms = int((time.perf_counter() - t_up) * 1000)
                fc_items = _fc_items_from_adapter(adapter)

                if fc_items:
                    input_tokens = usage.get("prompt_tokens") or estimate_prompt_tokens(
                        messages
                    )
                    output_tokens = usage.get("completion_tokens") or 0
                    result = make_resp_object(
                        response_id,
                        model,
                        "completed",
                        fc_items,
                        usage=build_resp_usage(input_tokens, output_tokens),
                    )
                else:
                    full_text = adapter.full_text
                    output_items = []
                    if adapter.full_reasoning and emit_think is not False:
                        output_items.append({
                            "id": reasoning_id,
                            "type": "reasoning",
                            "summary": [{
                                "type": "summary_text",
                                "text": adapter.full_reasoning,
                            }],
                            "status": "completed",
                        })
                    output_items.append({
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{
                            "type": "output_text",
                            "text": full_text,
                            "annotations": [],
                        }],
                    })
                    input_tokens = usage.get("prompt_tokens") or estimate_prompt_tokens(
                        messages
                    )
                    output_tokens = usage.get("completion_tokens") or estimate_tokens(
                        full_text
                    )
                    result = make_resp_object(
                        response_id,
                        model,
                        "completed",
                        output_items,
                        usage=build_resp_usage(input_tokens, output_tokens),
                    )

                success = True
                total_ms = int((time.perf_counter() - t_req) * 1000)
                logger.info(
                    "cli responses non-stream completed: model={} oidc_ms={} "
                    "upstream_ms={} total_ms={}",
                    model,
                    oidc_ms,
                    upstream_ms,
                    total_ms,
                )
                yield result
                return

            except UpstreamError as exc:
                fail_exc = exc
                if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                    logger.warning(
                        "cli responses retry: attempt={}/{} status={} token={}...",
                        attempt + 1,
                        max_retries,
                        exc.status,
                        token[:8],
                    )
                    yield ("retry", token, exc)
                    return
                logger.warning(
                    "cli responses upstream failed: model={} status={} attempt={}/{}",
                    model,
                    exc.status,
                    attempt + 1,
                    max_retries + 1,
                )
                raise

        finally:
            await _release_and_feedback(
                directory,
                acct=acct,
                token=token,
                selected_mode_id=selected_mode_id,
                success=success,
                fail_exc=fail_exc,
            )

    if stream:
        async def _run_stream() -> AsyncGenerator[str, None]:
            excluded: list[str] = []
            for attempt in range(max_retries + 1):
                async for item in _one_attempt(attempt, excluded, stream_out=True):
                    if (
                        isinstance(item, tuple)
                        and len(item) == 3
                        and item[0] == "retry"
                    ):
                        excluded.append(str(item[1]))
                        break
                    yield item  # type: ignore[misc]
                else:
                    return
            raise RateLimitError("No available accounts after retries")

        return _run_stream()

    excluded: list[str] = []
    for attempt in range(max_retries + 1):
        async for item in _one_attempt(attempt, excluded, stream_out=False):
            if isinstance(item, tuple) and len(item) == 3 and item[0] == "retry":
                excluded.append(str(item[1]))
                break
            if isinstance(item, dict):
                return item
        else:
            continue
    raise RateLimitError("No available accounts after retries")


__all__ = ["create"]
