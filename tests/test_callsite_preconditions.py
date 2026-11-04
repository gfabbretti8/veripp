"""An accepted precondition, checked against the callers that can be read.

The solver says a proposed precondition is satisfiable and excludes the
failing input. Nothing used to ask whether real callers honour it: a
"preconditioned" result could sit above a call site passing the very value
the precondition forbids. Where a call passes an integer literal for the
parameter the precondition names, that is a mechanical check, and a caller
that fails it is a lead rather than a conditional proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veripp.triage import (
    evaluate_precondition,
    literal_call_arguments,
    precondition_violations,
)

ROOT = Path(__file__).resolve().parent.parent


class TestEvaluate:
    @pytest.mark.parametrize("pre,bindings,expected", [
        ("d != 0", {"d": 0}, False),
        ("d != 0", {"d": 3}, True),
        ("n > 0 && n <= 4", {"n": 5}, False),
        ("n > 0 && n <= 4", {"n": 4}, True),
        ("n > 0 || m > 0", {"n": 0, "m": 1}, True),
        ("!flag", {"flag": 0}, True),
        ("!flag", {"flag": 2}, False),
        ("x / 2 > 1", {"x": 5}, True),
        ("p != NULL", {"p": 0}, False),
        ("len < 64", {"len": 0x40}, False),
        ("(a + b) % 2 == 0", {"a": 1, "b": 3}, True),
    ])
    def test_integer_comparisons_are_judged(self, pre, bindings, expected):
        assert evaluate_precondition(pre, bindings) is expected

    @pytest.mark.parametrize("pre,bindings", [
        ("d != 0", {}),                      # no literal for the parameter
        ("n > 0 && m > 0", {"n": 1}),        # one of two unknown
        ("p->len > 0", {"p": 0}),            # a field access
        ("f(x) > 0", {"x": 1}),              # a call
        ("(unsigned) x < 4", {"x": 1}),      # a cast
        ("x / 0 == 1", {"x": 1}),            # undefined, not false
        ("x > 1.5", {"x": 2}),               # a float literal
        ("__import__('os')", {}),            # not an expression veripp judges
    ])
    def test_anything_else_is_not_judged(self, pre, bindings):
        assert evaluate_precondition(pre, bindings) is None


class TestLiteralArguments:
    SRC = (
        "int div10(int n, int d);\n"                       # declaration: skipped
        "int div10(int n, int d) { return n / d; }\n"      # definition: skipped
        "int a(void) { return div10(10, 0); }\n"           # both literal
        "int b(int x) { return div10(x, 2); }\n"           # one literal
        "int c(void) { return div10(0x10, -1); }\n"        # hex and negative
        "int d(void) { return div10('a', 1u); }\n"         # char and suffix
        "int e(int x) { return div10(g(1), x); }\n"        # nested call: no literal
        "int f(void) { return div10(1); }\n"               # wrong arity: skipped
        "/* div10(5, 0) in a comment */\n"
    )

    def test_each_call_yields_the_parameters_passed_a_literal(self):
        sites = literal_call_arguments(self.SRC, "div10", ["n", "d"])
        assert sites == [
            (3, {"n": 10, "d": 0}),
            (4, {"d": 2}),
            (5, {"n": 16, "d": -1}),
            (6, {"n": 97, "d": 1}),
            (7, {}),
        ]


class TestViolations:
    SRC = (
        "int div10(int n, int d) { return n / d; }\n"
        "int ok(void) { return div10(10, 2); }\n"
        "int bad(void) { return div10(10, 0); }\n"
        "int unknown(int v) { return div10(10, v); }\n"
    )

    def test_only_the_breaking_call_is_reported(self):
        found = precondition_violations(self.SRC, "div10", ["n", "d"], ["d != 0"])
        assert found == ["line 3: d = 0 violates `d != 0`"]

    def test_a_precondition_on_something_else_is_left_alone(self):
        assert precondition_violations(self.SRC, "div10", ["n", "d"], ["n->len > 0"]) == []

    def test_linked_sources_are_checked_and_named(self):
        linked = ["int elsewhere(void) { return div10(1, 0); }\n"]
        found = precondition_violations(self.SRC, "div10", ["n", "d"], ["d != 0"], linked=linked)
        assert found[0].startswith("line 3:")
        assert found[1].startswith("linked source 0: line 1:")

    def test_several_parameters_are_named_together(self):
        src = "int f(int a, int b) { return a + b; }\nint g(void) { return f(5, 9); }\n"
        (found,) = precondition_violations(src, "f", ["a", "b"], ["a + b < 10"])
        assert found == "line 2: a = 5, b = 9 violate `a + b < 10`"


FIXTURE = """\
#include "veripp/contracts.hpp"

int div10(int d) { return 10 / d; }

int guarded(int d) { return d == 0 ? 0 : div10(d); }
int careless(void) { return div10(0); }
"""


@pytest.mark.esbmc
class TestEndToEnd:
    def test_verify_with_an_assumed_precondition_a_caller_breaks(self, tmp_path, capsys):
        """`--assume d != 0` makes the proof conditional; `careless` passes 0.
        The result says so and is not a pass."""
        from veripp.cli import EXIT_INCONCLUSIVE, main

        src = tmp_path / "f.c"
        src.write_text(FIXTURE, encoding="utf-8")
        code = main(["verify", str(src), "--function", "div10", "--assume", "d != 0", "--no-llm"])
        out = capsys.readouterr().out
        assert code == EXIT_INCONCLUSIVE, out
        assert "requires d != 0" in out
        assert "BUT a caller breaks the precondition" in out
        assert "line 6: d = 0 violates `d != 0`" in out

    def test_verify_json_carries_the_violations(self, tmp_path, capsys):
        import json

        from veripp.cli import main

        src = tmp_path / "f.c"
        src.write_text(FIXTURE, encoding="utf-8")
        main(["verify", str(src), "--function", "div10", "--assume", "d != 0", "--no-llm", "--json"])
        report = json.loads(capsys.readouterr().out)
        assert report["accepted_preconditions"] == ["d != 0"]
        assert report["precondition_violations"] == ["line 6: d = 0 violates `d != 0`"]

    def test_a_precondition_every_caller_honours_still_passes(self, tmp_path, capsys):
        from veripp.cli import EXIT_VERIFIED, main

        src = tmp_path / "f.c"
        src.write_text(FIXTURE.replace("div10(0)", "div10(5)"), encoding="utf-8")
        code = main(["verify", str(src), "--function", "div10", "--assume", "d != 0", "--no-llm"])
        out = capsys.readouterr().out
        assert code == EXIT_VERIFIED, out
        assert "BUT a caller" not in out

    def test_scan_flags_a_preconditioned_result_a_caller_breaks(self, tmp_path):
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions
        from veripp.paths import contracts_include_dir
        from veripp.scan import scan

        import importlib
        scripted = importlib.import_module("test_scan_triage").ScriptedLLM

        src = tmp_path / "f.c"
        src.write_text(FIXTURE, encoding="utf-8")
        config = VerifyConfig(timeout_s=90, include_dirs=[contracts_include_dir(), src.parent])
        report = scan(src, config, HarnessOptions(), jobs=2, llm=scripted(precondition="d != 0"))
        (pre,) = [r for r in report.preconditioned if r.name == "div10"]
        assert pre.precondition_violations == ["line 6: d = 0 violates `d != 0`"]
        summary = report.summary()
        assert "BUT a caller breaks it -- line 6: d = 0 violates `d != 0`" in summary
