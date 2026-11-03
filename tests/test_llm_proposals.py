"""An LLM's rewritten file is a proposal, never a proof of the original.

When the escalation ladder runs out, the agent asks a model for loop
invariants, and the model answers with a whole rewritten file. A verdict on
that file is only a verdict on the code under test if the two are the same
program -- which holds when the rewrite only inserts assertions that change no
state, because an assertion is checked rather than trusted. An assume, or an
edited line, is a different program: a proof of it says nothing about the
original. Before this was checked, both were run and reported as VERIFIED.
"""

from pathlib import Path

import pytest

from veripp import agent
from veripp.agent import _not_just_assertions, verify_with_agent
from veripp.esbmc import Outcome, SourceLoc, VerifyConfig, VerifyResult, ViolatedProperty

HARNESS = """\
#include "veripp/contracts.hpp"
int main() {
    unsigned n = VERIPP_NONDET_UINT();
    VERIPP_ASSUME(n <= 4);
    unsigned s = 0;
    for (unsigned i = 0; i < n; ++i) {
        s += i;
    }
    return 0;
}
"""

INVARIANT = '        __ESBMC_assert(s <= 16, "s stays small");'


def _insert_after(text: str, anchor: str, *lines: str) -> str:
    out = []
    for line in text.split("\n"):
        out.append(line)
        if line.strip() == anchor:
            out += list(lines)
    return "\n".join(out)


class TestOnlyAssertionsMayBeAdded:
    def test_an_inserted_assertion_is_accepted(self):
        after = _insert_after(HARNESS, "s += i;", INVARIANT)
        assert _not_just_assertions(HARNESS, after) is None

    def test_each_spelling_of_an_assertion_is_accepted(self):
        for line in ("    VERIPP_ASSERT(s >= 0);", "    assert(n <= 4); // bound",
                     '    __ESBMC_assert(sizeof(s) == 4, "width");'):
            after = _insert_after(HARNESS, "unsigned s = 0;", line)
            assert _not_just_assertions(HARNESS, after) is None, line

    def test_blank_lines_and_comments_may_ride_along(self):
        after = _insert_after(HARNESS, "s += i;", "", "        // invariant:", INVARIANT)
        assert _not_just_assertions(HARNESS, after) is None

    def test_an_added_assume_is_refused(self):
        after = _insert_after(HARNESS, "unsigned s = 0;", "    __ESBMC_assume(n == 0);")
        assert "assume" in _not_just_assertions(HARNESS, after)

    def test_an_assert_and_an_assume_on_one_line_are_refused(self):
        after = _insert_after(
            HARNESS, "unsigned s = 0;",
            '    __ESBMC_assert(1, "x"); __ESBMC_assume(n == 0);',
        )
        assert _not_just_assertions(HARNESS, after) is not None

    def test_a_deleted_call_is_refused(self):
        after = HARNESS.replace("        s += i;\n", "")
        assert "deletes" in _not_just_assertions(HARNESS, after)

    def test_an_edited_line_is_refused(self):
        after = HARNESS.replace("n <= 4", "n <= 0")
        assert "edits" in _not_just_assertions(HARNESS, after)

    @pytest.mark.parametrize("condition", [
        "s = 0", "s += 1", "s <<= 1", "++s", "s-- > 0", "reset(s)", "s; s = 1",
    ])
    def test_an_assertion_with_a_side_effect_is_refused(self, condition):
        after = _insert_after(HARNESS, "s += i;", f"        assert({condition});")
        assert _not_just_assertions(HARNESS, after) is not None, condition

    def test_an_insertion_after_an_unbraced_if_is_refused(self):
        # The assertion would become the body of the `if`, and the real body
        # would run unconditionally.
        before = HARNESS.replace(
            "    return 0;", "    if (n > 2)\n        s = 0;\n    return 0;"
        )
        after = _insert_after(before, "if (n > 2)", "        assert(n > 2);")
        assert "complete statement" in _not_just_assertions(before, after)

    def test_an_insertion_after_a_line_continuation_is_refused(self):
        before = HARNESS.replace(
            "int main() {", "#define TWICE(x) \\\n    ((x) * 2)\nint main() {"
        )
        after = _insert_after(before, "#define TWICE(x) \\", "    assert(1);")
        assert "continuation" in _not_just_assertions(before, after)

    def test_an_assertion_ending_in_a_continuation_is_refused(self):
        after = _insert_after(HARNESS, "unsigned s = 0;", "    assert(n <= 4); // \\")
        assert _not_just_assertions(HARNESS, after) is not None

    def test_two_assertions_cannot_comment_out_the_code_between_them(self):
        # Each line is a well-formed assertion on its own, yet together they
        # turn the loop into a comment.
        after = _insert_after(HARNESS, "unsigned s = 0;", "    assert(1 || /*);")
        after = _insert_after(after, "}", "    assert(*/ 1);")
        assert _not_just_assertions(HARNESS, after) is not None

    def test_an_assertion_inside_a_comment_checks_nothing(self):
        before = HARNESS.replace("    return 0;", "    /*\n    notes;\n    */\n    return 0;")
        after = _insert_after(before, "notes;", "    assert(0);")
        assert _not_just_assertions(before, after) is not None

    def test_redefining_the_assertion_is_refused(self):
        after = _insert_after(
            HARNESS, '#include "veripp/contracts.hpp"',
            "#define assert(c) __ESBMC_assume(c)",
        )
        assert _not_just_assertions(HARNESS, after) is not None

    def test_a_proposal_that_adds_nothing_is_refused(self):
        after = _insert_after(HARNESS, "s += i;", "        // looks fine to me")
        assert "no assertion" in _not_just_assertions(HARNESS, after)


