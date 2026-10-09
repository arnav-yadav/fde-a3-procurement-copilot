"""Shared agent plumbing: submit-tool schemas and the tool-calling loop."""
from __future__ import annotations

import copy
import json

from pydantic import BaseModel, ValidationError

from src import llm_client as llm
from src.schemas import AGENT_ALLOWED_FLAGS, ROLE_ORDER
from src.tools import RunContext, llm_content

_OVERLAP_ITEM = {
    "type": "object",
    "properties": {
        "software_id": {"type": "string"},
        "relationship": {"type": "string",
                         "enum": ["same_product_expansion", "substitute_could_meet_need", "related_not_substitute"]},
        "covers_stated_need": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["software_id", "relationship", "covers_stated_need", "reason"],
}
_IMPLIED_CLASS_ITEM = {
    "type": "object",
    "properties": {
        "data_class": {"type": "string", "enum": ["customer_pii", "employee_pii", "source_code", "production_access",
                                                  "confidential_documents", "credentials"]},
        "quote": {"type": "string", "description": "Verbatim words from the request (product name, justification "
                                                   "or integrations) that show this request will touch the data"},
    },
    "required": ["data_class", "quote"],
}
_EVIDENCE_ITEM = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "Tool name the fact came from, or copilot_analysis"},
        "finding": {"type": "string"},
        "reference": {"type": "string"},
    },
    "required": ["source", "finding"],
}

SUBMIT_RECOMMENDATION = {
    "type": "function",
    "function": {
        "name": "submit_recommendation",
        "description": "Submit the final structured recommendation for the human procurement reviewer.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision_type": {"type": "string", "enum": ["route_for_approval", "route_for_specialist_review",
                                                             "request_clarification", "use_existing_tool"]},
                "recommendation": {"type": "string", "description": "One plain sentence."},
                "next_step": {"type": "string", "description": "One or two sentences naming who acts next."},
                "required_approvals": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"role": {"type": "string", "enum": ROLE_ORDER}, "reason": {"type": "string"}},
                    "required": ["role", "reason"]}},
                "risk_flags": {"type": "array", "items": {"type": "string", "enum": AGENT_ALLOWED_FLAGS}},
                "missing_information": {"type": "array", "items": {"type": "string"}},
                "overlap_assessment": {"type": "array", "items": _OVERLAP_ITEM},
                "evidence": {"type": "array", "items": _EVIDENCE_ITEM},
                "injection_observed": {"type": "boolean"},
                "injection_excerpt": {"type": "string"},
                "implied_data_classes": {"type": "array", "items": _IMPLIED_CLASS_ITEM},
            },
            "required": ["decision_type", "recommendation", "next_step", "required_approvals", "risk_flags",
                         "missing_information", "overlap_assessment", "evidence", "injection_observed",
                         "implied_data_classes"],
        },
    },
}

