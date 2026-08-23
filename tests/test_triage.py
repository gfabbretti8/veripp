"""The triage loop: LLM proposes, solver disposes.

Uses a scripted LLM so the loop's mechanics are tested deterministically;
the live-LLM behaviour is measured separately by benchmarks/eval_triage.py.
"""

from pathlib import Path

import pytest

from veripp.agent import Budget, verify_with_agent
from veripp.esbmc import Outcome, VerifyConfig, VerifyResult
from veripp.harness import HarnessOptions, generate
from veripp.llm import LLMError, NullLLM, TriageContext
from veripp.paths import contracts_include_dir
from veripp.triage import TargetInfo, build_context, find_call_sites, triage_counterexample

SRC = """\
#include "veripp/contracts.hpp"

int scale(int value, int factor) {
    return value * factor;
}

int use_a(void) { return scale(3, 2); }
int use_b(void) { return scale(10, 4); }
"""


class ScriptedLLM:
    """Answers from a script; records what it was asked."""

    def __init__(self, kind="missing_assumption", precondition=None, fail=False):
        self.kind = kind
        self.precondition = precondition
        self.fail = fail
        self.contexts: list[TriageContext] = []

    def classify(self, context):
        self.contexts.append(context)
        if self.fail:
            raise LLMError("scripted outage")
        return self.kind

    def explain(self, context):
        return "scripted explanation"

    def propose_precondition(self, context):
        return self.precondition

    def propose_invariants(self, source, result):
        return None

    def propose_frontend_fix(self, source, result):
        return None


@pytest.fixture
def target(tmp_path):
    src = tmp_path / "scale.cpp"
    src.write_text(SRC)
    return TargetInfo(source=src, function="scale")


class TestContext:
    def test_call_sites_found_and_definition_excluded(self, target):
        sites = find_call_sites(target.source.read_text(), "scale")
        assert len(sites) == 2
        assert any("scale(3, 2)" in s for s in sites)
        assert not any("int scale(int value" in s for s in sites)

    def test_context_carries_what_the_pilot_needed(self, target):
        result = VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig(), raw_output="tail")
        harness = generate(target.source, "scale")
        path = harness.write(target.source.parent)
        ctx = build_context(target, path, result)
        assert ctx.parameters == ["value", "factor"]
        assert "int scale(int value, int factor)" in ctx.signature
        assert len(ctx.call_sites) == 2
        assert "VERIPP_NONDET_INT" in ctx.harness_code
        rendered = ctx.render()
        assert "Call sites" in rendered and "scale(10, 4)" in rendered


class TestTriage:
    def _result(self):
        return VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig())

    def test_missing_assumption_carries_the_proposal(self, target, tmp_path):
        llm = ScriptedLLM(precondition="factor > 0")
        d = triage_counterexample(target, tmp_path / "h.cpp", self._result(), llm)
        assert d.kind == "missing_assumption"
        assert d.proposed_precondition == "factor > 0"

    def test_llm_outage_degrades_conservatively(self, target, tmp_path):
        d = triage_counterexample(
            target, tmp_path / "h.cpp", self._result(), ScriptedLLM(fail=True)
        )
        assert d.kind == "real_bug"  # never hide a finding because the LLM is down
        assert d.llm_error
        assert "Triage unavailable" in d.explanation

    def test_null_llm_is_the_offline_default(self, target, tmp_path):
        d = triage_counterexample(target, tmp_path / "h.cpp", self._result(), NullLLM())
        assert d.kind == "real_bug"
        assert d.proposed_precondition is None


@pytest.mark.esbmc
class TestProposeCheckLoop:
    """End-to-end against the real solver: the loop's whole point."""

    def _run(self, target, llm):
        harness = generate(target.source, target.function)
        path = harness.write(target.source.parent)
        config = VerifyConfig(
            timeout_s=90, include_dirs=[contracts_include_dir(), target.source.parent]
        )
        return verify_with_agent(
            path, config, llm=llm, budget=Budget(),
            assumptions=harness.assumptions, harness=path, target=target,
        )

    def test_solver_accepts_a_good_precondition(self, target):
        # scale() overflows on nondet ints; bounding both factors fixes it.
        llm = ScriptedLLM(
            precondition="value > -1000 && value < 1000 && factor > -1000 && factor < 1000"
        )
        report = self._run(target, llm)
        assert report.final.outcome is Outcome.VERIFIED
        assert report.accepted_preconditions == [llm.precondition]
        assert "CONDITIONAL" in report.summary()
        assert any("PROPOSED precondition" in a for a in report.assumptions)
        # The triage saw the real call sites, not just the trace.
        assert llm.contexts and len(llm.contexts[0].call_sites) == 2

    def test_solver_rejects_a_bad_precondition(self, target):
        # Nonempty but insufficient: the overflow survives, so the loop must
        # stop at the budget and report a counterexample, not "verified".
        llm = ScriptedLLM(precondition="factor != 0")
        report = self._run(target, llm)
        assert report.final.outcome is Outcome.COUNTEREXAMPLE
        assert report.accepted_preconditions == []

    def test_out_of_scope_proposal_is_refused_not_verified(self, target):
        llm = ScriptedLLM(precondition="g_limit > factor")  # not a parameter
        report = self._run(target, llm)
        assert report.final.outcome is Outcome.COUNTEREXAMPLE
        assert report.accepted_preconditions == []

    def test_real_bug_classification_stops_the_loop(self, target):
        llm = ScriptedLLM(kind="real_bug", precondition="factor == 1")
        report = self._run(target, llm)
        assert report.final.outcome is Outcome.COUNTEREXAMPLE
        assert report.diagnosis.kind == "real_bug"
        assert len(report.attempts) == 1  # no regeneration happened
