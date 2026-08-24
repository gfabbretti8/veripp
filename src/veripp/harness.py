"""Harness generation: turn `--function f` into a main() ESBMC can check.

Given a self-contained source file and a target function, this emits a
verification harness: nondeterministic values for every parameter, harness
bounds on any buffer lengths, and the function's own `VERIPP_REQUIRES`
preconditions hoisted in front of the call so the solver constrains the
inputs instead of reporting the caller's mistakes as the callee's bugs.

Two rules the generator never breaks:

  * Every simplification it makes (a bounded array length, a default-
    constructed receiver, a non-null pointer) is recorded as an explicit
    assumption and travels with the result. A "VERIFIED" that hides its
    assumptions is worthless.
  * When it cannot model a parameter soundly it refuses to emit a harness
    rather than emit a plausible wrong one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .cppsig import (
    ClassInfo,
    Field,
    Param,
    StructInfo,
    Signature,
    SignatureError,
    collect_enum_types,
    collect_scalar_typedefs,
    find_class,
    find_function,
    find_struct,
    function_definitions,
    unresolved_callees,
    match_bracket,
    normalize_type,
    scrub,
)

#: Default bound on the length of a generated buffer. Small on purpose: BMC
#: cost grows fast, and off-by-one bugs show up at any length.
DEFAULT_MAX_ARRAY_LEN = 4

#: Default length of a generated call sequence. Each step multiplies the state
#: space, so this is deliberately small; raise it with --max-calls.
DEFAULT_MAX_CALLS = 4

#: Default depth for following pointer fields inside a constructed object.
DEFAULT_MAX_STRUCT_DEPTH = 2

def _looks_like_cxx(code: str) -> bool:
    """Whether a generated harness needs a C++ compiler.

    References and `::` have no C spelling, so a harness using them is C++
    whatever the source file is named.
    """
    return bool(re.search(r"&\s*\w+\s*=|::", code))


_LOOP_VAR = "veripp_i"
_RECEIVER = "veripp_obj"


class HarnessError(Exception):
    """The generator will not emit a harness it cannot justify."""


@dataclass
class HarnessOptions:
    max_array_len: int = DEFAULT_MAX_ARRAY_LEN
    #: Build an object by calling the library's own initialiser when one can
    #: be found, instead of filling every field independently. Off means the
    #: broader-but-less-real question: every field combination, including ones
    #: the type's invariants forbid.
    use_initializers: bool = True
    #: Translation units compiled alongside the harness (--link). Their
    #: definitions resolve callees, so they must be visible here too or veripp
    #: would warn about stubs the run does not actually have.
    link_sources: list[Path] = field(default_factory=list)
    #: Where to look for the headers a source `#include`s. Real projects keep
    #: their struct definitions in an include/ directory the build system
    #: points at with -I, so without these the generator cannot see the types
    #: it has to construct.
    include_dirs: list[Path] = field(default_factory=list)
    assume_pointers_nonnull: bool = True
    #: How far to follow pointer fields when building an object. Value fields
    #: terminate on their own; pointers do not, so the chain is cut here and
    #: the cut is reported as an assumption.
    max_struct_depth: int = DEFAULT_MAX_STRUCT_DEPTH
    #: Length of the generated call sequence for a class target. A single call
    #: on a fresh object explores almost nothing about a stateful type; the
    #: interesting states are the ones several calls build up.
    max_calls: int = DEFAULT_MAX_CALLS


@dataclass
class Harness:
    code: str
    signature: Signature
    assumptions: list[str] = field(default_factory=list)
    source: Path | None = None
    class_info: ClassInfo | None = None  # set for sequence harnesses
    #: Callees declared but not defined in this translation unit. Their side
    #: effects are not modelled, so they weaken whatever the run concludes.
    unresolved_calls: list[str] = field(default_factory=list)

    #: Source suffixes that must be compiled as C. Idiomatic C assigns
    #: malloc's void* without a cast, which is not valid C++, so a C file
    #: needs a C harness or it will not compile at all.
    C_SUFFIXES = frozenset({".c", ".h"})

    @property
    def language_suffix(self) -> str:
        source = self.source
        if source is not None and source.suffix.lower() in self.C_SUFFIXES:
            # A .h may hold either language; C++ constructs decide it.
            if source.suffix.lower() == ".h" and _looks_like_cxx(self.code):
                return ".cpp"
            return ".c"
        return ".cpp"

    def write(self, directory: Path, tag: str = "") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        suffix = f".{tag}" if tag else ""
        out = directory / f"veripp_harness_{self.signature.name}{suffix}{self.language_suffix}"
        out.write_text(self.code)
        return out


# ------------------------------------------------------------ type model ---

_NONDET_BY_TYPE = {
    "bool": "VERIPP_NONDET_BOOL()",
    "char": "VERIPP_NONDET_CHAR()",
    "signed char": "(signed char)VERIPP_NONDET_CHAR()",
    "unsigned char": "(unsigned char)VERIPP_NONDET_CHAR()",
    "short": "(short)VERIPP_NONDET_INT()",
    "unsigned short": "(unsigned short)VERIPP_NONDET_UINT()",
    "int": "VERIPP_NONDET_INT()",
    "unsigned": "VERIPP_NONDET_UINT()",
    "long": "VERIPP_NONDET_LONG()",
    "unsigned long": "VERIPP_NONDET_ULONG()",
    "long long": "(long long)VERIPP_NONDET_LONG()",
    "unsigned long long": "(unsigned long long)VERIPP_NONDET_ULONG()",
    "size_t": "VERIPP_NONDET_SIZE()",
    "ptrdiff_t": "(ptrdiff_t)VERIPP_NONDET_LONG()",
    "float": "VERIPP_NONDET_FLOAT()",
    "double": "VERIPP_NONDET_DOUBLE()",
    "int8_t": "(int8_t)VERIPP_NONDET_CHAR()",
    "uint8_t": "(uint8_t)VERIPP_NONDET_CHAR()",
    "int16_t": "(int16_t)VERIPP_NONDET_INT()",
    "uint16_t": "(uint16_t)VERIPP_NONDET_UINT()",
    "int32_t": "(int32_t)VERIPP_NONDET_INT()",
    "uint32_t": "(uint32_t)VERIPP_NONDET_UINT()",
    "int64_t": "(int64_t)VERIPP_NONDET_LONG()",
    "uint64_t": "(uint64_t)VERIPP_NONDET_ULONG()",
}

_LENGTH_NAMES = {
    "n", "l", "len", "length", "size", "count", "num", "nmemb", "sz", "cnt",
    "nelem", "n_elems", "num_elements", "capacity", "bytes", "nbytes", "cb",
}


#: Enum types seen in the translation unit being harnessed. An enum is an
#: integer, so filling one is a cast; without this the field is a hole.
_ENUMS: set[str] = set()


def nondet_for(type_: str, typedefs: dict[str, str] | None = None) -> str | None:
    canonical = normalize_type(type_, typedefs)
    if canonical in _ENUMS:
        # Any representable value, not only the declared enumerators -- which
        # is what a caller can actually pass through an integer conversion.
        return f"({canonical})VERIPP_NONDET_INT()"
    nondet = _NONDET_BY_TYPE.get(canonical)
    if nondet is not None and canonical != normalize_type(type_):
        # A project typedef resolved to a scalar; cast so the harness compiles
        # even where the alias is more than a plain renaming.
        return nondet
    return nondet


# ------------------------------------------------------------- generator ---


def generate(
    source: Path,
    function: str,
    options: HarnessOptions | None = None,
    extra_preconditions: list[str] | None = None,
) -> Harness:
    """Build a harness for `function` as defined in `source`.

    `extra_preconditions` are boolean C++ expressions over the function's
    parameters, proposed by triage and to be CHECKED BY THE SOLVER in the run
    that follows. They are labelled as proposals in the assumptions list; a
    "verified" under them is conditional on real callers satisfying them.
    """
    options = options or HarnessOptions()
    text = source.read_text()
    signature = find_function(text, function)
    # Struct definitions usually live in the library's own header, not the .cpp
    # being targeted, so resolve types against both.
    expanded = _with_local_includes(source, text, options.include_dirs)
    # Linked TUs resolve callees, so their definitions must be visible
    # here too, or veripp reports stubs the run does not actually have.
    expanded = "\n".join([expanded, *_linked_text(options)])
    typedefs = collect_scalar_typedefs(expanded)
    _ENUMS.clear()
    _ENUMS.update(collect_enum_types(expanded))
    _reject_conflicting_main(text, source)

    body: list[str] = []
    assumptions: list[str] = []
    lengths = _pair_buffers_with_lengths(signature.params, typedefs)

    # Scalars first: a buffer's length must exist before we bound it.
    buffers = set(lengths)
    for param in signature.params:
        if param.name in buffers:
            continue
        body += _emit_scalar(param, signature, assumptions, options, typedefs, expanded)

    for buffer_name, length in lengths.items():
        param = next(p for p in signature.params if p.name == buffer_name)
        body += _emit_buffer(param, length, options, assumptions, typedefs)

    hoisted = _hoist_requires(signature)
    if hoisted:
        body.append("")
        body.append("// preconditions declared with VERIPP_REQUIRES in "
                    f"`{signature.qualified_name}`")
        body += [f"VERIPP_ASSUME({expr});" for expr in hoisted]

    for expr in extra_preconditions or []:
        _validate_precondition(expr, signature)
        body.append("")
        body.append("// PROPOSED precondition (from triage); the solver checks the")
        body.append("// property under it, but nothing checks that real callers meet it.")
        body.append(f"VERIPP_ASSUME({expr});")
        assumptions.append(
            f"PROPOSED precondition (not validated against callers): {expr}"
        )

    body.append("")
    body += _emit_call(signature, assumptions)

    unresolved = unresolved_callees(expanded, signature.body)
    if unresolved:
        assumptions.append(
            "these callees are declared but not defined in this translation "
            f"unit, so their side effects are NOT modelled: {', '.join(unresolved)}"
            " (link the defining source with --link)"
        )
    return Harness(
        code=_render(source, signature, body, assumptions),
        signature=signature,
        assumptions=assumptions,
        source=source,
        unresolved_calls=unresolved,
    )


_LOCAL_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', re.M)
#: A project's own header is often included with angle brackets and found on
#: the -I path -- libyaml's api.c reaches yaml.h that way. Following these is
#: safe because a name is only followed when it exists in a directory the
#: build system named: <stdio.h> is not in the project's -I dirs.
_ANGLE_INCLUDE_RE = re.compile(r"^[ \t]*#[ \t]*include[ \t]*<([^>]+)>", re.M)


def _linked_text(options: HarnessOptions) -> list[str]:
    parts: list[str] = []
    for path in options.link_sources:
        try:
            parts.append(path.read_text(errors="replace"))
        except OSError:
            continue
    return parts


def _with_local_includes(
    source: Path, text: str, include_dirs: list[Path] | None = None, depth: int = 2
) -> str:
    """`text` plus the contents of the headers it `#include`s, transitively.

    Struct definitions and project typedefs live in the library's headers, not
    in the .cpp being targeted, so the generator cannot see the types it must
    construct without following them. Headers are resolved the way the
    compiler resolves them: next to the including file, then along -I paths
    from the compilation database. System includes are left alone -- their
    scalars are already known, and their contents would only slow the scan.
    """
    search = [source.parent, *(include_dirs or [])]
    seen: set[Path] = set()
    parts: list[str] = []

    def absorb(current: Path, body: str, remaining: int) -> None:
        parts.append(body)
        if remaining <= 0:
            return
        # Scanned on the raw text on purpose: scrub() blanks string literals,
        # which erases the filename in `#include "geom.h"`. Following a
        # commented-out include only widens the pool of visible type
        # definitions, which is harmless here.
        names = _LOCAL_INCLUDE_RE.findall(body) + [
            # Angle-bracket includes resolve only against the -I directories,
            # never next to the including file, which is what makes skipping
            # the system headers automatic.
            n for n in _ANGLE_INCLUDE_RE.findall(body)
            if any((d / n).is_file() for d in (include_dirs or []))
        ]
        for name in names:
            for directory in [current.parent, *search]:
                candidate = directory / name
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                if resolved in seen:
                    break
                seen.add(resolved)
                try:
                    absorb(candidate, candidate.read_text(errors="replace"), remaining - 1)
                except OSError:
                    pass
                break

    absorb(source, text, depth)
    return "\n".join(parts)


def _reject_conflicting_main(text: str, source: Path) -> None:
    """A source with an unguarded main() cannot be #included by a harness."""
    scrubbed = scrub(text)
    if not re.search(r"\bmain\s*\(", scrubbed):
        return
    if "VERIPP_HAS_OWN_MAIN" in scrubbed or "VERIPP_GENERATED_HARNESS" in scrubbed:
        return  # the file guards its own main against harness builds
    raise HarnessError(
        f"{source} defines main() unguarded, so a generated harness cannot "
        "include it. Wrap that main in `#if defined(VERIPP_HAS_OWN_MAIN)` "
        "(see veripp/contracts.hpp), or verify the file directly without "
        "--function."
    )


