"""A counterexample in a static function is put to the function's callers.

A harness hands a function anything its signature admits; a static helper
only ever sees what the callers in its own file pass. The checker is asked
whether the failing line still fails when the helper is reached through
each caller, up to an externally visible one.
"""

import json
from pathlib import Path

import pytest

from veripp import cli
from veripp.callers import (
    REACHED, UNREACHED, UNSETTLED, _narrowed, call_sites, escalate,
)
from veripp.cppsig import definition_spans, function_definitions
from veripp.esbmc import Outcome, VerifyConfig, run
from veripp.harness import HarnessOptions, generate
from veripp.paths import contracts_include_dir
from veripp.scan import FunctionResult, ScanReport, scan

LIB = """\
static int tbl[4] = {1, 2, 3, 4};

static int at(int i) { return tbl[i]; }

int first(void) { return at(0); }

int clamp(int i) {
    if (i < 0) i = 0;
    if (i > 3) i = 3;
    return at(i);
}

static int pick(int i) { return tbl[i]; }

static int mid(int i) { return pick(i + 1); }

int top(int i) {
    if (i < 0 || i > 3) return 0;
    return mid(i);
}
"""


class TestCallSites:
    def test_callers_are_the_functions_whose_bodies_call_it(self):
        sites = call_sites(LIB, "at")
        assert sites.callers == ["first", "clamp"]
        assert not sites.outside and not sites.address_taken

    def test_a_declaration_and_a_recursive_call_are_not_callers(self):
        text = ("static int f(int n);\n"
                "static int f(int n) { return n ? f(n - 1) : 0; }\n"
                "int api(int n) { return f(n); }\n")
        assert call_sites(text, "f").callers == ["api"]

    def test_a_definition_that_returns_a_pointer_is_not_a_call(self):
        """`*f(` has punctuation before the name, like a use."""
        text = ("static const char *f(int n);\n"
                "static const char *f(int n) { return n ? \"a\" : 0; }\n"
                "int api(int n) { return f(n) != 0; }\n")
        sites = call_sites(text, "f")
        assert sites.callers == ["api"] and sites.outside == 0

    def test_a_call_in_an_initialiser_is_outside_every_function(self):
        text = ("static int f(int n) { return n; }\n"
                "int seed = f(3);\n")
        assert call_sites(text, "f").outside == 1

    def test_a_call_in_a_macro_is_a_caller_nobody_can_name(self):
        text = ("static int f(int n) { return n; }\n"
                "#define F1() f(1)\n"
                "int api(void) { return F1(); }\n")
        sites = call_sites(text, "f")
        assert sites.callers == [] and sites.outside == 1

    def test_a_name_used_without_a_call_may_be_reached_through_a_pointer(self):
        text = ("static int f(int n) { return n; }\n"
                "int (*hook)(int) = f;\n")
        assert call_sites(text, "f").address_taken

    def test_a_field_of_the_same_name_is_not_the_function(self):
        text = ("static int len(int n) { return n; }\n"
                "int api(struct s *p) { return p->len + len(1); }\n")
        sites = call_sites(text, "len")
        assert sites.callers == ["api"] and not sites.address_taken

    def test_spans_are_the_bodies_and_the_names_are_unchanged(self):
        spans = definition_spans(LIB)
        assert [n for n, _, _ in spans] == function_definitions(LIB)
        name, start, end = spans[0]
        assert (name, LIB[start], LIB[end]) == ("at", "{", "}")


class TestOnlyTheCheckThatWasAsked:
    def test_a_bounds_question_drops_the_arithmetic_checks(self):
        asked = _narrowed(VerifyConfig(), "array bounds violated: array `t' upper bound")
        assert asked.bounds_check and asked.pointer_check
        assert not asked.overflow_check and not asked.nan_check
        assert not asked.memory_leak_check

    def test_a_division_question_keeps_only_division(self):
        asked = _narrowed(VerifyConfig(), "division by zero")
        assert asked.div_by_zero_check and not asked.bounds_check

    def test_an_overflow_question_does_not_turn_unsigned_wraparound_on(self):
        asked = _narrowed(VerifyConfig(), "arithmetic overflow on add")
        assert asked.overflow_check and not asked.unsigned_overflow_check

    def test_an_unknown_property_keeps_every_check(self):
        config = VerifyConfig()
        assert _narrowed(config, "something this has never seen") is config


def _ask(tmp_path, text, name):
    src = tmp_path / "lib.c"
    src.write_text(text, encoding="utf-8")
    config = VerifyConfig(timeout_s=60, include_dirs=[contracts_include_dir()])
    options = HarnessOptions()
    path = generate(src, name, options).write(tmp_path / "h")
    result = run(path, config)
    assert result.outcome is Outcome.COUNTEREXAMPLE, result.raw_output[-800:]
    return escalate(src, name, result.violated_property, config, options,
                    tmp_path / "h")


