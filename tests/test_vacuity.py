"""Vacuity: an unreachable harness proves everything and means nothing.

ESBMC answers "does the property hold under these assumptions". It cannot
notice that the assumptions are unsatisfiable, and neither can the model that
proposed them -- a weak model fails toward over-constraining, and the solver
applauds. This is the mechanical guard against that.
"""

import json

import pytest

from veripp import agent
from veripp.agent import REACHABLE, UNCONFIRMED, VACUOUS, probe_reachability
from veripp.cli import EXIT_INCONCLUSIVE, EXIT_VERIFIED, main
from veripp.esbmc import Outcome, SourceLoc, VerifyConfig, VerifyResult, ViolatedProperty
from veripp.harness import (
    REACHABILITY_MESSAGE,
    REACHABILITY_PROBE_MACRO,
    generate,
    has_reachability_marker,
    reachability_variant,
)
from veripp.paths import contracts_include_dir

SOURCE = """\
#include "veripp/contracts.hpp"
int div_it(int a, int b) { return a / b; }
"""


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "v.cpp"
    p.write_text(SOURCE, encoding="utf-8")
    return p


def test_probe_asserts_falsehood_before_returning():
    code = "int main() {\n    (void)f(1);\n    return 0;\n}\n"
    probed = reachability_variant(code)
    assert "__ESBMC_assert(0," in probed
    assert probed.index("__ESBMC_assert") < probed.index("return 0;")


def test_a_main_without_a_return_is_still_probed():
    # This used to append `static_assert(true, ...)` -- C++ only, and an
    # assertion that checks nothing, so the probe verified and every such
    # file read as VACUOUS.
    probed = reachability_variant("int main() {}\n")
    assert "static_assert" not in probed
    assert probed.index("__ESBMC_assert(0,") < probed.rindex("}")


def test_the_probe_never_becomes_the_body_of_an_if():
    probed = reachability_variant("int main() {\n  if (a) return 1;\n  g();\n}\n")
    assert "if (a) return 1;" in probed
    assert probed.index("g();") < probed.index("__ESBMC_assert")


def test_nowhere_to_probe_is_not_confirmed():
    assert reachability_variant("int f(void);\n") is None


def test_generated_harnesses_carry_the_probe_point(tmp_path):
    src = tmp_path / "g.c"
    src.write_text("int g(int x) { return x; }\n", encoding="utf-8")
    code = generate(src, "g").code
    assert has_reachability_marker(code)
    assert code.index("VERIPP_REACHED();") < code.rindex("return 0;")


def test_the_header_fails_the_probe_with_the_message_veripp_looks_for():
    header = (contracts_include_dir() / "veripp" / "contracts.hpp").read_text(
        encoding="utf-8"
    )
    assert f"defined({REACHABILITY_PROBE_MACRO})" in header
    assert f'"{REACHABILITY_MESSAGE}"' in header


class TestProbeOutcomes:
    """Only the probe's own assertion failing shows the harness can run."""

    @staticmethod
    def _probe(monkeypatch, tmp_path, *outcomes):
        harness = tmp_path / "veripp_harness_f.c"
        harness.write_text(
            "int main() {\n    VERIPP_REACHED();\n    return 0;\n}\n",
            encoding="utf-8",
        )
        seen: list[VerifyConfig] = []
        answers = iter(outcomes)

        def run(path, config, esbmc_bin=None):
            seen.append(config)
            outcome, message = next(answers)
            props = [ViolatedProperty(SourceLoc("x.c", 1), message)] if message else []
            return VerifyResult(outcome=outcome, config=config, properties=props)

        monkeypatch.setattr(agent, "run", run)
        state, note = probe_reachability(harness, VerifyConfig())
        assert all(REACHABILITY_PROBE_MACRO in c.defines for c in seen)
        return state, note, seen

    def test_the_marker_failing_means_reachable(self, monkeypatch, tmp_path):
        state, _, _ = self._probe(
            monkeypatch, tmp_path, (Outcome.COUNTEREXAMPLE, REACHABILITY_MESSAGE)
        )
        assert state == REACHABLE

    def test_verifying_again_means_vacuous(self, monkeypatch, tmp_path):
        state, _, _ = self._probe(monkeypatch, tmp_path, (Outcome.VERIFIED, None))
        assert state == VACUOUS

    @pytest.mark.parametrize("outcome", [
        Outcome.TIMEOUT, Outcome.TOOL_ERROR, Outcome.UNKNOWN,
        Outcome.UNWIND_LIMIT, Outcome.PARSE_ERROR,
    ])
    def test_anything_else_is_not_taken_as_reachable(self, monkeypatch, tmp_path, outcome):
        # The old probe treated every non-VERIFIED answer as reachable, so a
        # probe that timed out confirmed the proof it was meant to question.
        state, note, _ = self._probe(monkeypatch, tmp_path, (outcome, None))
        assert state == UNCONFIRMED
        assert outcome.value in note

    def test_an_artifact_in_the_way_is_looked_past(self, monkeypatch, tmp_path):
        state, _, seen = self._probe(
            monkeypatch, tmp_path,
            (Outcome.COUNTEREXAMPLE, "free() of non-dynamic memory"),
            (Outcome.COUNTEREXAMPLE, REACHABILITY_MESSAGE),
        )
        assert state == REACHABLE
        assert seen[-1].multi_property

    def test_another_failure_alone_is_not_the_probe(self, monkeypatch, tmp_path):
        state, _, _ = self._probe(
            monkeypatch, tmp_path,
            (Outcome.COUNTEREXAMPLE, "division by zero"),
            (Outcome.COUNTEREXAMPLE, "division by zero"),
        )
        assert state == UNCONFIRMED


