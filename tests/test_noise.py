"""Not every failure is a finding.

A generated harness simplifies things, and some failures follow from those
simplifications alone. Reporting them as bugs is how a verifier teaches people
to ignore it -- on lodepng they outnumbered real leads. These are the two
mechanical reductions; neither needs a model.
"""

from pathlib import Path

import pytest

from veripp.esbmc import (
    Outcome,
    SourceLoc,
    VerifyConfig,
    VerifyResult,
    ViolatedProperty,
)
from veripp.harness import HarnessOptions, generate
from veripp.triage import Diagnosis, TargetInfo, mechanical_artifact, triage_counterexample

SOURCE = """\
#include "veripp/contracts.hpp"

/* An output parameter: seven elements, and no length argument to say so. */
static void getpasses(unsigned* passw, unsigned* passh) {
    for (unsigned i = 0; i < 7; i++) { passw[i] = i; passh[i] = i * 2; }
}

/* Genuinely one element. */
static void set_one(int* out) { *out = 5; }

/* Literal indices decide the size. */
static void literals(int* v) { v[0] = 1; v[3] = 2; }

/* Non-strict bound reaches one further. */
static void inclusive(unsigned* p) { for (unsigned i = 0; i <= 7; i++) p[i] = i; }
"""


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "s.c"
    p.write_text(SOURCE, encoding="utf-8")
    return p


def _buffer_line(code: str, name: str) -> str:
    return next(l.strip() for l in code.splitlines() if f"{name}_buf[" in l or f"{name}_obj " in l)


class TestOutputParameterSizing:
    """A lone pointer is not necessarily a pointer to one element."""

    def test_loop_bound_sizes_the_buffer(self, src):
        code = generate(src, "getpasses").code
        assert _buffer_line(code, "passw") == "unsigned passw_buf[7];"
        assert _buffer_line(code, "passh") == "unsigned passh_buf[7];"

    def test_a_strict_bound_does_not_over_allocate(self, src):
        # `i < 7` reaches index 6. Handing out more memory than a caller has
        # would hide a real overflow.
        assert "[7]" in _buffer_line(generate(src, "getpasses").code, "passw")

    def test_a_non_strict_bound_reaches_one_further(self, src):
        assert "[8]" in _buffer_line(generate(src, "inclusive").code, "p")

    def test_literal_indices_decide_the_size(self, src):
        assert "[4]" in _buffer_line(generate(src, "literals").code, "v")

    def test_a_genuine_single_object_stays_single(self, src):
        code = generate(src, "set_one").code
        assert "out_obj" in code and "out_buf" not in code

    def test_the_extent_is_disclosed(self, src):
        harness = generate(src, "getpasses")
        assert any("at least 7 valid" in a for a in harness.assumptions)


class TestMechanicalArtifacts:
    def _result(self, description, file="lib.c"):
        return VerifyResult(
            Outcome.COUNTEREXAMPLE, VerifyConfig(),
            properties=[ViolatedProperty(
                loc=SourceLoc(file=file, line=1), description=description
            )],
        )

    def test_freeing_harness_memory_is_not_a_finding(self):
        why = mechanical_artifact(
            self._result("dereference failure: free() of non-dynamic memory"),
            Path("/tmp/veripp_harness_f.cpp"),
        )
        assert why and "stack or static storage" in why

    def test_a_failure_inside_the_harness_is_about_the_harness(self):
        harness = Path("/tmp/veripp_harness_f.cpp")
        why = mechanical_artifact(
            self._result("array bounds violated", str(harness)), harness
        )
        assert why and "generated harness itself" in why

    def test_a_real_property_is_left_alone(self):
        assert mechanical_artifact(
            self._result("division by zero"), Path("/tmp/veripp_harness_f.cpp")
        ) is None

    def test_triage_short_circuits_without_calling_a_model(self, tmp_path):
        class Explode:
            def classify(self, ctx):
                raise AssertionError("a model must not be consulted for this")

            explain = propose_precondition = classify

        harness = tmp_path / "veripp_harness_f.cpp"
        harness.write_text("int main() { return 0; }", encoding="utf-8")
        diagnosis = triage_counterexample(
            None, harness,
            self._result("dereference failure: free() of non-dynamic memory"),
            Explode(),
        )
        assert isinstance(diagnosis, Diagnosis)
        assert diagnosis.kind == "harness_issue"
        assert "harness artifact" in diagnosis.explanation


class TestNullSourceNote:
    """A null the harness introduced should be labelled as possibly ours.

    Pointer fields become null when the type cannot be constructed or the
    struct depth bound is reached. Seven of giflib's twelve leads were NULL
    dereferences of exactly such a field. Calling them artifacts would be
    wrong -- an unchecked pointer is a real bug class -- but the reader has
    to know which nulls veripp put there.
    """

    def _report(self, assumptions, description="dereference failure: NULL pointer"):
        from veripp.agent import AgentReport

        return AgentReport(
            final=VerifyResult(
                Outcome.COUNTEREXAMPLE, VerifyConfig(),
                properties=[ViolatedProperty(
                    loc=SourceLoc(file="lib.c", line=1), description=description
                )],
            ),
            assumptions=assumptions,
        )

    def test_a_null_from_an_unconstructible_type_is_flagged(self):
        text = self._report(
            ["pointer field `g.UserData` is null: `void` is not a type veripp can construct here"]
        ).summary()
        assert "NOTE" in text and "g.UserData" in text
        assert "--assume" in text

    def test_a_null_from_the_depth_bound_suggests_raising_it(self):
        text = self._report(
            ["pointer field `n.next` is null (struct depth bound 2 reached); deeper..."]
        ).summary()
        assert "--max-struct-depth" in text

    def test_no_note_when_nothing_was_nulled(self):
        assert "NOTE" not in self._report(["`x` points to one valid `int`"]).summary()

    def test_no_note_for_an_unrelated_property(self):
        text = self._report(
            ["pointer field `g.UserData` is null: `void` is not a type..."],
            description="division by zero",
        ).summary()
        assert "NOTE" not in text