def _pair_buffers_with_lengths(
    params: list[Param], typedefs: dict[str, str] | None = None
) -> dict[str, str]:
    """Map each pointer parameter to the parameter holding its length."""
    pairs: dict[str, str] = {}
    integer_params = {
        p.name: p
        for p in params
        if not p.is_pointer and not p.is_reference and _is_integral(p.type, typedefs)
    }
    for idx, param in enumerate(params):
        if not param.is_pointer:
            continue
        if nondet_for(param.pointee(), typedefs) is None:
            # A pointer to a struct is one object, not an array of them, even
            # when a `size`-ish parameter sits next to it: `ucvector_reserve(
            # ucvector* p, size_t size)` grows p's capacity, it does not
            # describe p's length.
            continue
        candidates = [
            f"{param.name}_len", f"{param.name}_size", f"{param.name}_count",
            f"n_{param.name}", f"{param.name}n",
        ]
        named = next((c for c in candidates if c in integer_params), None)
        if named is None:
            following = params[idx + 1 : idx + 3]
            named = next(
                (
                    p.name
                    for p in following
                    if p.name in integer_params and _is_length_name(p.name)
                ),
                None,
            )
        if named is not None:
            pairs[param.name] = named
    return pairs


def _is_length_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in _LENGTH_NAMES or lowered.endswith(
        ("_len", "_size", "_count", "_n", "len", "size", "count",
         "capacity", "bytes", "num", "cap", "sz")
    )


