"""Optional LangSmith tracing: off by default, a pure pass-through unless LANGSMITH_TRACING=true and a key is set.

A stand-in `langsmith` module records what would be sent; the real package is not needed for these tests."""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from src import tracing
from src.solution import handle_request_with_trace
from src.tools import RunContext
from tests.helpers import env, mock_vendor_api

OFF = {"LANGSMITH_TRACING": None, "LANGSMITH_API_KEY": None}
ON = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "test-key-not-real"}


def fake_langsmith():
    ls = types.ModuleType("langsmith")
    wrappers = types.ModuleType("langsmith.wrappers")
    ls.runs = []

    def traceable(**opts):
        def deco(fn):
            def run(*args, **kwargs):
                ls.runs.append({**opts, "inputs": opts["process_inputs"](dict(enumerate(args)) | kwargs)})
                return fn(*args, **kwargs)
            return run
        return deco

    ls.traceable = mock.Mock(side_effect=traceable)
    wrappers.wrap_openai = mock.Mock(side_effect=lambda client: ("wrapped", client))
    ls.wrappers = wrappers
    return ls, {"langsmith": ls, "langsmith.wrappers": wrappers}


def decide(arch: str = "single"):
    with mock_vendor_api():
        decision, trace = handle_request_with_trace("REQ-1008", arch, mode="rules_only")
    return decision.model_dump(), {k: trace[k] for k in ("decision_type", "approvals", "tool_log", "steps")}


class TracingTests(unittest.TestCase):
    def test_off_by_default_is_a_pass_through(self):
        ls, modules = fake_langsmith()
        client = object()
        with env(**OFF), mock.patch.dict(sys.modules, modules):
            self.assertFalse(tracing.tracing_enabled())
            self.assertIs(tracing.maybe_wrap(client), client)
            decision, trace = decide()
        ls.traceable.assert_not_called()
        ls.wrappers.wrap_openai.assert_not_called()
        self.assertEqual(ls.runs, [])
        self.assertEqual(trace["decision_type"], "route_for_approval")

    def test_tracing_true_without_key_is_a_pass_through(self):
        ls, modules = fake_langsmith()
        with env(LANGSMITH_TRACING="true", LANGSMITH_API_KEY=None), mock.patch.dict(sys.modules, modules):
            self.assertFalse(tracing.tracing_enabled())
            decide()
        ls.traceable.assert_not_called()

    def test_missing_package_is_a_pass_through(self):
        client = object()
        with env(**ON), mock.patch.dict(sys.modules, {"langsmith": None, "langsmith.wrappers": None}):
            self.assertIs(tracing.maybe_wrap(client), client)
            self.assertEqual(tracing.traced("x")(lambda a: a + 1)(1), 2)

    def test_on_traces_runs_and_outputs_are_identical(self):
        baseline = decide()
        ls, modules = fake_langsmith()
        with env(**ON, EVAL_CASE_ID="G-08"), mock.patch.dict(sys.modules, modules):
            self.assertTrue(tracing.tracing_enabled())
            self.assertEqual(tracing.maybe_wrap("client"), ("wrapped", "client"))
            traced = decide()
        strip = lambda t: {**t, "tool_log": [{k: v for k, v in e.items() if k != "duration_ms"} for e in t["tool_log"]],
                           "steps": [{**s, "ms": None} for s in t["steps"]]}
        self.assertEqual(traced[0], baseline[0])
        self.assertEqual(strip(traced[1]), strip(baseline[1]))
        names = [r["name"] for r in ls.runs]
        self.assertEqual(names[0], "handle_request")
        self.assertIn("policy_engine.evaluate", names)
        self.assertIn("check_budget", names)  # tool runs are named after the tool
        top = ls.runs[0]
        self.assertEqual(top["run_type"], "chain")
        self.assertEqual({k: top["metadata"].get(k) for k in ("request_id", "architecture", "mode", "case_id")},
                         {"request_id": "REQ-1008", "architecture": "single", "mode": "rules_only", "case_id": "G-08"})
        tool_run = next(r for r in ls.runs if r["name"] == "check_budget")
        self.assertEqual(tool_run["run_type"], "tool")

    def test_run_context_is_not_sent_as_an_input(self):
        ls, modules = fake_langsmith()
        with env(**ON), mock.patch.dict(sys.modules, modules):
            tracing.traced("stage")(lambda ctx, stage: stage)(ctx=RunContext("REQ-1"), stage="analyst")
        self.assertEqual(ls.runs[0]["inputs"], {"stage": "analyst"})


if __name__ == "__main__":
    unittest.main()