def test_an_unconfirmed_proof_is_not_a_pass(monkeypatch, capsys, src, tmp_path):
    """The probe timing out used to count as "reachable"."""

    def run(path, config, esbmc_bin=None):
        if REACHABILITY_PROBE_MACRO in config.defines:
            return VerifyResult(outcome=Outcome.TIMEOUT, config=config,
                                error="esbmc exceeded the 120s per-attempt timeout")
        return VerifyResult(outcome=Outcome.VERIFIED, config=config)

    monkeypatch.setattr(agent, "run", run)
    out = tmp_path / "r.json"
    code = main(["verify", str(src), "--function", "div_it", "--no-llm",
                 "--assume", "b > 0", "--json-out", str(out)])
    assert code == EXIT_INCONCLUSIVE
    printed = capsys.readouterr().out
    assert "UNCONFIRMED" in printed and "NOT a proof" in printed
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["reachability"] == "unconfirmed"
    assert report["outcome"] == "verified"


@pytest.mark.esbmc
class TestVacuityEndToEnd:
    def _run(self, capsys, src, *assumes):
        argv = ["verify", str(src), "--function", "div_it", "--no-llm", "--timeout", "120"]
        for a in assumes:
            argv += ["--assume", a]
        code = main(argv)
        return code, capsys.readouterr().out

    def test_satisfiable_preconditions_give_a_real_proof(self, capsys, src):
        code, out = self._run(capsys, src, "b > 0", "a > -100 && a < 100")
        assert code == EXIT_VERIFIED
        assert "VACUOUS" not in out
        assert "requires b > 0" in out

    def test_contradictory_preconditions_are_not_a_pass(self, capsys, src):
        code, out = self._run(capsys, src, "b > 0 && b < 0")
        assert code == EXIT_INCONCLUSIVE
        assert "VACUOUS" in out
        assert "NOT a proof" in out

    def test_a_harness_with_no_assumptions_is_probed_and_reachable(self, capsys, tmp_path):
        p = tmp_path / "n.cpp"
        p.write_text('#include "veripp/contracts.hpp"\nint id(int x) { return x; }\n', encoding="utf-8")
        code = main(["verify", str(p), "--function", "id", "--no-llm", "--timeout", "120"])
        assert code == EXIT_VERIFIED
        out = capsys.readouterr().out
        assert "VACUOUS" not in out and "UNCONFIRMED" not in out

    def test_an_assumption_the_harness_cannot_see_is_still_caught(self, capsys, tmp_path):
        # The precondition names a global, so it stays in the body rather than
        # being hoisted into the harness -- whose text then holds no
        # assumption at all. The probe used to be skipped on that basis, and
        # a function that divides by zero came back VERIFIED.
        p = tmp_path / "g.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "static int limit = 4;\n"
            "int check(int x) {\n"
            "    VERIPP_REQUIRES(limit > 100);\n"
            "    return 10 / x;\n"
            "}\n",
            encoding="utf-8",
        )
        code = main(["verify", str(p), "--function", "check", "--no-llm", "--timeout", "120"])
        assert code == EXIT_INCONCLUSIVE
        assert "VACUOUS" in capsys.readouterr().out

    def test_the_bound_veripp_adds_is_named_as_a_suspect(self, capsys, tmp_path):
        p = tmp_path / "s.c"
        p.write_text(SUM_SOURCE, encoding="utf-8")
        code = main(["verify", str(p), "--function", "sum", "--no-llm", "--timeout", "120"])
        assert code == EXIT_INCONCLUSIVE
        out = capsys.readouterr().out
        assert "VACUOUS" in out and "--max-array-len" in out

    def test_a_files_own_main_is_probed_outside_its_directory(self, capsys, tmp_path):
        p = tmp_path / "own.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "int main(void) {\n"
            "    int n = VERIPP_NONDET_INT();\n"
            "    VERIPP_ASSUME(n > 0 && n < 0);\n"
            "    return 10 / n;\n"
            "}\n",
            encoding="utf-8",
        )
        code = main(["verify", str(p), "--no-llm", "--timeout", "120"])
        assert code == EXIT_INCONCLUSIVE
        assert "VACUOUS" in capsys.readouterr().out
        # The probe used to be written beside the file -- and one was
        # committed to examples/ that way.
        assert sorted(q.name for q in tmp_path.iterdir()) == ["own.c"]


# The harness bounds `n` at --max-array-len (4) because it is the length of
# `a`; the function's own precondition asks for at least 8.
SUM_SOURCE = """\
#include "veripp/contracts.hpp"
int sum(const int *a, int n) {
    VERIPP_REQUIRES(n >= 8);
    int s = 0;
    for (int i = 0; i < n; i++) s += a[i];
    return s;
}
"""


@pytest.mark.esbmc
def test_scan_runs_the_probe_verify_runs(tmp_path):
    """The same function read PROVED in `scan` and VACUOUS in `verify`."""
    from veripp.harness import HarnessOptions
    from veripp.scan import scan

    p = tmp_path / "s.c"
    p.write_text(SUM_SOURCE, encoding="utf-8")
    config = VerifyConfig(timeout_s=120, include_dirs=[contracts_include_dir(), tmp_path])
    report = scan(p, config, HarnessOptions(), jobs=1, retry_budget=0)
    assert "sum" not in {r.name for r in report.proved}
    (hollow,) = report.unproved
    assert hollow.outcome == "vacuous"
    assert "--max-array-len" in hollow.detail
    summary = report.summary()
    assert "not shown to have checked anything" in summary