def _is_integral(type_: str, typedefs: dict[str, str] | None = None) -> bool:
    t = normalize_type(type_, typedefs)
    return t in _NONDET_BY_TYPE and t not in ("float", "double", "bool")


def _validate_precondition(expr: str, signature: Signature) -> None:
    """Refuse a proposed precondition that mentions anything not in scope.

    The guardrail behind the propose->check loop: a triage LLM may only
    constrain the function's parameters. Anything else either would not
    compile in main() or, worse, would compile and silently constrain the
    wrong thing.
    """
    names = {p.name for p in signature.params}
    # Only the ROOT of each expression has to be a parameter: `w->count` and
    # `w.inner.x` name fields of `w`, which the type system checks, not us.
    rooted = re.sub(r"(?:\.|->)\s*[A-Za-z_]\w*", "", scrub(expr))
    identifiers = set(re.findall(r"\b[A-Za-z_]\w*\b", rooted))
    unknown = identifiers - names - _CPP_WORDS
    if unknown:
        raise HarnessError(
            f"proposed precondition {expr!r} mentions "
            f"{', '.join(sorted(unknown))}, which are not parameters of "
            f"`{signature.qualified_name}`; refusing it"
        )
    if not identifiers & names:
        raise HarnessError(
            f"proposed precondition {expr!r} constrains no parameter of "
            f"`{signature.qualified_name}`; refusing it"
        )


def _emit_scalar(
    param: Param,
    signature: Signature,
    assumptions: list[str],
    options: HarnessOptions,
    typedefs: dict[str, str],
    source_text: str = "",
) -> list[str]:
    if param.is_pointer:
        if source_text and nondet_for(param.pointee(), typedefs) is None:
            obj = _try_object(param, signature, assumptions, options, typedefs, source_text)
            if obj is not None:
                return obj
        return _emit_lone_pointer(param, assumptions, options, typedefs, signature.body)
    if param.is_reference:
        if source_text and nondet_for(param.pointee(), typedefs) is None:
            obj = _try_object(param, signature, assumptions, options, typedefs, source_text)
            if obj is not None:
                return obj
        return _emit_reference(param, assumptions, typedefs)
    if source_text and nondet_for(param.type, typedefs) is None:
        obj = _try_object(param, signature, assumptions, options, typedefs, source_text)
        if obj is not None:
            return obj

    nondet = nondet_for(param.type, typedefs)
    if nondet is None:
        raise HarnessError(
            f"cannot build a nondeterministic value for parameter "
            f"`{param.type} {param.name}` of `{signature.qualified_name}`: only "
            "scalar types, and pointers to them, are supported so far. Write "
            "the harness by hand and verify it directly (no --function)."
        )
    return [f"{_decl_type(param.type)} {param.name} = {nondet};"]


