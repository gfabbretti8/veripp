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
    collect_pointer_typedefs,
    collect_scalar_typedefs,
    included_names,
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
    #: Build an object by calling the library's own constructors -- the
    #: functions that RETURN one -- choosing between them nondeterministically
    #: so no single shape is assumed. Off by default: it drags allocation into
    #: every harness, which costs solver time, and the field-filling default
    #: asks the broader question.
    use_constructors: bool = False
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
        out.write_text(self.code, encoding="utf-8")
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
    text = source.read_text(encoding="utf-8")
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
    # Rewrite `z_streamp p` to `z_stream* p` once, so everything downstream
    # sees an ordinary pointer.
    _expand_pointer_aliases(signature, collect_pointer_typedefs(expanded))
    _reject_conflicting_main(text, source)

    body: list[str] = []
    assumptions: list[str] = []
    teardown: list[str] = []
    hook_preamble, hook_body = _resolve_allocator_hooks(text, assumptions)
    body += hook_body

    lengths = _pair_buffers_with_lengths(signature.params, typedefs,
                                         body=signature.body)
    cursors = _pair_cursors_with_starts(signature.params, typedefs,
                                        body=signature.body)

    # Scalars first: a buffer's length must exist before we bound it.
    buffers = set(lengths)
    paired_cursor = set(cursors) | {other for other, _ in cursors.values()}
    by_name = {p.name: p for p in signature.params}
    for cursor_name, (other_name, forwards) in cursors.items():
        body += _emit_backward_cursor(
            by_name[cursor_name], by_name[other_name], forwards, options,
            assumptions, typedefs,
        )
    for param in signature.params:
        if param.name in buffers or param.name in paired_cursor:
            continue
        body += _emit_scalar(param, signature, assumptions, options, typedefs,
                             expanded, teardown)

    for buffer_name, length in lengths.items():
        if buffer_name in paired_cursor:
            continue
        param = next(p for p in signature.params if p.name == buffer_name)
        body += _emit_buffer(param, length, options, assumptions, typedefs,
                             signature.body, by_name[length].type)

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
    if teardown:
        body.append("")
        body += teardown

    unresolved = unresolved_callees(expanded, signature.body)
    # A resolved hook is a variable, not a bodiless function, and saying its
    # side effects are unmodelled is now false. An assumption block is only
    # useful if every line in it is true, so this one goes.
    resolved_hooks = {name for name, _ in _allocator_hooks(text)}
    unresolved = [c for c in unresolved if c not in resolved_hooks]
    if unresolved:
        assumptions.append(
            "these callees are declared but not defined in this translation "
            f"unit, so their side effects are NOT modelled: {', '.join(unresolved)}"
            " (link the defining source with --link)"
        )
    return Harness(
        code=_render(source, signature, body, assumptions, hook_preamble),
        signature=signature,
        assumptions=assumptions,
        source=source,
        unresolved_calls=unresolved,
    )





def _is_angle_only(text: str, name: str) -> bool:
    return f'"{name}"' not in text


def _linked_text(options: HarnessOptions) -> list[str]:
    parts: list[str] = []
    for path in options.link_sources:
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return parts


#: How many levels of #include to follow. Type definitions sit deeper than
#: they look: zlib's crc32.c reaches its typedefs through zutil.h -> zlib.h ->
#: zconf.h, and stopping at two levels lost every one of them. Headers are
#: only scanned as text, so the extra depth is cheap.
DEFAULT_INCLUDE_DEPTH = 5


