"""CLI chat completion service — routes to cli-chat-proxy.grok.com.

Uses SSO accounts, converts to OIDC access_token (device flow), then calls
OpenAI-compatible chat completions on the Grok CLI proxy. Reference:
https://github.com/HM2899/grokcli-2api
"""

from __future__ import annotations

import asyncio
import time
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
from app.dataplane.reverse.protocol.xai_oidc import (
    list_warm_sso_tokens,
    resolve_oidc_access_token,
)
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

# Map OpenAI-style effort aliases to values accepted by cli-chat-proxy.
_CLI_EFFORT_ALIASES = {
    "none": None,
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
}


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


def _reasoning_effort(
    emit_think: bool | None,
    reasoning_effort: str | None,
    *,
    default_effort: str,
) -> str | None:
    """Resolve CLI ``reasoning_effort`` for the upstream payload.

    Priority:
      1. explicit *reasoning_effort* from the client
      2. *emit_think* False → disabled
      3. config default (``chat.cli_reasoning_effort``, default ``medium``)
    """
    if reasoning_effort is not None:
        key = str(reasoning_effort).strip().lower()
        if key in _CLI_EFFORT_ALIASES:
            return _CLI_EFFORT_ALIASES[key]
    if emit_think is False:
        return None
    key = (default_effort or "medium").strip().lower()
    return _CLI_EFFORT_ALIASES.get(key, "medium")


def _resolve_access_token(sso_token: str, *, allow_convert: bool) -> str:
    # schedule_repair=True is the only enqueue path (no double-schedule in caller).
    return resolve_oidc_access_token(
        sso_token,
        allow_convert=allow_convert,
        schedule_repair=True,
    )


def _cli_max_retries(cfg) -> int:
    """CLI needs more account swaps: many pool tokens lack fresh OIDC."""
    base = selection_max_retries()
    cli = cfg.get_int("chat.cli_account_retries", 8)
    return max(base, max(0, cli))


def _is_oidc_deferred(exc: BaseException | None) -> bool:
    """True when failure is a deferred OIDC warm-up miss (not bad SSO)."""
    if not isinstance(exc, UpstreamError):
        return False
    body = str((exc.details or {}).get("body") or "")
    msg = str(exc.message or exc)
    return "oidc_unavailable" in body or "oidc_unavailable" in msg


def _feedback_for_cli_error(exc: BaseException | None) -> FeedbackKind:
    """Map CLI failures to account feedback without expiring on OIDC warm-up misses."""
    if _is_oidc_deferred(exc):
        return FeedbackKind.SERVER_ERROR
    return feedback_kind_for_error(exc)


def _prefer_warm_tokens() -> list[str] | None:
    """O(warm) prefer list from reverse SSO map — no full-pool scan."""
    try:
        warm = list_warm_sso_tokens()
        return warm or None
    except Exception:
        return None


def _should_hot_convert(
    *,
    last_resort: bool,
    attempt: int,
    max_retries: int,
) -> bool:
    """Block on device-flow only for the final account-selection attempt."""
    return last_resort and attempt >= max_retries


async def _release_and_feedback(
    directory,
    *,
    acct,
    token: str,
    selected_mode_id: int,
    success: bool,
    fail_exc: BaseException | None,
) -> None:
    await directory.release(acct)
    kind = (
        FeedbackKind.SUCCESS if success
        else _feedback_for_cli_error(fail_exc)
    )
    await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
    if success:
        asyncio.create_task(
            _quota_sync(token, selected_mode_id)
        ).add_done_callback(_log_task_exception)
    elif fail_exc is not None and not _is_oidc_deferred(fail_exc):
        asyncio.create_task(
            _fail_sync(token, selected_mode_id, fail_exc)
        ).add_done_callback(_log_task_exception)


def _usage_from_adapter(adapter: CliStreamAdapter, messages: list[dict]) -> tuple[dict, dict]:
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
    return usage_data, build_usage(prompt_tokens, completion_tokens)