def _try_object(param, signature, assumptions, options, typedefs, source_text):
    """Build a struct parameter, or return None so the caller reports why not."""
    try:
        return _emit_object(param, signature, assumptions, options, typedefs, source_text)
    except SignatureError:
        return None


def _emit_lone_pointer(
    param: Param,
    assumptions: list[str],
    options: HarnessOptions,
    typedefs: dict[str, str],
    body: str = "",
) -> list[str]:
    pointee = param.pointee()
    if param.is_const and normalize_type(pointee, typedefs) == "char":
        # A `const char*` with no length parameter is, in practice, a C
        # string. A single nondet char is the wrong model: anything that
        # walks to the terminator (strlen, parsers) reads past it, and the
        # counterexample blames the library for the harness's lie.
        cap = options.max_array_len
        storage = f"{param.name}_str"
        assumptions.append(
            f"`{param.name}` is a NUL-terminated string of at most {cap} "
            "characters (harness bound; string contents nondeterministic)"
        )
        return [
            f"char {storage}[{cap + 1}];",
            f"for (unsigned long {_LOOP_VAR} = 0; {_LOOP_VAR} < {cap}; ++{_LOOP_VAR})",
            f"    {storage}[{_LOOP_VAR}] = VERIPP_NONDET_CHAR();",
            f"{storage}[{cap}] = '\\0';",
            f"{_decl_type(param.type)} {param.name} = {storage};",
        ]
    nondet = nondet_for(pointee, typedefs)
    if nondet is None:
        raise HarnessError(
            f"parameter `{param.type} {param.name}` points to `{pointee}`, which "
            "the generator cannot construct; write the harness by hand."
        )

    # A pointer with no length parameter is not necessarily a pointer to one
    # element -- output parameters like `Adam7_getpassvalues(unsigned* passw,
    # ...)` write several. Giving it a single element made the function
    # overrun a buffer the harness made too small, and reported the library
    # for it. Size it from how the function actually indexes the pointer.
    extent = _indexed_extent(param, body, options)
    if extent <= 1:
        storage = f"{param.name}_obj"
        assumptions.append(
            f"`{param.name}` is non-null and points to one valid, "
            f"nondeterministic `{pointee}` (no length parameter was found, and "
            "the body indexes it only at 0)"
        )
        return [
            f"{pointee} {storage} = {nondet};",
            f"{_decl_type(param.type)} {param.name} = &{storage};",
        ]

    storage = f"{param.name}_buf"
    assumptions.append(
        f"`{param.name}` is non-null and points to at least {extent} valid "
        f"`{pointee}` elements (no length parameter; {extent} is the highest "
        "index the body uses)"
    )
    var = f"veripp_i_{param.name}"
    return [
        f"{pointee} {storage}[{extent}];",
        f"for (unsigned long {var} = 0; {var} < {extent}; ++{var})",
        f"    {storage}[{var}] = {nondet};",
        f"{_decl_type(param.type)} {param.name} = {storage};",
    ]


_MAX_INFERRED_EXTENT = 64


def _indexed_extent(param: Param, body: str, options: HarnessOptions) -> int:
    """How many elements of `param` the body touches, as far as we can tell.

    Reads literal indices (`p[3]`) and loop bounds over the pointer
    (`for (i = 0; i < 7; ++i) p[i]`). When the body indexes with something we
    cannot evaluate, fall back to the harness array bound rather than to one
    element: too small a buffer manufactures out-of-bounds reports against
    code that is fine.
    """
    if not body:
        return 1
    scrubbed = scrub(body)
    name = re.escape(param.name)
    highest = 0
    unknown = False

    for m in re.finditer(rf"\b{name}\s*\[\s*([^\]]+?)\s*\]", scrubbed):
        index = m.group(1).strip()
        if index.isdigit():
            highest = max(highest, int(index) + 1)
        else:
            unknown = True
            # `p[i]` where a nearby loop bounds i by a literal. `i < 7`
            # reaches index 6, `i <= 7` reaches 7 -- being generous here would
            # hand the function more memory than a caller has and hide a real
            # overflow, so the operator matters.
            for bound in re.finditer(
                rf"\b{re.escape(index)}\s*(<=?)\s*(\d+)", scrubbed
            ):
                limit = int(bound.group(2))
                highest = max(highest, limit + 1 if bound.group(1) == "<=" else limit)
    if not highest and not unknown:
        return 1
    if unknown and highest == 0:
        highest = options.max_array_len
    return min(highest, _MAX_INFERRED_EXTENT)


def _emit_reference(
    param: Param, assumptions: list[str], typedefs: dict[str, str]
) -> list[str]:
    pointee = param.pointee()
    nondet = nondet_for(pointee, typedefs)
    if nondet is None:
        raise HarnessError(
            f"parameter `{param.type} {param.name}` refers to `{pointee}`, which "
            "the generator cannot construct; write the harness by hand."
        )
    assumptions.append(
        f"`{param.name}` refers to a valid, nondeterministically initialised "
        f"`{pointee}` (it is an in/out parameter)"
    )
    return [f"{pointee} {param.name} = {nondet};"]


def _emit_buffer(
    param: Param,
    length: str,
    options: HarnessOptions,
    assumptions: list[str],
    typedefs: dict[str, str],
) -> list[str]:
    pointee = param.pointee()
    nondet = nondet_for(pointee, typedefs)
    if nondet is None:
        raise HarnessError(
            f"parameter `{param.type} {param.name}` points to `{pointee}`, which "
            "the generator cannot fill with nondeterministic values; write the "
            "harness by hand."
        )
    cap = options.max_array_len
    storage = f"{param.name}_buf"
    assumptions.append(
        f"`{param.name}` points to exactly `{length}` valid elements, with "
        f"{length} <= {cap} (harness bound on array length)"
    )
    return [
        f"VERIPP_ASSUME({length} <= {cap});",
        f"{pointee} {storage}[{cap}];",
        f"for (unsigned long {_LOOP_VAR} = 0; {_LOOP_VAR} < {cap}; ++{_LOOP_VAR})",
        f"    {storage}[{_LOOP_VAR}] = {nondet};",
        f"{_decl_type(param.type)} {param.name} = {storage};",
    ]


