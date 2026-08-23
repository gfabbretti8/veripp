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

from .cppsig import Param, Signature, SignatureError, find_function, match_bracket, scrub

#: Default bound on the length of a generated buffer. Small on purpose: BMC
#: cost grows fast, and off-by-one bugs show up at any length.
DEFAULT_MAX_ARRAY_LEN = 4

_LOOP_VAR = "veripp_i"
_RECEIVER = "veripp_obj"


class HarnessError(Exception):
    """The generator will not emit a harness it cannot justify."""


@dataclass
class HarnessOptions:
    max_array_len: int = DEFAULT_MAX_ARRAY_LEN
    assume_pointers_nonnull: bool = True


@dataclass
class Harness:
    code: str
    signature: Signature
    assumptions: list[str] = field(default_factory=list)
    source: Path | None = None

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / f"veripp_harness_{self.signature.name}.cpp"
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

_TYPE_ALIASES = {
    "signed": "int",
    "signed int": "int",
    "unsigned int": "unsigned",
    "signed long": "long",
    "long int": "long",
    "unsigned long int": "unsigned long",
    "signed long long": "long long",
    "long long int": "long long",
    "unsigned long long int": "unsigned long long",
    "signed short": "short",
    "short int": "short",
    "unsigned short int": "unsigned short",
    "long double": "double",  # ESBMC models it as double
}

_LENGTH_NAMES = {
    "n", "len", "length", "size", "count", "num", "nmemb", "sz", "cnt", "nelem",
    "n_elems", "num_elements", "capacity",
}


def normalize_type(type_: str) -> str:
    """Canonical spelling of a scalar type: no cv-qualifiers, no namespaces."""
    t = re.sub(r"\b(const|volatile|constexpr)\b", " ", type_)
    t = t.replace("std::", "").replace("::", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return _TYPE_ALIASES.get(t, t)


def nondet_for(type_: str) -> str | None:
    return _NONDET_BY_TYPE.get(normalize_type(type_))


# ------------------------------------------------------------- generator ---


def generate(
    source: Path,
    function: str,
    options: HarnessOptions | None = None,
) -> Harness:
    """Build a harness for `function` as defined in `source`."""
    options = options or HarnessOptions()
    text = source.read_text()
    signature = find_function(text, function)
    _reject_conflicting_main(text, source)

    body: list[str] = []
    assumptions: list[str] = []
    lengths = _pair_buffers_with_lengths(signature.params)

    # Scalars first: a buffer's length must exist before we bound it.
    buffers = set(lengths)
    for param in signature.params:
        if param.name in buffers:
            continue
        body += _emit_scalar(param, signature, assumptions)

    for buffer_name, length in lengths.items():
        param = next(p for p in signature.params if p.name == buffer_name)
        body += _emit_buffer(param, length, options, assumptions)

    hoisted = _hoist_requires(signature)
    if hoisted:
        body.append("")
        body.append("// preconditions declared with VERIPP_REQUIRES in "
                    f"`{signature.qualified_name}`")
        body += [f"VERIPP_ASSUME({expr});" for expr in hoisted]

    body.append("")
    body += _emit_call(signature, assumptions)

    return Harness(
        code=_render(source, signature, body, assumptions),
        signature=signature,
        assumptions=assumptions,
        source=source,
    )


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


def _pair_buffers_with_lengths(params: list[Param]) -> dict[str, str]:
    """Map each pointer parameter to the parameter holding its length."""
    pairs: dict[str, str] = {}
    integer_params = {
        p.name: p
        for p in params
        if not p.is_pointer and not p.is_reference and _is_integral(p.type)
    }
    for idx, param in enumerate(params):
        if not param.is_pointer:
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
                    if p.name in integer_params and p.name.lower() in _LENGTH_NAMES
                ),
                None,
            )
        if named is not None:
            pairs[param.name] = named
    return pairs


def _is_integral(type_: str) -> bool:
    t = normalize_type(type_)
    return t in _NONDET_BY_TYPE and t not in ("float", "double", "bool")


def _emit_scalar(param: Param, signature: Signature, assumptions: list[str]) -> list[str]:
    if param.is_pointer:
        return _emit_lone_pointer(param, assumptions)
    if param.is_reference:
        return _emit_reference(param, assumptions)

    nondet = nondet_for(param.type)
    if nondet is None:
        raise HarnessError(
            f"cannot build a nondeterministic value for parameter "
            f"`{param.type} {param.name}` of `{signature.qualified_name}`: only "
            "scalar types, and pointers to them, are supported so far. Write "
            "the harness by hand and verify it directly (no --function)."
        )
    return [f"{_decl_type(param.type)} {param.name} = {nondet};"]


def _emit_lone_pointer(param: Param, assumptions: list[str]) -> list[str]:
    pointee = param.pointee()
    nondet = nondet_for(pointee)
    if nondet is None:
        raise HarnessError(
            f"parameter `{param.type} {param.name}` points to `{pointee}`, which "
            "the generator cannot construct; write the harness by hand."
        )
    storage = f"{param.name}_obj"
    assumptions.append(
        f"`{param.name}` is non-null and points to one valid, "
        f"nondeterministic `{pointee}` (no length parameter was found)"
    )
    return [
        f"{pointee} {storage} = {nondet};",
        f"{_decl_type(param.type)} {param.name} = &{storage};",
    ]


def _emit_reference(param: Param, assumptions: list[str]) -> list[str]:
    pointee = param.pointee()
    nondet = nondet_for(pointee)
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
    param: Param, length: str, options: HarnessOptions, assumptions: list[str]
) -> list[str]:
    pointee = param.pointee()
    nondet = nondet_for(pointee)
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


def _render(source: Path, signature: Signature, body: list[str], assumptions: list[str]) -> str:
    params = ", ".join(f"{p.type} {p.name}".replace("  ", " ") for p in signature.params)
    assumption_lines = "\n".join(f"//   - {a}" for a in assumptions) or "//   - none"
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
