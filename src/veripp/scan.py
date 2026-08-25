"""Whole-file scanning: verify every function veripp can harness.

Naming one function at a time is fine for investigating a suspicion. Adopting
a verifier means pointing it at a file and asking what it can already prove --
and being told plainly what it cannot reach and why.
"""

from __future__ import annotations

import concurrent.futures as cf
from dataclasses import dataclass, field
import re
from pathlib import Path

from .cppsig import (
    SignatureError,
    function_definitions,
    match_bracket,
    scrub,
)
from dataclasses import replace as _replace

from .esbmc import Outcome, VerifyConfig, run
from .harness import HarnessError, HarnessOptions, generate
from .paths import scratch_dir
from .triage import mechanical_artifact


@dataclass
class FunctionResult:
    name: str
    outcome: str                       # Outcome value, or "refused"
    signature: str = ""
    detail: str = ""                   # violated property, or why it was refused
    assumptions: list[str] = field(default_factory=list)
    stubbed_calls: list[str] = field(default_factory=list)
    duration_s: float | None = None
    #: Set when the failure follows from the harness's own simplifications.
    artifact: str | None = None
    #: Where the property fails. Carried from the checker so a finding can be
    #: reported at a line -- SARIF, and therefore an inline annotation on a
    #: pull request, is useless without one.
    file: str = ""
    line: int = 0
    column: int = 0
    cwes: list[str] = field(default_factory=list)
    #: Termination, asked only for proved functions that contain a loop.
    #: None = not asked. Kept out of `outcome` on purpose: it is a liveness
    #: property, and `verify` reports it the same way. The two commands
    #: disagreeing about one function is the bug this file already guards
    #: against for the unwind ladder.
    terminates: bool | None = None

    @property
    def proved(self) -> bool:
        return self.outcome == Outcome.VERIFIED.value


