"""Optional LangSmith tracing (off by default). Tracing only: no agent framework, no change to behaviour.

On only when LANGSMITH_TRACING=true AND LANGSMITH_API_KEY is set, read at call time. Otherwise every helper
is a pass-through and `langsmith` is never imported. The local trace (`trace.steps`, `tool_log`, `events`)
is always written regardless. Eval trials run with tracing off.
"""
from __future__ import annotations

import functools
import os
from typing import Callable

_DROP_INPUTS = {"ctx", "self"}  # the RunContext is large and already in the local trace


def tracing_enabled() -> bool:
    return (os.getenv("LANGSMITH_TRACING", "").strip().lower() == "true"
            and bool(os.getenv("LANGSMITH_API_KEY", "").strip()))


def maybe_wrap(client):
    """The OpenAI client wrapped by langsmith.wrappers.wrap_openai when tracing is on, else unchanged."""
    if not tracing_enabled():
        return client
    try:
        from langsmith.wrappers import wrap_openai
    except ImportError:
        return client
    return wrap_openai(client)


def _inputs(inputs: dict) -> dict:
    return {k: v for k, v in inputs.items() if k not in _DROP_INPUTS}


def traced(name: str | Callable[..., str], run_type: str = "chain",
           metadata: Callable[..., dict] | None = None, **meta) -> Callable:
    """Decorator: a LangSmith run around the call when tracing is on; a plain call otherwise.

    `name` may be a function of the call's arguments (e.g. the tool name); `metadata` likewise adds per-call
    metadata to the static `meta`."""
    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not tracing_enabled():
                return fn(*args, **kwargs)
            try:
                from langsmith import traceable
            except ImportError:
                return fn(*args, **kwargs)
            run_name = name(*args, **kwargs) if callable(name) else name
            run_meta = {**meta, **(metadata(*args, **kwargs) if metadata else {})}
            run_meta = {k: v for k, v in run_meta.items() if v not in (None, "")}
            return traceable(name=run_name, run_type=run_type, metadata=run_meta,
                             process_inputs=_inputs)(fn)(*args, **kwargs)
        return wrapper
    return decorate
