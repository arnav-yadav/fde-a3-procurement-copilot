from __future__ import annotations

import unittest
from unittest import mock

from src import policy_engine as pe
from src.tools import RunContext
from src.vendor_client import get_vendor_risk_classified
from tests.helpers import env, mock_vendor_api


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body


class VendorClientTests(unittest.TestCase):
    def test_ok(self):
        with mock_vendor_api():
            r = get_vendor_risk_classified("SignFlow")
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(r["record"]["security_review_status"], "approved")

    def test_404_no_retry(self):
        with mock_vendor_api():
            r = get_vendor_risk_classified("No Such Vendor")
        self.assertEqual((r["outcome"], r["status_code"], r["attempts"]), ("not_found", 404, 1))

    def test_503_retried_then_unavailable(self):
        with mock_vendor_api(), mock.patch("src.vendor_client.time.sleep"):
            r = get_vendor_risk_classified("NimbusAI")
        self.assertEqual((r["outcome"], r["status_code"], r["attempts"]), ("unavailable", 503, 2))

    def test_503_then_ok_recovers(self):
        responses = iter([_Resp(503, {"detail": "busy"}), _Resp(200, {"security_review_status": "approved"})])
        with mock.patch("src.vendor_client.requests.get", side_effect=lambda *a, **k: next(responses)), \
                mock.patch("src.vendor_client.time.sleep"):
            r = get_vendor_risk_classified("X")
        self.assertEqual((r["outcome"], r["attempts"]), ("ok", 2))

    def test_connection_refused_closed_port(self):
        with env(VENDOR_RISK_BASE_URL="http://127.0.0.1:9"), mock.patch("src.vendor_client.time.sleep"):
            r = get_vendor_risk_classified("SignFlow")
        self.assertEqual((r["outcome"], r["attempts"]), ("unavailable", 2))
        self.assertIn("ConnectionError", r["error"])

    def test_casing_and_whitespace_resolution(self):
        with mock_vendor_api("evals/fixtures"):
            s = pe.vendor_status(" signalwatch ")
        self.assertEqual(s["vendor_name_queried"], "SignalWatch")
        self.assertEqual(s["registry"]["vendor_id"], "V005")
        self.assertEqual(s["api"]["outcome"], "ok")
        self.assertTrue(s["derived"]["conflict"])

    def test_name_with_slash_routes(self):
        with mock_vendor_api():
            r = get_vendor_risk_classified("A/B Tools")
        self.assertEqual(r["outcome"], "not_found")

    def test_tool_never_raises_and_caches(self):
        with mock_vendor_api():
            ctx = RunContext("REQ-1001")
            a = ctx.execute("get_vendor_status", {"vendor_name": "SignFlow"})
            b = ctx.execute("get_vendor_status", {"vendor_name": " signflow "})
            bad = ctx.execute("get_vendor_status", {})
            unknown = ctx.execute("no_such_tool", {})
        self.assertIs(a, b)
        self.assertEqual(bad["status"], "error")
        self.assertEqual(unknown["status"], "error")
        self.assertEqual(ctx.counters["tool_calls"], 2)  # the cache hit is not counted; error counted
        self.assertTrue(ctx.tool_log[1]["cache_hit"])


if __name__ == "__main__":
    unittest.main()