@dataclass
class ScanReport:
    source: Path
    results: list[FunctionResult] = field(default_factory=list)
    candidates: int = 0

    @property
    def terminating(self) -> list[FunctionResult]:
        """Proved functions that were also proved to terminate."""
        return [r for r in self.results if r.terminates is True]

    @property
    def termination_unproved(self) -> list[FunctionResult]:
        """Asked, and the checker could not prove it. Not a claim that they
        loop forever -- ESBMC proves termination but does not refute it."""
        return [r for r in self.results if r.terminates is False]

    @property
    def proved(self) -> list[FunctionResult]:
        return [r for r in self.results if r.proved]

    @property
    def counterexamples(self) -> list[FunctionResult]:
        """Failures worth a human's attention: artifacts are excluded."""
        return [
            r for r in self.results
            if r.outcome == Outcome.COUNTEREXAMPLE.value and not r.artifact
        ]

    @property
    def artifacts(self) -> list[FunctionResult]:
        return [r for r in self.results if r.artifact]

    @property
    def refused(self) -> list[FunctionResult]:
        return [r for r in self.results if r.outcome == "refused"]

    @property
    def inconclusive(self) -> list[FunctionResult]:
        done = {Outcome.VERIFIED.value, Outcome.COUNTEREXAMPLE.value, "refused"}
        return [r for r in self.results if r.outcome not in done]

    def refusal_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.refused:
            counts[_reason(r.detail)] = counts.get(_reason(r.detail), 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    #: A wrapper whose only job is to define a macro and include the real
    #: header. Very common for single-header libraries, and scanning it finds
    #: nothing at all.
    implementation_hint: str | None = None

    def summary(self) -> str:
        total = self.candidates or len(self.results)
        if not total:
            lines = [f"Scanned {self.source}", "  no function definitions found."]
            if self.implementation_hint:
                lines.append(f"  {self.implementation_hint}")
            else:
                lines.append(
                    "  Nothing here defines a function -- if this is a header of "
                    "declarations, scan the .c/.cpp that implements them."
                )
            return "\n".join(lines)
        attempted = len(self.results) - len(self.refused)
        lines = [
            f"Scanned {self.source}",
            f"  {total} function definitions found, {attempted} harnessable "
            f"({100 * attempted / total:.0f}%)",
            "",
            f"  PROVED           {len(self.proved):4d}  "
            "free of undefined behaviour, within the stated bounds and "
            "assumptions",
            *([
                f"    ...of which    {len(self.terminating):4d}  "
                "also proved to terminate"
                + (
                    f" ({len(self.termination_unproved)} asked, not proved --"
                    " an open question, not a bug)"
                    if self.termination_unproved else ""
                )
            ] if self.terminating or self.termination_unproved else []),
            f"  COUNTEREXAMPLE   {len(self.counterexamples):4d}  "
            "a property fails for some input -- triage each one",
            f"  HARNESS ARTIFACT {len(self.artifacts):4d}  "
            "failed because of how the harness was built, not the code",
            f"  INCONCLUSIVE     {len(self.inconclusive):4d}  "
            "timed out, hit the unwind bound, or the frontend refused it",
            f"  NOT HARNESSABLE  {len(self.refused):4d}  "
            "veripp could not build inputs for the signature",
        ]
        if self.refused:
            lines += ["", "  why functions were not harnessable:"]
            for reason, count in self.refusal_reasons().items():
                lines.append(f"    {count:4d}  {reason}")
        if self.counterexamples:
            lines += ["", "  counterexamples (most likely to matter first):"]
            for r in sorted(self.counterexamples, key=lambda r: r.name)[:20]:
                lines.append(f"    {r.name}: {r.detail}")
            if len(self.counterexamples) > 20:
                lines.append(f"    ... and {len(self.counterexamples) - 20} more")

        # End on something to do. A tally answers "what happened"; the reader's
        # actual question is "what now", and the answer differs by result.
        nudge = self._next_step()
        if nudge:
            lines += ["", nudge]
        return "\n".join(lines)

    def _next_step(self) -> str | None:
        """The single most useful next command, given how this scan went."""
        where = self.source

        if self.counterexamples:
            first = sorted(self.counterexamples, key=lambda r: r.name)[0]
            return (
                f"  next:  veripp verify {where} --function {first.name}\n"
                "         to see the failing input. A counterexample holds in the\n"
                "         generated harness; check a caller can really reach it."
            )
        if self.inconclusive:
            return (
                f"  next:  veripp verify {where} --function "
                f"{sorted(self.inconclusive, key=lambda r: r.name)[0].name} "
                "--unwind 16 --timeout 300\n"
                "         nothing was proved or disproved for these; more budget\n"
                "         sometimes settles it."
            )
        if self.artifacts:
            return (
                "  next:  veripp harness "
                f"{where} --function "
                f"{sorted(self.artifacts, key=lambda r: r.name)[0].name}\n"
                "         these failed on how the harness was built, not on the\n"
                "         code; read it to see what it assumed."
            )
        if self.proved and not self.refusal_reasons():
            return None
        return None


_IMPL_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w*IMPL\w*)", re.M | re.I)
_LOCAL_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', re.M)


def _implementation_hint(source: Path, text: str) -> str | None:
    """Advice for a file that only switches on an implementation header.

    Single-header libraries ship a .c whose whole content is a #define and an
    #include. Scanning it finds nothing, and "0 functions" is a useless answer
    when the fix is one flag away.
    """
    includes = _LOCAL_INCLUDE.findall(text)
    if not includes:
        return None
    header = includes[0]
    defines = _IMPL_DEFINE_RE.findall(text)
    flags = " ".join(f"-D {d}" for d in defines)
    target = source.parent / header
    where = target if target.is_file() else header
    return (
        f"This file only switches on `{header}`. The code is in the header: "
        f"veripp scan {where}" + (f" {flags}" if flags else "")
    )


def _reason(detail: str) -> str:
    """Collapse a refusal message to its class, for counting."""
    for needle, label in (
        ("cannot build a nondeterministic value", "parameter type not modelled"),
        ("cannot construct", "parameter is a type veripp cannot construct"),
        ("defined", "overloaded (disambiguate with --function 'f(types)')"),
        ("destructor", "destructor"),
        ("constructor", "constructor"),
        ("operator", "operator"),
        ("template", "template"),
        ("initialiser list", "not a definition (constructor initialiser list)"),
        ("variadic", "variadic"),
        ("no definition", "declaration only, no definition here"),
    ):
        if needle in detail:
            return label
    return detail[:60]


_LOOP_RE = re.compile(r"\b(while|for|goto)\b")


