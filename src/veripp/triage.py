"""Counterexample triage: real bug vs missing assumption vs harness issue.

The pilot that shaped this module: veripp's conservative offline default
labelled three real-library counterexamples `real_bug`; reading the
functions' call sites overturned all three, and every proposed precondition
was confirmed by the solver. So triage here is built around call sites, and
every proposal it produces goes back through ESBMC before it is believed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .cppsig import SignatureError, find_function, scrub
from .esbmc import VerifyResult
from .harness import HarnessOptions
from .llm import LLMClient, LLMError, TriageContext

MAX_CALL_SITES = 20

#: Failures that are the harness's doing by construction, whatever the code
#: under test does. Recognising them needs no model, and reporting them as
#: findings is how a verifier teaches people to ignore it.
_MECHANICAL_ARTIFACTS: tuple[tuple[str, str], ...] = (
    (
        "free() of non-dynamic memory",
        "the harness supplies this pointer from stack or static storage, so "
        "any free() of it fails by construction. Nothing is said about the "
        "code under test; verify a caller that allocates, or pre-fill the "
        "field with heap memory",
    ),
    (
        "Operand of free must have zero pointer offset",
        "the harness supplies this pointer, so the offset it is freed at is "
        "the harness's doing, not the library's",
    ),
)


def mechanical_artifact(result: VerifyResult, harness_path: Path) -> str | None:
    """Why this counterexample is an artifact, when that is decidable offline.

    A generated harness makes simplifications, and some failures follow from
    those simplifications alone. Those are not findings, and a tool whose
    loudest output is its own artifacts trains people to stop reading it.
    """
    prop = result.violated_property
    if prop is None:
        return None
    for needle, why in _MECHANICAL_ARTIFACTS:
        if needle in prop.description:
            return why
    # A property that fails inside the generated file, rather than in the code
    # under test, is by definition about the harness.
    if prop.loc.file and Path(prop.loc.file).name == harness_path.name:
        return (
            "the failing property is in the generated harness itself, not in "
            "the code under test"
        )
    return None


@dataclass
class TargetInfo:
    """What `--function` pointed at: enough to rebuild context and harnesses."""

    source: Path
    function: str
    options: HarnessOptions = field(default_factory=HarnessOptions)


@dataclass
class Diagnosis:
    kind: str  # "real_bug" | "missing_assumption" | "harness_issue"
    explanation: str
    proposed_precondition: str | None = None
    llm_error: str | None = None  # set when triage degraded to offline defaults


def build_context(
    target: TargetInfo | None, harness_path: Path, result: VerifyResult
) -> TriageContext:
    """Assemble what the triage LLM sees. Call sites are the decisive part."""
    function = "(whole file)"
    signature = ""
    parameters: list[str] = []
    function_source = ""
    call_sites: list[str] = []

    if target is not None:
        text = target.source.read_text()
        try:
            sig = find_function(text, target.function)
            function = sig.qualified_name
            params = ", ".join(f"{p.type} {p.name}" for p in sig.params)
            signature = f"{sig.return_type} {sig.qualified_name}({params})"
            parameters = [p.name for p in sig.params]
            function_source = _definition_snippet(text, sig.name, sig.body)
            call_sites = find_call_sites(text, sig.name)
        except SignatureError:
            function = target.function

    try:
        harness_code = harness_path.read_text()
    except OSError:
        harness_code = ""

    inputs = "\n".join(str(a) for a in result.input_assignments())
    return TriageContext(
        function=function,
        signature=signature or function,
        parameters=parameters,
        function_source=function_source,
        call_sites=call_sites,
        harness_code=harness_code,
        violated_property=str(result.violated_property or ""),
        counterexample_inputs=inputs or "(none recorded)",
        raw_output_tail=result.raw_output[-3000:],
    )


def find_call_sites(source: str, name: str) -> list[str]:
    """Lines in `source` that call `name`, excluding its own definition.

    What real callers pass is the evidence that separates a real bug from a
    missing precondition; without it triage is guessing.
    """
    scrubbed = scrub(source)
    lines = source.splitlines()
    sites: list[str] = []
    seen: set[int] = set()
    for m in re.finditer(rf"\b{re.escape(name)}\s*\(", scrubbed):
        if _is_declaration(scrubbed, m.start()):
            continue
        line_no = scrubbed.count("\n", 0, m.start()) + 1
        if line_no in seen:
            continue
        seen.add(line_no)
        sites.append(f"line {line_no}: {lines[line_no - 1].strip()}")
        if len(sites) >= MAX_CALL_SITES:
            break
    return sites


_CALL_PRECEDERS = {"return", "else", "case", "do", "throw", "goto", "and", "or", "not"}


def _is_declaration(scrubbed: str, name_start: int) -> bool:
    """A name immediately preceded by a type identifier is being declared.

    `int scale(` / `unsigned long f(` are declarations; `return scale(`,
    `= scale(`, `(scale(`, `x + scale(` are uses. Keywords like `return`
    can precede a call, so they do not count as type identifiers.
    """
    i = name_start - 1
    while i >= 0 and scrubbed[i].isspace():
        i -= 1
    if i < 0 or not (scrubbed[i].isalnum() or scrubbed[i] == "_"):
        return False  # punctuation before the name: a use
    end = i + 1
    while i >= 0 and (scrubbed[i].isalnum() or scrubbed[i] == "_"):
        i -= 1
    return scrubbed[i + 1 : end] not in _CALL_PRECEDERS


def _definition_snippet(text: str, name: str, body: str, max_chars: int = 3000) -> str:
    idx = text.find(body)
    if idx < 0:
        return body[:max_chars]
    start = text.rfind("\n", 0, max(0, text.rfind("\n", 0, idx) - 1)) + 1
    return text[start : idx + len(body) + 1][:max_chars]


def triage_counterexample(
    target: TargetInfo | None,
    harness_path: Path,
    result: VerifyResult,
    llm: LLMClient,
) -> Diagnosis:
    """Classify one counterexample; degrade to offline defaults on LLM failure."""
    context = build_context(target, harness_path, result)

    artifact = mechanical_artifact(result, harness_path)
    if artifact is not None:
        # Decidable without a model, so do not spend a call -- or risk one
        # disagreeing.
        return Diagnosis(
            kind="harness_issue",
            explanation=(
                f"{context.violated_property or 'Property violated'}. This is a "
                f"harness artifact: {artifact}."
            ),
        )

    try:
        kind = llm.classify(context)
        explanation = llm.explain(context)
        proposed = None
        # `harness_issue` gets a proposal too. When a harness fills a struct
        # with every possible field value, a model may reasonably call that a
        # wrongly-modelled input rather than a missing precondition -- and the
        # remedy is identical: constrain the input and let the solver rule.
        if kind in ("missing_assumption", "harness_issue") and context.parameters:
            proposed = llm.propose_precondition(context)
        return Diagnosis(kind=kind, explanation=explanation,
                         proposed_precondition=proposed)
    except LLMError as exc:
        return Diagnosis(
            kind="real_bug",  # conservative: surface it rather than hide it
            explanation=(
                f"{context.violated_property or 'Property violated'}. "
                f"Triage unavailable ({exc}); reporting conservatively as a "
                "potential real bug."
            ),
            llm_error=str(exc),
        )
