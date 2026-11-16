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
    src.write_text(SRC, encoding="utf-8")
    return TargetInfo(source=src, function="scale")


class TestContext:
    def test_call_sites_found_and_definition_excluded(self, target):
        sites = find_call_sites(target.source.read_text(encoding="utf-8"), "scale")
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


    def test_context_states_linkage_and_caller_count_as_facts(self, target):
        result = VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig(), raw_output="tail")
        harness = generate(target.source, "scale")
        path = harness.write(target.source.parent)
        ctx = build_context(target, path, result)
        assert any("external linkage" in f and "2 call sites" in f for f in ctx.facts), ctx.facts
        rendered = ctx.render()
        assert "Facts established mechanically" in rendered

    def test_context_names_a_null_the_harness_left(self, tmp_path):
        from veripp.esbmc import SourceLoc, ViolatedProperty

        src = tmp_path / "node.c"
        src.write_text(
            "typedef struct Node { struct Node *next; int v; } Node;\n"
            "int second(Node *n) { return n->next->v; }\n",
            encoding="utf-8",
        )
        target = TargetInfo(source=src, function="second")
        harness = generate(src, "second", HarnessOptions(max_struct_depth=0))
        path = harness.write(tmp_path)
        prop = ViolatedProperty(loc=SourceLoc(str(src), 2), description="dereference failure: NULL pointer")
        result = VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig(), properties=[prop])
        ctx = build_context(target, path, result)
        assert any("`next`" in f and "harness's" in f for f in ctx.facts), ctx.facts


    def test_context_names_a_bound_the_harness_chose(self, tmp_path):
        from veripp.esbmc import SourceLoc, ViolatedProperty

        src = tmp_path / "t.c"
        src.write_text("int get(const void *tableBase, unsigned h) { const unsigned *t = tableBase; return t[h]; }\n",
                       encoding="utf-8")
        target = TargetInfo(source=src, function="get")
        harness = generate(src, "get")
        path = harness.write(tmp_path)
        prop = ViolatedProperty(loc=SourceLoc(str(src), 1), description="dereference failure: array bounds violated")
        result = VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig(), properties=[prop])
        ctx = build_context(target, path, result)
        assert any("`tableBase`" in f and "no length" in f for f in ctx.facts), ctx.facts


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


class TestAlignmentArtifact:
    """Allocation through a function pointer produces a false alignment
    failure, and it dominated cJSON's findings: 14 of 33 counterexamples were
    this one pattern, every one about the allocator being opaque to the
    checker rather than about the library.

    Reproduced minimally: a struct allocated via `global.allocate(...)` fails,
    while the identical code calling malloc directly verifies.
    """

    @staticmethod
    def _result(description: str):
        """`violated_property` is derived from `properties`, not a field."""
        from veripp.esbmc import (
            Outcome, SourceLoc, VerifyConfig, VerifyResult, ViolatedProperty,
        )

        return VerifyResult(
            outcome=Outcome.COUNTEREXAMPLE,
            config=VerifyConfig(),
            properties=[ViolatedProperty(
                loc=SourceLoc(file="lib.c", line=7, column=9, function="f"),
                description=description,
                expression="",
                cwes=[],
            )],
        )

    def test_it_is_classified_as_an_artifact(self, tmp_path) -> None:
        from veripp.triage import mechanical_artifact

        why = mechanical_artifact(
            self._result("dereference failure: Incorrect alignment when accessing data object"),
            tmp_path / "harness.c",
        )
        assert why is not None
        assert "allocator" in why

    def test_the_reason_says_how_to_check_it_properly(self) -> None:
        """An artifact note that does not say what to do instead just moves
        the finding to a quieter column."""
        from veripp.triage import _MECHANICAL_ARTIFACTS

        why = next(w for n, w in _MECHANICAL_ARTIFACTS if "alignment" in n)
        assert "--link" in why or "compile_commands" in why

    def test_a_real_failure_is_still_a_finding(self, tmp_path) -> None:
        from veripp.triage import mechanical_artifact

        for real in ("arithmetic overflow on add",
                     "array bounds violated: array `t' upper bound",
                     "division by zero"):
            assert mechanical_artifact(self._result(real), tmp_path / "h.c") is None, real


