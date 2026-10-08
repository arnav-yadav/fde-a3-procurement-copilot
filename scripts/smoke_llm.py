"""LLM smoke test: provider config, model listing, and one forced tool-call round trip.

Usage: python scripts/smoke_llm.py
Exits non-zero with a clear message on failure.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src  # noqa: F401  (loads .env)
from src.config import get_settings

DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_recommendation",
        "description": "Submit a test recommendation.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision_type": {"type": "string", "enum": ["route_for_approval", "request_clarification"]},
                "reason": {"type": "string"},
            },
            "required": ["decision_type", "reason"],
        },
    },
}


def die(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


def main() -> None:
    try:
        from openai import OpenAI
    except ImportError:
        die("openai SDK not installed. Run: python -m pip install -r requirements.txt")

    s = get_settings()
    print(f"provider : {s.llm_provider}")
    print(f"base_url : {s.llm_base_url}")
    print(f"model    : {s.model_name}")
    print(f"min gap  : {s.llm_min_interval_seconds}s between calls")
    if not s.llm_api_key:
        die(f"No API key for provider '{s.llm_provider}'. Set it in .env (see .env.example).")

    client = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=s.llm_timeout_seconds, max_retries=0)

    try:
        ids = sorted(m.id.removeprefix("models/") for m in client.models.list())
        print(f"models   : {len(ids)} available")
        if s.model_name not in ids:
            close = [i for i in ids if s.model_name.split("-")[0] in i][:15]
            print(f"[WARN] MODEL_NAME '{s.model_name}' is not in the model list. Similar: {close}")
    except Exception as exc:  # listing is diagnostic only
        print(f"[WARN] Could not list models: {type(exc).__name__}: {exc}")

    messages = [
        {"role": "system", "content": "You are a test harness. Always answer by calling the provided tool."},
        {"role": "user", "content": "A request is missing its annual cost. Submit your recommendation."},
    ]
    modes = [
        ("named tool_choice", [DUMMY_TOOL], {"type": "function", "function": {"name": "submit_recommendation"}}),
        ("tool_choice=required", [DUMMY_TOOL], "required"),
    ]
    for label, tools, choice in modes:
        started = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=s.model_name, messages=messages, tools=tools, tool_choice=choice,
                temperature=s.llm_temperature,
            )
        except Exception as exc:
            print(f"[WARN] forced tool via {label} failed: {type(exc).__name__}: {str(exc)[:300]}")
            continue
        latency = (time.perf_counter() - started) * 1000
        calls = resp.choices[0].message.tool_calls or []
        if not calls:
            print(f"[WARN] {label}: no tool call returned (content={resp.choices[0].message.content!r:.200})")
            continue
        args = json.loads(calls[0].function.arguments or "{}")
        print(f"[ OK ] forced tool via {label}")
        print(f"       tool    : {calls[0].function.name}")
        print(f"       args    : {args}")
        print(f"       latency : {latency:.0f} ms")
        print(f"       usage   : {resp.usage.model_dump() if resp.usage else None}")
        print("\nSMOKE TEST PASSED")
        return
    die("No forced tool-call mode worked. Check MODEL_NAME (try a different model) and the API key.")


if __name__ == "__main__":
    main()
