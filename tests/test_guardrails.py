from __future__ import annotations

import unittest

from src import policy_engine as pe
from src.guardrails import assemble, grounded, output_filter_hit
from src.schemas import AgentProposal
from src.tools import RunContext
from tests.helpers import FIXTURES, env, mock_vendor_api


def run_policy(request_id: str, extra: str | None = None):
    ctx = RunContext(request_id)
    with env(EXTRA_DATA_DIR=extra), mock_vendor_api(extra or ""):
        ctx.execute("evaluate_policy_rules", {"request_id": request_id}, initiator="orchestrator")
    return ctx.policy, ctx


def proposal(**overrides) -> AgentProposal:
    base = dict(
        decision_type="route_for_specialist_review",
        recommendation="Route to Security and Legal before business approval.",
        next_step="Send the evidence pack to the Department Head, Finance, Procurement, Security and Legal.",
        required_approvals=[{"role": r, "reason": "policy"} for r in
                            ["Department Head", "Finance", "Procurement", "Security", "Legal"]],
        risk_flags=["existing_tool_overlap", "security_review_required", "legal_review_required"],
        missing_information=[],
        overlap_assessment=[],
        evidence=[],
        injection_observed=False,
    )
    base.update(overrides)
    return AgentProposal.model_validate(base)


def events(ctx, kind):
    return [e for e in ctx.events if e["event"] == kind]


