from __future__ import annotations

import unittest
from datetime import date, timedelta

from src import data_access as da
from src import policy_engine as pe
from src.config import get_reference_date
from src.normalize import normalize_request
from src.tools import RunContext
from tests.helpers import env, mock_vendor_api

REF = get_reference_date()


def evaluate(request_id: str, outage: bool = False, extra: str | None = None):
    with env(EXTRA_DATA_DIR=extra), mock_vendor_api(extra or "", outage=outage):
        return pe.evaluate(request_id, RunContext(request_id))


def api_ok(status, review_date=None, outside=False):
    return {"outcome": "ok", "record": {"security_review_status": status, "last_review_date": review_date,
                                        "stores_data_outside_region": outside}}


def registry(status="Approved", review_date=None, procurement="Approved", legal="Approved"):
    return {"vendor_id": "V1", "vendor_name": "X", "procurement_status": procurement, "security_status": status,
            "security_review_date": review_date or "", "legal_terms_status": legal}


DAYS_AGO = lambda n: (REF - timedelta(days=n)).isoformat()  # noqa: E731


class ReferenceDateTests(unittest.TestCase):
    def test_reference_date_from_policy(self):
        self.assertEqual(REF, date(2026, 9, 30))


class TierTests(unittest.TestCase):
    def test_boundaries(self):
        cases = [
            (1000, ["Manager"]),
            (1000.01, ["Department Head", "Procurement"]),
            (10000, ["Department Head", "Procurement"]),
            (10000.01, ["Department Head", "Finance", "Procurement"]),
            (25000, ["Department Head", "Finance", "Procurement"]),
            (25000.01, ["Department Head", "Finance", "CFO", "Procurement"]),
            (None, ["Procurement"]),
            (0, ["Manager"]),
        ]
        for cost, roles in cases:
            with self.subTest(cost=cost):
                self.assertEqual(pe.tier_roles(cost)[0], roles)


class BudgetTests(unittest.TestCase):
    def _check(self, cost, dept_row):
        with mock_budget(cost, dept_row):
            return pe.budget_check("REQ-T")

    def test_equal_is_within(self):
        r = self._check(15000, {"department": "Marketing", "annual_software_budget_usd": "1", "committed_usd": "0",
                                "available_usd": "15000"})
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["within_budget"])

    def test_over(self):
        r = self._check(15000.01, {"department": "Marketing", "annual_software_budget_usd": "1", "committed_usd": "0",
                                   "available_usd": "15000"})
        self.assertEqual(r["status"], "insufficient")

    def test_no_row(self):
        r = self._check(100, None)
        self.assertEqual(r["status"], "unverified")


class mock_budget:
    """Patch request + budget lookups for budget_check unit tests."""
    def __init__(self, cost, row):
        from unittest import mock
        req = {"request_id": "REQ-T", "requester_id": "E001", "annual_cost_usd": cost}
        self.patches = [
            mock.patch.object(da, "get_request", return_value=req),
            mock.patch.object(da, "find_budget", return_value=row),
        ]

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class ReviewAgeAndConflictTests(unittest.TestCase):
    def test_365_vs_366(self):
        d365 = pe.derive_vendor(registry("Approved", DAYS_AGO(365)), {"outcome": "not_found"}, REF)
        d366 = pe.derive_vendor(registry("Approved", DAYS_AGO(366)), {"outcome": "not_found"}, REF)
        self.assertTrue(d365["cleared"])
        self.assertFalse(d365["expired"])
        self.assertTrue(d366["expired"])
        self.assertFalse(d366["cleared"])

    def test_future_review_not_current(self):
        d = pe.derive_vendor(registry("Approved", DAYS_AGO(-1)), {"outcome": "not_found"}, REF)
        self.assertFalse(d["cleared"])
        self.assertFalse(d["expired"])

    def test_conflict_matrix(self):
        same = DAYS_AGO(100)
        cases = [
            # (registry status, registry date, api status, api date, conflict)
            ("Approved", same, "approved", same, False),
            ("Approved", same, "approved", DAYS_AGO(50), True),   # both approved, dates differ
            ("Approved", same, "expired", same, True),
            ("Approved", same, "not_completed", None, True),
            ("Approved", same, "pending", None, True),
            ("Approved", same, "in_progress", None, True),
            ("Pending", None, "approved", same, True),
            ("Expired", same, "approved", same, True),
            ("Unknown", None, "approved", same, True),
            ("Pending", None, "not_completed", None, False),
            ("Expired", same, "expired", same, False),
            ("Unknown", None, "weird", None, False),
        ]
        for rs, rd, ast, ad, expected in cases:
            with self.subTest(registry=rs, api=ast):
                d = pe.derive_vendor(registry(rs, rd), api_ok(ast, ad), REF)
                self.assertEqual(d["conflict"], expected)

    def test_no_conflict_when_api_not_ok(self):
        for outcome in ("unavailable", "not_found"):
            d = pe.derive_vendor(registry("Approved", DAYS_AGO(10)), {"outcome": outcome}, REF)
            self.assertFalse(d["conflict"])
            self.assertTrue(d["cleared"])

    def test_not_registered(self):
        d = pe.derive_vendor(None, {"outcome": "not_found"}, REF)
        self.assertFalse(d["cleared"])
        self.assertTrue(d["new_vendor"])
        self.assertFalse(d["legal_terms_approved"])

    def test_api_expired_by_age(self):
        d = pe.derive_vendor(registry("Approved", DAYS_AGO(10)), api_ok("approved", DAYS_AGO(400)), REF)
        self.assertTrue(d["expired"])
        self.assertFalse(d["cleared"])

    def test_classes(self):
        self.assertEqual(pe.classify_registry("Approved"), "approved")
        self.assertEqual(pe.classify_registry(""), "unknown")
        self.assertEqual(pe.classify_registry("Other"), "unknown")
        self.assertEqual(pe.classify_api("not_completed"), "pending")
        self.assertEqual(pe.classify_api("expired"), "expired")
        self.assertEqual(pe.classify_api("???"), "unknown")


