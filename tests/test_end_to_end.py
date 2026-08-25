"""End-to-end runs against the real checker. Skipped when esbmc is absent."""

import json

import pytest

from veripp.cli import EXIT_COUNTEREXAMPLE, EXIT_VERIFIED, main

pytestmark = pytest.mark.esbmc


def run_cli(capsys, *argv) -> tuple[int, str]:
    code = main(["verify", *argv, "--no-llm"])
    return code, capsys.readouterr().out


def test_ring_buffer_verifies(capsys, examples):
    code, out = run_cli(capsys, str(examples / "ring_buffer.cpp"))
    assert code == EXIT_VERIFIED
    assert "Result: verified" in out
    assert "BOUNDED proof" in out  # never claim more than was checked


def test_off_by_one_is_caught(capsys, examples):
    code, out = run_cli(capsys, str(examples / "off_by_one.cpp"))
    assert code == EXIT_COUNTEREXAMPLE
    assert "array bounds violated" in out


def test_generated_harness_finds_the_same_bug(capsys, examples):
    # `s += a[i]` violates two properties at once: the out-of-bounds read and
    # the signed overflow it feeds. Which one a given ESBMC reports first is
    # its business, so isolate the one this test is about.
    code, out = run_cli(
        capsys, str(examples / "off_by_one.cpp"), "--function", "sum_array",
        "--no-overflow-check",
    )
    assert code == EXIT_COUNTEREXAMPLE
    assert "array bounds violated" in out
    assert "harness bound on array length" in out
    assert "n = 4" in out  # the concrete input, from the trace


def test_generated_harness_verifies_a_member_function(capsys, examples):
    code, out = run_cli(capsys, str(examples / "ring_buffer.cpp"), "--function", "push")
    assert code == EXIT_VERIFIED
    assert "default-constructed `RingBuffer`" in out


def test_too_small_a_bound_escalates_instead_of_reporting_a_bug(capsys, examples):
    """With --unwind 3 ESBMC says VERIFICATION FAILED (unwinding assertion)."""
    code, out = run_cli(capsys, str(examples / "ring_buffer.cpp"), "--unwind", "3")
    assert code == EXIT_VERIFIED
    assert "counterexample" not in out
    assert "unwind=12" in out  # the ladder widened the bound and concluded
    assert "Attempts: 2" in out


def test_json_output_carries_the_assumptions(capsys, examples):
    main([
        "verify", str(examples / "off_by_one.cpp"),
        "--function", "sum_array", "--no-llm", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "counterexample"
    assert payload["bounded"] is True
    assert payload["function"] == "sum_array"
    assert payload["assumptions"]
    assert payload["violated_property"]["loc"]["line"] == 7
    assert payload["config"]["unwind"] == 8
    assert any(a["lvalue"] == "n" for step in payload["trace"] for a in step["assignments"])


def test_a_precondition_turns_a_counterexample_into_a_proof(capsys, tmp_path):
    src = tmp_path / "avg.cpp"
    src.write_text(
        '#include "veripp/contracts.hpp"\n'
        "int average(int total, int count) { return total / count; }\n"
        "int average_guarded(int total, int count) {\n"
        "    VERIPP_REQUIRES(count > 0);\n"
        "    return total / count;\n"
        "}\n"
    , encoding="utf-8")
    bug_code, bug_out = run_cli(capsys, str(src), "--function", "average")
    assert bug_code == EXIT_COUNTEREXAMPLE
    assert "division by zero" in bug_out
    assert "count = 0" in bug_out

    ok_code, _ = run_cli(capsys, str(src), "--function", "average_guarded")
    assert ok_code == EXIT_VERIFIED
