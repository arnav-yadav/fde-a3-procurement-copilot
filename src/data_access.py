"""Data loaders.

Each loader reads `data/` and, when EXTRA_DATA_DIR is set, appends the same-named file
from that directory (eval-only fixtures). Requests submitted through the web app live in
`runtime/submitted_requests.json`. Files are tiny, so they are re-read on every call;
this keeps behaviour correct when the eval harness changes EXTRA_DATA_DIR between cases.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from src.config import DATA_DIR, ROOT, RUNTIME_DIR, get_settings

SUBMITTED_REQUESTS = RUNTIME_DIR / "submitted_requests.json"


def norm_key(value: object) -> str:
    """Normalise a name for lookups: collapse whitespace, casefold."""
    return " ".join(str(value).split()).casefold()


def _extra_file(name: str) -> Path | None:
    extra = get_settings().extra_data_dir
    if extra is None:
        return None
    path = extra / name
    return path if path.is_file() else None


def _read_csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return [{k: (v if v is not None else "") for k, v in row.items()} for row in csv.DictReader(f)]


def load_csv_records(name: str) -> list[dict]:
    """Rows from data/<name> plus EXTRA_DATA_DIR/<name>, as dicts of strings."""
    rows = _read_csv_rows(DATA_DIR / name)
    extra = _extra_file(name)
    if extra:
        rows += _read_csv_rows(extra)
    return rows


def _load_frame(name: str) -> pd.DataFrame:
    frames = [pd.read_csv(DATA_DIR / name)]
    extra = _extra_file(name)
    if extra:
        frames.append(pd.read_csv(extra))
    return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]


# Starter-pack pandas loaders (kept for compatibility).
def load_employees() -> pd.DataFrame:
    return _load_frame("employees.csv")


def load_budgets() -> pd.DataFrame:
    return _load_frame("department_budgets.csv")


def load_software_catalog() -> pd.DataFrame:
    return _load_frame("software_catalog.csv")


def load_vendors() -> pd.DataFrame:
    return _load_frame("vendors.csv")


def load_purchase_history() -> pd.DataFrame:
    return _load_frame("purchase_history.csv")


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_submitted_requests() -> list[dict]:
    return _read_json(SUBMITTED_REQUESTS, [])


def save_submitted_requests(rows: list[dict]) -> None:
    SUBMITTED_REQUESTS.parent.mkdir(parents=True, exist_ok=True)
    SUBMITTED_REQUESTS.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def load_requests(include_submitted: bool = False) -> list[dict]:
    rows = list(_read_json(DATA_DIR / "requests.json", []))
    extra = _extra_file("requests.json")
    if extra:
        rows += _read_json(extra, [])
    if include_submitted:
        rows += load_submitted_requests()
    return rows


def get_request(request_id: str) -> dict:
    """Search data/requests.json, then extra fixtures, then runtime submissions."""
    for request in load_requests(include_submitted=True):
        if str(request.get("request_id", "")).strip() == str(request_id).strip():
            return request
    raise KeyError(f"Unknown request_id: {request_id}")


def load_vendor_risk_data() -> dict:
    """Backing data of the mock API (eval/test use only; the app goes through the API)."""
    data = dict(_read_json(DATA_DIR / "vendor_risk.json", {}))
    extra = _extra_file("vendor_risk.json")
    if extra:
        data.update(_read_json(extra, {}))
    return data


def load_policy_text() -> str:
    return (DATA_DIR / "procurement_policy.md").read_text(encoding="utf-8")


def find_vendor(name: object) -> dict | None:
    if name is None or not str(name).strip():
        return None
    key = norm_key(name)
    for row in load_csv_records("vendors.csv"):
        if norm_key(row.get("vendor_name", "")) == key:
            return row
    return None


def find_employee(employee_id: object) -> dict | None:
    if employee_id is None or not str(employee_id).strip():
        return None
    key = norm_key(employee_id)
    for row in load_csv_records("employees.csv"):
        if norm_key(row.get("employee_id", "")) == key:
            return row
    return None


def find_budget(department: object) -> dict | None:
    if department is None:
        return None
    key = norm_key(department)
    for row in load_csv_records("department_budgets.csv"):
        if norm_key(row.get("department", "")) == key:
            return row
    return None


__all__ = [
    "ROOT", "DATA_DIR", "norm_key", "load_csv_records", "load_requests", "get_request",
    "load_submitted_requests", "save_submitted_requests", "load_vendor_risk_data",
    "load_policy_text", "find_vendor", "find_employee", "find_budget",
    "load_employees", "load_budgets", "load_software_catalog", "load_vendors", "load_purchase_history",
]
