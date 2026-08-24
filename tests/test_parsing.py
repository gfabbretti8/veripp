import pytest

"""Parser tests against pinned real ESBMC 8.4 output (tests/golden/)."""

from veripp.esbmc import Outcome, VerifyConfig, parse_output

CFG = VerifyConfig()


def test_verified(golden):
    result = parse_output(golden("verified"), CFG, exit_code=0)
    assert result.outcome is Outcome.VERIFIED
    assert result.is_conclusive
    assert result.properties == []


def test_counterexample(golden):
    result = parse_output(golden("counterexample"), CFG, exit_code=1)
    assert result.outcome is Outcome.COUNTEREXAMPLE

    prop = result.violated_property
    assert prop is not None
    assert prop.description == "dereference failure: array bounds violated"
    assert prop.loc.file.endswith("off_by_one.cpp")
    assert (prop.loc.line, prop.loc.column, prop.loc.function) == (7, 9, "sum_array")
    assert "CWE-125" in prop.cwes
    assert not prop.is_unwinding_assertion


def test_counterexample_trace_has_variable_assignments(golden):
    result = parse_output(golden("counterexample"), CFG, exit_code=1)

    # The trace must carry concrete values, not just line numbers: `n = 4` is
    # the whole point of the counterexample.
    inputs = {a.lvalue: a.value for a in result.input_assignments()}
    assert inputs["n"] == "4"
    assert inputs["a"] == "{ 0, 0, 0, 0 }"
    assert all(step.function for step in result.trace)
    assert result.trace[0].state == 1

    # Binary expansions are stripped from the value but kept in `raw`.
    n_assignment = next(a for a in result.assignments() if a.lvalue == "n")
    assert "(" not in n_assignment.value
    assert "00000100" in n_assignment.raw


def test_unwinding_assertion_is_not_a_counterexample(golden):
    """The bound was too small. Calling that a bug is the worst thing we can do."""
    result = parse_output(golden("unwind_limit"), CFG, exit_code=1)

    assert "VERIFICATION FAILED" in result.raw_output
    assert result.outcome is Outcome.UNWIND_LIMIT
    assert not result.is_conclusive
    assert result.violated_property.is_unwinding_assertion


def test_unknown(golden):
    result = parse_output(golden("unknown"), CFG, exit_code=1)
    assert result.outcome is Outcome.UNKNOWN
    assert not result.is_conclusive
    assert "inductive step" in (result.error or "")


def test_conversion_error(golden):
    result = parse_output(golden("conversion_error"), CFG, exit_code=6)
    assert result.outcome is Outcome.PARSE_ERROR
    assert "CONVERSION ERROR" in result.error
    assert "main" in result.error


def test_missing_include_reports_the_clang_message(golden):
    result = parse_output(golden("missing_include"), CFG, exit_code=6)
    assert result.outcome is Outcome.PARSE_ERROR
    assert "'veripp/contracts.hpp' file not found" in result.error


def test_parse_error(golden):
    result = parse_output(golden("parse_error"), CFG, exit_code=6)
    assert result.outcome is Outcome.PARSE_ERROR
    assert "expected '}'" in result.error


def test_unrecognised_option_is_a_tool_error():
    output = (
        "libc++abi: terminating due to uncaught exception of type "
        "boost::wrapexcept<boost::program_options::unknown_option>: "
        "unrecognised option '--div-by-zero-check'\n"
    )
    result = parse_output(output, CFG, exit_code=134)
    assert result.outcome is Outcome.TOOL_ERROR
    assert "--div-by-zero-check" in result.error


def test_no_verdict_at_all():
    result = parse_output("", CFG, exit_code=0)
    assert result.outcome is Outcome.UNKNOWN


