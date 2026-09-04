"""Runner and output parser for the ESBMC model checker.

This module is deliberately LLM-free: it builds command lines, runs the
verifier, and parses its output into structured results. It is the sole
source of truth for verification outcomes.

Everything here is calibrated against real ESBMC 8.4 output (see
`tests/golden/` for the captured transcripts the parser is tested on).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

#: ESBMC does not predefine a macro identifying itself, so veripp defines one.
#: `include/veripp/contracts.hpp` keys all of its behaviour off it.
ESBMC_MACRO = "__ESBMC__"


class Outcome(Enum):
    VERIFIED = "verified"              # VERIFICATION SUCCESSFUL
    COUNTEREXAMPLE = "counterexample"  # VERIFICATION FAILED with a real property
    UNWIND_LIMIT = "unwind_limit"      # bound too small to conclude
    PARSE_ERROR = "parse_error"        # frontend rejected the input
    TIMEOUT = "timeout"
    TOOL_ERROR = "tool_error"          # esbmc itself crashed or was misinvoked
    UNKNOWN = "unknown"                # VERIFICATION UNKNOWN (e.g. k-induction gave up)


@dataclass
class VerifyConfig:
    """Parameters of one verification attempt. Recorded verbatim in reports."""

    unwind: int = 32
    timeout_s: int = 120
    k_induction: bool = False
    incremental_bmc: bool = False
    # Every check below is on by default because it catches undefined
    # behaviour -- a real bug in anyone's C -- and was measured not to fire on
    # correct code. The measurement matters: an unmeasured check produced 14
    # of cJSON's 33 findings once, all of them noise, and a tool whose loudest
    # output is its own artifacts trains people to stop reading it.
    overflow_check: bool = True
    bounds_check: bool = True
    pointer_check: bool = True
    div_by_zero_check: bool = True
    memory_leak_check: bool = True
    uninitialised_check: bool = True
    #: Ask for a verdict on EVERY property instead of stopping at the first
    #: violation. Off by default because deciding all of them costs more than
    #: stopping at one, and timeouts are already the largest unhelpful
    #: outcome. Switched on for a second look at a run whose only failure was
    #: a harness artifact, where stopping first means nothing was checked.
    multi_property: bool = False
    ub_shift_check: bool = True
    #: Usable only because veripp writes the harness. With unconstrained
    #: nondet doubles a/b is NaN for inf/inf, so this check reports every
    #: floating-point division in correct code -- which is why raw ESBMC users
    #: leave it off. veripp constrains float inputs to finite values, and the
    #: check then does its job: quiet on code that guards its divisor, still
    #: catching a genuine 0.0/0.0.
    nan_check: bool = True

    #: Off by default, and this is a judgement rather than an oversight:
    #: unsigned wraparound is DEFINED behaviour in C. djb2 (`h * 33u + c`) is
    #: correct code, and this check reports it as a failure. Wanted only if
    #: you believe your own code should never wrap.
    unsigned_overflow_check: bool = False

    #: Termination is a liveness property, not a safety one, and needs its own
    #: ESBMC mode. Never folded into "verified": a k-induction proof reports
    #: SUCCESSFUL for a function that loops forever, because an infinite loop
    #: violates no assertion.
    termination: bool = False
    extra_args: list[str] = field(default_factory=list)
    include_dirs: list[Path] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    #: Extra translation units compiled alongside the harness. Linking the TU
    #: that defines a callee is a SOUNDNESS matter, not a convenience: ESBMC
    #: havocs an undefined function's return value but does not model its
    #: writes through pointer arguments, so an unlinked callee is silently
    #: assumed to have no side effects.
    link_sources: list[Path] = field(default_factory=list)
    cpp_std: str = "c++17"

    #: Standard used when the harness is C. The C++ standard in `cpp_std`
    #: cannot be passed to a .c file -- ESBMC rejects it outright.
    c_std: str = "c11"

    def std_for(self, source: Path) -> str:
        return self.c_std if source.suffix.lower() == ".c" else self.cpp_std

    def to_args(self, source: Path | None = None) -> list[str]:
        std = self.std_for(source) if source is not None else self.cpp_std
        args: list[str] = ["--std", std]
        if self.k_induction:
            args.append("--k-induction")
        elif self.incremental_bmc:
            args.append("--incremental-bmc")
        else:
            args += ["--unwind", str(self.unwind)]
        if self.overflow_check:
            args.append("--overflow-check")
        if self.unsigned_overflow_check:
            args.append("--unsigned-overflow-check")
        if self.memory_leak_check:
            args.append("--memory-leak-check")
        if self.multi_property:
            args.append("--multi-property")
        if self.uninitialised_check:
            args.append("--uninitialised-vars-check")
        # ESBMC's --ub-shift-check implicitly turns arithmetic overflow
        # checking back on, so passing it alongside --no-overflow-check would
        # silently ignore what the user asked for. They travel together: if
        # overflow checking is off, this goes off with it.
        if self.nan_check:
            args.append("--nan-check")
        if self.ub_shift_check and self.overflow_check:
            args.append("--ub-shift-check")
        if self.termination:
            args.append("--termination")
        # Bounds, pointer and division checks are on by default in ESBMC and
        # have no positive flag; the negative flags below let a config disable
        # them explicitly.
        if not self.bounds_check:
            args.append("--no-bounds-check")
        if not self.pointer_check:
            args.append("--no-pointer-check")
        if not self.div_by_zero_check:
            args.append("--no-div-by-zero-check")
        for inc in self.include_dirs:
            args += ["-I", str(inc)]
        for macro in [ESBMC_MACRO, *self.defines]:
            args += ["-D", macro]
        args += self.extra_args
        return args

    def describe(self) -> str:
        """One-line statement of the bounds this result was obtained under."""
        if self.k_induction:
            mode = "k-induction (unbounded if it converges)"
        elif self.incremental_bmc:
            mode = "incremental BMC"
        else:
            mode = f"bounded, unwind={self.unwind}"
        checks = [
            name
            for name, on in (
                ("overflow", self.overflow_check),
                ("unsigned-overflow", self.unsigned_overflow_check),
                ("bounds", self.bounds_check),
                ("pointer", self.pointer_check),
                ("div-by-zero", self.div_by_zero_check),
                ("memory-leak", self.memory_leak_check),
                ("uninitialised", self.uninitialised_check),
                ("ub-shift", self.ub_shift_check and self.overflow_check),
                ("nan", self.nan_check),
                ("termination", self.termination),
            )
            if on
        ]
        line = f"{mode}; checks: {', '.join(checks) or 'none'}; std={self.cpp_std}"
        # Raw ESBMC flags can silently weaken a result (--no-bounds-check is
        # one word), so a verdict obtained under them has to say so. Only the
        # checker flags are named; -I/-D and the harness plumbing are noise
        # here and are already reflected in the harness itself.
        passthrough = [a for a in self.extra_args if a.startswith("--")]
        if passthrough:
            line += f"; raw ESBMC flags: {' '.join(passthrough)}"
        return line


@dataclass
class SourceLoc:
    file: str
    line: int
    column: int | None = None
    function: str | None = None

    def __str__(self) -> str:
        loc = f"{self.file}:{self.line}"
        if self.column is not None:
            loc += f":{self.column}"
        return f"{loc} in {self.function}" if self.function else loc


@dataclass
class Assignment:
    """One variable assignment from a counterexample state."""

    lvalue: str
    value: str          # human-readable value, binary expansion stripped
    raw: str            # the line exactly as ESBMC printed it

    def __str__(self) -> str:
        return f"{self.lvalue} = {self.value}"


@dataclass
class TraceStep:
    file: str
    line: int
    column: int | None = None
    function: str | None = None
    state: int | None = None
    thread: int | None = None
    assignments: list[Assignment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "; ".join(str(a) for a in self.assignments)

    @property
    def loc(self) -> SourceLoc:
        return SourceLoc(self.file, self.line, self.column, self.function)


@dataclass
class ViolatedProperty:
    loc: SourceLoc
    description: str
    expression: str | None = None
    cwes: list[str] = field(default_factory=list)

    @property
    def is_unwinding_assertion(self) -> bool:
        return "unwinding assertion" in self.description

    def __str__(self) -> str:
        out = f"{self.description}\n  at {self.loc}"
        if self.expression:
            out += f"\n  {self.expression}"
        if self.cwes:
            out += f"\n  CWE: {', '.join(self.cwes)}"
        return out


_NO_BODY_RE = re.compile(r"^WARNING: no body for function (\S+)\s*$", re.M)


def bodiless_functions(output: str) -> list[str]:
    """Functions ESBMC found no definition for, in call order of first sight.

    These are the holes in a result. ESBMC havocs their return values, which
    is sound, but assumes they do not write through their pointer arguments,
    which is not: a "verified" that depends on one is only valid if the
    function really has no such side effect.
    """
    seen: dict[str, None] = {}
    for m in _NO_BODY_RE.finditer(output):
        seen.setdefault(m.group(1), None)
    return list(seen)


@dataclass
class VerifyResult:
    outcome: Outcome
    config: VerifyConfig
    properties: list[ViolatedProperty] = field(default_factory=list)
    trace: list[TraceStep] = field(default_factory=list)
    raw_output: str = ""
    duration_s: float | None = None
    exit_code: int | None = None
    error: str | None = None  # frontend/tool error message, when there is one

    @property
    def stubbed_calls(self) -> list[str]:
        """Callees with no body in this run (see `bodiless_functions`)."""
        return bodiless_functions(self.raw_output)

    @property
    def violated_property(self) -> ViolatedProperty | None:
        return self.properties[0] if self.properties else None

    @property
    def is_conclusive(self) -> bool:
        return self.outcome in (Outcome.VERIFIED, Outcome.COUNTEREXAMPLE)

    @property
    def failing_step(self) -> TraceStep | None:
        """The last state of the trace: where the property was violated."""
        return self.trace[-1] if self.trace else None

    def assignments(self) -> list[Assignment]:
        """Every assignment in the counterexample, in execution order."""
        return [a for step in self.trace for a in step.assignments]

    def input_summary(self, limit: int = 12, width: int = 90) -> list[str]:
        """Readable counterexample inputs: what a developer needs to reproduce.

        The raw trace is faithful but unusable for objects -- ESBMC prints the
        whole containing struct as the "value" of every field write, so an
        8-element array field produces eight near-identical multi-line dumps.
        This collapses array indices, keeps only the final value written to
        each location, and truncates the dumps.
        """
        latest: dict[str, str] = {}
        counts: dict[str, int] = {}
        for a in self.input_assignments():
            key = re.sub(r"\[\s*\d+\s*\]", "[*]", a.lvalue)
            latest[key] = a.value
            counts[key] = counts.get(key, 0) + 1

        lines: list[str] = []
        for key, value in list(latest.items())[:limit]:
            value = " ".join(value.split())
            if len(value) > width:
                value = value[: width - 3] + "..."
            suffix = f"   ({counts[key]} elements)" if counts[key] > 1 else ""
            lines.append(f"{key} = {value}{suffix}")
        if len(latest) > limit:
            lines.append(f"... and {len(latest) - limit} more")
        return lines

    def input_assignments(self) -> list[Assignment]:
        """Assignments made in main(): the concrete inputs that trigger the bug.

        These are what a developer needs to reproduce a counterexample, and
        they are the part of a trace that survives being read out of context.
        """
        return [
            a
            for step in self.trace
            if step.function == "main"
            for a in step.assignments
        ]


# --------------------------------------------------------------- parsing ---
#
# Shapes this parser is calibrated against (ESBMC 8.4):
#
#   VERIFICATION SUCCESSFUL
#   VERIFICATION FAILED    (preceded by [Counterexample] and a property block)
#   VERIFICATION UNKNOWN   (k-induction gave up)
#   ERROR: PARSING ERROR / ERROR: CONVERSION ERROR
#
# The critical distinction is that an insufficient unwind bound is *also*
# reported as VERIFICATION FAILED, with `unwinding assertion loop N` as the
# violated property. Reporting that as a counterexample would turn "I don't
# know" into "your code is broken", which is exactly the kind of overclaim
# this project refuses to make.

_STATE_RE = re.compile(
    r"^State (?P<state>\d+) file (?P<file>.+?) line (?P<line>\d+)"
    r"(?: column (?P<column>\d+))?"
    r"(?: function (?P<function>\S+))?"
    r"(?: thread (?P<thread>\d+))?\s*$",
    re.M,
)
_PROPERTY_RE = re.compile(r"^Violated property:\s*$", re.M)
_PROP_LOC_RE = re.compile(
    r"^\s*file (?P<file>.+?) line (?P<line>\d+)"
    r"(?: column (?P<column>\d+))?"
    r"(?: function (?P<function>\S+))?\s*$"
)
_ASSIGNMENT_RE = re.compile(r"^\s{2,}(?P<lvalue>\S.*?)\s*=\s*(?P<value>.*)$")
_BINARY_SUFFIX_RE = re.compile(r"\s*\((?:[01?]{8}(?:\s+[01?]{8})*)\)\s*$")
_DASHES_RE = re.compile(r"^\s*-{5,}\s*$")
_ERROR_RE = re.compile(r"^ERROR: (?P<message>.+)$", re.M)
_CLANG_ERROR_RE = re.compile(r"^(?P<message>.*?:\d+:\d+: (?:error|fatal error): .+)$", re.M)


def find_esbmc() -> str | None:
    """The checker to use, most explicit choice first.

    Most deliberate first. $VERIPP_ESBMC names a binary outright. A checker
    from `veripp install-checker` comes next: it was asked for by name, and it
    passed the soundness probes before being kept. Then PATH -- putting an
    esbmc there is a decision somebody made, and silently preferring our own
    would mean an ESBMC developer could not test their own build against
    veripp. The wheel bundled with `pip install veripp` is last: it is the
    default for people who have not chosen anything, not an override for
    people who have. A bad checker found on PATH is not a danger here, since
    `doctor` probes whichever one is selected and refuses to stand behind it.
    """
    if named := os.environ.get("VERIPP_ESBMC"):
        return named
    from .checker import bundled_esbmc, managed_esbmc

    return managed_esbmc() or shutil.which("esbmc") or bundled_esbmc()


def run(source: Path, config: VerifyConfig, esbmc_bin: str | None = None) -> VerifyResult:
    """Run one ESBMC invocation on a self-contained source file."""
    binary = esbmc_bin or find_esbmc()
    if binary is None:
        raise RuntimeError(
            "esbmc not found on PATH. Install from "
            "https://github.com/esbmc/esbmc/releases, or `brew install esbmc`"
        )
    cmd = [binary, str(source), *(str(s) for s in config.link_sources),
           *config.to_args(source)]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=config.timeout_s)
    except subprocess.TimeoutExpired as exc:
        return VerifyResult(
            Outcome.TIMEOUT,
            config,
            raw_output=_as_text(exc.stdout) + _as_text(exc.stderr),
            duration_s=time.monotonic() - started,
            error=f"esbmc exceeded the {config.timeout_s}s per-attempt timeout",
        )

    duration = time.monotonic() - started
    output = proc.stdout + ("\n" if proc.stdout and proc.stderr else "") + proc.stderr
    result = parse_output(output, config, exit_code=proc.returncode)
    result.duration_s = duration
    return result


def _as_text(stream) -> str:
    if stream is None:
        return ""
    return stream if isinstance(stream, str) else stream.decode("utf-8", "replace")


def parse_output(output: str, config: VerifyConfig, exit_code: int | None = None) -> VerifyResult:
    """Turn one ESBMC transcript into a structured result."""
    properties = _parse_properties(output)
    trace = _parse_trace(output)

    def result(outcome: Outcome, error: str | None = None) -> VerifyResult:
        return VerifyResult(
            outcome=outcome,
            config=config,
            properties=properties,
            trace=trace,
            raw_output=output,
            exit_code=exit_code,
            error=error,
        )

    if "VERIFICATION SUCCESSFUL" in output:
        return result(Outcome.VERIFIED)

    if "VERIFICATION FAILED" in output:
        # An unwinding assertion means the bound was too small, not that the
        # program is wrong. Only report a counterexample when at least one
        # violated property is a genuine one.
        real = [p for p in properties if not p.is_unwinding_assertion]
        if properties and not real:
            return result(Outcome.UNWIND_LIMIT)
        if real:
            # Keep the genuine properties first so `violated_property` is one.
            properties[:] = real + [p for p in properties if p.is_unwinding_assertion]
        return result(Outcome.COUNTEREXAMPLE)

    if "VERIFICATION UNKNOWN" in output:
        return result(Outcome.UNKNOWN, error=_unknown_reason(output))

    if "Timed out" in output:
        return result(Outcome.TIMEOUT, error="esbmc reported its own timeout")

    frontend = _frontend_error(output)
    if frontend is not None:
        return result(Outcome.PARSE_ERROR, error=frontend)

    tool = _tool_error(output, exit_code)
    if tool is not None:
        return result(Outcome.TOOL_ERROR, error=tool)

    return result(Outcome.UNKNOWN, error="esbmc produced no recognisable verdict")


# Kept for the internal callers/tests that predate the public name.
_parse_output = parse_output


def _parse_properties(output: str) -> list[ViolatedProperty]:
    properties: list[ViolatedProperty] = []
    for header in _PROPERTY_RE.finditer(output):
        block: list[str] = []
        for line in output[header.end() :].splitlines()[1:]:
            if not line.strip() or not line.startswith(" "):
                break
            block.append(line)
        if not block:
            continue
        loc_match = _PROP_LOC_RE.match(block[0])
        if loc_match is None:
            continue
        rest = [line.strip() for line in block[1:] if line.strip()]
        cwes: list[str] = []
        detail: list[str] = []
        for line in rest:
            if line.startswith("CWE:"):
                cwes += [c.strip() for c in line[len("CWE:") :].split(",") if c.strip()]
            else:
                detail.append(line)
        # When ESBMC prints both a description and the guard it checked, the
        # guard comes last. A single line is the description.
        expression = detail.pop() if len(detail) > 1 else None
        properties.append(
            ViolatedProperty(
                loc=SourceLoc(
                    file=loc_match.group("file"),
                    line=int(loc_match.group("line")),
                    column=int(loc_match.group("column") or 0) or None,
                    function=loc_match.group("function"),
                ),
                description=" ".join(detail) or "(no description)",
                expression=expression,
                cwes=cwes,
            )
        )
    return properties


def _parse_trace(output: str) -> list[TraceStep]:
    steps: list[TraceStep] = []
    matches = list(_STATE_RE.finditer(output))
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(output)
        step = TraceStep(
            file=m.group("file"),
            line=int(m.group("line")),
            column=int(m.group("column")) if m.group("column") else None,
            function=m.group("function"),
            state=int(m.group("state")),
            thread=int(m.group("thread")) if m.group("thread") else None,
            assignments=_parse_assignments(output[m.end() : end]),
        )
        steps.append(step)
    return steps


def _parse_assignments(block: str) -> list[Assignment]:
    """Assignments from one trace state, joining multi-line struct values.

    A struct value spans lines and its continuations contain `=` of their own
    (`.inner=nil, .next=nil }`), so "does this line look like an assignment"
    is not enough to tell a new assignment from a continuation. Brace balance
    is: while the value so far has an unclosed `{`, every following line
    belongs to it.
    """
    assignments: list[Assignment] = []
    open_braces = 0
    for line in block.splitlines():
        if not line.strip() or _DASHES_RE.match(line):
            continue
        if open_braces <= 0 and line.strip().startswith("Violated property"):
            break

        if open_braces > 0 and assignments:  # inside a multi-line value
            assignments[-1].value += " " + line.strip()
            assignments[-1].raw += "\n" + line
            open_braces += line.count("{") - line.count("}")
            continue

        m = _ASSIGNMENT_RE.match(line)
        if m is None:
            if assignments:
                assignments[-1].value += " " + line.strip()
                assignments[-1].raw += "\n" + line
            continue
        value = _BINARY_SUFFIX_RE.sub("", m.group("value")).strip()
        assignments.append(Assignment(lvalue=m.group("lvalue"), value=value, raw=line))
        open_braces = value.count("{") - value.count("}")
    return assignments


def _unknown_reason(output: str) -> str | None:
    for marker in (
        "The inductive step is unable to prove the property",
        "Unable to prove or falsify the program, giving up.",
        "Unwinding assertion",
    ):
        if marker in output:
            return marker.rstrip(".")
    return None


def _frontend_error(output: str) -> str | None:
    errors = [m.group("message") for m in _ERROR_RE.finditer(output)]
    frontend = [e for e in errors if "PARSING ERROR" in e or "CONVERSION ERROR" in e]
    if not frontend:
        return None
    details = [m.group("message").strip() for m in _CLANG_ERROR_RE.finditer(output)]
    details += [e for e in errors if e not in frontend]
    head = frontend[0].strip()
    return f"{head}: {details[0]}" if details else head


def _tool_error(output: str, exit_code: int | None) -> str | None:
    for line in output.splitlines():
        if "unrecognised option" in line or "unrecognized option" in line:
            return line.strip()
        if "terminating due to uncaught exception" in line:
            return line.strip()
    if exit_code not in (None, 0, 1):
        return f"esbmc exited with status {exit_code}"
    return None


# ------------------------------------------------ soundness self-check ---
#
# A model checker that answers "verified" on a program that provably fails is
# worse than no checker: every result built on it is a false proof. ESBMC 8.4
# has exactly such a hole (esbmc/esbmc#6508 -- fixed on master, unreleased):
# an out-of-bounds write to a member array is missed when the index is another
# member of the same object reached through `this` or a pointer, which is the
# ordinary container idiom. veripp refuses to present "verified" from a
# checker that fails this probe without saying so.

SOUNDNESS_PROBES: dict[str, tuple[str, str]] = {
    "member-array bounds (esbmc#6508)": (
        "struct S { int a[4]; unsigned n; };\n"
        "static void push(struct S *s, int v) { s->a[s->n++] = v; }\n"
        "int main(void) { struct S s; s.n = 0;\n"
        "  for (int i = 0; i < 5; ++i) push(&s, i);\n"
        "  return 0; }\n",
        "c11",
    ),
    "local-array bounds": (
        "int main(void) { int a[4]; unsigned n = 0;\n"
        "  for (int i = 0; i < 5; ++i) a[n++] = i;\n"
        "  return 0; }\n",
        "c11",
    ),
}


#: Code that pulls ARM's vector intrinsics. ESBMC's clang frontend does not
#: recognise every ARM builtin type -- `__mfp8` among them -- so on an arm64
#: host any translation unit reaching arm_neon.h dies with a CONVERSION
#: ERROR before a single property is checked. mbedTLS does exactly that.
#: Nothing is wrong with the code or the checker's logic; the same file
#: verifies on x86_64.
_ARM_INTRINSIC_PROBE = "#include <arm_neon.h>\nint main(void) { return 0; }\n"


def check_arm_intrinsics(esbmc_bin: str | None = None,
                         timeout_s: int = 60) -> bool | None:
    """Whether this checker can parse code that includes ARM intrinsics.

    Returns None when the host is not arm64, or when the header is absent --
    there is nothing to warn about in either case.
    """
    import platform
    import tempfile

    if platform.machine().lower() not in ("arm64", "aarch64"):
        return None
    binary = esbmc_bin or find_esbmc()
    if binary is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "arm_probe.c"
        path.write_text(_ARM_INTRINSIC_PROBE, encoding="utf-8")
        try:
            proc = subprocess.run(
                [binary, str(path), "--std", "c11"],
                capture_output=True, text=True, timeout=timeout_s,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
    out = proc.stdout + proc.stderr
    if "file not found" in out or "arm_neon.h" in out and "not found" in out:
        return None                      # no such header here; nothing to say
    return "Unrecognized clang builtin type" not in out


def check_soundness(esbmc_bin: str | None = None, timeout_s: int = 60) -> dict[str, bool]:
    """Run known-failing programs; each MUST be reported as failing.

    Returns probe name -> whether the checker correctly rejected it. A False
    means this installation silently misses that class of bug.
    """
    import tempfile

    binary = esbmc_bin or find_esbmc()
    if binary is None:
        raise RuntimeError("esbmc not found on PATH")

    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, (code, std) in SOUNDNESS_PROBES.items():
            path = Path(tmp) / "probe.c"
            path.write_text(code, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [binary, str(path), "--std", std, "--unwind", "8"],
                    capture_output=True, text=True, timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                results[name] = False
                continue
            out = proc.stdout + proc.stderr
            results[name] = "VERIFICATION FAILED" in out
    return results