class DataClassTests(unittest.TestCase):
    def test_level_table(self):
        cases = {
            "none": [], "internal": [], "internal_documents": [], "internal_marketing": [], "public": [],
            "source_code": ["source_code"], "customer_pii": ["customer_pii"], "employee_pii": ["employee_pii"],
            "confidential_documents": ["confidential_documents"], "production_telemetry": ["production_access"],
            "production_data": ["production_access"], "production_access": ["production_access"],
            "credentials": ["credentials"], "secrets": ["credentials"],
        }
        for level, classes in cases.items():
            with self.subTest(level=level):
                self.assertEqual(pe.data_classes_for_level(level), (classes, True))

    def test_keyword_fallback(self):
        self.assertEqual(pe.data_classes_for_level("hr_personal_records")[0], ["employee_pii"])
        self.assertEqual(pe.data_classes_for_level("personal_data")[0], ["customer_pii"])
        self.assertIn("source_code", pe.data_classes_for_level("git_repo_access")[0])
        self.assertEqual(pe.data_classes_for_level("cloud_admin")[0], ["production_access"])
        self.assertEqual(pe.data_classes_for_level("password_vault")[0], ["credentials"])
        self.assertEqual(pe.data_classes_for_level("weird_level"), ([], False))

    def test_integration_keywords(self):
        cases = {
            "Document repository": "confidential_documents",
            "Google Drive": "confidential_documents",
            "Git repositories": "source_code",
            "GitHub": "source_code",
            "Production cloud account": "production_access",
            "AWS": "production_access",
            "Product analytics": None,
            "HRIS (Workday)": "employee_pii",
            "Payroll": "employee_pii",
            "CRM": "customer_pii",
            "HelpDeskly": "customer_pii",
            "Support tickets": "customer_pii",
            "HashiCorp Vault": "credentials",
            "SSO": None,
            "Slack": None,
            "Okta SAML": None,
        }
        for integ, cls in cases.items():
            with self.subTest(integration=integ):
                self.assertEqual(pe.integration_class(integ), cls)


class NormalizationTests(unittest.TestCase):
    def test_integrations_empty_vs_none(self):
        self.assertEqual(normalize_request({"requested_integrations": []}).requested_integrations, [])
        self.assertIsNone(normalize_request({"requested_integrations": None}).requested_integrations)
        self.assertIsNone(normalize_request({}).requested_integrations)
        self.assertEqual(normalize_request({"requested_integrations": " SSO "}).requested_integrations, ["SSO"])

    def test_cost_and_users(self):
        self.assertEqual(normalize_request({"annual_cost_usd": 800}).annual_cost_usd, 800.0)
        r = normalize_request({"annual_cost_usd": -5})
        self.assertIsNone(r.annual_cost_usd)
        self.assertIn("annual cost (invalid value)", r.notes)
        self.assertIsNone(normalize_request({"annual_cost_usd": "lots"}).annual_cost_usd)
        self.assertIsNone(normalize_request({"user_count": 0}).user_count)
        self.assertEqual(normalize_request({"user_count": 3}).user_count, 3)

    def test_data_access_unknown(self):
        for v in (None, "", "unknown", " Unknown "):
            self.assertIsNone(normalize_request({"data_access_level": v}).data_access_level)

    def test_name_normalisation(self):
        self.assertEqual(da.norm_key("  Signal   Watch "), "signal watch")
        self.assertEqual(da.find_vendor(" signalwatch ")["vendor_name"], "SignalWatch")
        self.assertEqual(da.find_employee(" e001 ")["employee_id"], "E001")
        self.assertIsNone(da.find_vendor("Nope Inc"))


