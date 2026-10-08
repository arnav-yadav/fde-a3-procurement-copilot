"""Web app placeholder (Phase 0). The full reviewer UI and API arrive in Phase 6."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Procurement Request Copilot")


@app.get("/", response_class=PlainTextResponse)
def index() -> str:
    return "ok"


@app.get("/api/health")
def health() -> dict:
    return {"app": "ok"}