def _decl_type(type_: str) -> str:
    """Declaration type for a harness local: drop a top-level reference."""
    t = re.sub(r"\s+", " ", type_).strip()
    # `T * const` is fine to declare (assigned once), but `T &` is not a
    # variable declaration the harness can initialise separately.
    return t[:-1].strip() if t.endswith("&") else t


def _emit_call(signature: Signature, assumptions: list[str]) -> list[str]:
    args = ", ".join(p.name for p in signature.params)
    if signature.class_name and not signature.is_static:
        assumptions.append(
            f"exactly one call, on a default-constructed `{signature.class_name}`: "
            "object states reachable only through other call sequences are NOT "
            "explored (write a sequence harness by hand for those)"
        )
        lines = [f"{signature.class_name} {_RECEIVER};",
                 f"(void){_RECEIVER}.{signature.name}({args});"]
    elif signature.class_name:
        lines = [f"(void){signature.class_name}::{signature.name}({args});"]
    else:
        lines = [f"(void){signature.name}({args});"]
    if signature.returns_void:
        lines[-1] = lines[-1].replace("(void)", "", 1)
    return lines


_REQUIRES_RE = re.compile(r"\bVERIPP_REQUIRES\s*\(")
_CPP_WORDS = {
    "true", "false", "nullptr", "sizeof", "static_cast", "const_cast",
    "reinterpret_cast", "int", "unsigned", "long", "short", "char", "bool",
    "float", "double", "size_t", "signed", "void", "const",
}


def _hoist_requires(signature: Signature) -> list[str]:
    """Preconditions from the body that can be stated over the parameters alone.

    A `VERIPP_REQUIRES` that mentions a member or a global cannot be restated
    in main(), so it is left where it is: it still constrains the run, just one
    statement later.
    """
    names = {p.name for p in signature.params}
    scrubbed = scrub(signature.body)
    hoisted: list[str] = []
    for m in _REQUIRES_RE.finditer(scrubbed):
        lparen = scrubbed.index("(", m.end() - 1)
        try:
            rparen = match_bracket(scrubbed, lparen)
        except SignatureError:
            continue
        expr = signature.body[lparen + 1 : rparen].strip()
        identifiers = set(re.findall(r"\b[A-Za-z_]\w*\b", scrub(expr)))
        if not identifiers <= (names | _CPP_WORDS):
            continue  # mentions something that does not exist in main()
        if identifiers & names:
            hoisted.append(" ".join(expr.split()))
    return hoisted


_HEADER = """// Generated by veripp — do not edit; regenerate with:
//     veripp harness {source} --function {function}
//
// Harness for `{qualified}`:
//   {signature}
//
// Assumptions baked into this harness (reported with every result):
{assumptions}

#define VERIPP_GENERATED_HARNESS 1
#include "veripp/contracts.hpp"
#include "{include}"

#if !defined(__ESBMC__)
#error "veripp harnesses are built by ESBMC only (it is invoked with -D__ESBMC__)"
#endif

int main() {{
{body}
    return 0;
}}
"""


def _comment_lines(assumptions: list[str]) -> str:
    """Assumptions as a `//` block. Each becomes exactly one line: a stray
    newline here would end the comment and emit uncompilable C++."""
    return "\n".join(f"//   - {' '.join(a.split())}" for a in assumptions) or "//   - none"


def _render(source: Path, signature: Signature, body: list[str], assumptions: list[str]) -> str:
    params = ", ".join(f"{p.type} {p.name}".replace("  ", " ") for p in signature.params)
    assumption_lines = _comment_lines(assumptions)
    while body and not body[0].strip():
        body.pop(0)
    indented = "\n".join(("    " + line if line else "") for line in body)
    return _HEADER.format(
        source=source,
        function=signature.name,
        qualified=signature.qualified_name,
        signature=f"{signature.return_type} {signature.qualified_name}({params})"
        + (" const" if signature.is_const else ""),
        assumptions=assumption_lines,
        include=source.resolve(),
        body=indented,
    )


# ------------------------------------------------- sequence harnesses ----

_STEP_VAR = "veripp_step"
_CHOICE_VAR = "veripp_choice"