class LegalAndCrossRegionTests(unittest.TestCase):
    def _legal(self, cost, procurement="New", legal="Approved"):
        from unittest import mock
        req = {"request_id": "REQ-T", "requester_id": "E001", "product_name": "P", "vendor_name": "V",
               "category": "Zzz", "annual_cost_usd": cost, "user_count": 1, "business_justification": "x",
               "data_access_level": "none", "requested_integrations": []}
        reg = registry("Approved", DAYS_AGO(10), procurement=procurement, legal=legal)
        with mock.patch.object(da, "get_request", return_value=req), \
                mock.patch.object(da, "find_vendor", return_value=reg), \
                mock.patch("src.policy_engine.get_vendor_risk_classified",
                           return_value={"outcome": "ok", "record": {"security_review_status": "approved",
                                                                     "last_review_date": DAYS_AGO(10)}}):
            return pe.evaluate("REQ-T", RunContext("REQ-T"))

    def test_new_vendor_boundary(self):
        self.assertNotIn("Legal", self._legal(9999.99).roles)
        self.assertIn("Legal", self._legal(10000).roles)
        self.assertNotIn("Legal", self._legal(50000, procurement="Approved").roles)

    def test_terms_not_approved(self):
        self.assertIn("Legal", self._legal(500, procurement="Approved", legal="Draft").roles)

    def test_cross_region(self):
        p = evaluate("REQ-1004")  # customer_pii + vendor stores data outside region
        self.assertIn("Privacy", p.roles)
        self.assertTrue(any(r.rule == "R8" for r in p.approval("Legal").reasons))


# T5 expected-output table (hand-checked; must match golden_cases.json)
OFFICIAL_EXPECTED = {
    "REQ-1001": (["Manager"], ["existing_tool_overlap"]),
    "REQ-1002": (["Department Head", "Finance", "Procurement", "Security", "Legal"],
                 ["existing_tool_overlap", "security_review_required", "legal_review_required"]),
    "REQ-1003": (["Department Head", "Finance", "Procurement", "Security"],
                 ["existing_tool_overlap", "security_review_required"]),
    "REQ-1004": (["Department Head", "Procurement", "Security", "Privacy", "Legal"],
                 ["existing_tool_overlap", "security_review_required", "privacy_review_required", "legal_review_required"]),
    "REQ-1005": (["Department Head", "Finance", "Procurement", "Security", "Privacy", "Legal"],
                 ["budget_insufficient", "security_review_required", "privacy_review_required", "legal_review_required"]),
    "REQ-1006": (["Procurement"], ["missing_information", "prompt_injection_detected", "existing_tool_overlap"]),
    "REQ-1007": (["Department Head", "Finance", "Procurement", "Security"],
                 ["existing_tool_overlap", "security_review_required", "vendor_review_expired",
                  "conflicting_vendor_evidence"]),
    "REQ-1008": (["Department Head", "Procurement"], ["existing_tool_overlap"]),
    "REQ-1009": (["Department Head", "Finance", "Procurement", "Security", "Privacy", "Legal"],
                 ["security_review_required", "privacy_review_required", "legal_review_required",
                  "vendor_risk_unavailable"]),
    "REQ-1010": (["Manager"], []),
}


class OfficialTableTests(unittest.TestCase):
    def test_t5_table(self):
        for rid, (roles, flags) in OFFICIAL_EXPECTED.items():
            with self.subTest(request=rid):
                p = evaluate(rid)
                self.assertEqual(p.roles, roles)
                self.assertEqual(set(p.flags), set(flags))

    def test_vendor_api_called_once_per_run(self):
        from unittest import mock
        with mock_vendor_api(), mock.patch("src.policy_engine.get_vendor_risk_classified",
                                           wraps=pe.get_vendor_risk_classified) as spy:
            ctx = RunContext("REQ-1002")
            pe.evaluate("REQ-1002", ctx)
            ctx.execute("get_vendor_status", {"vendor_name": " brandboard "})
            pe.evaluate("REQ-1002", ctx)
        self.assertEqual(spy.call_count, 1)

    def test_decision_precedence(self):
        p = evaluate("REQ-1006")
        self.assertEqual(pe.decide(p, p.roles, p.flags), "request_clarification")
        p = evaluate("REQ-1008")
        self.assertEqual(pe.decide(p, p.roles, p.flags), "route_for_approval")
        overlap = [{"software_id": "SW003", "covers_stated_need": True}]
        self.assertEqual(pe.decide(p, p.roles, p.flags, overlap), "use_existing_tool")
        # a candidate outside overlap_flag_candidates can never justify use_existing_tool
        self.assertEqual(pe.decide(p, p.roles, p.flags, [{"software_id": "SW999", "covers_stated_need": True}]),
                         "route_for_approval")
        p = evaluate("REQ-1007")
        self.assertEqual(pe.decide(p, p.roles, p.flags), "route_for_specialist_review")


if __name__ == "__main__":
    unittest.main()
