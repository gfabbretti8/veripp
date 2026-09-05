"""Counterexample triage: real bug vs missing assumption vs harness issue.

The pilot that shaped this module: veripp's conservative offline default
labelled three real-library counterexamples `real_bug`; reading the
functions' call sites overturned all three, and every proposed precondition
was confirmed by the solver. So triage here is built around call sites, and
every proposal it produces goes back through ESBMC before it is believed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .cppsig import SignatureError, find_function, scrub
from .esbmc import VerifyResult, ViolatedProperty
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
    (
        # finite / finite can exceed a double's range and give infinity. That
        # is defined IEEE behaviour, and ESBMC cannot separate it from integer
        # overflow, so it fires on any function doing float arithmetic.
        "arithmetic overflow on floating-point ieee_",
        "dividing or multiplying two finite doubles can exceed the range of a "
        "double and give infinity. That is defined IEEE behaviour rather than "
        "undefined behaviour, and ESBMC cannot separate it from integer "
        "overflow. Treat it as a finding only if overflow to infinity matters "
        "in your domain",
    ),
    (
        # Reproduced in eight lines: allocating through a function pointer
        # yields this, while the same code calling malloc directly verifies.
        # The checker cannot establish the alignment of a pointer returned by
        # a call it could not resolve, so it assumes the worst. In cJSON this
        # one pattern accounted for 14 of 33 counterexamples -- every one of
        # them about the allocator being opaque, none about the library.
        "Incorrect alignment when accessing data object",
        "the pointer comes from an allocator the checker could not resolve "
        "(commonly an indirect call through a hooks struct), so it cannot "
        "establish the alignment and assumes the worst. Real allocators "
        "return suitably aligned memory. Link the allocator with --link, or "
        "point veripp at compile_commands.json, to check this properly",
    ),
)

#: Allocators whose absence makes every pointer downstream unconstrained.
#: `parson_malloc`-style indirection is the usual way a body goes missing:
#: the library declares a function POINTER so callers can swap the allocator,
#: and the checker cannot resolve the call to its own model of malloc.
_ALLOCATORS = frozenset({
    "malloc", "calloc", "realloc", "aligned_alloc", "memalign",
    "posix_memalign", "valloc", "pvalloc", "strdup", "strndup", "operator new",
})

#: Properties that follow from an unresolved allocator alone. Unlike the
#: alignment rule above these fire on real bugs too, so they only count as
#: artifacts when an allocator is actually among the stubbed calls.
_UNRESOLVED_ALLOCATION_PROPERTIES: tuple[str, ...] = (
    "dereference failure: invalid pointer",
    "dereference failure: NULL pointer",
    "dereference failure: Access to object out of bounds",
    "dereference failure: Access of non-dynamic memory",
    "forgotten memory",
)


def _is_allocator(name: str) -> bool:
    """Whether a bodiless callee is one that hands back memory.

    The C names are exact; everything else is a library wrapping them, and
    they all say so in the name -- lwIP's `pbuf_alloc`, `mem_malloc` and
    `memp_malloc`, glib's `g_malloc`, ffmpeg's `av_malloc`. Matching on
    "alloc" is a heuristic, but it only ever applies to a callee that really
    has no body in the run, and what it does is downgrade a counterexample to
    an artifact with the reason spelled out. Being wrong costs a lead that
    was about to be wrong anyway.
    """
    lowered = name.lower()
    return name in _ALLOCATORS or "alloc" in lowered


def _stubbed_allocators(result: VerifyResult) -> list[str]:
    return sorted(a for a in result.stubbed_calls if _is_allocator(a))


#: How `Harness.write` names what it produces. A path without this prefix is
#: the user's own file, not something veripp made.
GENERATED_HARNESS_PREFIX = "veripp_harness_"


#: Wording ESBMC uses that settles the direction on its own.
_EXPLICIT_WRITES = ("memset of", "on DST", "writing memory segment")
_EXPLICIT_READS = ("reading memory segment", "on SRC")

#: The dereferenced expression standing on the left of a plain `=`. Not `==`,
#: not `<=`; and `+=` and friends read as well as write, so they count.
_ASSIGNMENT_RE = re.compile(r"^[^=!<>]*[\]\)\w]\s*(?:[-+*/|&^]|<<|>>)?=(?!=)")


def access_kind(prop: ViolatedProperty) -> str | None:
    """Whether a violated dereference is a write, a read, or undetermined.

    A read past a buffer and a write past one are the same property to a
    solver and very different things afterwards. Measuring lwIP's allocator
    made that concrete: it puts a `struct mem` header after every block, and
    the first byte of that header is zero -- so a walk-to-NUL running off a
    pbuf stops after one byte, while a write off the same pbuf lands in the
    heap's own metadata.

    ESBMC says which it is for some properties and not for the commonest
    one, `array bounds violated`. Where it does not, the source line is
    read: an assignment to the failing expression is a write. Where neither
    settles it the answer is None, because guessing the direction would be
    guessing the severity.
    """
    description = prop.description
    if any(marker in description for marker in _EXPLICIT_WRITES):
        return "write"
    if any(marker in description for marker in _EXPLICIT_READS):
        return "read"
    line = _source_line(prop)
    if line is None:
        return None
    if _ASSIGNMENT_RE.match(line.strip()):
        return "write"
    return None


def _source_line(prop: ViolatedProperty) -> str | None:
    if not prop.loc.file or not prop.loc.line:
        return None
    try:
        lines = Path(prop.loc.file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    index = prop.loc.line - 1
    return lines[index] if 0 <= index < len(lines) else None


def real_failures(result: VerifyResult, harness_path: Path) -> list[ViolatedProperty]:
    """The violated properties that are not explained by the harness.

    Only meaningful on a run that asked for a verdict on every property.
    ESBMC otherwise stops at the first violation, so a single artifact hides
    everything behind it -- and a scan reporting fifteen harness artifacts is
    reporting fifteen functions about which nothing at all was checked.
    """
    return [
        prop
        for prop in result.properties
        if mechanical_artifact(replace(result, properties=[prop]), harness_path)
        is None
    ]


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
    # A pointer that came from an allocator with no body is unconstrained, so
    # touching it fails whatever the library does. Requiring the allocator to
    # be genuinely stubbed keeps real use-after-free and wild-pointer findings
    # -- which report the same properties -- out of this bucket.
    stubbed = _stubbed_allocators(result)
    if stubbed and any(
        needle in prop.description for needle in _UNRESOLVED_ALLOCATION_PROPERTIES
    ):
        return (
            f"the pointer comes from {', '.join(stubbed)}, which had no body "
            "in this run (commonly an indirect call through a function "
            "pointer the library exposes so the allocator can be swapped). "
            "Its return value is therefore unconstrained, and any use of it "
            "fails regardless of what the code under test does. Link the "
            "allocator with --link, or point veripp at compile_commands.json, "
            "to check this properly"
        )
    # A property that fails inside the generated file, rather than in the code
    # under test, is by definition about the harness. `veripp verify FILE`
    # with no --function has no generated harness -- the file under test IS
    # the input -- and this rule would then call every finding in it an
    # artifact, so the name has to say the file was generated.
    if (
        prop.loc.file
        and harness_path.name.startswith(GENERATED_HARNESS_PREFIX)
        and Path(prop.loc.file).name == harness_path.name
    ):
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
        text = target.source.read_text(encoding="utf-8")
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
        harness_code = harness_path.read_text(encoding="utf-8")
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
