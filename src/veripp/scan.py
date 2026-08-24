"""Whole-file scanning: verify every function veripp can harness.

Naming one function at a time is fine for investigating a suspicion. Adopting
a verifier means pointing it at a file and asking what it can already prove --
and being told plainly what it cannot reach and why.
"""

from __future__ import annotations

import concurrent.futures as cf
from dataclasses import dataclass, field
from pathlib import Path

from .cppsig import SignatureError, function_definitions
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

    @property
    def proved(self) -> bool:
        return self.outcome == Outcome.VERIFIED.value


@dataclass
class ScanReport:
    source: Path
    results: list[FunctionResult] = field(default_factory=list)
    candidates: int = 0

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

    def summary(self) -> str:
        total = self.candidates or len(self.results)
        attempted = len(self.results) - len(self.refused)
        lines = [
            f"Scanned {self.source}",
            f"  {total} function definitions found, {attempted} harnessable "
            f"({100 * attempted / total:.0f}%)",
            "",
            f"  PROVED           {len(self.proved):4d}  "
            "no overflow, out-of-bounds, null deref or division by zero, "
            "within the stated bounds and assumptions",
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
        return "\n".join(lines)


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


def scan(
    source: Path,
    config: VerifyConfig,
    options: HarnessOptions | None = None,
    jobs: int = 4,
    only: list[str] | None = None,
    progress=None,
) -> ScanReport:
    """Harness and verify every function in `source` that veripp can model."""
    options = options or HarnessOptions()
    text = source.read_text(errors="replace")
    # `main` is an entry point, not a target: harnessing it would just wrap
    # the program's own main in another one.
    names = only or [n for n in function_definitions(text) if n != "main"]
    report = ScanReport(source=source, candidates=len(names))
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
        except (OSError, RuntimeError) as exc:
            return FunctionResult(name=name, outcome="tool_error",
                                  signature=printable, detail=str(exc))
        prop = result.violated_property
        return FunctionResult(
            name=name,
            outcome=result.outcome.value,
            signature=printable,
            detail=(prop.description if prop else (result.error or "")),
            assumptions=harness.assumptions,
            stubbed_calls=result.stubbed_calls,
            duration_s=result.duration_s,
            artifact=mechanical_artifact(result, path),
        )

    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for done, result in enumerate(pool.map(one, names), start=1):
            report.results.append(result)
            if progress is not None:
                progress(done, len(names), result)
    report.results.sort(key=lambda r: r.name)
    return report