def generate_sequence(
    source: Path,
    class_name: str,
    options: HarnessOptions | None = None,
    assertions: list[str] | None = None,
) -> Harness:
    """Drive a bounded, nondeterministic sequence of public method calls.

    A single call on a default-constructed object is a very weak question to
    ask about a stateful type: the states that matter are the ones a sequence
    of calls builds up. This emits the harness a person would write by hand --
    construct, then loop `max_calls` times picking any public method with
    nondeterministic arguments -- so every reachable state within that bound
    is explored, not just the initial one.
    """
    options = options or HarnessOptions()
    text = source.read_text()
    info = find_class(text, class_name)
    expanded = _with_local_includes(source, text, options.include_dirs)
    # Linked TUs resolve callees, so their definitions must be visible
    # here too, or veripp reports stubs the run does not actually have.
    expanded = "\n".join([expanded, *_linked_text(options)])
    typedefs = collect_scalar_typedefs(expanded)
    _ENUMS.clear()
    _ENUMS.update(collect_enum_types(expanded))
    _reject_conflicting_main(text, source)

    callable_methods: list[Signature] = []
    unsupported: dict[str, str] = dict(info.skipped)
    for method in info.methods:
        try:
            _method_arguments(method, typedefs, options, [], probe=True, source_text=expanded)
        except HarnessError as exc:
            unsupported[method.name] = str(exc).split(";")[0]
            continue
        callable_methods.append(method)

    if not callable_methods:
        raise HarnessError(
            f"no public method of `{class_name}` can be driven with "
            "nondeterministic arguments"
            + (f" (skipped: {', '.join(sorted(unsupported))})" if unsupported else "")
        )

    assumptions: list[str] = []
    body = _emit_construction(info, options, typedefs, assumptions)
    assumptions.append(
        f"at most {options.max_calls} calls are made, each one any public "
        f"method of `{class_name}` with nondeterministic arguments; longer "
        "sequences are NOT explored"
    )
    if unsupported:
        assumptions.append(
            "these public methods are never called, so states only they can "
            f"produce are unexplored: {', '.join(sorted(unsupported))}"
        )

    body.append("")
    body.append(f"for (int {_STEP_VAR} = 0; {_STEP_VAR} < {options.max_calls}; ++{_STEP_VAR}) {{")
    body.append(f"    unsigned {_CHOICE_VAR} = VERIPP_NONDET_UINT();")
    body.append(f"    VERIPP_ASSUME({_CHOICE_VAR} < {len(callable_methods)});")
    for idx, method in enumerate(callable_methods):
        keyword = "if" if idx == 0 else "} else if"
        body.append(f"    {keyword} ({_CHOICE_VAR} == {idx}) {{")
        args: list[str] = []
        for line in _method_arguments(
            method, typedefs, options, assumptions, source_text=expanded
        ):
            body.append(f"        {line}")
        args = [p.name for p in method.params]
        call = f"{_RECEIVER}.{method.name}({', '.join(args)})"
        body.append(f"        {call};" if method.returns_void else f"        (void){call};")
    body.append("    }")
    for expr in assertions or []:
        body.append(f"    VERIPP_ASSERT({expr});")
    body.append("}")

    signature = Signature(
        name=class_name, return_type="", params=[], class_name=class_name
    )
    return Harness(
        code=_render_sequence(source, info, callable_methods, body, assumptions, options),
        signature=signature,
        assumptions=assumptions,
        source=source,
        class_info=info,
    )


def _method_arguments(
    method: Signature,
    typedefs: dict[str, str],
    options: HarnessOptions,
    assumptions: list[str],
    probe: bool = False,
    source_text: str = "",
) -> list[str]:
    """Declarations for one call's arguments, scoped to that branch."""
    lines: list[str] = []
    sink: list[str] = [] if probe else assumptions
    lengths = _pair_buffers_with_lengths(method.params, typedefs)
    buffers = set(lengths)
    for param in method.params:
        if param.name in buffers:
            continue
        lines += _emit_scalar(param, method, sink, options, typedefs, source_text)
    for buffer_name, length in lengths.items():
        param = next(p for p in method.params if p.name == buffer_name)
        lines += _emit_buffer(param, length, options, sink, typedefs)
    return lines


def _emit_construction(
    info: ClassInfo,
    options: HarnessOptions,
    typedefs: dict[str, str],
    assumptions: list[str],
) -> list[str]:
    if info.default_constructible:
        assumptions.append(
            f"the sequence starts from a default-constructed `{info.name}`"
        )
        return [f"{info.name} {_RECEIVER};"]

    for ctor in sorted(info.constructors, key=lambda c: len(c.params)):
        try:
            lines = _method_arguments(ctor, typedefs, options, [], probe=True)
        except HarnessError:
            continue
        lines = _method_arguments(ctor, typedefs, options, assumptions)
        args = ", ".join(p.name for p in ctor.params)
        assumptions.append(
            f"the sequence starts from `{info.name}({args})` with "
            "nondeterministic constructor arguments"
        )
        return lines + [f"{info.name} {_RECEIVER}({args});"]

    raise HarnessError(
        f"`{info.name}` has no default constructor and none of its "
        "constructors can be called with nondeterministic arguments"
    )


_SEQUENCE_HEADER = """// Generated by veripp -- do not edit; regenerate with:
//     veripp harness {source} --class {cls} --max-calls {calls}
//
// Sequence harness for `{cls}`: every state reachable in at most {calls}
// public method calls, each with nondeterministic arguments.
//
// Methods driven:
{methods}
//
// Assumptions baked into this harness (reported with every result):
{assumptions}

#define VERIPP_GENERATED_HARNESS 1
#include "veripp/contracts.hpp"
#include "{include}"

#if !defined(__ESBMC__)
#error "veripp harnesses are built by ESBMC only (it is invoked with -D__ESBMC__)"
#endif

int main() {{
{body}
    return 0;
}}
"""


def _render_sequence(source, info, methods, body, assumptions, options) -> str:
    method_lines = "\n".join(
        "//   - {} {}({}){}".format(
            m.return_type,
            m.name,
            ", ".join(f"{p.type} {p.name}" for p in m.params),
            " const" if m.is_const else "",
        )
        for m in methods
    )
    assumption_lines = _comment_lines(assumptions)
    indented = "\n".join(("    " + line if line else "") for line in body)
    return _SEQUENCE_HEADER.format(
        source=source,
        cls=info.name,
        calls=options.max_calls,
        methods=method_lines,
        assumptions=assumption_lines,
        include=source.resolve(),
        body=indented,
    )


# ------------------------------------------------- object construction ---