def _with_local_includes(
    source: Path,
    text: str,
    include_dirs: list[Path] | None = None,
    depth: int = DEFAULT_INCLUDE_DEPTH,
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
        # A project's own header is often included with angle brackets and
        # found on the -I path (libyaml's api.c reaches yaml.h that way).
        # Following those is safe because a name is only followed when it
        # exists in a directory the build system named, which is what keeps
        # <stdio.h> out automatically.
        names = [
            n for n in included_names(body, angle=True)
            if '"' in body or not _is_angle_only(body, n)
            or any((d / n).is_file() for d in (include_dirs or []))
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
                    absorb(candidate, candidate.read_text(encoding="utf-8", errors="replace"), remaining - 1)
                except OSError:
                    pass
                break

    absorb(source, text, depth)
    return "\n".join(parts)


def _expand_pointer_aliases(signature: Signature, aliases: dict[str, str]) -> None:
    """Rewrite `z_streamp p` to `z_stream* p` in place.

    Doing it once here means everything downstream -- is_pointer, pointee,
    object construction -- sees an ordinary pointer and needs no special case.
    """
    if not aliases:
        return
    for param in signature.params:
        base = re.sub(r"\b(const|volatile)\b", " ", param.type).strip()
        expansion = aliases.get(base)
        if expansion is not None:
            param.type = expansion


def _reject_conflicting_main(text: str, source: Path) -> None:
    """A source with an unguarded main() cannot be #included by a harness."""
    scrubbed = scrub(text)
    match = re.search(r"\bmain\s*\(", scrubbed)
    if match is None:
        return
    if "VERIPP_HAS_OWN_MAIN" in scrubbed or "VERIPP_GENERATED_HARNESS" in scrubbed:
        return  # the file guards its own main against harness builds
    if _inside_conditional(scrubbed, match.start()):
        # zlib's crc32.c carries a table-generator main under `#ifdef
        # MAKECRCH`. The harness does not define that macro, so the main is
        # not compiled -- and refusing on sight cost every function in the
        # file.
        return
    raise HarnessError(
        f"{source} defines main() unguarded, so a generated harness cannot "
        "include it. Wrap that main in `#if defined(VERIPP_HAS_OWN_MAIN)` "
        "(see veripp/contracts.hpp), or verify the file directly without "
        "--function."
    )


_COND_OPEN = re.compile(r"^[ \t]*#[ \t]*if(?:def|ndef)?\b", re.M)
_COND_CLOSE = re.compile(r"^[ \t]*#[ \t]*endif\b", re.M)


def _inside_conditional(scrubbed: str, offset: int) -> bool:
    """Whether `offset` sits inside an #if/#endif block.

    Not a preprocessor -- it cannot know whether the branch is taken -- but
    enough to tell a real main from one behind a macro the harness never
    defines.
    """
    head = scrubbed[:offset]
    return len(_COND_OPEN.findall(head)) > len(_COND_CLOSE.findall(head))


def _pair_buffers_with_lengths(
    params: list[Param], typedefs: dict[str, str] | None = None,
    body: str = "",
) -> dict[str, str]:
    """Map each pointer parameter to the parameter holding its length.

    A pointer the body walks to a NUL is deliberately left unpaired, even
    when a length parameter is in scope. `lwip_strnstr(buffer, token, n)`
    bounds `buffer` by `n`, but `token` is a C string whose end is its
    terminator -- pairing it with `n` too produces a `token` that is not
    terminated, and the resulting over-read lands inside the checker's own
    strlen rather than in the code under test. Pointers that are merely
    read, `memcmp(a, b, n)`, keep sharing one length, which is correct.
    """
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
        if _walks_to_terminator(param.name, body):
            continue
        candidates = [
            f"{param.name}_len", f"{param.name}_size", f"{param.name}_count",
            f"n_{param.name}", f"{param.name}n",
        ]
        named = next((c for c in candidates if c in integer_params), None)
        if named is None:
            # Scan forward, but stop at the next pointer: in C a length
            # follows the buffer it describes, so a length sitting after
            # another pointer belongs to that one. Without this,
            # `base64_encode(dst, dlen, olen, src, slen)` pairs the scalar
            # out-parameter `olen` with `slen` and models it as an array.
            following: list[Param] = []
            for candidate in params[idx + 1 : idx + 3]:
                if candidate.is_pointer or candidate.is_reference:
                    break
                following.append(candidate)
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


#: Byte types a DER/ASN.1 encoder writes through.
_CURSOR_POINTEES = ("char", "unsigned char", "signed char")


def _pair_cursors_with_starts(
    params: list[Param], typedefs: dict[str, str] | None = None,
    body: str = "",
) -> dict[str, tuple[str, bool]]:
    """Map a write-backwards cursor to the parameter naming its buffer start.

    `mbedtls_asn1_write_len(unsigned char **p, const unsigned char *start,
    size_t len)` is the shape every DER encoder in mbedTLS and OpenSSL uses:
    `*p` is a cursor that begins at the END of the buffer and walks down
    toward `start`, writing with `*--(*p)` and checking headroom with
    `required > (*p - start)`.

    Modelled as two pointers into one buffer, which is what callers do:
    `unsigned char *c = buf + sizeof(buf); write_len(&c, buf, len);`.
    Without this the double pointer is simply unconstructible, and 21 of
    asn1write.c's 22 functions were refused outright.
    """
    pairs: dict[str, tuple[str, bool]] = {}
    for idx, param in enumerate(params[:-1]):
        if param.type.replace("const", "").count("*") != 2:
            continue
        inner = normalize_type(param.pointee().replace("*", "").strip(), typedefs)
        if inner not in _CURSOR_POINTEES:
            continue
        nxt = params[idx + 1]
        if not nxt.is_pointer or nxt.type.replace("const", "").count("*") != 1:
            continue
        if normalize_type(nxt.pointee(), typedefs) != inner:
            continue
        forwards = _cursor_direction(param.name, body)
        if forwards is None:
            forwards = _direction_from_name(nxt.name)
        if forwards is None:
            continue  # direction unknown: refuse rather than guess a layout
        pairs[param.name] = (nxt.name, forwards)
    return pairs


def _cursor_direction(name: str, body: str) -> bool | None:
    """True if the cursor advances, False if it retreats, None if unclear.

    The two conventions put the companion pointer at opposite ends of the
    same buffer, so guessing gets the layout exactly backwards. mbedTLS
    writes DER backwards -- `*--(*p)`, with `start` at the buffer's
    beginning -- while its own OID encoder writes forwards, `*p += n`, with
    `bound` at the end. Assuming the DER convention for
    oid_subidentifier_encode_into put the end where the start belongs, so
    `bound - *p` went negative, wrapped huge as size_t, and sailed past the
    guard: a fabricated out-of-bounds write.
    """
    if not body:
        return None
    scrubbed = scrub(body)
    n = re.escape(name)
    retreats = re.search(rf"\*\s*--\s*\(\s*\*\s*{n}\s*\)", scrubbed) or \
        re.search(rf"\(\s*\*\s*{n}\s*\)\s*-=", scrubbed) or \
        re.search(rf"\*\s*{n}\s*-=", scrubbed)
    advances = re.search(rf"\(\s*\*\s*{n}\s*\)\s*\+=", scrubbed) or \
        re.search(rf"\*\s*{n}\s*\+=", scrubbed) or \
        re.search(rf"\*\s*\(\s*{n}\s*\)\s*\+\+", scrubbed)
    if retreats and not advances:
        return False
    if advances and not retreats:
        return True
    return None


#: When a body delegates its cursor arithmetic it shows no direction of its
#: own -- most of mbedTLS's ASN.1 writers just call write_len/write_tag. The
#: companion parameter's name is the convention these APIs use to say which
#: end it sits at, and it is disclosed in the harness assumptions either way.
_START_NAMES = ("start", "begin", "beginning", "base", "buf_start")
_END_NAMES = ("bound", "end", "limit", "stop", "buf_end", "last")


def _direction_from_name(other_name: str) -> bool | None:
    """True (forwards) if the companion names the buffer's end, False if it
    names the start, None if the name says nothing."""
    lowered = other_name.lower().lstrip("_")
    if lowered in _START_NAMES:
        return False
    if lowered in _END_NAMES:
        return True
    return None


def _emit_backward_cursor(
    cursor: Param, other: Param, forwards: bool, options: HarnessOptions,
    assumptions: list[str], typedefs: dict[str, str],
) -> list[str]:
    """One buffer bracketed by the cursor and its companion pointer.

    Which end each sits at depends on the direction the body writes, so the
    caller passes it in rather than this guessing.
    """
    cap = options.max_array_len
    inner = normalize_type(cursor.pointee().replace("*", "").strip(), typedefs)
    storage = f"{cursor.name}_buf"
    holder = f"{cursor.name}_cursor"
    if forwards:
        assumptions.append(
            f"`*{cursor.name}` and `{other.name}` bracket one buffer of {cap} "
            f"`{inner}`: `*{cursor.name}` at its start and `{other.name}` one "
            "past its end, since the body advances the cursor"
        )
        ends = [
            f"{inner} *{holder} = {storage};",
            f"{_decl_type(other.type)} {other.name} = {storage} + {cap};",
        ]
    else:
        assumptions.append(
            f"`{other.name}` and `*{cursor.name}` bracket one buffer of {cap} "
            f"`{inner}`: `{other.name}` at its start and `*{cursor.name}` one "
            "past its end, which is how a caller sets up a backwards DER writer"
        )
        ends = [
            f"{_decl_type(other.type)} {other.name} = {storage};",
            f"{inner} *{holder} = {storage} + {cap};",
        ]
    return [
        f"{inner} {storage}[{cap}];",
        f"for (unsigned long {_LOOP_VAR} = 0; {_LOOP_VAR} < {cap}; ++{_LOOP_VAR})",
        f"    {storage}[{_LOOP_VAR}] = VERIPP_NONDET_CHAR();",
        *ends,
        f"{_decl_type(cursor.type)} {cursor.name} = &{holder};",
    ]


def _emit_scalar(
    param: Param,
    signature: Signature,
    assumptions: list[str],
    options: HarnessOptions,
    typedefs: dict[str, str],
    source_text: str = "",
    teardown: list[str] | None = None,
) -> list[str]:
    if param.is_pointer:
        if source_text and nondet_for(param.pointee(), typedefs) is None:
            obj = _try_object(param, signature, assumptions, options, typedefs,
                              source_text, teardown)
            if obj is not None:
                return obj
        return _emit_lone_pointer(param, assumptions, options, typedefs, signature.body)
    if param.is_reference:
        if source_text and nondet_for(param.pointee(), typedefs) is None:
            obj = _try_object(param, signature, assumptions, options, typedefs,
                          source_text, teardown)
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


def _try_object(param, signature, assumptions, options, typedefs, source_text,
                teardown=None):
    """Build a struct parameter, or return None so the caller reports why not."""
    try:
        return _emit_object(param, signature, assumptions, options, typedefs,
                            source_text, teardown)
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
    _pointee = normalize_type(pointee, typedefs)
    if _pointee in _STRING_POINTEES and (
        # `const char *` without a length is a C string by convention. The
        # unsigned and signed spellings are not: `const unsigned char *` is
        # how C says "binary data", and mbedTLS hands exactly that to
        # asn1_write_named_bitstring(p, start, buf, bits) -- a bitstring,
        # not a string. Those need the body to actually walk to a NUL.
        (param.is_const and _pointee == "char")
        or _walks_to_terminator(param.name, body)
        # strcmp-shaped pairs test the terminator on ONE operand and walk
        # the other in lockstep: cJSON's case_insensitive_strcmp checks
        # `*string1 == '\0'` and never tests string2, which is still a C
        # string by contract. So a const char-like pointer in a function
        # that walks *anything* to a NUL is a string too -- while the
        # bitstring writer, which tests no terminator anywhere, is not.
        or (param.is_const and _body_tests_any_terminator(body))
    ):
        # A `const char*` with no length parameter is, in practice, a C
        # string. A single nondet char is the wrong model: anything that
        # walks to the terminator (strlen, parsers) reads past it, and the
        # counterexample blames the library for the harness's lie.
        #
        # A *mutable* `char*` is the same contract whenever the body reads
        # its way to a terminator -- an in-place rewriter like
        # `cJSON_Minify(char *json)` cannot be const and is still a C string.
        # Requiring that evidence keeps genuine output buffers, which are
        # written rather than walked, on the sizing path below.
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


#: Character types a C string can be built from. `unsigned char*` is not an
#: exotic case: libraries that treat text as bytes -- cJSON, most parsers --
#: spell every string that way, and modelling those as a two-element buffer
#: manufactures an out-of-bounds report for each one.
_STRING_POINTEES = ("char", "unsigned char", "signed char")


#: Reading a char pointer until it hits NUL -- `while (*p)`, `p[0] != 0`,
#: `!*p`. This is the signal that the parameter is a string rather than a
#: buffer the function fills: its length is bounded by a terminator the
#: caller promises, not by any index appearing in the body.
_TERMINATOR_TESTS = (
    r"while\s*\(\s*\*\s*{name}\s*\)",
    r"\*\s*{name}\s*(?:==|!=)\s*0",
    r"{name}\s*\[\s*0\s*\]\s*(?:==|!=)\s*0",
    r"!\s*\*\s*{name}\b",
    r"\(\s*\*\s*{name}\s*\)\s*\[\s*0\s*\]\s*(?:==|!=)\s*0",
    # Handing the pointer to a <string.h> routine that stops at NUL is the
    # same promise, stated by delegation: strlen(token) is only meaningful
    # for a terminated string. The bounded forms are deliberately absent --
    # strncmp(s, "-0", 2) reads at most two bytes and promises nothing about
    # termination. Treating it as evidence stopped parson's is_decimal(string,
    # length) from pairing `string` with `length`, and manufactured an
    # out-of-bounds read in a function that guards every access. Without this, a function whose *other*
    # parameter carries the length -- lwip_strnstr(buffer, token, n) -- has
    # that length applied to the string too, and the over-read lands inside
    # the checker's own strlen rather than in the code under test.
    r"\b(?:strlen|strcmp|strcpy|strcat|strchr|strrchr|strstr|strdup|strspn|"
    r"strcspn|strpbrk|strtok|atoi|atol|atof|strtol|strtoul|strtod)"
    r"\s*\(\s*(?:\([^)]*\)\s*)?{name}\s*[,)]",
)

#: The NUL character literal, spelled without escapes so that nothing has to
#: carry a backslash through both this source and a regular expression. The
#: first attempt matched two literal backslashes and silently never fired.
_NUL_LITERAL = "'\\0'"


def _body_tests_any_terminator(body: str) -> bool:
    """Whether the body walks some pointer to a NUL, i.e. handles strings."""
    if not body:
        return False
    normalised = scrub(body.replace(_NUL_LITERAL, "0"))
    return any(
        re.search(pattern.format(name=r"[A-Za-z_]\w*"), normalised)
        for pattern in _TERMINATOR_TESTS
    )


#: Bounded <string.h> routines. Unlike `strlen`, these promise nothing about
#: termination -- which is why they are absent from _TERMINATOR_TESTS -- but
#: they do say what the buffer holds: text. All of them stop at a NUL in
#: either operand, so a caller passing one a slice with a NUL inside it is
#: passing a string shorter than the length it also passed. The binary
#: equivalents (memcmp, memchr, memcpy) are deliberately not here.
_TEXT_SLICE_USES = (
    r"\b(?:strncmp|strncasecmp|strnicmp|strncpy|strncat|strnlen|strndup)"
    r"\s*\(\s*(?:\([^)]*\)\s*)?{name}\s*[,)]"
)


def _is_text_slice(name: str, body: str) -> bool:
    """Whether `body` treats `name` as text of a given length, not as bytes."""
    if not body:
        return False
    normalised = scrub(body.replace(_NUL_LITERAL, "0"))
    return bool(re.search(_TEXT_SLICE_USES.format(name=re.escape(name)),
                          normalised))


def _walks_to_terminator(name: str, body: str) -> bool:
    """Whether `body` reads `name` until a NUL, i.e. treats it as a string."""
    if not body:
        return False
    normalised = scrub(body.replace(_NUL_LITERAL, "0"))
    escaped = re.escape(name)
    return any(
        re.search(pattern.format(name=escaped), normalised)
        for pattern in _TERMINATOR_TESTS
    )


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
    body: str = "",
    length_type: str | None = None,
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
    # A signed length left free is negative half the time, and every str- or
    # mem- routine converts it to a huge size_t. That reads far past the
    # buffer whatever the library does, and no caller passes it.
    signed_length = length_type is not None and not normalize_type(
        length_type, typedefs
    ).startswith(("unsigned", "size_t"))
    assumptions.append(
        f"`{param.name}` points to exactly `{length}` valid elements, with "
        + (f"0 <= {length} <= {cap}" if signed_length else f"{length} <= {cap}")
        + " (harness bound on array length)"
    )
    lines = [
        f"VERIPP_ASSUME({length} <= {cap});",
        f"{pointee} {storage}[{cap}];",
        f"for (unsigned long {_LOOP_VAR} = 0; {_LOOP_VAR} < {cap}; ++{_LOOP_VAR})",
        f"    {storage}[{_LOOP_VAR}] = {nondet};",
    ]
    if signed_length:
        lines.insert(1, f"VERIPP_ASSUME({length} >= 0);")
    if _is_text_slice(param.name, body):
        # A slice with a NUL inside it is a string shorter than the length
        # that came with it, and the bounded str-routines the body uses stop
        # there -- so a comparison against a shorter literal can "succeed"
        # and the index that follows runs off the end of the LITERAL. That is
        # what tinyexpr's find_builtin(name, len) was reported for, and no
        # tokeniser can produce it: identifiers do not contain NULs.
        lines += [
            # Clamped by the cap as well: the assumption must not be the
            # thing that reads out of bounds.
            f"for (unsigned long {_LOOP_VAR} = 0; {_LOOP_VAR} < {cap}UL && "
            f"{_LOOP_VAR} < (unsigned long){length}; ++{_LOOP_VAR})",
            f"    VERIPP_ASSUME({storage}[{_LOOP_VAR}] != 0);",
        ]
        assumptions.append(
            f"`{param.name}` holds {length} characters of text with no "
            f"terminator among them -- the body compares it with bounded "
            f"<string.h> routines, which stop at a NUL, so a caller passing "
            f"a shorter string than `{length}` claims is NOT modelled"
        )
    lines.append(f"{_decl_type(param.type)} {param.name} = {storage};")
    return lines


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
{preamble}
int main() {{
{body}
    return 0;
}}
"""


def _comment_lines(assumptions: list[str]) -> str:
    """Assumptions as a `//` block. Each becomes exactly one line: a stray
    newline here would end the comment and emit uncompilable C++."""
    return "\n".join(f"//   - {' '.join(a.split())}" for a in assumptions) or "//   - none"


#: A file-scope pointer variable initialised to one of the C allocators --
#: `static JSON_Malloc_Function parson_malloc = malloc;`. Libraries write
#: this so callers can swap the allocator; the side effect is that the
#: checker sees an indirect call to an INTRINSIC, loses its model of it, and
#: hands back an unconstrained pointer that fails at the first use.
_ALLOCATOR_HOOK_RES = (
    # Through a typedef: `static JSON_Malloc_Function parson_malloc = malloc;`
    re.compile(
        r"^[ \t]*(?:static[ \t]+)?(?!return\b)[A-Za-z_]\w*[\w \t\*]*?[ \t\*]"
        r"(\w+)[ \t]*=[ \t]*(malloc|calloc|realloc|free)[ \t]*;",
        re.M,
    ),
    # Spelled out: `static void *(*global_malloc)(size_t) = malloc;`
    re.compile(
        r"^[ \t]*(?:static[ \t]+)?[A-Za-z_][\w \t\*]*\([ \t]*\*[ \t]*"
        r"(\w+)[ \t]*\)[ \t]*\([^;()]*\)[ \t]*=[ \t]*"
        r"(malloc|calloc|realloc|free)[ \t]*;",
        re.M,
    ),
)

#: Wrapper bodies, by the allocator each stands in for. Calling malloc
#: DIRECTLY from a function that has a body is the whole trick: the call the
#: library makes is then to something the checker can see through, and the
#: allocation at the end of it is modelled again.
_HOOK_WRAPPERS: dict[str, str] = {
    "malloc": "static void *veripp_hook_malloc(size_t n) { return malloc(n); }",
    "calloc": "static void *veripp_hook_calloc(size_t n, size_t s)"
              " { return calloc(n, s); }",
    "realloc": "static void *veripp_hook_realloc(void *p, size_t n)"
               " { return realloc(p, n); }",
    "free": "static void veripp_hook_free(void *p) { free(p); }",
}


#: `#define internal_malloc malloc` -- cJSON writes its hook table in terms
#: of these, so the table reads `{ internal_malloc, internal_free, ... }`.
_ALLOCATOR_ALIAS_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]+(malloc|calloc|realloc|free)[ \t]*$",
    re.M,
)

#: A file-scope struct of them: cJSON's
#: `static internal_hooks global_hooks = { internal_malloc, internal_free,
#: internal_realloc };`. The type's own definition gives the field names, in
#: the order the initialiser fills them.
_HOOK_TABLE_RE = re.compile(
    r"^[ \t]*(?:static[ \t]+)?(?:struct[ \t]+)?(\w+)[ \t]+(\w+)[ \t]*="
    r"[ \t]*\{([^{}]*)\}[ \t]*;",
    re.M,
)

#: A function-pointer member, in declaration order: `void *(CDECL *allocate)
#: (size_t size);`. Read straight from the struct body rather than through
#: the signature parser, which has no reason to model these.
_FUNCTION_POINTER_MEMBER_RE = re.compile(r"\(\s*\w*\s*\*\s*(\w+)\s*\)\s*\(")


def _allocator_aliases(source_text: str) -> dict[str, str]:
    return dict(_ALLOCATOR_ALIAS_RE.findall(source_text))


def _hook_table_fields(source_text: str, type_name: str) -> list[str]:
    """Function-pointer members of `type_name`, in declaration order."""
    match = re.search(
        r"\b(?:struct|typedef[ \t]+struct)\b[^{;]*\b" + re.escape(type_name)
        + r"\b[^{;]*\{(.*?)\}", source_text, re.S,
    )
    if match is None:
        match = re.search(
            r"typedef[ \t]+struct\b[^{;]*\{(.*?)\}[ \t\n]*"
            + re.escape(type_name) + r"[ \t]*;", source_text, re.S,
        )
    if match is None:
        return []
    return _FUNCTION_POINTER_MEMBER_RE.findall(match.group(1))


def _hook_tables(source_text: str) -> dict[str, str]:
    """Hook-table variables by their type: `{"internal_hooks": "global_hooks"}`."""
    aliases = _allocator_aliases(source_text)
    tables: dict[str, str] = {}
    for type_name, table, initialiser in _HOOK_TABLE_RE.findall(source_text):
        resolved = [aliases.get(e.strip(), e.strip())
                    for e in initialiser.split(",")]
        if any(r in _HOOK_WRAPPERS for r in resolved):
            tables.setdefault(type_name, table)
    return tables


def _allocator_hooks(source_text: str) -> list[tuple[str, str]]:
    """File-scope allocator hooks, as (lvalue, allocator) pairs.

    The lvalue is a plain variable for the scalar form and `table.field` for
    the struct form; both are assignable from the harness, which includes the
    library's source and so sees its statics.
    """
    aliases = _allocator_aliases(source_text)
    seen: dict[str, str] = {}
    for pattern in _ALLOCATOR_HOOK_RES:
        for name, allocator in pattern.findall(source_text):
            seen.setdefault(name, allocator)
    for type_name, table, initialiser in _HOOK_TABLE_RE.findall(source_text):
        entries = [e.strip() for e in initialiser.split(",")]
        resolved = [aliases.get(e, e) for e in entries]
        if not any(r in _HOOK_WRAPPERS for r in resolved):
            continue
        fields = _hook_table_fields(source_text, type_name)
        if len(fields) != len(resolved):
            continue
        for field, allocator in zip(fields, resolved):
            if allocator in _HOOK_WRAPPERS:
                seen.setdefault(f"{table}.{field}", allocator)
    return sorted(seen.items())


def _resolve_allocator_hooks(
    source_text: str, assumptions: list[str]
) -> tuple[list[str], list[str]]:
    """Point the library's allocator hooks at wrappers with real bodies.

    This does not change what the library does -- the hooks already hold
    these very allocators, which is what an unconfigured caller gets. It
    changes what the checker can see: an indirect call to the `malloc`
    intrinsic resolves to something bodiless, and every pointer downstream
    becomes unconstrained. Through a wrapper that calls malloc directly, the
    same code verifies.
    """
    hooks = _allocator_hooks(source_text)
    if not hooks:
        return [], []
    preamble = [_HOOK_WRAPPERS[a] for a in sorted({a for _, a in hooks})]
    body = [f"{name} = veripp_hook_{allocator};" for name, allocator in hooks]
    assumptions.append(
        "the library's allocator hooks ("
        + ", ".join(name for name, _ in hooks)
        + ") point at wrappers that call the same allocators directly. This "
        "is the library's own default, restated so the checker can see "
        "through the indirection -- without it every allocated pointer is "
        "unconstrained and fails at its first use"
    )
    return preamble, body


def _render(source: Path, signature: Signature, body: list[str],
            assumptions: list[str], preamble: list[str] | None = None) -> str:
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
        preamble="\n" + "\n".join(preamble) + "\n" if preamble else "",
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
    text = source.read_text(encoding="utf-8")
    info = find_class(text, class_name)
    expanded = _with_local_includes(source, text, options.include_dirs)
    # Linked TUs resolve callees, so their definitions must be visible
    # here too, or veripp reports stubs the run does not actually have.
    expanded = "\n".join([expanded, *_linked_text(options)])
    typedefs = collect_scalar_typedefs(expanded)
    _ENUMS.clear()
    _ENUMS.update(collect_enum_types(expanded))
    pointer_aliases = collect_pointer_typedefs(expanded)
    for method in info.methods:
        _expand_pointer_aliases(method, pointer_aliases)
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
    teardown: list[str] | None = None,
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

    # Before asking what the fields could be, ask whether the library itself
    # will hand us one. Ahead of find_struct on purpose: when a constructor
    # is available the struct's shape stops being the harness's business.
    if options.use_constructors and param.is_pointer:
        factories = _find_factories(source_text, type_name, signature.name, typedefs)
        if factories:
            return _emit_from_factories(
                param, type_name, factories, assumptions,
                _find_destructor(source_text, type_name, signature.name, typedefs),
                teardown,
            )
        chain = _find_constructor_chain(
            source_text, type_name, signature.name, typedefs
        )
        if chain is not None:
            source_type, chain_factories, accessor = chain
            return _emit_from_chain(
                param, type_name, source_type, chain_factories, accessor,
                assumptions,
                _find_destructor(source_text, source_type, signature.name, typedefs),
                teardown,
            )

    # cJSON passes its allocator table down as a parameter -- every call site
    # writes `&global_hooks`. Filling those function pointers at random gives
    # the function an allocator that does not exist, and the first thing it
    # allocates is unusable. Copy the table the library actually uses.
    table = _hook_tables(source_text).get(type_name)
    if table is not None:
        storage = f"{param.name}_obj"
        assumptions.append(
            f"`{param.name}` is a copy of `{table}`, the library's own "
            f"allocator table, which is what its call sites pass. Filling "
            f"its function pointers nondeterministically instead would hand "
            f"the code an allocator that does not exist"
        )
        return [
            f"{type_name} {storage} = {table};",
            f"{_decl_type(param.type)} {param.name} = &{storage};"
            if param.is_pointer else f"{type_name}& {param.name} = {storage};",
        ]

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


#: What a struct calls its cursor into its own buffer.
_CURSOR_FIELD_NAMES = ("offset", "pos", "position", "index", "ix", "cursor",
                       "read", "used", "consumed")


def _cursor_fields(info: StructInfo, pairs: dict[str, str],
                   typedefs: dict[str, str]) -> list[tuple[str, str]]:
    """Cursor fields paired with the length they may not pass.

    cJSON's `parse_buffer` is content + length + offset, and every function
    that takes one is entitled to assume the offset is inside the buffer --
    `can_read` maintains exactly that. Filled independently, the solver hands
    it `length = 4, offset = 0xFFFFFFFFFFFFFFFF` and blames cJSON for the
    read. That single combination accounted for most of the counterexamples
    left in cJSON after the allocator was resolved: parse_string,
    print_string_ptr, get_object_item and the four cJSON_*ObjectItem
    functions that reach them.

    A function that ADVANCES the cursor is where a real violation of this
    would be found, and it is still checked there -- the assumption is on the
    state the function is handed, not on what it does with it.
    """
    if not pairs:
        return []
    integers = {
        f.name for f in info.fields
        if not f.is_pointer and f.array_len is None and _is_integral(f.type, typedefs)
    }
    lengths = set(pairs.values())
    cursors = [
        name for name in integers
        if name not in lengths and name.lower() in _CURSOR_FIELD_NAMES
    ]
    if len(lengths) != 1:
        return []          # ambiguous: say nothing rather than guess a pair
    length = next(iter(lengths))
    return [(cursor, length) for cursor in sorted(cursors)]


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
        # cJSON carries its allocator table INSIDE parse_buffer, so the
        # nondeterministic fill reached it one level down and handed the
        # parser function pointers to nowhere.
        table = _hook_tables(source_text).get(
            re.sub(r"^\s*(?:(?:const|volatile|struct)\s+)+", "", f.type).strip()
        )
        if table is not None:
            lines.append(f"{target} = {table};")
            assumptions.append(
                f"field `{target}` is a copy of `{table}`, the library's own "
                "allocator table, rather than nondeterministic function "
                "pointers that point nowhere"
            )
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
    for cursor, length in _cursor_fields(info, pairs, typedefs):
        constraints.append(f"VERIPP_ASSUME({prefix}.{cursor} <= {prefix}.{length});")
        assumptions.append(
            f"`{prefix}.{cursor}` is at most `{prefix}.{length}`: the cursor "
            "is inside the buffer it indexes, which is what the caller "
            "holding this object maintains. A cursor past the end is not "
            "explored here -- the function that advances it is where that "
            "would be a finding"
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


#: How many constructors to offer the solver. Each is a branch and an
#: allocation, so the whole point is lost if the switch dwarfs the function
#: under test.
_MAX_FACTORIES = 6


def _find_factories(
    source_text: str, type_name: str, target: str, typedefs: dict[str, str]
) -> list[str]:
    """Nullary functions in this file that RETURN a `type_name *`.

    A type usually has several -- cJSON alone ships CreateObject, CreateArray,
    CreateNull, CreateTrue -- and they produce genuinely different objects.
    An earlier attempt picked one and measured worse, which is unsurprising:
    choosing a single factory narrows the question in a way no caller asked
    for. Returning all of them lets the harness pick nondeterministically, so
    every constructible shape stays in scope while none of the impossible
    ones a field-by-field fill invents do.
    """
    wanted = normalize_type(type_name, typedefs)
    found: list[str] = []
    for name in function_definitions(source_text):
        if name == target:
            continue
        try:
            sig = find_function(source_text, name)
        except SignatureError:
            continue
        if sig.params:
            continue                      # needs inputs we would have to invent
        if not sig.body:
            # Declared here, defined elsewhere: the allocation is not
            # modelled, so calling it yields a pointer with nothing behind it
            # and every dereference downstream fabricates a counterexample.
            continue
        ret = sig.return_type.replace("*", " ").strip()
        if "*" not in sig.return_type:
            continue
        ret = re.sub(r"^\s*(?:(?:const|volatile|struct|union|class)\s+)+", "",
                     ret).strip()
        if normalize_type(ret, typedefs) != wanted:
            continue
        found.append(name)
        if len(found) >= _MAX_FACTORIES:
            break
    return found


#: How a C library spells "give this back".
_DESTRUCTOR_HINTS = ("free", "delete", "destroy", "release", "dispose", "close")


def _find_destructor(
    source_text: str, type_name: str, target: str, typedefs: dict[str, str]
) -> str | None:
    """The library's own deallocator for `type_name`, if it has an obvious one.

    A harness that constructs an object and walks away leaks it, and the leak
    check fires in `main` -- which is an artifact, but a costly one: the run
    stops at the first violation, so the question the harness was built to
    ask never gets answered. Constructing and then freeing is also simply
    what a caller does, and it puts the deallocation path under the checker
    rather than outside it.
    """
    wanted = normalize_type(type_name, typedefs)
    best: str | None = None
    for name in function_definitions(source_text):
        if name == target or not any(h in name.lower() for h in _DESTRUCTOR_HINTS):
            continue
        try:
            sig = find_function(source_text, name)
        except SignatureError:
            continue
        if len(sig.params) != 1 or not sig.params[0].is_pointer or not sig.body:
            continue
        if normalize_type(sig.params[0].pointee(), typedefs) != wanted:
            continue
        # Prefer the shortest name: parson has json_value_free and
        # json_value_free_serialized_string; the plain one is the pair to
        # the constructor.
        if best is None or len(name) < len(best):
            best = name
    return best


def _find_constructor_chain(
    source_text: str, type_name: str, target: str, typedefs: dict[str, str]
) -> tuple[str, list[str], str] | None:
    """Reach `type_name` in two steps: construct something else, then ask it.

    Half of a C API's handle types are never returned by a constructor. In
    parson `JSON_Value` has three, but `JSON_Object` and `JSON_Array` have
    none -- you get one by building a value and asking for it. Nineteen of
    the file's functions take exactly those two types, so stopping at the
    direct case leaves the larger half of the API on the field-filling path.

    Returns (source type, its constructors, the accessor).
    """
    wanted = normalize_type(type_name, typedefs)
    best: tuple[str, list[str], str] | None = None
    for name in function_definitions(source_text):
        if name == target:
            continue
        try:
            sig = find_function(source_text, name)
        except SignatureError:
            continue
        if len(sig.params) != 1 or not sig.params[0].is_pointer or not sig.body:
            continue
        if "*" not in sig.return_type:
            continue
        ret = re.sub(r"^\s*(?:(?:const|volatile|struct|union|class)\s+)+", "",
                     sig.return_type.replace("*", " ")).strip()
        if normalize_type(ret, typedefs) != wanted:
            continue
        source_type = re.sub(
            r"^\s*(?:(?:const|volatile|struct|union|class)\s+)+", "",
            sig.params[0].pointee(),
        ).strip()
        if normalize_type(source_type, typedefs) == wanted:
            continue                      # T -> T is a walk, not construction
        factories = _find_factories(source_text, source_type, target, typedefs)
        if not factories:
            continue
        # Prefer the plainest accessor: json_value_get_object over
        # json_object_get_wrapping_value.
        if best is None or len(name) < len(best[2]):
            best = (source_type, factories, name)
    return best


def _emit_from_chain(
    param: Param, type_name: str, source_type: str, factories: list[str],
    accessor: str, assumptions: list[str], destructor: str | None,
    teardown: list[str] | None,
) -> list[str]:
    """Construct the thing that owns one, then ask it for the one we need."""
    holder = f"{param.name}_src"
    storage = f"{param.name}_obj"
    assumptions.append(
        f"`{param.name}` is what `{accessor}` returns for a `{source_type}` "
        f"built by one of {', '.join(factories)} -- the solver chooses which, "
        f"and the result is assumed non-null because a caller holding a "
        f"`{type_name}` got it the same way. States reached by mutating the "
        "object afterwards are NOT explored"
    )
    lines = [f"{source_type} *{holder} = 0;"]
    if len(factories) == 1:
        lines.append(f"{holder} = {factories[0]}();")
    else:
        lines.append(f"switch (VERIPP_NONDET_INT() % {len(factories)}) {{")
        for index, name in enumerate(factories[:-1]):
            lines.append(f"    case {index}: {holder} = {name}(); break;")
        lines.append(f"    default: {holder} = {factories[-1]}(); break;")
        lines.append("}")
    lines += [
        f"VERIPP_ASSUME({holder} != 0);",
        f"{type_name} *{storage} = {accessor}({holder});",
        f"VERIPP_ASSUME({storage} != 0);",
        f"{_decl_type(param.type)} {param.name} = {storage};",
    ]
    if destructor is not None and teardown is not None:
        teardown.append(f"{destructor}({holder});")
        assumptions.append(
            f"the harness frees the `{source_type}` that owns `{param.name}` "
            f"with `{destructor}` after the call, as a caller would"
        )
    return lines


def _emit_from_factories(
    param: Param, type_name: str, factories: list[str], assumptions: list[str],
    destructor: str | None = None, teardown: list[str] | None = None,
) -> list[str]:
    """Let the solver choose which of the library's constructors ran."""
    storage = f"{param.name}_obj"
    assumptions.append(
        f"`{param.name}` is whatever one of {', '.join(factories)} returns -- "
        "the harness lets the solver choose which, so every shape the library "
        "can build is in scope and none that it cannot. States reached by "
        "mutating the object afterwards are NOT explored"
    )
    lines = [f"{type_name} *{storage} = 0;"]
    if len(factories) == 1:
        lines.append(f"{storage} = {factories[0]}();")
    else:
        lines.append(f"switch (VERIPP_NONDET_INT() % {len(factories)}) {{")
        for index, name in enumerate(factories[:-1]):
            lines.append(f"    case {index}: {storage} = {name}(); break;")
        lines.append(f"    default: {storage} = {factories[-1]}(); break;")
        lines.append("}")
    # A constructor that returned null models an allocation failure, which is
    # a different question from the one being asked; the caller would have
    # checked. Without this every such harness fails on the null instead.
    lines.append(f"VERIPP_ASSUME({storage} != 0);")
    lines.append(f"{_decl_type(param.type)} {param.name} = {storage};")
    if destructor is not None and teardown is not None:
        teardown.append(f"{destructor}({storage});")
        assumptions.append(
            f"the harness frees `{param.name}` with `{destructor}` after the "
            "call, as a caller would. Without it the object leaks in the "
            "harness and the run stops on that instead of on the question "
            "being asked"
        )
    return lines


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