async def completions(
    *,
    model: str,
    messages: list[dict],
    stream: bool = True,
    emit_think: bool | None = None,
    reasoning_effort: str | None = None,
    temperature: float = 0.7,
    top_p: float = 0.95,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
) -> dict | AsyncGenerator[str, None]:
    """Entry point for cli-chat-proxy chat completions."""
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
    response_id = make_response_id()

    logger.info(
        "cli chat request: model={} stream={} messages={} tools={} effort={}",
        model,
        stream,
        len(messages),
        len(tools or []),
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
        """Reserve → OIDC → upstream. Yields SSE strings or a single result dict.

        Raises UpstreamError on terminal failure; sets excluded via caller on retry.
        Yields control tokens:
          - ("retry", token, exc) when caller should swap account
          - str chunks for stream
          - dict for non-stream result
        """
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
                        "cli oidc miss swap: attempt={}/{} status={} "
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
                tools=tools,
                tool_choice=tool_choice,
                reasoning_effort=effort,
            )

            try:
                t_up = time.perf_counter()
                if stream_out:
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
                            elif (
                                ev["kind"] == "reasoning"
                                and emit_think is not False
                            ):
                                think = make_thinking_chunk(
                                    response_id, model, ev["text"]
                                )
                                yield f"data: {orjson.dumps(think).decode()}\n\n"
                            elif ev["kind"] == "tool_calls":
                                tc = ev.get("tool_call") or {}
                                idx = int(ev.get("index") or 0)
                                fn = (
                                    tc.get("function")
                                    if isinstance(tc.get("function"), dict)
                                    else {}
                                )
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
                    usage_data, usage = _usage_from_adapter(adapter, messages)
                    upstream_ms = int((time.perf_counter() - t_up) * 1000)
                    if adapter.tool_calls:
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
                    total_ms = int((time.perf_counter() - t_req) * 1000)
                    logger.info(
                        "cli chat stream completed: attempt={}/{} model={} "
                        "tokens={} oidc_ms={} upstream_ms={} total_ms={} "
                        "hot_convert={}",
                        attempt + 1,
                        max_retries + 1,
                        model,
                        usage_data.get(
                            "total_tokens",
                            usage["prompt_tokens"] + usage["completion_tokens"],
                        ),
                        oidc_ms,
                        upstream_ms,
                        total_ms,
                        allow_convert,
                    )
                    return

                # non-stream: aggregate upstream SSE
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

                if adapter.tool_calls:
                    parsed = [
                        ParsedToolCall(
                            call_id=str(tc.get("id") or f"call_{i}"),
                            name=str((tc.get("function") or {}).get("name") or ""),
                            arguments=str(
                                (tc.get("function") or {}).get("arguments") or "{}"
                            ),
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
                total_ms = int((time.perf_counter() - t_req) * 1000)
                logger.info(
                    "cli chat non-stream completed: model={} tokens={} "
                    "oidc_ms={} upstream_ms={} total_ms={} hot_convert={}",
                    model,
                    usage_data.get(
                        "total_tokens",
                        usage["prompt_tokens"] + usage["completion_tokens"],
                    ),
                    oidc_ms,
                    upstream_ms,
                    total_ms,
                    allow_convert,
                )
                yield result
                return

            except UpstreamError as exc:
                fail_exc = exc
                if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                    logger.warning(
                        "cli chat retry: attempt={}/{} status={} "
                        "token={}... oidc_ms={}",
                        attempt + 1,
                        max_retries,
                        exc.status,
                        token[:8],
                        oidc_ms,
                    )
                    yield ("retry", token, exc)
                    return
                logger.warning(
                    "cli chat upstream failed: model={} status={} "
                    "attempt={}/{} oidc_ms={} body={}",
                    model,
                    exc.status,
                    attempt + 1,
                    max_retries + 1,
                    oidc_ms,
                    (exc.details or {}).get("body", "")[:200],
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

    # ── Streaming ─────────────────────────────────────────────────────────────
    if stream:
        async def _run_stream() -> AsyncGenerator[str, None]:
            excluded: list[str] = []
            for attempt in range(max_retries + 1):
                async for item in _one_attempt(
                    attempt, excluded, stream_out=True
                ):
                    if (
                        isinstance(item, tuple)
                        and len(item) == 3
                        and item[0] == "retry"
                    ):
                        excluded.append(str(item[1]))
                        break
                    yield item  # type: ignore[misc]
                else:
                    # generator exhausted without retry → success or raised
                    return
            raise RateLimitError("No available accounts after retries")

        return _run_stream()

    # ── Non-streaming ─────────────────────────────────────────────────────────
    excluded: list[str] = []
    for attempt in range(max_retries + 1):
        async for item in _one_attempt(attempt, excluded, stream_out=False):
            if isinstance(item, tuple) and len(item) == 3 and item[0] == "retry":
                excluded.append(str(item[1]))
                break
            if isinstance(item, dict):
                return item
        else:
            # exhausted without break — if we returned a dict we'd have returned;
            # if raised, wouldn't get here. Fall through to next attempt only on break.
            continue
    raise RateLimitError("No available accounts after retries")


__all__ = ["completions"]
