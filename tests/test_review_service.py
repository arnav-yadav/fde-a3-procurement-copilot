"""Reviewer workflow (T17 behaviour): queue, analysis view, submission, override-reason rule, audit log."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import data_access as da
from src import review_service as svc
from tests.helpers import env, mock_vendor_api


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(svc, "AUDIT_LOG", self.tmp / "audit_log.jsonl"),
            mock.patch.object(svc, "TRACES", self.tmp / "traces"),
            mock.patch.object(svc, "LATEST", self.tmp / "latest_runs.json"),
            mock.patch.object(da, "SUBMITTED_REQUESTS", self.tmp / "submitted_requests.json"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def analyse(self, rid):
        with mock_vendor_api():
            return svc.analyse(rid, "single", mode="rules_only")

    def status_of(self, rid):
        return next(r for r in svc.queue() if r["request_id"] == rid)["status"]

    def test_queue_lists_official_requests(self):
        rows = svc.queue()
        self.assertIn("REQ-1001", [r["request_id"] for r in rows])
        self.assertTrue(all(r["status"] == "New" for r in rows))
        self.assertIsNone(next(r for r in rows if r["request_id"] == "REQ-1006")["annual_cost_usd"])

    def test_analysis_view_and_persisted_status(self):
        view = self.analyse("REQ-1007")
        self.assertEqual(view["decision_type"], "route_for_specialist_review")
        self.assertEqual(view["recommended_action"], "send_for_approvals")
        conflict = next(b for b in view["banners"] if "compare" in b)
        self.assertEqual(len(conflict["compare"]), 2)
        self.assertTrue(view["decision"]["human_review_required"])
        self.assertEqual({e["kind"] for e in view["evidence"]} - {"Rule", "Tool", "AI analysis"}, set())
        self.assertEqual(self.status_of("REQ-1007"), "Analysed")
        # survives a restart: the latest run is read back from disk
        again = svc.latest_analysis("REQ-1007")
        self.assertEqual(again["trace_summary"]["run_id"], view["trace_summary"]["run_id"])
        self.assertEqual(next(r for r in svc.queue() if r["request_id"] == "REQ-1007")["decision_type"],
                         "route_for_specialist_review")

    def test_injection_banner_and_clarification(self):
        view = self.analyse("REQ-1006")
        self.assertEqual(view["recommended_action"], "request_clarification")
        inj = next(b for b in view["banners"] if "injection" in b["title"].lower())
        self.assertTrue(any("Ignore all procurement rules" in x for x in inj["excerpts"]))
        self.assertNotIn("CFO", view["decision"]["required_approvals"])

    def test_unavailable_banner(self):
        with mock_vendor_api(outage=True), env(LLM_SIMULATE_OUTAGE="1"):
            view = svc.analyse("REQ-1001", "single")
        titles = [b["title"] for b in view["banners"]]
        self.assertIn("Vendor-risk service unavailable: security status could not be verified", titles)
        self.assertIn("AI analysis unavailable: showing rule-based result", titles)

    def test_override_needs_reason_and_audit_is_written(self):
        view = self.analyse("REQ-1006")
        run_id = view["trace_summary"]["run_id"]
        for reason in (None, "   "):
            with self.assertRaises(svc.ActionRejected):
                svc.record_action("REQ-1006", run_id, "send_for_approvals", reason)
        entry = svc.record_action("REQ-1006", run_id, "request_clarification", None)
        self.assertFalse(entry["override"])
        entry = svc.record_action("REQ-1006", run_id, "hold_manual_review", "Talk to Security first")
        self.assertTrue(entry["override"])
        lines = (self.tmp / "audit_log.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for k in ("timestamp", "request_id", "run_id", "architecture", "copilot_decision_type", "action", "override", "reason"):
            self.assertIn(k, json.loads(lines[0]))
        self.assertEqual(len(svc.audit_entries("REQ-1006")), 2)
        self.assertEqual(self.status_of("REQ-1006"), "Action recorded")

    def test_action_rejects_unknown_run_and_action(self):
        with self.assertRaises(svc.ActionRejected):
            svc.record_action("REQ-1001", "nope", "send_for_approvals", None)
        view = self.analyse("REQ-1001")
        with self.assertRaises(svc.ActionRejected):
            svc.record_action("REQ-1001", view["trace_summary"]["run_id"], "approve_purchase", "x")
        with self.assertRaises(svc.ActionRejected):  # a run of another request
            svc.record_action("REQ-1002", view["trace_summary"]["run_id"], "send_for_approvals", None)

    def test_consequence_texts(self):
        view = self.analyse("REQ-1002")
        text = svc.consequence("send_for_approvals", view, "Sarah Lee")
        self.assertIn("Sends the evidence pack to 5 approvers: Department Head, Finance, Procurement, Security, Legal.", text)
        self.assertIn("Nothing is approved or purchased", text)
        self.assertIn("No one is notified", svc.consequence("hold_manual_review", view, "Sarah Lee"))

    def test_submit_request_with_blanks_and_markup(self):
        rec = svc.submit_request({"product_name": "<img src=x onerror=alert(1)>", "requested_integrations": [],
                                  "annual_cost_usd": None, "vendor_name": "  "})
        self.assertEqual(rec["request_id"], "REQ-U001")
        self.assertIsNone(rec["vendor_name"])
        self.assertEqual(svc.submit_request({})["request_id"], "REQ-U002")
        view = self.analyse("REQ-U001")
        self.assertEqual(view["decision_type"], "request_clarification")
        self.assertIn("requester (unknown employee ID)", view["missing_fields"])
        with self.assertRaises(Exception):
            svc.submit_request({"annual_cost_usd": -5})

    def test_unknown_request(self):
        with self.assertRaises(svc.NotFound):
            svc.get_request("REQ-NOPE")
        with self.assertRaises(svc.NotFound):
            svc.analyse("REQ-NOPE", "single", mode="rules_only")


if __name__ == "__main__":
    unittest.main()