def _emit_object(
    param: Param,
    signature: Signature,
    assumptions: list[str],
    options: HarnessOptions,
    typedefs: dict[str, str],
    source_text: str,
) -> list[str]:
    """Build an object for a struct/class parameter and pass it in.

    Fields are filled nondeterministically, so the harness explores every
    field combination -- including combinations no real caller would produce.
    That is the honest default: the alternative is to guess an invariant and
    silently narrow the proof. Where the object has a real invariant, state it
    with --assume (or let triage propose it) and the solver will check the
    property under it.
    """
    type_name = param.pointee() if (param.is_pointer or param.is_reference) else param.type
    type_name = re.sub(
        r"^\s*(?:(?:const|volatile|struct|union|class|enum)\s+)+", "", type_name
    ).strip()
    info = find_struct(source_text, type_name)

    storage = f"{param.name}_obj"

    initializer = (
        _find_initializer(source_text, type_name, signature.name, typedefs)
        if options.use_initializers else None
    )
    if initializer is not None:
        assumptions.append(
            f"`{param.name}` is a `{type_name}` as `{initializer}` leaves it, "
            "not an arbitrary one: states reached by mutating it afterwards "
            "are NOT explored (--no-initializers fills every field instead, "
            "which also admits combinations the type forbids)"
        )
        pass_as = (
            f"{_decl_type(param.type)} {param.name} = &{storage};"
            if param.is_pointer else f"{type_name}& {param.name} = {storage};"
        )
        return [f"{type_name} {storage};", f"{initializer}(&{storage});", pass_as]

    lines = [f"{type_name} {storage};"]
    lines += _fill_fields(info, storage, 0, options, typedefs, source_text, assumptions,
                          seen={type_name})
    assumptions.append(
        f"`{param.name}` points to one `{type_name}` with every field "
        "nondeterministic: field combinations no real caller can produce are "
        "included, so a counterexample may be an unreachable object state"
    )
    if param.is_pointer:
        lines.append(f"{_decl_type(param.type)} {param.name} = &{storage};")
    elif param.is_reference:
        lines.append(f"{type_name}& {param.name} = {storage};")
    else:
        lines.append(f"{type_name}& {param.name} = {storage};")
    return lines


def _pair_struct_fields(
    info: StructInfo, typedefs: dict[str, str]
) -> dict[str, str]:
    """Pointer fields paired with the sibling field holding their count.

    `LodePNGInfo` has `size_t itext_num` beside `char** itext_keys`, and
    `LodePNGIText_cleanup` walks `itext_num` entries of `itext_keys`. Filling
    both independently -- one element, and a count up to 2**64 -- guarantees an
    out-of-bounds read that no caller could ever cause, and lodepng gets
    blamed for it. This was the single largest source of false findings.
    """
    integers = {
        f.name for f in info.fields
        if not f.is_pointer and f.array_len is None and _is_integral(f.type, typedefs)
    }
    pointers = [
        f for f in info.fields
        if f.is_pointer and f.array_len is None
        # A self-referential pointer is a link in a structure, not a counted
        # buffer: `Widget* next` beside `int count` must stay a chain to
        # follow, not become an array.
        and normalize_type(f.pointee(), typedefs) != normalize_type(info.name, typedefs)
    ]
    pairs: dict[str, str] = {}
    for f in pointers:
        stem = f.name.rsplit("_", 1)[0] if "_" in f.name else f.name
        candidates = [f"{stem}_{s}" for s in ("num", "count", "size", "len", "n")]
        candidates += [f"{f.name}_{s}" for s in ("num", "count", "size", "len")]
        match = next((c for c in candidates if c in integers), None)
        if match is None and len(pointers) == 1:
            # An unambiguous struct: one buffer, one plausible count.
            match = next((c for c in integers if _is_length_name(c)), None)
        if match is not None:
            pairs[f.name] = match
    return pairs


def _fill_fields(
    info: StructInfo,
    prefix: str,
    depth: int,
    options: HarnessOptions,
    typedefs: dict[str, str],
    source_text: str,
    assumptions: list[str],
    seen: set[str],
) -> list[str]:
    if info.is_union:
        # A union's members share storage, so assigning them in turn would
        # leave only the last one set and quietly narrow the input. Left
        # alone, ESBMC treats the object as nondeterministic bytes, which
        # covers every member at once.
        assumptions.append(
            f"`{prefix}` is a union, left nondeterministic: its members share "
            "storage, so filling them one by one would model only the last"
        )
        return []
    lines: list[str] = []
    # Constraints are emitted after every field is assigned: a bound written
    # before its count field is filled would be overwritten by the nondet
    # assignment that follows, silently losing the constraint.
    constraints: list[str] = []
    pairs = _pair_struct_fields(info, typedefs)
    for f in info.fields:
        target = f"{prefix}.{f.name}"
        if f.name in pairs:
            lines += _fill_counted_pointer_field(
                f, pairs[f.name], prefix, target, options, typedefs,
                assumptions, constraints,
            )
            continue
        if f.array_len is not None:
            lines += _fill_array_field(f, target, typedefs, assumptions)
            continue
        if f.is_pointer:
            lines += _fill_pointer_field(
                f, target, depth, options, typedefs, source_text, assumptions, seen
            )
            continue
        nondet = nondet_for(f.type, typedefs)
        if nondet is not None:
            lines.append(f"{target} = {nondet};")
            continue
        nested = _try_struct(source_text, f.type)
        if nested is not None and nested.name not in seen:
            lines += _fill_fields(
                nested, target, depth, options, typedefs, source_text, assumptions,
                seen | {nested.name},
            )
        else:
            assumptions.append(
                f"field `{target}` of type `{f.type}` is left uninitialised "
                "(ESBMC treats it as nondeterministic, but veripp did not "
                "model its structure)"
            )
    for raw, why in info.unsupported.items():
        assumptions.append(f"member `{raw}` was not initialised: {why}")
    return lines + constraints


