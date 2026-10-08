"""Runtime settings (read at call time) and policy metadata (parsed from the policy file).

Settings are re-read on every `get_settings()` call because the eval harness changes
environment variables between cases (fault injection, EXTRA_DATA_DIR).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RUNTIME_DIR = ROOT / "runtime"

EXPECTED_POLICY_VERSION = "2026.09"

PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_envs": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "default_model": "gemini-3.5-flash",
        "default_min_interval": 6.0,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_envs": ("GROQ_API_KEY",),
        "default_model": "openai/gpt-oss-120b",
        "default_min_interval": 2.0,
    },
}

log = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _float_env(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    vendor_risk_base_url: str
    app_port: int
    llm_provider: str
    llm_base_url: str
    llm_api_key: str | None
    model_name: str
    llm_temperature: float
    llm_timeout_seconds: float
    llm_max_retries: int
    llm_min_interval_seconds: float
    llm_simulate_outage: bool
    extra_data_dir: Path | None

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)


def get_settings() -> Settings:
    provider = (_env("LLM_PROVIDER", "gemini") or "gemini").lower()
    if provider not in PROVIDERS:
        log.warning("Unknown LLM_PROVIDER %r; falling back to gemini", provider)
        provider = "gemini"
    spec = PROVIDERS[provider]
    api_key = next((_env(k) for k in spec["key_envs"] if _env(k)), None)

    extra = _env("EXTRA_DATA_DIR")
    extra_path: Path | None = None
    if extra:
        extra_path = Path(extra)
        if not extra_path.is_absolute():
            extra_path = ROOT / extra_path

    return Settings(
        vendor_risk_base_url=(_env("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001") or "").rstrip("/"),
        app_port=_int_env("APP_PORT", 8000),
        llm_provider=provider,
        llm_base_url=spec["base_url"],
        llm_api_key=api_key,
        model_name=_env("MODEL_NAME", spec["default_model"]),
        llm_temperature=_float_env("LLM_TEMPERATURE", 0.0),
        llm_timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", 30.0),
        llm_max_retries=_int_env("LLM_MAX_RETRIES", 3),
        llm_min_interval_seconds=_float_env("LLM_MIN_INTERVAL_SECONDS", spec["default_min_interval"]),
        llm_simulate_outage=_env("LLM_SIMULATE_OUTAGE", "0") in ("1", "true", "yes"),
        extra_data_dir=extra_path,
    )


_REF_DATE_RE = re.compile(r"Data snapshot / evaluation reference date:\*\*\s*(\d{4}-\d{2}-\d{2})")
_DATA_README_RE = re.compile(r"Reference date for date-based policy checks:\s*(\d{4}-\d{2}-\d{2})")
_VERSION_RE = re.compile(r"Policy version:\*\*\s*([\d.]+)")


@lru_cache(maxsize=1)
def get_reference_date() -> date:
    """The policy evaluation date. Never today's date."""
    policy = (DATA_DIR / "procurement_policy.md").read_text(encoding="utf-8")
    m = _REF_DATE_RE.search(policy)
    if not m:
        readme = DATA_DIR / "README.md"
        text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
        m = _DATA_README_RE.search(text)
    if not m:
        raise RuntimeError(
            "Could not parse the evaluation reference date from data/procurement_policy.md "
            "or data/README.md. Date-based policy checks cannot run."
        )
    return date.fromisoformat(m.group(1))


@lru_cache(maxsize=1)
def get_policy_version() -> str | None:
    policy = (DATA_DIR / "procurement_policy.md").read_text(encoding="utf-8")
    m = _VERSION_RE.search(policy)
    version = m.group(1) if m else None
    if version != EXPECTED_POLICY_VERSION:
        log.warning(
            "Policy version is %s, expected %s: the thresholds in the policy engine may be stale.",
            version, EXPECTED_POLICY_VERSION,
        )
    return version
