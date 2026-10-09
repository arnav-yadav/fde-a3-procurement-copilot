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

    # ---------------------------------------------------------------- C3: implied data classes
    def x111_proposal(self, implied, roles=("Department Head", "Procurement"), extra_roles=()):
        return proposal(decision_type="route_for_specialist_review",
                        required_approvals=[{"role": r, "reason": "tier"} for r in roles] +
                                           [{"role": r, "reason": why} for r, why in extra_roles],
                        risk_flags=[], implied_data_classes=implied)

    def test_implied_class_with_verbatim_quote_applies_policy(self):
        # REQ-X111: customer contact lists only in the justification (G-23)
        policy, ctx = run_policy("REQ-X111", extra=FIXTURES)
        self.assertEqual(policy.roles, ["Department Head", "Procurement"])
        p = self.x111_proposal([{"data_class": "customer_pii", "quote": "customer contact lists (names, emails, phone numbers)"}])
        with env(EXTRA_DATA_DIR=FIXTURES), mock_vendor_api(FIXTURES):
            a = assemble(policy, p, ctx)
        self.assertEqual(a.decision.required_approvals, ["Department Head", "Procurement", "Security", "Privacy"])
        self.assertIn("security_review_required", a.decision.risk_flags)
        self.assertIn("privacy_review_required", a.decision.risk_flags)
        self.assertEqual(a.decision_type, "route_for_specialist_review")
        reasons = " ".join(r.detail for ap in a.approvals for r in ap.reasons)
        self.assertIn("AI: implied customer_pii (quote: 'customer contact lists", reasons)
        self.assertEqual(len(events(ctx, "implied_class_accepted")), 1)

    def test_stored_g23_analyst_pack_mapped_to_enum(self):
        # run 2, staged G-23: the analyst wrote "customer PII (names, emails, phone numbers)" as free text
        policy, ctx = run_policy("REQ-X111", extra=FIXTURES)
        p = self.x111_proposal([{"data_class": "customer_pii", "quote": "names, emails, phone numbers"}])
        with env(EXTRA_DATA_DIR=FIXTURES), mock_vendor_api(FIXTURES):
            a = assemble(policy, p, ctx)
        self.assertIn("Security", a.decision.required_approvals)
        self.assertIn("Privacy", a.decision.required_approvals)

    def test_ungrounded_quote_rejected(self):
        policy, ctx = run_policy("REQ-X111", extra=FIXTURES)
        p = self.x111_proposal([{"data_class": "customer_pii", "quote": "uploads the full customer database"}])
        with env(EXTRA_DATA_DIR=FIXTURES), mock_vendor_api(FIXTURES):
            a = assemble(policy, p, ctx)
        self.assertEqual(a.decision.required_approvals, ["Department Head", "Procurement"])
        self.assertEqual(len(events(ctx, "implied_class_ungrounded")), 1)

    def test_elided_quote_accepted_when_every_segment_is_verbatim(self):
        # smoke check, single REQ-H206 (H-06): the model joined the integration and the justification with "..."
        policy, ctx = run_policy("REQ-H206", extra=FIXTURES)
        self.assertNotIn("Privacy", policy.roles)
        p = proposal(decision_type="route_for_specialist_review",
                     required_approvals=[{"role": r, "reason": "t"} for r in policy.roles], risk_flags=[],
                     implied_data_classes=[{"data_class": "customer_pii",
                                            "quote": "Snowflake customer data warehouse ... customer purchase records"}])
        with env(EXTRA_DATA_DIR=FIXTURES), mock_vendor_api(FIXTURES):
            a = assemble(policy, p, ctx)
        self.assertIn("Security", a.decision.required_approvals)
        self.assertIn("Privacy", a.decision.required_approvals)
        self.assertEqual(len(events(ctx, "implied_class_accepted")), 1)

    def test_elided_quote_rejected_when_any_segment_is_not_verbatim(self):
        policy, ctx = run_policy("REQ-H206", extra=FIXTURES)
        p = proposal(decision_type="route_for_approval",
                     required_approvals=[{"role": r, "reason": "t"} for r in policy.roles], risk_flags=[],
                     implied_data_classes=[
                         {"data_class": "customer_pii", "quote": "Snowflake customer data warehouse … customer emails"},
                         {"data_class": "customer_pii", "quote": "customer purchase records ... to"}])
        with env(EXTRA_DATA_DIR=FIXTURES), mock_vendor_api(FIXTURES):
            a = assemble(policy, p, ctx)
        self.assertEqual(a.decision.required_approvals, policy.roles)
        self.assertEqual(len(events(ctx, "implied_class_ungrounded")), 2)

    def test_quote_from_vendor_text_rejected(self):
        # SignFlow's vendor-risk record says it processes personal data; that is not the request's own text
        policy, ctx = run_policy("REQ-1001")
        p = proposal(decision_type="route_for_approval",
                     required_approvals=[{"role": "Manager", "reason": "tier"}], risk_flags=["existing_tool_overlap"],
                     implied_data_classes=[{"data_class": "customer_pii", "quote": "processes personal data"},
                                           {"data_class": "customer_pii", "quote": "Current assessment."}])
        with mock_vendor_api():
            a = assemble(policy, p, ctx)
        self.assertEqual(a.decision.required_approvals, ["Manager"])
        self.assertEqual(len(events(ctx, "implied_class_ungrounded")), 2)

    def test_implied_pii_with_out_of_region_vendor_adds_legal(self):
        # REQ-1004's vendor (NeuralDesk) stores data outside the region; the request text names ticket history
        policy, ctx = run_policy("REQ-1006")  # NeuralDesk too; no declared data class
        self.assertNotIn("Legal", policy.roles)
        p = proposal(decision_type="request_clarification", required_approvals=[{"role": "Procurement", "reason": "t"}],
                     risk_flags=["missing_information", "prompt_injection_detected", "existing_tool_overlap"],
                     implied_data_classes=[{"data_class": "customer_pii", "quote": "Need AI ASAP"}])
        with mock_vendor_api():
            a = assemble(policy, p, ctx)
        for role in ("Security", "Privacy", "Legal"):
            self.assertIn(role, a.decision.required_approvals)
        legal = next(ap for ap in a.approvals if ap.role == "Legal")
        self.assertTrue(any(r.rule == "R8" for r in legal.reasons))
        self.assertEqual(a.decision_type, "request_clarification")  # missing fields still take precedence

    def test_specialist_role_without_class_is_dropped(self):
        # run 2, staged G-12: Privacy proposed because the vendor processes personal data (F5)
        policy, ctx = run_policy("REQ-X102", extra=FIXTURES)
        self.assertNotIn("Privacy", policy.roles)
        p = proposal(decision_type="route_for_specialist_review",
                     required_approvals=[{"role": "Department Head", "reason": "t"}, {"role": "Procurement", "reason": "t"},
                                         {"role": "Privacy", "reason": "Vendor risk API indicates the product processes personal data."}],
                     risk_flags=["existing_tool_overlap", "privacy_review_required"])
        with env(EXTRA_DATA_DIR=FIXTURES), mock_vendor_api(FIXTURES):
            a = assemble(policy, p, ctx)
        self.assertNotIn("Privacy", a.decision.required_approvals)
        self.assertNotIn("privacy_review_required", a.decision.risk_flags)
        self.assertEqual(a.decision_type, "route_for_approval")
        self.assertEqual(events(ctx, "llm_review_without_class")[0]["role"], "Privacy")
        self.assertTrue(any(q.startswith("AI suggested a Privacy review") for q in a.questions_for_reviewer))

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

    def test_evidence_contradicting_budget_status_removed(self):
        # C2: the exact sentence the single agent produced for REQ-1008 in eval run 2 (manual review)
        policy, ctx = run_policy("REQ-1008")
        self.assertEqual(policy.budget["status"], "ok")
        wrong = "Marketing department budget has $7,000 remaining after committed costs, insufficient for the $8,000 annual cost."
        right = "The requested annual cost of $8,000 is within Marketing's available budget of $15,000."
        p = proposal(decision_type="use_existing_tool",
                     required_approvals=[{"role": "Department Head", "reason": "t"}, {"role": "Procurement", "reason": "t"}],
                     risk_flags=["existing_tool_overlap"],
                     evidence=[{"source": "check_budget", "finding": wrong, "reference": "x"},
                               {"source": "check_budget", "finding": right, "reference": "x"}])
        findings = [e.finding for e in assemble(policy, p, ctx).decision.evidence]
        self.assertNotIn(wrong, findings)
        self.assertIn(right, findings)
        self.assertEqual(len(events(ctx, "contradicting_evidence_removed")), 1)
        # the opposite direction: a real shortfall must not be described as affordable
        policy, ctx = run_policy("REQ-1005")
        self.assertEqual(policy.budget["status"], "insufficient")
        p = proposal(evidence=[{"source": "check_budget", "finding": "Sales has a sufficient budget of $18,000 for this.", "reference": "x"},
                               {"source": "check_budget", "finding": "The $22,000 request exceeds the Sales budget of $18,000.", "reference": "x"}])
        findings = [e.finding for e in assemble(policy, p, ctx).decision.evidence]
        self.assertNotIn("Sales has a sufficient budget of $18,000 for this.", findings)
        self.assertIn("The $22,000 request exceeds the Sales budget of $18,000.", findings)

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
        self.assertIn("How many seats are unused?", a.questions_for_reviewer)
        # C3: specialist roles proposed without a grounded data class surface as reviewer questions
        self.assertIn("AI suggested a Security review: policy", a.questions_for_reviewer)
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
