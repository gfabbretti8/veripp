"""Parser tests against pinned real ESBMC 8.4 output (tests/golden/)."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from veripp.agent import AgentReport
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
        assert "unwind=32" in VerifyConfig().describe()
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


class TestPropertySet:
    """Which properties veripp asks the checker about.

    Every check enabled by default catches undefined behaviour and was
    measured against a bug/clean pair and against real code that already
    verified. The ones left off are left off for a reason, not by oversight.
    """

    def test_undefined_behaviour_checks_are_on(self) -> None:
        from veripp.esbmc import VerifyConfig

        args = " ".join(str(a) for a in VerifyConfig().to_args(Path("x.c")))
        for flag in ("--overflow-check", "--memory-leak-check",
                     "--uninitialised-vars-check", "--ub-shift-check"):
            assert flag in args, f"{flag} should be on by default"

    def test_unsigned_overflow_is_off_by_default(self) -> None:
        """Unsigned wraparound is DEFINED behaviour in C. djb2 -- `h*33u + c`
        -- is correct code, and this check calls it a failure. Enabling it by
        default would report non-bugs on every hash and checksum."""
        from veripp.esbmc import VerifyConfig

        args = " ".join(str(a) for a in VerifyConfig().to_args(Path("x.c")))
        assert "--unsigned-overflow-check" not in args

    def test_termination_is_not_folded_into_the_default(self) -> None:
        """A liveness property, and one a safety proof says nothing about: a
        k-induction run reports SUCCESSFUL for a function that loops forever,
        because an infinite loop violates no assertion."""
        from veripp.esbmc import VerifyConfig

        args = " ".join(str(a) for a in VerifyConfig().to_args(Path("x.c")))
        assert "--termination" not in args
        assert "--termination" in " ".join(
            str(a) for a in VerifyConfig(termination=True).to_args(Path("x.c"))
        )

    def test_ub_shift_never_overrides_a_disabled_overflow_check(self) -> None:
        """ESBMC's --ub-shift-check implicitly re-enables arithmetic overflow
        checking. Passing both would silently ignore --no-overflow-check --
        the tool disregarding an instruction it was given. It broke the CVE
        demo, where the whole point is isolating a division by zero from an
        unrelated overflow.
        """
        from veripp.esbmc import VerifyConfig

        args = " ".join(
            str(a) for a in VerifyConfig(overflow_check=False).to_args(Path("x.c"))
        )
        assert "--overflow-check" not in args
        assert "--ub-shift-check" not in args, (
            "ub-shift silently turns overflow checking back on"
        )

    def test_the_verdict_names_every_check_that_ran(self) -> None:
        """"verified" must never be ambiguous about what it covered."""
        from veripp.esbmc import VerifyConfig

        described = VerifyConfig().describe()
        for name in ("overflow", "bounds", "pointer", "div-by-zero",
                     "memory-leak", "uninitialised", "ub-shift"):
            assert name in described, f"{name} missing from: {described}"
        # And a check that did not run must not be listed.
        assert "ub-shift" not in VerifyConfig(overflow_check=False).describe()

    def test_nan_checking_is_on_because_the_harness_makes_it_usable(self) -> None:
        """Raw ESBMC users leave --nan-check off for a reason: with
        unconstrained nondet doubles, a/b is NaN for inf/inf, so it reports
        every floating-point division in correct code. veripp writes the
        harness and constrains float inputs to finite values, which turns a
        check nobody can use into one that works.
        """
        from veripp.esbmc import VerifyConfig

        args = " ".join(str(a) for a in VerifyConfig().to_args(Path("x.c")))
        assert "--nan-check" in args

    def test_the_harness_constrains_floats_to_finite_values(self) -> None:
        header = (
            Path(__file__).resolve().parent.parent
            / "src/veripp/include/veripp/contracts.hpp"
        ).read_text(encoding="utf-8")
        assert "veripp_finite_double" in header
        assert "__ESBMC_assume" in header
        assert "value == value" in header, "the NaN exclusion"

    def test_float_overflow_to_infinity_is_explained_not_just_reported(self) -> None:
        """finite / finite can exceed a double's range and give infinity --
        defined IEEE behaviour, which ESBMC cannot separate from integer
        overflow. Reporting it like a memory error would mislead."""
        from veripp.triage import _MECHANICAL_ARTIFACTS

        reasons = [why for needle, why in _MECHANICAL_ARTIFACTS
                   if "floating-point" in needle]
        assert reasons, "float overflow has no explanation"
        assert "defined IEEE behaviour" in reasons[0]


def _verified():
    """A minimal VerifyResult standing in for a successful safety run."""
    return SimpleNamespace(outcome=Outcome.VERIFIED, config=VerifyConfig())


class TestTermination:
    """Termination is a liveness property and never folds into "verified".

    Measured against ESBMC 8.4 on `while (n != 0) n -= 2;`:

        --k-induction               non-terminating -> SUCCESSFUL   <-- trap
        --k-induction --termination non-terminating -> UNKNOWN
        --termination               non-terminating -> UNKNOWN

    So a k-induction success says nothing about termination, and the only
    honest source for the termination field is a run that asked for it.
    """

    def test_termination_flag_is_forced_not_inherited(self, monkeypatch):
        from dataclasses import replace
        import veripp.agent as agent

        seen = {}

        def fake_run(src, config):
            seen["termination"] = config.termination
            seen["k_induction"] = config.k_induction
            return SimpleNamespace(outcome=Outcome.VERIFIED)

        monkeypatch.setattr(agent, "run", fake_run)
        # k-induction on, termination off: the trap configuration.
        cfg = replace(VerifyConfig(), k_induction=True, termination=False)
        agent._check_termination(Path("x.c"), cfg)
        assert seen["termination"] is True, (
            "the termination question must force --termination; a k-induction "
            "SUCCESSFUL alone is measured to hold for non-terminating code"
        )

    def test_not_proved_is_not_a_claim_of_looping_forever(self):
        # ESBMC proves termination but does not refute it, so False must read
        # as "not proved" everywhere it is rendered.
        assert AgentReport(final=_verified()).terminates is None

    def test_no_loop_means_the_question_is_not_asked(self, tmp_path):
        import veripp.agent as agent

        src = tmp_path / "straight.c"
        src.write_text("int f(int x){ return x + 1; }  // no loop here\n")
        target = SimpleNamespace(source=src, function="f")
        assert agent._might_not_terminate(target) is False

    def test_a_loop_in_a_comment_does_not_buy_a_run(self, tmp_path):
        import veripp.agent as agent

        src = tmp_path / "prose.c"
        src.write_text("/* we loop for each element, conceptually */\nint f(void){return 0;}\n")
        target = SimpleNamespace(source=src, function="f")
        assert agent._might_not_terminate(target) is False


@pytest.mark.esbmc
class TestANonTerminatingFunctionNeverReadsAsVerified:
    """The acceptance test for the termination work.

    Raw ESBMC reports SUCCESSFUL under --k-induction for `while (n != 0)
    n -= 2;` with n odd -- a loop that never finishes. Two things stop that
    reaching a user as "verified": the termination question always forces
    --termination rather than reading an inherited verdict (pinned by
    TestTermination), and veripp's default check set is rich enough that the
    inductive step does not converge here at all.

    The second is incidental and could change with an ESBMC release; the
    first is the guarantee. This test watches the user-visible outcome.
    """

    def test_it_reads_as_inconclusive(self, tmp_path):
        src = tmp_path / "spin.c"
        src.write_text(
            "unsigned spin(void) {\n"
            "    unsigned n = 7;\n"
            "    while (n != 0) { n -= 2; }\n"
            "    return n;\n"
            "}\n"
        )
        out = subprocess.run(
            [sys.executable, "-m", "veripp.cli", "verify", str(src),
             "--function", "spin", "--unwind", "2", "--no-llm", "--json"],
            capture_output=True, text=True, timeout=900,
        )
        payload = json.loads(out.stdout)
        assert payload["outcome"] != "verified", (
            "a function that never terminates was reported as verified"
        )
        assert payload["terminates"] is not True
