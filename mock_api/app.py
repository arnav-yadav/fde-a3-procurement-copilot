from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).resolve().parents[1]


def _norm_key(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def _load_data() -> dict:
    data = json.loads((ROOT / "data" / "vendor_risk.json").read_text(encoding="utf-8"))
    extra_dir = os.getenv("EXTRA_DATA_DIR", "").strip()
    if extra_dir:
        extra_path = Path(extra_dir)
        if not extra_path.is_absolute():
            extra_path = ROOT / extra_path
        extra_file = extra_path / "vendor_risk.json"
        if extra_file.is_file():
            data.update(json.loads(extra_file.read_text(encoding="utf-8")))
    return data


DATA = _load_data()
INDEX = {_norm_key(name): name for name in DATA}

app = FastAPI(title="FDE Mock Vendor Risk API", version="1.1")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# `:path` keeps names containing "/" routable; FastAPI already percent-decodes once.
@app.get("/vendor-risk/{vendor_name:path}")
def vendor_risk(vendor_name: str) -> dict:
    canonical = INDEX.get(_norm_key(vendor_name))
    record = DATA.get(canonical) if canonical else None
    if record is None:
        raise HTTPException(status_code=404, detail=f"No vendor-risk record for '{vendor_name}'")
    if record.get("force_error"):
        raise HTTPException(status_code=503, detail=record.get("error_message", "Vendor-risk service unavailable"))
    return {"vendor_name": canonical, **record}
