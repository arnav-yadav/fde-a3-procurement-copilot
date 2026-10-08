"""Deterministic prompt-injection scanner (R10). Detection never changes any other rule output."""
from __future__ import annotations

import re

INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+|any\s+|the\s+|previous\s+|prior\s+|above\s+)*(procurement\s+)?(rules|instructions|polic(y|ies))",
        r"disregard\s+(all\s+|any\s+|the\s+)*(rules|instructions|polic(y|ies))",
        r"\b(treat|mark|consider|report)\s+(this|it|the\s+request|this\s+vendor|the\s+vendor)\b[^.]{0,40}\b(approved|pre-?approved|low\s+risk|cfo[- ]approved)",
        r"\b(pre-?approved|cfo[- ]approved|already\s+approved)\b",
        r"\b(auto-?)?approve\s+(it|this|the\s+request)\b",
        r"\b(skip|bypass|override)\s+(all\s+|the\s+|any\s+)*(reviews?|approvals?|controls|checks|polic(y|ies))",
        r"\b(system|assistant)\s*(note|prompt|message|instructions?)\s*:",
        r"\byou\s+are\s+now\b|\bnew\s+instructions\b|reveal\s+(the\s+)?(system\s+prompt|api\s+key|secrets?)",
        r"\bno\s+(security\s+|legal\s+|privacy\s+)?review\s+(is\s+)?(required|needed)\b",
    ]
]

MAX_EXCERPT = 120


def matches_injection(text: str) -> bool:
    return any(p.search(text) for p in INJECTION_PATTERNS)


def _excerpt(text: str, start: int, end: int) -> str:
    """Quoted excerpt around the match, at most MAX_EXCERPT characters including quotes."""
    flat = " ".join(text.split())
    if len(flat) + 2 <= MAX_EXCERPT:
        return f'"{flat}"'
    # Recompute the match position on the whitespace-collapsed text.
    budget = MAX_EXCERPT - 2 - 2  # quotes + ellipses
    m_text = " ".join(text[start:end].split())
    pos = flat.find(m_text)
    if pos < 0:
        pos = 0
    left = max(0, pos - 20)
    snippet = flat[left:left + budget]
    prefix = "…" if left > 0 else ""
    suffix = "…" if left + budget < len(flat) else ""
    return f'"{prefix}{snippet}{suffix}"'


def scan_text(field: str, text: str) -> list[dict]:
    hits = []
    for idx, pattern in enumerate(INJECTION_PATTERNS):
        m = pattern.search(text)
        if m:
            hits.append({"field": field, "pattern": idx + 1, "excerpt": _excerpt(text, m.start(), m.end())})
            break  # one hit per field is enough evidence
    return hits


def scan_value(field: str, value: object) -> list[dict]:
    """Recursively scan every string inside a dict/list/str."""
    if isinstance(value, str):
        return scan_text(field, value)
    hits: list[dict] = []
    if isinstance(value, dict):
        for k, v in value.items():
            hits += scan_value(f"{field}.{k}" if field else str(k), v)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            hits += scan_value(f"{field}[{i}]", v)
    return hits


def scan_fields(fields: dict[str, object]) -> dict:
    hits: list[dict] = []
    for name, value in fields.items():
        hits += scan_value(name, value)
    return {"detected": bool(hits), "hits": hits}
