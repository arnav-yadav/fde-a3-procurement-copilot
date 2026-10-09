"""Scripted stand-in for src.llm_client.chat / chat_forced (no network). Records what each call was offered."""
from __future__ import annotations

import contextlib
import copy
import itertools
import json
from unittest import mock

from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion_message_function_tool_call import ChatCompletionMessageFunctionToolCall, Function

from src import llm_client

_ids = itertools.count(1)


def tool_message(*calls: tuple[str, dict]) -> ChatCompletionMessage:
    return ChatCompletionMessage(role="assistant", content=None, tool_calls=[
        ChatCompletionMessageFunctionToolCall(id=f"call_{next(_ids)}", type="function",
                                              function=Function(name=name, arguments=json.dumps(args)))
        for name, args in calls])


class FakeLLM:
    """script: list of turns; each turn is a list of (tool_name, args) the model 'calls'."""

    def __init__(self, script: list[list[tuple[str, dict]]]):
        self.script = list(script)
        self.calls: list[dict] = []

    def _respond(self, kind, messages, tools, tool_choice, ctx):
        self.calls.append({"kind": kind, "tools": [t["function"]["name"] for t in (tools or [])],
                           "tool_choice": tool_choice, "messages": copy.deepcopy(messages)})
        if not self.script:
            raise AssertionError("FakeLLM: no scripted response left")
        if ctx is not None:
            ctx.counters["llm_calls"] += 1
            ctx.llm_log.append({"ms": 1.0, "wait_ms": 0.0, "attempts": 1, "prompt_tokens": 100,
                                "completion_tokens": 10, "total_tokens": 110})
        return llm_client.ChatResult(tool_message(*self.script.pop(0)), None, 1.0, 0.0, 1)

    def chat(self, messages, tools=None, tool_choice="auto", ctx=None):
        return self._respond("chat", messages, tools, tool_choice, ctx)

    def chat_forced(self, messages, tool, ctx=None):
        return self._respond("forced", messages, [tool], "forced", ctx)

    @contextlib.contextmanager
    def patched(self):
        with mock.patch.object(llm_client, "chat", side_effect=self.chat), \
                mock.patch.object(llm_client, "chat_forced", side_effect=self.chat_forced):
            yield self


PACK = {"need_summary": "Task tracking for campaign launches.", "implied_data_classes": [], "overlap_assessment": [],
        "vendor_observations": ["Registry and vendor-risk API agree: approved."], "uncertainties": [],
        "injection_observed": False, "evidence": [], "gaps": [], "unsupported_claims": []}


def recommendation(**overrides) -> dict:
    base = {"decision_type": "route_for_approval", "recommendation": "Route to the approvers for this tier.",
            "next_step": "Send the evidence pack to the approvers.", "required_approvals": [], "risk_flags": [],
            "missing_information": [], "overlap_assessment": [], "evidence": [], "injection_observed": False,
            "implied_data_classes": []}
    base.update(overrides)
    return base
