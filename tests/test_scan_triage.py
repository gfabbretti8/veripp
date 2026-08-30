"""LLM triage inside `veripp scan`.

The mechanical pass stays LLM-free; counterexamples then go through the same
agent loop `verify` uses. Nothing in the report changes on a model's word
alone: an outcome moves to "preconditioned" only when the solver verified
the function under the proposal and the vacuity probe confirmed something
was actually checked.
"""

from pathlib import Path

import pytest

from veripp.esbmc import VerifyConfig
from veripp.harness import HarnessOptions
from veripp.llm import LLMError, NullLLM
from veripp.paths import contracts_include_dir
from veripp.scan import scan

# One real bug for triage to chew on, one caller showing the precondition
# real callers respect, and one function that proves outright -- which must
# never cost an LLM call.
SOURCE = """\
int div10(int d) { return 10 / d; }
int use_it(void) { return div10(2) + div10(5); }
"""


class ScriptedLLM:
    """Answers from a script; counts what it was asked."""

    def __init__(self, kind="missing_assumption", precondition=None, fail=False):
        self.kind = kind
        self.precondition = precondition
        self.fail = fail
        self.classified: list[str] = []

    def classify(self, context):
        self.classified.append(context.function)
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
def src(tmp_path):
    p = tmp_path / "d.c"
    p.write_text(SOURCE, encoding="utf-8")
    return p


def _scan(src, **kw):
    config = VerifyConfig(
        timeout_s=90,
        include_dirs=[contracts_include_dir(), src.parent],
    )
    return scan(src, config, HarnessOptions(), jobs=2, **kw)


@pytest.mark.esbmc
class TestTriagePass:
    def test_a_solver_accepted_precondition_moves_the_outcome(self, src):
        llm = ScriptedLLM(precondition="d != 0")
        report = _scan(src, llm=llm)
        assert report.triaged
        (pre,) = report.preconditioned
        assert pre.name == "div10"
        assert pre.preconditions == ["d != 0"]
        assert pre.triage_kind == "missing_assumption"
        # Moved, not duplicated -- and never into the unconditional bucket.
        assert "div10" not in {r.name for r in report.counterexamples}
        assert "div10" not in {r.name for r in report.proved}
        assert "PRECONDITIONED" in report.summary()
        assert "d != 0" in report.summary()

    def test_the_proved_never_cost_an_llm_call(self, src):
        llm = ScriptedLLM(precondition="d != 0")
        _scan(src, llm=llm)
        assert "use_it" not in llm.classified

    def test_a_vacuous_proposal_does_not_upgrade(self, src):
        # Satisfies the solver trivially by making the call unreachable; the
        # vacuity probe must catch it and the counterexample must survive.
        llm = ScriptedLLM(precondition="d > 100 && d < 0")
        report = _scan(src, llm=llm)
        assert not report.preconditioned
        assert "div10" in {r.name for r in report.counterexamples}

    def test_a_real_bug_verdict_is_a_label_not_a_change(self, src):
        llm = ScriptedLLM(kind="real_bug", precondition=None)
        report = _scan(src, llm=llm)
        (ce,) = [r for r in report.counterexamples if r.name == "div10"]
        assert ce.triage_kind == "real_bug"
        assert not report.preconditioned
        assert "triage: real bug" in report.summary()

    def test_an_llm_outage_leaves_the_result_untriaged(self, src):
        llm = ScriptedLLM(fail=True)
        report = _scan(src, llm=llm)
        (ce,) = [r for r in report.counterexamples if r.name == "div10"]
        assert ce.triage_error
        assert ce.triage_kind is None
        assert "triage unavailable" in report.summary()

    def test_no_llm_means_the_old_report_exactly(self, src):
        report = _scan(src, llm=NullLLM())
        assert not report.triaged
        assert "div10" in {r.name for r in report.counterexamples}


# 200 iterations: past the mechanical pass's widened bound (32 * 4) but
# within the agent ladder's next step (32 * 4 * 4), so it lands exactly in
# the retry pass.
RETRY_SOURCE = """\
int deep_loop(void) {
    int s = 0;
    for (int i = 0; i < 200; i++) s += 1;
    return s;
}
"""


@pytest.mark.esbmc
class TestRetryPass:
    def test_an_inconclusive_settles_on_the_wider_ladder(self, tmp_path):
        p = tmp_path / "r.c"
        p.write_text(RETRY_SOURCE, encoding="utf-8")
        report = _scan(p)
        assert report.retried == 1
        assert report.settled == 1
        assert "deep_loop" in {r.name for r in report.proved}
        assert not report.inconclusive
        assert "settled on a second, harder attempt" in report.summary()

    def test_budget_zero_means_the_old_report_exactly(self, tmp_path):
        p = tmp_path / "r.c"
        p.write_text(RETRY_SOURCE, encoding="utf-8")
        report = _scan(p, retry_budget=0)
        assert report.retried == 0
        assert "deep_loop" in {r.name for r in report.inconclusive}

    def test_the_retry_needs_no_llm(self, tmp_path):
        p = tmp_path / "r.c"
        p.write_text(RETRY_SOURCE, encoding="utf-8")
        report = _scan(p, llm=NullLLM())
        assert report.settled == 1
        assert not report.triaged