# ------------------------------------------------------------ agent loop ---


_REACHED = ViolatedProperty(
    SourceLoc("probe.c", 1), "veripp: harness is reachable under its assumptions"
)


def _fake_run(ran: list[str], original: Outcome):
    """The checker, reduced to what each file is: the original is stuck, any
    variant the LLM wrote verifies, and a reachability probe is reachable."""

    def run(source: Path, config: VerifyConfig, esbmc_bin=None) -> VerifyResult:
        ran.append(source.name)
        # A probe is a file named *.reachable.* or a run with the probe macro
        # defined, depending on how veripp builds it; either way it fails its
        # own assertion, as a reachable harness does.
        if ".reachable." in source.name or "VERIPP_REACHABILITY_PROBE" in config.defines:
            return VerifyResult(outcome=Outcome.COUNTEREXAMPLE, config=config,
                                properties=[_REACHED])
        if ".inv." in source.name or ".fix." in source.name:
            outcome = Outcome.VERIFIED
        else:
            outcome = original
        return VerifyResult(outcome=outcome, config=config,
                            error="frontend says no" if original is Outcome.PARSE_ERROR else None)

    return run


class ProposingLLM:
    """Offline, except that it rewrites files the way a model would."""

    PROVIDER = "scripted"

    def __init__(self, rewrite):
        self.rewrite = rewrite

    def classify(self, context):
        return "real_bug"

    def explain(self, context):
        return "scripted"

    def propose_precondition(self, context):
        return None

    def _variant(self, source: Path, tag: str) -> Path:
        out = source.with_name(f"{source.stem}.{tag}{source.suffix}")
        out.write_text(self.rewrite(source.read_text(encoding="utf-8")), encoding="utf-8")
        return out

    def propose_invariants(self, source, result):
        return self._variant(source, "inv")

    def propose_frontend_fix(self, source, result):
        return self._variant(source, "fix")


@pytest.fixture
def harness(tmp_path):
    path = tmp_path / "veripp_harness_f.c"
    path.write_text(HARNESS, encoding="utf-8")
    return path


def _verify(harness, monkeypatch, rewrite, original=Outcome.UNKNOWN):
    ran: list[str] = []
    monkeypatch.setattr(agent, "run", _fake_run(ran, original))
    report = verify_with_agent(
        harness, VerifyConfig(), llm=ProposingLLM(rewrite), harness=harness,
        assumptions=["n <= 4"],
    )
    return report, ran


def test_an_assertion_only_proposal_is_proved_and_reported_as_such(harness, monkeypatch):
    report, ran = _verify(
        harness, monkeypatch, lambda t: _insert_after(t, "s += i;", INVARIANT)
    )
    variant = harness.with_name("veripp_harness_f.inv.c")
    assert report.verified
    # The report names the file that was actually proved, and what the model
    # added to it -- not the untouched original.
    assert report.harness == variant
    assert report.llm_invariants == [INVARIANT.strip()]
    summary = report.summary()
    assert f"harness: {variant}" in summary
    assert "harness modified by LLM" in summary and INVARIANT.strip() in summary

    from veripp.cli import _payload

    assert _payload(report, None)["llm_invariants"] == [INVARIANT.strip()]


@pytest.mark.parametrize("rewrite", [
    lambda t: _insert_after(t, "unsigned s = 0;", "    __ESBMC_assume(n == 0);"),
    lambda t: t.replace("n <= 4", "n <= 0"),
    lambda t: t.replace("        s += i;\n", ""),
    lambda t: _insert_after(t, "unsigned s = 0;", "    assert(n = 0);"),
], ids=["assume", "edit", "delete", "side-effect"])
def test_any_other_rewrite_is_refused_and_never_run(harness, monkeypatch, rewrite):
    report, ran = _verify(harness, monkeypatch, rewrite)
    assert not report.verified
    assert report.final.outcome is Outcome.UNKNOWN
    assert "refused" in report.narrative
    assert not any(".inv." in name for name in ran)
    assert report.harness == harness
    assert report.llm_invariants == []


def test_a_frontend_rewrite_is_handed_back_not_proved(harness, monkeypatch):
    report, ran = _verify(
        harness, monkeypatch, lambda t: t.replace("unsigned", "int"),
        original=Outcome.PARSE_ERROR,
    )
    fixed = harness.with_name("veripp_harness_f.fix.c")
    assert report.final.outcome is Outcome.PARSE_ERROR
    assert not report.verified
    assert not any(".fix." in name for name in ran)
    # The rewrite is still useful to a person, so it is named, with what it
    # has and has not been checked for.
    assert str(fixed) in report.narrative
    assert "not checked for equivalence" in report.narrative