@pytest.mark.esbmc
class TestEscalate:
    def test_callers_that_guard_the_input_clear_it(self, tmp_path):
        found = _ask(tmp_path, LIB, "at")
        assert found.verdict == UNREACHED, found.note
        assert {c.caller: c.status for c in found.checks} == {
            "first": "clear", "clamp": "clear"}
        assert "`first`" in found.note and "`clamp`" in found.note

    def test_an_entry_point_that_reaches_it_is_named_with_its_inputs(self, tmp_path):
        found = _ask(tmp_path, LIB, "mid")
        assert found.verdict == REACHED
        assert found.chain == ["top"] and found.entry == "top"
        assert found.inputs == ["i = 3"]

    def test_it_climbs_through_a_static_caller_to_the_entry_point(self, tmp_path):
        found = _ask(tmp_path, LIB, "pick")
        assert found.verdict == REACHED
        assert found.chain == ["top", "mid"], found.note
        assert found.note == "reached from top -> mid -> pick"

    def test_a_static_caller_that_reaches_it_is_cleared_by_its_own_callers(self, tmp_path):
        text = ("static int tbl[4];\n"
                "static int pick(int i) { return tbl[i]; }\n"
                "static int mid(int i) { return pick(i); }\n"
                "int top(int i) { return mid(i & 3); }\n")
        found = _ask(tmp_path, text, "pick")
        assert found.verdict == UNREACHED, found.note
        assert "`mid`" in found.note

    def test_a_caller_whose_harness_never_makes_the_call_clears_nothing(self, tmp_path):
        """`api` only calls the helper for an index no int satisfies. Nothing
        fails on the line through it -- because nothing gets there."""
        text = ("static int tbl[4];\n"
                "static int pick(int i) { return tbl[i]; }\n"
                "int api(int i) { if (i > 5 && i < 3) return pick(i); return 0; }\n")
        found = _ask(tmp_path, text, "pick")
        assert found.verdict == UNSETTLED
        assert found.checks[0].status == "not_called"
        assert "never gets to the call" in found.note

    def test_a_function_whose_address_is_taken_is_never_cleared(self, tmp_path):
        text = ("static int tbl[4];\n"
                "static int pick(int i) { return tbl[i]; }\n"
                "int (*hook)(int) = pick;\n"
                "int api(void) { return pick(0); }\n")
        found = _ask(tmp_path, text, "pick")
        assert found.verdict == UNSETTLED and "through a pointer" in found.note

    def test_no_callers_is_not_an_answer(self, tmp_path):
        text = ("static int tbl[4];\n"
                "static int pick(int i) { return tbl[i]; }\n")
        found = _ask(tmp_path, text, "pick")
        assert found.verdict == UNSETTLED and "no callers" in found.note

    def test_a_failure_elsewhere_in_the_caller_does_not_hide_the_line(self, tmp_path):
        """The caller overflows before it calls the helper. Asked about
        every property, the run still answers for the helper's line."""
        text = ("static int tbl[4];\n"
                "static int pick(int i) { return tbl[i]; }\n"
                "int api(int i, int j) { int k = i + j; return pick(k); }\n")
        found = _ask(tmp_path, text, "pick")
        assert found.verdict == REACHED and found.chain == ["api"]

    def test_a_failure_inside_a_library_model_is_matched_at_the_calling_line(self, tmp_path):
        text = ("#include <string.h>\n"
                "static unsigned len(const char *s, int k) {\n"
                "    return k ? strlen(s + k) : 0; }\n"
                "unsigned ok(const char *s) { return len(s, 0); }\n"
                "unsigned bad(const char *s) { return len(s, 9); }\n")
        found = _ask(tmp_path, text, "len")
        assert found.verdict == REACHED and found.chain == ["bad"], found.note