def _body_has_loop(scrubbed: str, name: str) -> bool:
    """Whether `name`'s own body contains something that could not finish.

    `verify` asks this of the whole translation unit, because it runs one
    function and an extra run costs nothing much. A scan runs hundreds, and
    a file-wide test would charge every proved function for one loop anywhere
    in the file, so this looks at the function's own body.

    The narrower test can miss a target whose body is loop-free but whose
    callee loops. That shows up as termination being *unasked* rather than
    answered wrongly -- the report says nothing about termination for that
    function, which is the honest outcome for a question nobody put.

    Takes already-scrubbed source: scrub() removes comments and string
    literals, so prose about looping "for each element" cannot trigger this.
    """
    for m in re.finditer(rf"\b{re.escape(name)}\s*\(", scrubbed):
        try:
            rparen = match_bracket(scrubbed, scrubbed.index("(", m.end() - 1))
        except (SignatureError, ValueError):
            continue
        brace = scrubbed.find("{", rparen)
        if brace == -1 or ";" in scrubbed[rparen + 1 : brace]:
            continue  # a declaration or a call, not a definition
        try:
            end = match_bracket(scrubbed, brace)
        except SignatureError:
            continue
        return bool(_LOOP_RE.search(scrubbed[brace:end]))
    return False


def scan(
    source: Path,
    config: VerifyConfig,
    options: HarnessOptions | None = None,
    jobs: int = 4,
    only: list[str] | None = None,
    progress=None,
    escalations: int = 1,
) -> ScanReport:
    """Harness and verify every function in `source` that veripp can model."""
    options = options or HarnessOptions()
    text = source.read_text(encoding="utf-8", errors="replace")
    # `main` is an entry point, not a target: harnessing it would just wrap
    # the program's own main in another one.
    names = only or [n for n in function_definitions(text) if n != "main"]
    scrubbed = scrub(text)   # once: comments and string literals removed
    report = ScanReport(source=source, candidates=len(names))
    if not names:
        report.implementation_hint = _implementation_hint(source, text)
        return report
    workdir = scratch_dir("veripp-scan-")

    def one(name: str) -> FunctionResult:
        try:
            harness = generate(source, name, options)
        except (HarnessError, SignatureError) as exc:
            return FunctionResult(name=name, outcome="refused", detail=str(exc))
        sig = harness.signature
        printable = "{} {}({})".format(
            sig.return_type,
            sig.qualified_name,
            ", ".join(f"{p.type} {p.name}" for p in sig.params),
        )
        try:
            path = harness.write(workdir, tag=name)
            result = run(path, config)
            # `verify` widens the bound when it runs out; without the same
            # here, `scan` reports "inconclusive" for functions `verify` would
            # have settled, and the two commands disagree about one function.
            attempt, widened = 0, config
            while (
                result.outcome is Outcome.UNWIND_LIMIT and attempt < escalations
            ):
                widened = _replace(widened, unwind=widened.unwind * 4)
                result = run(path, widened)
                attempt += 1
            # Same policy as `verify`: only for a function that proved, and
            # only when there is a loop that could fail to terminate. Costs
            # one extra run on those, and nothing on the rest.
            terminates = None
            if result.outcome is Outcome.VERIFIED and _body_has_loop(
                scrubbed, name
            ):
                terminates = (
                    run(path, _replace(widened, termination=True)).outcome
                    is Outcome.VERIFIED
                )
        except (OSError, RuntimeError) as exc:
            return FunctionResult(name=name, outcome="tool_error",
                                  signature=printable, detail=str(exc))
        prop = result.violated_property
        return FunctionResult(
            name=name,
            terminates=terminates,
            outcome=result.outcome.value,
            signature=printable,
            detail=(prop.description if prop else (result.error or "")),
            assumptions=harness.assumptions,
            stubbed_calls=result.stubbed_calls,
            duration_s=result.duration_s,
            artifact=mechanical_artifact(result, path),
            file=(prop.loc.file if prop and prop.loc else ""),
            line=(prop.loc.line if prop and prop.loc else 0),
            column=(prop.loc.column if prop and prop.loc else 0),
            cwes=list(getattr(prop, "cwes", []) or []) if prop else [],
        )

    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for done, result in enumerate(pool.map(one, names), start=1):
            report.results.append(result)
            if progress is not None:
                progress(done, len(names), result)
    report.results.sort(key=lambda r: r.name)
    return report