class TestConfigArgs:
    def test_defines_the_esbmc_macro(self):
        # ESBMC does not predefine __ESBMC__; contracts.hpp depends on it.
        args = VerifyConfig().to_args()
        assert args[args.index("-D") + 1] == "__ESBMC__"

    def test_bounded_mode(self):
        args = VerifyConfig(unwind=16).to_args()
        assert ["--unwind", "16"] == args[args.index("--unwind") : args.index("--unwind") + 2]
        assert "--k-induction" not in args

    def test_k_induction_replaces_the_bound(self):
        args = VerifyConfig(k_induction=True).to_args()
        assert "--k-induction" in args
        assert "--unwind" not in args

    def test_only_negative_flags_exist_for_default_on_checks(self):
        # ESBMC 8.4 has no --bounds-check/--div-by-zero-check; passing one
        # aborts the process, so a config must never emit them.
        on = VerifyConfig().to_args()
        assert "--div-by-zero-check" not in on
        assert "--bounds-check" not in on
        off = VerifyConfig(bounds_check=False, div_by_zero_check=False).to_args()
        assert "--no-bounds-check" in off
        assert "--no-div-by-zero-check" in off

    def test_describe_states_the_bound(self):
        assert "unwind=8" in VerifyConfig().describe()
        assert "k-induction" in VerifyConfig(k_induction=True).describe()


def test_a_segfaulting_esbmc_is_a_tool_error_not_a_verdict():
    """ESBMC 8.4 segfaults converting some real C++ translation units.

    Seen for real on tinyxml2: it prints "Converting" and dies with SIGSEGV,
    leaving no verdict at all. Anything other than TOOL_ERROR here would let
    the agent escalate against a crashing binary, or worse, report the silence
    as a result.
    """
    output = (
        "Target: 64-bit little-endian aarch64-unknown-macos with esbmclibc\n"
        "Parsing harness.cpp\nConverting\n"
    )
    result = parse_output(output, CFG, exit_code=139)
    assert result.outcome is Outcome.TOOL_ERROR
    assert "139" in result.error
    assert not result.is_conclusive


@pytest.mark.esbmc
def test_soundness_probes_are_wired_and_meaningful():
    """The probes must actually be programs that fail; a good checker rejects both.

    This does not assert the local esbmc is sound -- 8.4 is not, by design of
    the check -- only that the probe harness reports per-probe booleans.
    """
    from veripp.esbmc import SOUNDNESS_PROBES, check_soundness

    results = check_soundness()
    assert set(results) == set(SOUNDNESS_PROBES)
    assert all(isinstance(v, bool) for v in results.values())
    # A checker that misses a plain local-array overflow is beyond salvage.
    assert results["local-array bounds"] is True


def test_multiline_struct_values_are_one_assignment():
    """ESBMC prints a struct value across lines, and the continuations contain
    `=` of their own -- so line shape alone cannot separate them."""
    output = (
        "[Counterexample]\n\n"
        "State 1 file h.cpp line 23 column 5 function main thread 0\n"
        "----------------------------------------------------\n"
        "  w_obj.count = { .count=nondet_symbol(nondet0), .name=nil,\n"
        "    .inner=nil, .next=nil }\n"
        "\nVERIFICATION FAILED\n"
    )
    result = parse_output(output, CFG, exit_code=1)
    assignments = result.assignments()
    assert len(assignments) == 1
    assert assignments[0].lvalue == "w_obj.count"
    assert ".next=nil }" in assignments[0].value


def test_input_summary_collapses_arrays_and_truncates():
    states = "".join(
        f"State {i} file h.cpp line 25 column 9 function main thread 0\n"
        "----------------------------------------------------\n"
        f"  obj.name[{i}] = {'x' * 200}\n\n"
        for i in range(8)
    )
    result = parse_output("[Counterexample]\n\n" + states + "VERIFICATION FAILED\n",
                          CFG, exit_code=1)
    summary = result.input_summary()
    assert len(summary) == 1
    assert "obj.name[*]" in summary[0]
    assert "(8 elements)" in summary[0]
    assert len(summary[0]) < 160  # truncated, not a wall of text