class TestUninitialisedInsideTheLibraryModel:
    """parson's read_file was reported for `use of uninitialized variable:
    nread`, and `nread` occurs nowhere in parson.c: it is a local in ESBMC's
    model of fread/fseek, attributed to the calling line."""

    def _prop(self, tmp_path, source_text, name="nread"):
        from veripp.esbmc import SourceLoc, ViolatedProperty

        src = tmp_path / "p.c"
        src.write_text(source_text, encoding="utf-8")
        harness = tmp_path / "veripp_harness_f.c"
        harness.write_text('#include "p.c"\nint main(void) { return 0; }\n', encoding="utf-8")
        prop = ViolatedProperty(loc=SourceLoc(str(src), 1),
                                description=f"use of uninitialized variable: {name}")
        return prop, harness

    def test_a_name_absent_from_the_file_and_the_harness_is_the_checkers(self, tmp_path):
        from veripp.triage import mechanical_artifact

        prop, harness = self._prop(tmp_path, "int f(void) { FILE *fp = fopen(\"x\", \"r\"); return fseek(fp, 0, 2); }\n")
        result = VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig(), properties=[prop])
        why = mechanical_artifact(result, harness)
        assert why is not None and "`nread`" in why and "model" in why

    def test_a_name_the_code_declares_is_the_codes(self, tmp_path):
        from veripp.triage import mechanical_artifact

        prop, harness = self._prop(tmp_path, "int f(void) { int nread; return nread; }\n")
        result = VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig(), properties=[prop])
        assert mechanical_artifact(result, harness) is None

    def test_a_failure_located_inside_the_checkers_model_is_judged_the_same_way(self, tmp_path):
        """ESBMC puts the location at /esbmc-vfs/libc/library/io.c:203 in
        fread -- a path that does not exist here. The harness and the source
        it includes are what gets searched."""
        from veripp.esbmc import SourceLoc, ViolatedProperty
        from veripp.triage import mechanical_artifact

        src = tmp_path / "p.c"
        src.write_text("int f(void) { FILE *fp = fopen(\"x\", \"r\"); return fseek(fp, 0, 2); }\n",
                       encoding="utf-8")
        harness = tmp_path / "veripp_harness_f.c"
        harness.write_text(f'#include "{src}"\nint main(void) {{ return f(); }}\n', encoding="utf-8")
        prop = ViolatedProperty(loc=SourceLoc("/esbmc-vfs/libc/library/io.c", 203, function="fread"),
                                description="use of uninitialized variable: nread")
        result = VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig(), properties=[prop])
        why = mechanical_artifact(result, harness)
        assert why is not None and "library model (io.c)" in why

        src.write_text("int f(void) { int nread; return nread; }\n", encoding="utf-8")
        assert mechanical_artifact(result, harness) is None, "the source declares it"

    def test_a_name_only_in_a_comment_does_not_count_as_declared(self, tmp_path):
        from veripp.triage import mechanical_artifact

        prop, harness = self._prop(tmp_path, "/* nread */ int f(void) { return fgetc(0); }\n")
        result = VerifyResult(Outcome.COUNTEREXAMPLE, VerifyConfig(), properties=[prop])
        assert mechanical_artifact(result, harness) is not None


class TestThePromptIsASizeAModelCanBeSent:
    """The checker prints the whole containing object for every field write.
    One write into a struct holding two 32 KB arrays made a 758,000-character
    triage prompt (miniz, tdefl_record_match)."""

    def test_a_huge_value_is_cut_and_the_cut_is_said(self) -> None:
        from veripp.triage import MAX_INPUT_VALUE_CHARS, _inputs_for_prompt

        value = "d_obj = { " + "0, " * 250_000 + ".m_flags=7 }"
        text = _inputs_for_prompt([value])
        assert len(text) < MAX_INPUT_VALUE_CHARS + 200
        assert text.startswith("d_obj = { 0, ")
        assert text.endswith(".m_flags=7 }")       # the end of the dump survives
        assert "characters of this value omitted" in text

    def test_many_values_stop_at_a_total_and_say_how_many_were_left(self) -> None:
        from veripp.triage import MAX_INPUTS_CHARS, _inputs_for_prompt

        values = [f"a[{i}] = " + "9" * 1500 for i in range(40)]
        text = _inputs_for_prompt(values)
        assert len(text) < MAX_INPUTS_CHARS + 200
        assert "further assignments omitted" in text.splitlines()[-1]

    def test_ordinary_inputs_are_sent_whole(self) -> None:
        from veripp.triage import _inputs_for_prompt

        assert _inputs_for_prompt(["n = 4", "a_buf = { 1, 2, 3, -1 }"]) == (
            "n = 4\na_buf = { 1, 2, 3, -1 }"
        )


class TestTheCallerIsShownAboveTheCall:
    """`f(encname, n)` names an argument; what it is, is in the caller. Shown
    the call line alone, four model tiers dismissed a confirmed over-read as
    a harness artifact (benchmarks/TRIAGE.md)."""

    SOURCE = (
        "static int decode(const char *p, int n) { return p[n]; }\n"
        "\n"
        "int other(void) {\n"
        "    return 7;\n"
        "}\n"
        "\n"
        "void on_packet(const char *packet, int len) {\n"
        "    const char *name = packet + 12;   /* off the wire */\n"
        "    if (len < 12) return;\n"
        "    decode(name, len);\n"
        "}\n"
    )

    def test_the_lines_that_say_where_an_argument_comes_from_are_there(self) -> None:
        from veripp.triage import call_sites_in_context

        (block,) = call_sites_in_context(self.SOURCE, "decode")
        assert "const char *name = packet + 12;" in block
        assert block.splitlines()[-1].startswith(">") and "decode(name, len);" in block

    def test_it_stops_at_the_top_of_the_caller(self) -> None:
        from veripp.triage import call_sites_in_context

        (block,) = call_sites_in_context(self.SOURCE, "decode")
        assert "void on_packet(" in block          # the caller's signature
        assert "return 7;" not in block            # not the function above it

    def test_the_prompt_carries_it(self) -> None:
        from veripp.llm import TriageContext

        text = TriageContext(
            function="decode", signature="int decode(const char *p, int n)",
            parameters=["p", "n"], function_source="...",
            call_sites=["line 10: decode(name, len);"],
            caller_context=[">    10 |     decode(name, len);"],
        ).render()
        assert "The caller's code leading up to each call" in text

    def test_a_function_nobody_calls_adds_nothing(self) -> None:
        from veripp.llm import TriageContext
        from veripp.triage import call_sites_in_context

        assert call_sites_in_context(self.SOURCE, "other") == []
        text = TriageContext(function="f", signature="f", parameters=[],
                             function_source="").render()
        assert "leading up to each call" not in text
