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
    try:
        kind = llm.classify(context)
        explanation = llm.explain(context)
        proposed = None
        if kind == "missing_assumption" and context.parameters:
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