SUBMIT_EVIDENCE_PACK = {
    "type": "function",
    "function": {
        "name": "submit_evidence_pack",
        "description": "Submit the evidence pack for the independent Policy & Risk Reviewer.",
        "parameters": {
            "type": "object",
            "properties": {
                "need_summary": {"type": "string"},
                "implied_data_classes": {"type": "array", "items": _IMPLIED_CLASS_ITEM},
                "overlap_assessment": {"type": "array", "items": _OVERLAP_ITEM},
                "vendor_observations": {"type": "array", "items": {"type": "string"}},
                "uncertainties": {"type": "array", "items": {"type": "string"}},
                "injection_observed": {"type": "boolean"},
                "injection_excerpt": {"type": "string"},
                "evidence": {"type": "array", "items": _EVIDENCE_ITEM},
                "gaps": {"type": "array", "items": {"type": "string"}},
                "unsupported_claims": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["need_summary", "implied_data_classes", "overlap_assessment", "vendor_observations",
                         "uncertainties", "injection_observed", "evidence", "gaps", "unsupported_claims"],
        },
    },
}


# Staged reviewer: submit_recommendation plus an explicit record of disagreements with the analyst (C4).
SUBMIT_REVIEW = copy.deepcopy(SUBMIT_RECOMMENDATION)
SUBMIT_REVIEW["function"]["parameters"]["properties"]["analyst_disagreements"] = {
    "type": "array",
    "description": "One entry per analyst overlap entry or implied data class you do NOT accept. Empty if you agree with all.",
    "items": {"type": "object", "properties": {
        "item": {"type": "string", "description": "software_id of the overlap entry, or the data class"},
        "analyst_said": {"type": "string"}, "reviewer_says": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["item", "analyst_said", "reviewer_says", "reason"]},
}
SUBMIT_REVIEW["function"]["parameters"]["required"].append("analyst_disagreements")


def parse_submission(raw_args: str | None, model_cls: type[BaseModel]) -> tuple[BaseModel | None, str | None, object]:
    """Returns (model, error_text, raw_parsed)."""
    try:
        data = json.loads(raw_args or "{}")
    except json.JSONDecodeError as exc:
        return None, f"arguments are not valid JSON: {exc}", raw_args
    if isinstance(data, dict):  # tolerate explicit nulls for optional strings
        for k in ("injection_excerpt",):
            if data.get(k) is None:
                data.pop(k, None)
        for ev in data.get("evidence") or []:
            if isinstance(ev, dict) and ev.get("reference") is None:
                ev.pop("reference", None)
    try:
        return model_cls.model_validate(data), None, data
    except ValidationError as exc:
        return None, exc.json(include_url=False)[:2000], data


def tool_loop(ctx: RunContext, messages: list[dict], tool_names: list[str], submit_tool: dict,
              model_cls: type[BaseModel], max_turns: int, nudge: str, stage: str) -> tuple[BaseModel | None, list]:
    """Run the LLM with tools until it submits a valid `submit_tool` call.

    The last turn offers only the submit tool (forced). One repair attempt is allowed after an
    invalid submission. Returns (validated submission or None, raw submissions for the trace)."""
    from src.tools import tool_schemas

    submit_name = submit_tool["function"]["name"]
    all_tools = tool_schemas(tool_names) + [submit_tool]
    raw_submissions: list = []
    repairs_left = 1
    force_next = False
    turn = 0
    while turn < max_turns + 1:  # +1 leaves room for a single repair turn
        turn += 1
        forced = force_next or turn >= max_turns
        if turn > max_turns and not force_next:
            break
        exposed = [submit_tool] if forced else all_tools
        result = llm.chat_forced(messages, submit_tool, ctx=ctx) if forced else llm.chat(messages, all_tools, ctx=ctx)
        msg = result.message
        messages.append(llm.message_to_dict(msg))
        calls = msg.tool_calls or []
        ctx.event("llm_turn", stage=stage, turn=turn, forced=forced,
                  tool_calls=[c.function.name for c in calls], text=(msg.content or "")[:300],
                  tools_exposed={"count": len(exposed), "schema_chars": len(json.dumps(exposed))})
        if not calls:
            messages.append({"role": "user", "content": nudge})
            continue
        submission = None
        force_next = False
        for call in calls:
            name = call.function.name
            if name == submit_name:
                model, err, raw = parse_submission(call.function.arguments, model_cls)
                raw_submissions.append(raw)
                if model is not None and submission is None:
                    submission = model
                    content = "Submission accepted."
                elif model is None and repairs_left > 0:
                    repairs_left -= 1
                    force_next = True
                    ctx.event("submission_invalid", stage=stage, error=err[:500])
                    content = f"Submission invalid. Fix these errors and call {submit_name} again:\n{err}"
                else:
                    ctx.event("submission_invalid", stage=stage, error=(err or "")[:500])
                    content = "Submission rejected."
            elif name in tool_names:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                    out = ctx.execute(name, args, initiator="agent")
                except (json.JSONDecodeError, ValueError) as exc:
                    out = {"status": "error", "error": f"invalid arguments: {exc}"}
                content = llm_content(out)
            else:
                content = json.dumps({"status": "error", "error": f"tool '{name}' is not available"})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        if submission is not None:
            return submission, raw_submissions
    return None, raw_submissions


def forced_submission(ctx: RunContext, messages: list[dict], submit_tool: dict, model_cls: type[BaseModel],
                      stage: str, attempts: int = 2) -> tuple[BaseModel | None, list]:
    """One forced call to `submit_tool` (no other tools), with one repair attempt. Raises LLMUnavailable."""
    name = submit_tool["function"]["name"]
    raw_submissions: list = []
    for attempt in range(1, attempts + 1):
        result = llm.chat_forced(messages, submit_tool, ctx=ctx)
        msg = result.message
        calls = [c for c in (msg.tool_calls or []) if c.function.name == name]
        ctx.event("llm_turn", stage=stage, turn=attempt, forced=True,
                  tool_calls=[c.function.name for c in (msg.tool_calls or [])], text=(msg.content or "")[:300],
                  tools_exposed={"count": 1, "schema_chars": len(json.dumps([submit_tool]))})
        if not calls:
            messages.append(llm.message_to_dict(msg))
            messages.append({"role": "user", "content": f"Call {name} now."})
            continue
        model, err, raw = parse_submission(calls[0].function.arguments, model_cls)
        raw_submissions.append(raw)
        if model is not None:
            return model, raw_submissions
        ctx.event("submission_invalid", stage=stage, error=(err or "")[:500])
        messages.append(llm.message_to_dict(msg))
        for c in msg.tool_calls or []:
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": f"Submission invalid. Fix these errors and call {name} again:\n{err}"})
    return None, raw_submissions
