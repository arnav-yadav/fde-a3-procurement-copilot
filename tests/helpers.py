"""Test helpers: route the vendor client through the in-process mock API (no network)."""
from __future__ import annotations

import contextlib
import os
from unittest import mock
from urllib.parse import urlsplit

import requests
from fastapi.testclient import TestClient

from mock_api.app import create_app

FIXTURES = "evals/fixtures"


@contextlib.contextmanager
def env(**values):
    """Temporarily set (or unset, with None) environment variables."""
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def mock_vendor_api(extra_dir: str | None = "", outage: bool = False):
    """Patch requests.get in the vendor client to hit an in-process mock API.

    outage=True simulates a dead service (connection refused)."""
    client = TestClient(create_app(extra_dir))

    def fake_get(url, timeout=None, **kwargs):
        if outage:
            raise requests.ConnectionError("simulated: connection refused")
        parts = urlsplit(url)
        return client.get(parts.path)

    with mock.patch("src.vendor_client.requests.get", side_effect=fake_get):
        yield