def _fill_counted_pointer_field(
    f: Field,
    count_field: str,
    prefix: str,
    target: str,
    options: HarnessOptions,
    typedefs: dict[str, str],
    assumptions: list[str],
    constraints: list[str],
) -> list[str]:
    """Give a counted pointer field a real array, and bound its count to it."""
    cap = options.max_array_len
    pointee = f.pointee()
    storage = f"{target.replace('.', '_')}_items"
    counter = f"{prefix}.{count_field}"

    nondet = nondet_for(pointee, typedefs)
    if nondet is not None:
        fill = [
            f"for (unsigned long veripp_k = 0; veripp_k < {cap}; ++veripp_k)",
            f"    {storage}[veripp_k] = {nondet};",
        ]
        note = f"{cap} nondeterministic `{pointee}` elements"
    elif pointee.rstrip().endswith("*"):
        # An array of pointers (char** and friends). Null is the one value
        # that is safe to free and safe to leave unread.
        fill = [
            f"for (unsigned long veripp_k = 0; veripp_k < {cap}; ++veripp_k)",
            f"    {storage}[veripp_k] = 0;",
        ]
        note = f"{cap} null `{pointee}` elements"
    else:
        constraints.append(f"VERIPP_ASSUME({counter} == 0);")
        return [f"{target} = 0;"]

    assumptions.append(
        f"`{target}` points to {note}, and `{counter}` is bounded to {cap} to "
        "match; the two are filled together because the code reads one "
        "according to the other"
    )
    constraints.append(f"VERIPP_ASSUME({counter} <= {cap});")
    return [
        f"{pointee} {storage}[{cap}];",
        *fill,
        f"{target} = {storage};",
    ]


def _fill_array_field(
    f: Field, target: str, typedefs: dict[str, str], assumptions: list[str]
) -> list[str]:
    nondet = nondet_for(f.type, typedefs)
    if nondet is None:
        assumptions.append(
            f"array field `{target}` of `{f.type}` is left uninitialised "
            "(element type not modelled)"
        )
        return []
    if not f.array_len or not f.array_len.isdigit():
        assumptions.append(
            f"array field `{target}` has a non-literal extent "
            f"(`{f.array_len}`) and is left uninitialised"
        )
        return []
    var = f"veripp_i_{f.name}"
    return [
        f"for (unsigned long {var} = 0; {var} < {f.array_len}; ++{var})",
        f"    {target}[{var}] = {nondet};",
    ]


def _fill_pointer_field(
    f: Field,
    target: str,
    depth: int,
    options: HarnessOptions,
    typedefs: dict[str, str],
    source_text: str,
    assumptions: list[str],
    seen: set[str],
) -> list[str]:
    if depth >= options.max_struct_depth:
        assumptions.append(
            f"pointer field `{target}` is null (struct depth bound "
            f"{options.max_struct_depth} reached); deeper object graphs are "
            "NOT explored"
        )
        return [f"{target} = 0;"]

    pointee = f.pointee()
    nondet = nondet_for(pointee, typedefs)
    storage = f"{target.replace('.', '_')}_target"
    if nondet is not None:
        assumptions.append(f"pointer field `{target}` points to one nondeterministic `{pointee}`")
        return [f"{pointee} {storage} = {nondet};", f"{target} = &{storage};"]

    nested = _try_struct(source_text, pointee)
    if nested is None:
        assumptions.append(
            f"pointer field `{target}` is null: `{pointee}` is not a type "
            "veripp can construct here"
        )
        return [f"{target} = 0;"]

    lines = [f"{pointee} {storage};"]
    lines += _fill_fields(
        nested, storage, depth + 1, options, typedefs, source_text, assumptions,
        seen | {pointee},
    )
    lines.append(f"{target} = &{storage};")
    assumptions.append(f"pointer field `{target}` points to a nondeterministic `{pointee}`")
    return lines


_INIT_SUFFIXES = ("_init", "_new", "_create", "_reset")


def _find_initializer(
    source_text: str, type_name: str, target: str, typedefs: dict[str, str]
) -> str | None:
    """A function in this TU that puts a fresh `type_name` into a valid state.

    Filling a struct field by field admits combinations the type's own
    invariants forbid, and those produce failures no caller could cause. When
    the library ships the constructor -- `ucvector_init`, `HuffmanTree_init`,
    `lodepng_info_init` -- using it asks a narrower question about a state
    that genuinely occurs, which is worth far more than a broad question about
    states that cannot.
    """
    stem = re.sub(r"^(lode|_)+", "", type_name).lower()
    for name in function_definitions(source_text):
        if name == target or not name.lower().endswith(_INIT_SUFFIXES):
            continue
        base = name.lower()
        for suffix in _INIT_SUFFIXES:
            base = base[: -len(suffix)] if base.endswith(suffix) else base
        if stem not in base.replace("_", "") and base.replace("_", "") not in stem:
            continue
        try:
            sig = find_function(source_text, name)
        except SignatureError:
            continue
        if len(sig.params) != 1 or not sig.params[0].is_pointer:
            continue  # an initialiser needing more inputs is not a drop-in
        if normalize_type(sig.params[0].pointee(), typedefs) != normalize_type(
            type_name, typedefs
        ):
            continue
        return name
    return None


def _try_struct(source_text: str, type_name: str) -> StructInfo | None:
    name = re.sub(r"^\s*(const|volatile|struct|class)\s+", "", type_name).strip()
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        return None
    try:
        return find_struct(source_text, name)
    except SignatureError:
        return None


# --------------------------------------------------------- vacuity check ---

_REACHABILITY_MESSAGE = "veripp: harness is reachable under its assumptions"


def reachability_variant(code: str) -> str:
    """The same harness with a deliberately failing assertion at the end.

    A precondition that contradicts itself, or is merely too strong to be
    satisfiable, makes the call unreachable -- and an unreachable program
    satisfies every property. ESBMC reports VERIFICATION SUCCESSFUL and the
    "proof" means nothing.

    Running this variant inverts the question: the trailing assertion is
    always false, so a *reachable* harness must produce a counterexample.
    If this variant verifies, the original proof was vacuous.
    """
    marker = "    return 0;"
    index = code.rfind(marker)
    if index < 0:
        return code + f'\nstatic_assert(true, "{_REACHABILITY_MESSAGE}");\n'
    probe = f'    VERIPP_ASSERT(0 && "{_REACHABILITY_MESSAGE}");\n'
    return code[:index] + probe + code[index:]
