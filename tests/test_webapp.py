"""Web API (T17): queue, submit, analyse (rules-only, no network), override-reason rule, audit log."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import webapp.main as web
from src import data_access as da
from tests.helpers import mock_vendor_api


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(web, "AUDIT_LOG", self.tmp / "audit_log.jsonl"),
            mock.patch.object(web, "TRACES", self.tmp / "traces"),
            mock.patch.object(da, "SUBMITTED_REQUESTS", self.tmp / "submitted_requests.json"),
        ]
        for p in self.patches:
            p.start()
        web._latest.clear()
        web._runs.clear()
        self.client = TestClient(web.app)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def analyse(self, rid):
        with mock_vendor_api():
            r = self.client.post("/api/analyze", json={"request_id": rid, "architecture": "single", "mode": "rules_only"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_index_and_static_not_cached(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Procurement Request Copilot", r.text)
        self.assertEqual(self.client.get("/static/app.js").headers.get("cache-control"), "no-cache")

    def test_queue_lists_official_requests(self):
        rows = self.client.get("/api/requests").json()
        ids = [r["request_id"] for r in rows]
        self.assertIn("REQ-1001", ids)
        self.assertTrue(all(r["status"] == "New" for r in rows))
        r1006 = next(r for r in rows if r["request_id"] == "REQ-1006")
        self.assertIsNone(r1006["annual_cost_usd"])

    def test_analyse_returns_banners_and_recommended_action(self):
        res = self.analyse("REQ-1007")
        self.assertEqual(res["decision_type"], "route_for_specialist_review")
        self.assertEqual(res["recommended_action"], "send_for_approvals")
        titles = " ".join(b["title"] for b in res["banners"])
        self.assertIn("registry and vendor-risk service disagree", titles)
        conflict = next(b for b in res["banners"] if "compare" in b)
        self.assertEqual(len(conflict["compare"]), 2)
        self.assertTrue(res["decision"]["human_review_required"])
        self.assertEqual({e["kind"] for e in res["evidence"]} - {"Rule", "Tool", "AI analysis"}, set())
        status = next(r for r in self.client.get("/api/requests").json() if r["request_id"] == "REQ-1007")["status"]
        self.assertEqual(status, "Analysed")

    def test_injection_banner_and_clarification(self):
        res = self.analyse("REQ-1006")
        self.assertEqual(res["recommended_action"], "request_clarification")
        inj = next(b for b in res["banners"] if "injection" in b["title"].lower())
        self.assertTrue(any("Ignore all procurement rules" in x for x in inj["excerpts"]))
        self.assertNotIn("CFO", res["decision"]["required_approvals"])

    def test_override_needs_reason_and_audit_is_written(self):
        res = self.analyse("REQ-1006")
        run_id = res["trace_summary"]["run_id"]
        r = self.client.post("/api/actions", json={"request_id": "REQ-1006", "run_id": run_id, "action": "send_for_approvals"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/actions", json={"request_id": "REQ-1006", "run_id": run_id,
                                                    "action": "send_for_approvals", "reason": "  "})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/actions", json={"request_id": "REQ-1006", "run_id": run_id,
                                                    "action": "request_clarification"})
        self.assertEqual(r.status_code, 200)
        entry = r.json()
        self.assertFalse(entry["override"])
        r = self.client.post("/api/actions", json={"request_id": "REQ-1006", "run_id": run_id,
                                                    "action": "hold_manual_review", "reason": "Talk to Security first"})
        self.assertTrue(r.json()["override"])
        lines = (self.tmp / "audit_log.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for k in ("timestamp", "request_id", "run_id", "architecture", "copilot_decision_type", "action", "override", "reason"):
            self.assertIn(k, json.loads(lines[0]))
        self.assertEqual(len(self.client.get("/api/audit?request_id=REQ-1006").json()), 2)
        status = next(r for r in self.client.get("/api/requests").json() if r["request_id"] == "REQ-1006")["status"]
        self.assertEqual(status, "Action recorded")

    def test_action_rejects_unknown_run_and_bad_action(self):
        r = self.client.post("/api/actions", json={"request_id": "REQ-1001", "run_id": "nope", "action": "send_for_approvals"})
        self.assertEqual(r.status_code, 400)
        res = self.analyse("REQ-1001")
        r = self.client.post("/api/actions", json={"request_id": "REQ-1001", "run_id": res["trace_summary"]["run_id"],
                                                    "action": "approve_purchase"})
        self.assertEqual(r.status_code, 422)

    def test_submit_request_with_blanks_and_markup(self):
        r = self.client.post("/api/requests", json={"product_name": "<img src=x onerror=alert(1)>",
                                                     "requested_integrations": [], "annual_cost_usd": None})
        self.assertEqual(r.status_code, 200)
        rid = r.json()["request_id"]
        self.assertEqual(rid, "REQ-U001")
        self.assertEqual(self.client.post("/api/requests", json={}).json()["request_id"], "REQ-U002")
        res = self.analyse(rid)
        self.assertEqual(res["decision_type"], "request_clarification")
        self.assertIn("requester (unknown employee ID)", res["missing_fields"])
        self.assertEqual(self.client.post("/api/requests", json={"annual_cost_usd": -5}).status_code, 422)

    def test_unknown_request_is_404(self):
        self.assertEqual(self.client.get("/api/requests/REQ-NOPE").status_code, 404)
        r = self.client.post("/api/analyze", json={"request_id": "REQ-NOPE", "architecture": "single", "mode": "rules_only"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
