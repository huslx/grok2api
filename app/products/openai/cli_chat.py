"""CLI chat completion service — routes to cli-chat-proxy.grok.com.

Uses SSO accounts, converts to OIDC access_token (device flow), then calls
OpenAI-compatible chat completions on the Grok CLI proxy. Reference:
https://github.com/HM2899/grokcli-2api
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

import orjson

from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.account.runtime import get_refresh_service
from app.control.model.registry import resolve as resolve_model
from app.dataplane.account.selector import current_strategy
from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall
from app.dataplane.reverse.protocol.xai_cli_chat import (
    CliStreamAdapter,
    build_cli_payload,
    stream_cli_chat,
)
from app.dataplane.reverse.protocol.xai_oidc import resolve_oidc_access_token
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_s
from app.platform.tokens import estimate_prompt_tokens, estimate_tokens
from app.products._account_selection import reserve_account, selection_max_retries
from app.products.openai.chat import _configured_retry_codes, _should_retry_upstream
from ._format import (
    build_usage,
    make_chat_response,
    make_response_id,
    make_stream_chunk,
    make_thinking_chunk,
    make_tool_call_chunk,
    make_tool_call_done_chunk,
    make_tool_call_response,
)


def _log_task_exception(task: "asyncio.Task") -> None:
    exc = task.exception() if not task.cancelled() else None
    if exc:
        logger.warning("background task failed: task={} error={}", task.get_name(), exc)


async def _quota_sync(token: str, mode_id: int) -> None:
    try:
        if current_strategy() != "quota" and mode_id != 5:
            return
        svc = get_refresh_service()
        if svc:
            await svc.refresh_call_async(token, mode_id)
    except Exception as exc:
        logger.warning(
            "cli quota sync failed: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            exc,
        )


async def _fail_sync(token: str, mode_id: int, exc: BaseException | None = None) -> None:
    try:
        svc = get_refresh_service()
        if svc:
            await svc.record_failure_async(token, mode_id, exc)
    except Exception as e:
        logger.warning(
            "cli fail sync error: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            e,
        )


def _reasoning_effort(emit_think: bool | None) -> str | None:
    if emit_think is False:
        return None
    return "high"  # grok-4.5 defaults to high effort


def _resolve_access_token(sso_token: str) -> str:
    # Device-flow conversion is blocking (HTTP). Offload to a worker thread.
    return resolve_oidc_access_token(sso_token)


async def completions(
    *,
    model: str,
    messages: list[dict],
    stream: bool = True,
    emit_think: bool | None = None,
    temperature: float = 0.7,
    top_p: float = 0.95,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
) -> dict | AsyncGenerator[str, None]:
    """Entry point for cli-chat-proxy chat completions."""
    cfg = get_config()
    spec = resolve_model(model)
    effort = _reasoning_effort(emit_think)
    # CLI models can think for a long time — default higher than web chat.
    timeout_s = cfg.get_float("chat.timeout", 120.0)
    if timeout_s < 300:
        timeout_s = 600.0
    max_retries = selection_max_retries()
    retry_codes = _configured_retry_codes(cfg)
    response_id = make_response_id()

    logger.info(
        "cli chat request: model={} stream={} messages={} tools={}",
        model,
        stream,
        len(messages),
        len(tools or []),
    )

    from app.dataplane.account import _directory as _acct_dir
    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    # ── Streaming ─────────────────────────────────────────────────────────────
    if stream:
        async def _run_stream() -> AsyncGenerator[str, None]:
            excluded: list[str] = []
            for attempt in range(max_retries + 1):
                acct, selected_mode_id = await reserve_account(
                    directory,
                    spec,
                    now_s_override=now_s(),
                    exclude_tokens=excluded or None,
                )
                if acct is None:
                    raise RateLimitError("No available accounts for this model tier")

                token = acct.token
                success = False
                fail_exc: BaseException | None = None
                _retry = False
                adapter = CliStreamAdapter()

                try:
                    try:
                        access = await asyncio.to_thread(_resolve_access_token, token)
                    except UpstreamError as exc:
                        fail_exc = exc
                        if attempt < max_retries:
                            _retry = True
                            logger.warning(
                                "cli oidc convert retry: attempt={}/{} status={}",
                                attempt + 1, max_retries, exc.status,
                            )
                        else:
                            raise
                        continue

                    payload = build_cli_payload(
                        messages=messages,
                        model=model,
                        stream=True,
                        temperature=temperature,
                        top_p=top_p,
                        tools=tools,
                        tool_choice=tool_choice,
                        reasoning_effort=effort,
                    )

                    try:
                        yield ": heartbeat\n\n"
                        tool_first_seen: set[int] = set()
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
                            for ev in adapter.feed_data_obj(obj):
                                if ev["kind"] == "text":
                                    chunk = make_stream_chunk(
                                        response_id, model, ev["text"]
                                    )
                                    yield f"data: {orjson.dumps(chunk).decode()}\n\n"
                                elif ev["kind"] == "reasoning" and emit_think is not False:
                                    think = make_thinking_chunk(
                                        response_id, model, ev["text"]
                                    )
                                    yield f"data: {orjson.dumps(think).decode()}\n\n"
                                elif ev["kind"] == "tool_calls":
                                    tc = ev.get("tool_call") or {}
                                    idx = int(ev.get("index") or 0)
                                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                                    is_first = idx not in tool_first_seen
                                    if is_first:
                                        tool_first_seen.add(idx)
                                    chunk = make_tool_call_chunk(
                                        response_id,
                                        model,
                                        idx,
                                        str(tc.get("id") or f"call_{idx}"),
                                        str(fn.get("name") or ""),
                                        str(fn.get("arguments") or ""),
                                        is_first=is_first,
                                    )
                                    yield f"data: {orjson.dumps(chunk).decode()}\n\n"

                        adapter.mark_done()
                        tools_out = adapter.tool_calls
                        usage_data = adapter.usage or {}
                        prompt_tokens = int(
                            usage_data.get("prompt_tokens")
                            or usage_data.get("input_tokens")
                            or estimate_prompt_tokens(messages)
                        )
                        completion_tokens = int(
                            usage_data.get("completion_tokens")
                            or usage_data.get("output_tokens")
                            or estimate_tokens(adapter.full_text + adapter.full_reasoning)
                        )
                        usage = build_usage(prompt_tokens, completion_tokens)

                        if tools_out:
                            done = make_tool_call_done_chunk(
                                response_id, model, usage=usage
                            )
                            yield f"data: {orjson.dumps(done).decode()}\n\n"
                        else:
                            final = make_stream_chunk(
                                response_id, model, "", is_final=True
                            )
                            final["usage"] = usage
                            yield f"data: {orjson.dumps(final).decode()}\n\n"
                        yield "data: [DONE]\n\n"
                        success = True
                        logger.info(
                            "cli chat stream completed: attempt={}/{} model={} tokens={}",
                            attempt + 1,
                            max_retries + 1,
                            model,
                            usage_data.get("total_tokens", prompt_tokens + completion_tokens),
                        )

                    except UpstreamError as exc:
                        fail_exc = exc
                        if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                            _retry = True
                            logger.warning(
                                "cli chat retry: attempt={}/{} status={} token={}...",
                                attempt + 1, max_retries, exc.status, token[:8],
                            )
                        else:
                            logger.warning(
                                "cli chat upstream failed: model={} status={} attempt={}/{} body={}",
                                model,
                                exc.status,
                                attempt + 1,
                                max_retries + 1,
                                (exc.details or {}).get("body", "")[:200],
                            )
                            raise

                finally:
                    await directory.release(acct)
                    kind = (
                        FeedbackKind.SUCCESS if success
                        else feedback_kind_for_error(fail_exc) if fail_exc
                        else FeedbackKind.SERVER_ERROR
                    )
                    await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
                    if success:
                        asyncio.create_task(
                            _quota_sync(token, selected_mode_id)
                        ).add_done_callback(_log_task_exception)
                    else:
                        asyncio.create_task(
                            _fail_sync(token, selected_mode_id, fail_exc)
                        ).add_done_callback(_log_task_exception)

                if success or not _retry:
                    return
                excluded.append(token)

        return _run_stream()

    # ── Non-streaming ─────────────────────────────────────────────────────────
    excluded: list[str] = []
    for attempt in range(max_retries + 1):
        acct, selected_mode_id = await reserve_account(
            directory,
            spec,
            now_s_override=now_s(),
            exclude_tokens=excluded or None,
        )
        if acct is None:
            raise RateLimitError("No available accounts for this model tier")

        token = acct.token
        success = False
        fail_exc: BaseException | None = None
        adapter = CliStreamAdapter()

        try:
            try:
                access = await asyncio.to_thread(_resolve_access_token, token)
            except UpstreamError as exc:
                fail_exc = exc
                if attempt < max_retries:
                    excluded.append(token)
                    continue
                raise

            payload = build_cli_payload(
                messages=messages,
                model=model,
                stream=True,  # always stream upstream; aggregate locally
                temperature=temperature,
                top_p=top_p,
                tools=tools,
                tool_choice=tool_choice,
                reasoning_effort=effort,
            )

            try:
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
                usage_data = adapter.usage or {}
                prompt_tokens = int(
                    usage_data.get("prompt_tokens")
                    or usage_data.get("input_tokens")
                    or estimate_prompt_tokens(messages)
                )
                completion_tokens = int(
                    usage_data.get("completion_tokens")
                    or usage_data.get("output_tokens")
                    or estimate_tokens(adapter.full_text + adapter.full_reasoning)
                )
                usage = build_usage(prompt_tokens, completion_tokens)

                if adapter.tool_calls:
                    parsed = [
                        ParsedToolCall(
                            call_id=str(tc.get("id") or f"call_{i}"),
                            name=str((tc.get("function") or {}).get("name") or ""),
                            arguments=str((tc.get("function") or {}).get("arguments") or "{}"),
                        )
                        for i, tc in enumerate(adapter.tool_calls)
                        if isinstance(tc, dict)
                    ]
                    result = make_tool_call_response(
                        model,
                        parsed,
                        response_id=response_id,
                        usage=usage,
                        prompt_content=messages,
                    )
                else:
                    content = adapter.full_text
                    if adapter.full_reasoning and emit_think is not False:
                        content = (
                            f"<think>{adapter.full_reasoning}</think>\n{content}"
                            if content
                            else f"<think>{adapter.full_reasoning}</think>"
                        )
                    result = make_chat_response(
                        model, content, response_id=response_id, usage=usage
                    )
                success = True
                logger.info(
                    "cli chat non-stream completed: model={} tokens={}",
                    model,
                    usage_data.get("total_tokens", prompt_tokens + completion_tokens),
                )
                return result

            except UpstreamError as exc:
                fail_exc = exc
                if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                    logger.warning(
                        "cli chat non-stream retry: attempt={}/{} status={}",
                        attempt + 1, max_retries, exc.status,
                    )
                    excluded.append(token)
                    continue
                raise

        finally:
            await directory.release(acct)
            kind = (
                FeedbackKind.SUCCESS if success
                else feedback_kind_for_error(fail_exc) if fail_exc
                else FeedbackKind.SERVER_ERROR
            )
            await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
            if success:
                asyncio.create_task(
                    _quota_sync(token, selected_mode_id)
                ).add_done_callback(_log_task_exception)
            else:
                asyncio.create_task(
                    _fail_sync(token, selected_mode_id, fail_exc)
                ).add_done_callback(_log_task_exception)

    raise RateLimitError("No available accounts after retries")


__all__ = ["completions"]
