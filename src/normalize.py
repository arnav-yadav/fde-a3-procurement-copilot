"""Request normalisation (T4.2). Urgency is informational only and never changes rules."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

UNKNOWN_DATA_LEVELS = {"", "unknown"}


@dataclass
class NormalizedRequest:
    request_id: str
    requester_id: str | None
    product_name: str | None
    vendor_name: str | None
    category: str | None
    annual_cost_usd: float | None
    user_count: int | None
    business_justification: str | None
    data_access_level: str | None
    requested_integrations: list[str] | None
    urgency: str | None
    notes: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _clean_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").removeprefix("$").strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def normalize_cost(value: object) -> tuple[float | None, str | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    number = _parse_number(value)
    if number is None or number < 0:
        return None, "annual cost (invalid value)"
    return number, None


def normalize_user_count(value: object) -> int | None:
    number = _parse_number(value)
    if number is None or number <= 0 or number != int(number):
        return None
    return int(number)


def normalize_data_access(value: object) -> str | None:
    text = _clean_str(value)
    if text is None:
        return None
    text = text.casefold()
    return None if text in UNKNOWN_DATA_LEVELS else text


def normalize_integrations(value: object) -> list[str] | None:
    """`[]` = none requested; None/absent = missing (fix F6)."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else None
    if isinstance(value, (list, tuple)):
        return [s for s in (_clean_str(v) for v in value) if s]
    return None


def normalize_request(raw: dict) -> NormalizedRequest:
    notes: list[str] = []
    cost, cost_note = normalize_cost(raw.get("annual_cost_usd"))
    if cost_note:
        notes.append(cost_note)
    return NormalizedRequest(
        request_id=str(raw.get("request_id", "")).strip(),
        requester_id=_clean_str(raw.get("requester_id")),
        product_name=_clean_str(raw.get("product_name")),
        vendor_name=_clean_str(raw.get("vendor_name")),
        category=_clean_str(raw.get("category")),
        annual_cost_usd=cost,
        user_count=normalize_user_count(raw.get("user_count")),
        business_justification=_clean_str(raw.get("business_justification")),
        data_access_level=normalize_data_access(raw.get("data_access_level")),
        requested_integrations=normalize_integrations(raw.get("requested_integrations")),
        urgency=_clean_str(raw.get("urgency")),
        notes=notes,
        raw=dict(raw),
    )
