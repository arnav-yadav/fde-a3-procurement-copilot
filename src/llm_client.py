"""OpenAI-compatible LLM client for Gemini or Groq (T7): throttle, retries, wait accounting."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

from src.config import get_settings
from src.tracing import maybe_wrap, tracing_enabled

MAX_RETRY_AFTER_SECONDS = 60.0


class LLMUnavailable(Exception):
    """The LLM cannot be used for this run (outage, auth, missing model, retries exhausted)."""


class LLMBadRequest(Exception):
    """The provider rejected the request shape (HTTP 400/422), e.g. an unsupported tool_choice."""


@dataclass
class ChatResult:
    message: object  # openai ChatCompletionMessage
    usage: dict | None
    latency_ms: float
    wait_ms: float
    attempts: int


_throttle_lock = threading.Lock()
_last_call_start = 0.0


def _throttle(min_interval: float) -> float:
    """Block until min_interval has passed since the previous call start. Returns ms waited."""
    global _last_call_start
    with _throttle_lock:
        now = time.monotonic()
        wait = max(0.0, _last_call_start + min_interval - now)
        if wait:
            time.sleep(wait)
        _last_call_start = time.monotonic()
    return wait * 1000


_client_cache: dict = {}


def _client(settings):
    from openai import OpenAI
    key = (settings.llm_base_url, settings.llm_api_key, settings.llm_timeout_seconds, tracing_enabled())
    if key not in _client_cache:
        _client_cache[key] = maybe_wrap(OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                                               timeout=settings.llm_timeout_seconds, max_retries=0))
    return _client_cache[key]


def _retry_after(exc) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else None
    except Exception:
        return None


def chat(messages: list[dict], tools: list[dict] | None = None, tool_choice="auto", ctx=None) -> ChatResult:
    import openai

    s = get_settings()
    if s.llm_simulate_outage:
        raise LLMUnavailable("simulated outage (LLM_SIMULATE_OUTAGE=1)")
    if not s.llm_api_key:
        raise LLMUnavailable(f"no API key configured for provider '{s.llm_provider}'")

    client = _client(s)
    kwargs = {"model": s.model_name, "messages": messages, "temperature": s.llm_temperature}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    wait_ms = 0.0
    attempts = 0
    last_error = ""
    while attempts <= s.llm_max_retries:
        attempts += 1
        wait_ms += _throttle(s.llm_min_interval_seconds)
        started = time.perf_counter()
        try:
            resp = client.chat.completions.create(**kwargs)
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
        except openai.RateLimitError as exc:
            last_error = f"RateLimitError: {str(exc)[:200]}"
            backoff = _retry_after(exc)
            if backoff is not None and backoff > MAX_RETRY_AFTER_SECONDS:
                raise LLMUnavailable(f"rate limited for {backoff:.0f}s: {last_error}") from exc
            backoff = backoff if backoff is not None else 2 ** attempts + random.random()
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            backoff = 2 ** attempts + random.random()
        except openai.APIStatusError as exc:
            if exc.status_code in (400, 422):
                raise LLMBadRequest(f"HTTP {exc.status_code}: {str(exc)[:500]}") from exc
            if exc.status_code >= 500:
                last_error = f"HTTP {exc.status_code}: {str(exc)[:200]}"
                backoff = 2 ** attempts + random.random()
            else:
                raise LLMUnavailable(f"HTTP {exc.status_code}: {str(exc)[:300]}") from exc
        else:
            latency = (time.perf_counter() - started) * 1000
            usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
            if ctx is not None:
                ctx.counters["llm_calls"] += 1
                ctx.counters["llm_ms"] += latency
                ctx.counters["llm_wait_ms"] += wait_ms
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    ctx.counters[k] += int((usage or {}).get(k) or 0)
                if isinstance(getattr(ctx, "llm_log", None), list):
                    ctx.llm_log.append({"ms": round(latency, 1), "wait_ms": round(wait_ms, 1), "attempts": attempts,
                                        **{k: int((usage or {}).get(k) or 0)
                                           for k in ("prompt_tokens", "completion_tokens", "total_tokens")}})
            if not resp.choices:
                raise LLMUnavailable("provider returned no choices")
            return ChatResult(resp.choices[0].message, usage, latency, wait_ms, attempts)

        if attempts > s.llm_max_retries:
            break
        time.sleep(backoff)
        wait_ms += backoff * 1000

    if ctx is not None:
        ctx.counters["llm_wait_ms"] += wait_ms
    raise LLMUnavailable(f"retries exhausted after {attempts} attempts: {last_error}")


def chat_forced(messages: list[dict], tool: dict, ctx=None) -> ChatResult:
    """Force a call to `tool`: named tool_choice first; if rejected, offer only that tool with 'required'."""
    name = tool["function"]["name"]
    try:
        return chat(messages, tools=[tool], tool_choice={"type": "function", "function": {"name": name}}, ctx=ctx)
    except LLMBadRequest:
        if ctx is not None:
            ctx.event("forced_tool_choice_fallback", tool=name)
        return chat(messages, tools=[tool], tool_choice="required", ctx=ctx)


def message_to_dict(message) -> dict:
    """Assistant message for the next request. Keeps provider extras (e.g. Gemini thought signatures)."""
    d = message.model_dump(exclude_none=True)
    d["role"] = "assistant"
    if not d.get("tool_calls"):
        d.pop("tool_calls", None)
    if d.get("content") is None:
        d["content"] = ""
    return d