@pytest.mark.esbmc
class TestScan:
    def _scan(self, tmp_path, **kw):
        src = tmp_path / "lib.c"
        src.write_text(LIB, encoding="utf-8")
        config = VerifyConfig(timeout_s=60, include_dirs=[contracts_include_dir()])
        return scan(src, config, HarnessOptions(), jobs=2, retry_budget=0, **kw)

    def test_what_no_caller_reaches_is_listed_apart(self, tmp_path):
        report = self._scan(tmp_path)
        assert [r.name for r in report.unreached] == ["at"]
        assert "at" not in {r.name for r in report.counterexamples}
        assert report.callers_asked and report.caller_checked == 3
        summary = report.summary()
        assert "NO CALLER REACHES   1" in summary
        assert "at: array bounds violated" in summary
        assert "not through `first`, `clamp`" in summary

    def test_what_an_entry_point_reaches_says_so_with_its_inputs(self, tmp_path):
        report = self._scan(tmp_path)
        found = {r.name: r for r in report.counterexamples}
        assert found["mid"].caller_reach == "reached"
        assert found["mid"].reached_from == ["top", "mid"]
        assert found["pick"].reached_from == ["top", "mid", "pick"]
        summary = report.summary()
        assert "static, 1 caller here; reached from top -> mid]" in summary
        assert "      top: i = 3" in summary

    def test_every_function_is_still_in_one_row(self, tmp_path):
        report = self._scan(tmp_path)
        rows = (len(report.proved) + len(report.counterexamples)
                + len(report.unreached) + len(report.artifacts)
                + len(report.inconclusive) + len(report.refused))
        assert rows == len(report.results) == 6

    def test_a_budget_of_zero_asks_nobody(self, tmp_path):
        report = self._scan(tmp_path, caller_budget=0)
        assert not report.callers_asked and not report.unreached
        assert {"at", "mid", "pick"} <= {r.name for r in report.counterexamples}
        assert all(not r.caller_reach for r in report.results)

    def test_the_json_report_and_the_exit_code(self, tmp_path, capsys):
        src = tmp_path / "lib.c"
        src.write_text(LIB.split("static int pick")[0], encoding="utf-8")
        code = cli.main(["scan", str(src), "--json", "--no-llm", "--no-cache",
                         "--retry-budget", "0"])
        report = json.loads(capsys.readouterr().out)
        assert report["counterexamples"] == []
        (cleared,) = report["no_caller_reaches"]
        assert cleared["function"] == "at" and "`clamp`" in cleared["callers"]
        # Nothing that a caller can reach failed.
        assert code == 0

    def test_verify_asks_too_and_does_not_change_its_verdict(self, tmp_path, capsys):
        src = tmp_path / "lib.c"
        src.write_text(LIB, encoding="utf-8")
        code = cli.main(["verify", str(src), "--function", "at", "--no-llm"])
        out = capsys.readouterr().out
        assert "no caller makes this line fail" in out
        assert "nothing has checked whether a real caller" not in out
        assert code == 1     # the function does fail on those inputs
        cli.main(["verify", str(src), "--function", "mid", "--no-llm", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["callers"]["verdict"] == "reached"
        assert payload["callers"]["reached_from"] == ["top"]
        assert payload["callers"]["inputs"] == ["i = 3"]

    def test_verify_leaves_an_external_function_alone(self, tmp_path, capsys):
        src = tmp_path / "lib.c"
        src.write_text("int tbl[4];\nint get(int i) { return tbl[i]; }\n"
                       "int api(void) { return get(0); }\n", encoding="utf-8")
        cli.main(["verify", str(src), "--function", "get", "--no-llm", "--json"])
        assert json.loads(capsys.readouterr().out)["callers"] is None


class TestReportWithoutAChecker:
    def _result(self, name, **kw):
        return FunctionResult(name=name, outcome="counterexample", detail="d",
                              linkage="static", callers=1, **kw)

    def test_a_reached_helper_ranks_with_the_entry_points(self):
        report = ScanReport(source=Path("f.c"), candidates=3)
        report.results = [
            self._result("a_helper"),
            self._result("z_reached", caller_reach="reached",
                         caller_note="reached from api -> z_reached",
                         reached_from=["api", "z_reached"],
                         reached_inputs=["n = 5"]),
        ]
        summary = report.summary()
        assert summary.index("z_reached: d") < summary.index("a_helper: d")
        assert "      api: n = 5" in summary

    def test_unsettled_is_said_and_stays_a_counterexample(self):
        report = ScanReport(source=Path("f.c"), candidates=1)
        report.results = [self._result("h", caller_reach="unsettled",
                                       caller_note="`api`: timed out")]
        assert [r.name for r in report.counterexamples] == ["h"]
        assert "whether a caller reaches it is not settled" in report.summary()

    def test_a_cached_scan_that_never_asked_does_not_serve_a_run_that_does(self):
        payload = {"results": [{"outcome": "counterexample", "artifact": None,
                                "linkage": "static"}]}
        assert cli._cache_serves(payload, None, 0, 0)
        assert not cli._cache_serves(payload, None, 0, 120)
        assert cli._cache_serves({**payload, "callers_asked": True}, None, 0, 120)

    def test_the_new_fields_survive_the_cache(self):
        report = ScanReport(source=Path("f.c"), candidates=1, callers_asked=True,
                            caller_checked=1)
        report.results = [self._result("h", caller_reach="unreached",
                                       caller_note="not through `api`")]
        back = cli._report_from_cache(Path("f.c"), cli._cache_payload(report))
        assert back.callers_asked and [r.name for r in back.unreached] == ["h"]
