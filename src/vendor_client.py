from __future__ import annotations

import time
from urllib.parse import quote

import requests

from src.config import get_settings

TIMEOUT_SECONDS = 3.0
RETRY_BACKOFF_SECONDS = 0.5


def _url(vendor_name: str) -> str:
    return f"{get_settings().vendor_risk_base_url}/vendor-risk/{quote(vendor_name, safe='')}"


def get_vendor_risk(vendor_name: str, timeout_seconds: float = TIMEOUT_SECONDS) -> dict:
    """Low-level API client (kept for backwards compatibility). Raises on HTTP errors."""
    response = requests.get(_url(vendor_name), timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


def get_vendor_risk_classified(vendor_name: str) -> dict:
    """Call the vendor-risk API and classify the outcome. Never raises.

    outcome: "ok" (record returned) | "not_found" (404, no retry)
             | "unavailable" (5xx / timeout / connection error after one retry,
               or any other unexpected response).
    """
    endpoint = _url(vendor_name)
    started = time.perf_counter()
    attempts = 0
    status_code: int | None = None
    error: str | None = None

    while attempts < 2:
        attempts += 1
        try:
            response = requests.get(endpoint, timeout=TIMEOUT_SECONDS)
            status_code = response.status_code
            if status_code == 200:
                return _result("ok", status_code, response.json(), None, attempts, started, endpoint)
            if status_code == 404:
                return _result("not_found", status_code, None, _detail(response), attempts, started, endpoint)
            error = f"HTTP {status_code}: {_detail(response)}"
            if status_code < 500:
                break  # other 4xx: not retryable
        except (requests.ConnectionError, requests.Timeout) as exc:
            status_code = None
            error = f"{type(exc).__name__}: {exc}"
        except (requests.RequestException, ValueError) as exc:
            status_code = None
            error = f"{type(exc).__name__}: {exc}"
            break
        if attempts < 2:
            time.sleep(RETRY_BACKOFF_SECONDS)

    return _result("unavailable", status_code, None, error, attempts, started, endpoint)


def _detail(response: requests.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
    except ValueError:
        pass
    return response.text[:200]


def _result(outcome, status_code, record, error, attempts, started, endpoint) -> dict:
    return {
        "outcome": outcome,
        "status_code": status_code,
        "record": record,
        "error": error,
        "attempts": attempts,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "endpoint": endpoint,
    }