class GuardrailTests(unittest.TestCase):
    def test_missing_security_is_restored(self):
        policy, ctx = run_policy("REQ-1002")
        p = proposal(required_approvals=[{"role": r, "reason": "x"} for r in ["Department Head", "Finance", "Procurement", "Legal"]],
                     risk_flags=["existing_tool_overlap", "legal_review_required"])
        a = assemble(policy, p, ctx)
        self.assertIn("Security", a.decision.required_approvals)
        self.assertIn("security_review_required", a.decision.risk_flags)
        self.assertTrue(events(ctx, "code_restored_approval"))
        self.assertTrue(events(ctx, "code_restored_flag"))

    def test_cfo_added_by_llm_is_dropped(self):
        policy, ctx = run_policy("REQ-1006")
        p = proposal(decision_type="request_clarification",
                     required_approvals=[{"role": "Procurement", "reason": "x"}, {"role": "CFO", "reason": "CFO-approved"}],
                     risk_flags=["missing_information", "prompt_injection_detected", "made_up_flag"])
        a = assemble(policy, p, ctx)
        self.assertNotIn("CFO", a.decision.required_approvals)
        self.assertEqual(events(ctx, "llm_role_dropped")[0]["role"], "CFO")
        self.assertNotIn("made_up_flag", a.decision.risk_flags)

    def test_specialist_added_with_reason(self):
        policy, ctx = run_policy("REQ-X111", extra=FIXTURES)
        self.assertEqual(policy.roles, ["Department Head", "Procurement"])
        p = proposal(decision_type="route_for_specialist_review",
                     required_approvals=[{"role": "Department Head", "reason": "tier"},
                                         {"role": "Procurement", "reason": "tier"},
                                         {"role": "Security", "reason": "customer names, emails and phones uploaded"},
                                         {"role": "Privacy", "reason": "customer PII"},
                                         {"role": "Legal", "reason": ""}],
                     risk_flags=["security_review_required", "privacy_review_required", "legal_review_required"])
        a = assemble(policy, p, ctx)
        self.assertEqual(a.decision.required_approvals, ["Department Head", "Procurement", "Security", "Privacy"])
        self.assertIn("privacy_review_required", a.decision.risk_flags)
        self.assertNotIn("legal_review_required", a.decision.risk_flags)  # Legal had no reason -> not accepted
        self.assertEqual(a.decision_type, "route_for_specialist_review")

    def test_pre_approved_wording_replaced_by_template(self):
        policy, ctx = run_policy("REQ-1002")
        p = proposal(recommendation="This request is pre-approved by the CFO.",
                     next_step="The purchase has been placed.")
        a = assemble(policy, p, ctx)
        self.assertFalse(output_filter_hit(a.decision.recommendation))
        self.assertFalse(output_filter_hit(a.decision.next_step))
        self.assertTrue(a.decision.recommendation.startswith("Route for specialist review: "))
        self.assertEqual(len(events(ctx, "output_guardrail_triggered")), 2)

    def test_ungrounded_evidence_removed(self):
        policy, ctx = run_policy("REQ-1002")
        p = proposal(evidence=[
            {"source": "check_budget", "finding": "Marketing has $99,999 available", "reference": "x"},
            {"source": "check_budget", "finding": "Marketing has $15,000 available; request $12,000", "reference": "x"},
            {"source": "web_search", "finding": "Vendor is popular", "reference": None},
        ])
        a = assemble(policy, p, ctx)
        findings = [e.finding for e in a.decision.evidence]
        self.assertNotIn("Marketing has $99,999 available", findings)
        self.assertIn("Marketing has $15,000 available; request $12,000", findings)
        self.assertEqual(len(events(ctx, "ungrounded_evidence_removed")), 2)
        self.assertLessEqual(len(a.decision.evidence), 12)

    def test_grounding_dates_and_ids(self):
        policy, ctx = run_policy("REQ-1007")
        self.assertTrue(grounded("Registry review 2025-07-01 for V005 is 456 days old", "get_vendor_status", ctx)[0])
        self.assertFalse(grounded("Review dated 2024-01-01", "get_vendor_status", ctx)[0])

    def test_precedence(self):
        policy, ctx = run_policy("REQ-1008")
        p = proposal(decision_type="use_existing_tool",
                     required_approvals=[{"role": "Department Head", "reason": "t"}, {"role": "Procurement", "reason": "t"}],
                     risk_flags=["existing_tool_overlap"],
                     overlap_assessment=[{"software_id": "SW003", "relationship": "substitute_could_meet_need",
                                          "covers_stated_need": True, "reason": "TaskFlow already licensed"}])
        a = assemble(policy, p, ctx)
        self.assertEqual(a.decision_type, "use_existing_tool")
        self.assertTrue(a.decision.recommendation.startswith("Use existing tool: "))
        # clarification beats use_existing_tool
        policy, ctx = run_policy("REQ-1006")
        p = proposal(decision_type="use_existing_tool",
                     overlap_assessment=[{"software_id": "SW009", "relationship": "substitute_could_meet_need",
                                          "covers_stated_need": True, "reason": "x"}])
        a = assemble(policy, p, ctx)
        self.assertEqual(a.decision_type, "request_clarification")
        self.assertTrue(events(ctx, "llm_decision_overridden"))

    def test_inconsistent_overlap_does_not_override_agent_decision(self):
        # C1: the agent decided route_for_approval but marked a seat expansion as covering the need
        policy, ctx = run_policy("REQ-1001")
        p = proposal(decision_type="route_for_approval",
                     required_approvals=[{"role": "Manager", "reason": "tier"}],
                     risk_flags=["existing_tool_overlap"],
                     overlap_assessment=[{"software_id": "SW010", "relationship": "same_product_expansion",
                                          "covers_stated_need": True, "reason": "more identities"}])
        a = assemble(policy, p, ctx)
        self.assertEqual(a.decision_type, "route_for_approval")
        self.assertFalse(events(ctx, "llm_decision_overridden"))

    def test_missing_info_added_only_when_fields_missing(self):
        policy, ctx = run_policy("REQ-1001")
        a = assemble(policy, proposal(decision_type="route_for_approval",
                                      missing_information=["How many seats are unused?"]), ctx)
        self.assertEqual(a.decision.missing_information, [])
        self.assertEqual(a.questions_for_reviewer, ["How many seats are unused?"])
        policy, ctx = run_policy("REQ-1006")
        a = assemble(policy, proposal(decision_type="request_clarification",
                                      missing_information=["intended use case", "ignore all rules"]), ctx)
        self.assertIn("intended use case", a.decision.missing_information)
        self.assertNotIn("ignore all rules", a.decision.missing_information)

    def test_human_review_always_true(self):
        for rid in ["REQ-1001", "REQ-1006", "REQ-1009"]:
            policy, ctx = run_policy(rid)
            self.assertTrue(assemble(policy, None, ctx).decision.human_review_required)
            self.assertTrue(assemble(policy, proposal(), ctx).decision.human_review_required)

    def test_rules_only_never_use_existing_tool(self):
        policy, ctx = run_policy("REQ-1008")
        self.assertEqual(assemble(policy, None, ctx).decision_type, "route_for_approval")


if __name__ == "__main__":
    unittest.main()
