"""A small, dependency-free C++ declaration scanner.

This is *not* a C++ parser. It is a deliberately narrow scanner that finds the
definition of one named function in one translation unit and recovers its
signature, which is all the M1 harness generator needs. Anything it cannot
recognise it refuses to guess about: it raises `SignatureError` so the caller
can report an honest failure instead of emitting a wrong harness.

M2 replaces this with a libclang-based slicer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_OPEN = {"(": ")", "[": "]", "{": "}", "<": ">"}

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


def normalize_type(type_: str, typedefs: dict[str, str] | None = None) -> str:
    """Canonical spelling of a type: no cv-qualifiers, keywords or namespaces.

    `struct Node` and `Node` name the same type, and C code mixes the two
    freely, so they have to compare equal.

    `typedefs` maps project-local aliases (`mz_ulong`) to their underlying
    types; chains are resolved by `collect_scalar_typedefs`.
    """
    t = re.sub(r"\b(const|volatile|constexpr|struct|union|enum|class)\b", " ", type_)
    t = t.replace("std::", "").replace("::", " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*([*&])", r"\1", t)  # `int *` == `int*`
    if typedefs:
        suffix = ""
        while t and t[-1] in "*&":
            suffix = t[-1] + suffix
            t = t[:-1].strip()
        t = typedefs.get(t, t) + suffix
        t = re.sub(r"\s*([*&])", r"\1", t)
    return _TYPE_ALIASES.get(t, t)


_TYPEDEF_RE = re.compile(
    r"\btypedef\s+((?:[A-Za-z_]\w*\s+)*[A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*;"
)
#: `typedef z_stream FAR *z_streamp;` -- an alias for a pointer, and zlib's
#: entire public API is written in terms of one.
_POINTER_TYPEDEF_RE = re.compile(
    r"\btypedef\s+((?:[A-Za-z_]\w*\s+)*[A-Za-z_]\w*)\s+(\*+)\s*([A-Za-z_]\w*)\s*;"
)
#: `#define Z_U8 unsigned long long` -- an object-like macro standing in for a
#: type, which a typedef then aliases.
_TYPE_MACRO_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+"
    r"((?:(?:unsigned|signed|const|long|short|int|char|float|double|_Bool)\b[ \t]*)+"
    r"|[A-Za-z_]\w*)[ \t]*$",
    re.M,
)


def collect_pointer_typedefs(source: str) -> dict[str, str]:
    """Aliases that name a pointer: `typedef z_stream *z_streamp;`.

    Without these, a parameter of type `z_streamp` is not seen as a pointer at
    all, and every function in zlib's public API is refused.
    """
    scrubbed = scrub(source)
    vanishing = empty_macros(source)
    aliases: dict[str, str] = {}
    for m in _POINTER_TYPEDEF_RE.finditer(scrubbed):
        base = " ".join(w for w in m.group(1).split() if w not in vanishing)
        if base:
            aliases[m.group(3)] = f"{base}{m.group(2)}"
    return aliases
_USING_RE = re.compile(r"\busing\s+([A-Za-z_]\w*)\s*=\s*([^;<>{}]+);")


_EMPTY_MACRO_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]*$", re.M)


def empty_macros(source: str) -> set[str]:
    """Macros defined as nothing, which appear inside type expressions.

    zlib writes `typedef Byte FAR Bytef;` where FAR is `#define FAR` -- the
    token vanishes at compile time but sits in the middle of the type as far
    as a scanner is concerned, and the alias fails to resolve.
    """
    return set(_EMPTY_MACRO_RE.findall(source))


def collect_scalar_typedefs(source: str) -> dict[str, str]:
    """Project-local aliases of scalar types: `typedef unsigned long mz_ulong;`.

    Only aliases that bottom out at a plain scalar are kept -- a typedef of a
    struct or a function pointer is not something a harness can nondet-fill,
    so resolving it would only produce a better-looking wrong answer.
    """
    scrubbed = scrub(source)
    vanishing = empty_macros(source)

    def strip_macros(type_: str) -> str:
        kept = [w for w in type_.split() if w not in vanishing]
        return " ".join(kept)

    raw: dict[str, str] = {}
    # An object-like macro standing in for a type is one more link in the
    # chain: zlib reaches `unsigned long long` as z_word_t -> Z_U8 -> the type.
    for m in _TYPE_MACRO_RE.finditer(source):
        raw[m.group(1)] = strip_macros(m.group(2).strip())
    for m in _TYPEDEF_RE.finditer(scrubbed):
        raw[m.group(2)] = strip_macros(m.group(1).strip())
    for m in _USING_RE.finditer(scrubbed):
        raw[m.group(1)] = m.group(2).strip()

    scalars = {
        "void", "bool", "char", "signed char", "unsigned char", "short",
        "unsigned short", "int", "unsigned", "long", "unsigned long",
        "long long", "unsigned long long", "float", "double", "size_t",
        "ptrdiff_t", "int8_t", "uint8_t", "int16_t", "uint16_t", "int32_t",
        "uint32_t", "int64_t", "uint64_t",
    }
    resolved: dict[str, str] = {}
    for alias in raw:
        seen, current = set(), alias
        while current in raw and current not in seen:
            seen.add(current)
            current = normalize_type(raw[current])
        if current in scalars and current != alias:
            resolved[alias] = current
    return resolved


class SignatureError(Exception):
    """The scanner could not recover a signature it is willing to stand behind."""


#: A cv-qualifier applied to the pointer itself, as in `cJSON * const item`.
#: It says the pointer cannot be reassigned, which is nothing to a harness --
#: but it hides the `*`, and a parameter not recognised as a pointer is
#: refused outright.
_TRAILING_CV_RE = re.compile(r"(?:\s*\b(?:const|volatile)\b)+\s*$")


@dataclass
class Param:
    type: str
    name: str

    @property
    def _bare(self) -> str:
        """The type with any cv-qualifier on the pointer itself removed."""
        return _TRAILING_CV_RE.sub("", self.type.rstrip()).rstrip()

    @property
    def is_pointer(self) -> bool:
        return self._bare.endswith("*")

    @property
    def is_reference(self) -> bool:
        return self._bare.endswith("&")

    @property
    def is_const(self) -> bool:
        return bool(re.match(r"^\s*const\b", self.type))

    def pointee(self) -> str:
        """Type pointed/referred to, with the outer `*`/`&` and `const` removed."""
        t = self._bare
        if t.endswith("*") or t.endswith("&"):
            t = t[:-1].rstrip()
        return re.sub(r"^\s*const\b\s*", "", t).strip()


@dataclass
class Field:
    """One data member of a struct or class."""

    type: str
    name: str
    array_len: str | None = None   # "4" for `int a[4]`, "" for `int a[]`
    access: str = "public"

    @property
    def is_pointer(self) -> bool:
        return self.type.rstrip().endswith("*")

    def pointee(self) -> str:
        t = self.type.rstrip()
        if t.endswith("*"):
            t = t[:-1].rstrip()
        return re.sub(r"^\s*const\b\s*", "", t).strip()


@dataclass
class StructInfo:
    """A struct/class viewed as data: what a harness must fill in."""

    name: str
    fields: list[Field] = field(default_factory=list)
    is_union: bool = False
    unsupported: dict[str, str] = field(default_factory=dict)  # field -> why


@dataclass
class ClassInfo:
    """A class and the public surface a sequence harness can drive."""

    name: str
    is_struct: bool
    templated: bool
    methods: list["Signature"] = field(default_factory=list)
    default_constructible: bool = True
    constructors: list["Signature"] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # method -> why


@dataclass
class Signature:
    name: str
    return_type: str
    params: list[Param] = field(default_factory=list)
    class_name: str | None = None
    is_static: bool = False
    is_const: bool = False
    body: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.class_name}::{self.name}" if self.class_name else self.name

    @property
    def returns_void(self) -> bool:
        return self.return_type.replace(" ", "") in ("void", "")


# ---------------------------------------------------------------- lexing ---


def scrub(text: str) -> str:
    """Blank out comments and literal contents, preserving length and newlines.

    Offsets into the result are valid offsets into the input, so we can scan a
    version of the file with no strings or comments and still slice the real
    source.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i : i + 2]
        if two == "//":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif two == "/*":
            while i < n and text[i - 1 : i + 1] != "*/":
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
        elif c in "\"'":
            quote = c
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    out[i] = " "
                    i += 1
                if i < n and text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def match_bracket(text: str, start: int) -> int:
    """Index of the bracket closing the one at `start`. Raises if unbalanced."""
    opener = text[start]
    closer = _OPEN[opener]
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    raise SignatureError(f"unbalanced {opener!r} at offset {start}")


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep`, ignoring separators nested in (), [], {} or <>."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------- classes ---


@dataclass
class _ClassRange:
    name: str
    start: int  # offset of the '{'
    end: int  # offset of the matching '}'
    templated: bool = False


# `union` belongs here: C libraries routinely hand out a union as a handle
# type (LZ4_stream_t is `union LZ4_stream_u`), and a scanner that only knows
# class and struct refuses every function taking one.
_CLASS_RE = re.compile(r"\b(class|struct|union)\s+([A-Za-z_]\w*)\s*(?::[^;{]*)?\{")


def find_class_ranges(scrubbed: str) -> list[_ClassRange]:
    ranges = []
    for m in _CLASS_RE.finditer(scrubbed):
        brace = scrubbed.index("{", m.end() - 1)
        try:
            ranges.append(
                _ClassRange(
                    m.group(2),
                    brace,
                    match_bracket(scrubbed, brace),
                    templated=_preceded_by_template(scrubbed, m.start()),
                )
            )
        except SignatureError:
            continue
    return ranges


def _preceded_by_template(scrubbed: str, pos: int) -> bool:
    """True if a `template <...>` clause sits immediately before `pos`."""
    head = scrubbed[:pos].rstrip()
    if not head.endswith(">"):
        return False
    depth = 0
    for i in range(len(head) - 1, -1, -1):
        if head[i] == ">":
            depth += 1
        elif head[i] == "<":
            depth -= 1
            if depth == 0:
                return head[:i].rstrip().endswith("template")
    return False


# ------------------------------------------------------------- signature ---

_DECL_STOP = ";{}:"
_LEADING_QUALS = {
    "static", "inline", "virtual", "explicit", "constexpr", "consteval",
    "friend", "extern", "template",
}
_TRAILING_QUALS = {"const", "noexcept", "override", "final", "volatile", "&", "&&"}


def parse_target(target: str) -> tuple[str, list[str] | None]:
    """Split `--function` syntax: `f` or `f(int, unsigned long)`.

    The optional parameter list disambiguates overloads. Types are compared
    after normalisation, so `f(const int*,unsigned)` and
    `f(int *, unsigned int)` name the same overload.
    """
    target = target.strip()
    if "(" not in target:
        return target, None
    lparen = target.index("(")
    name = target[:lparen].strip()
    inner = target[lparen:].strip()
    if not inner.endswith(")") or not re.fullmatch(r"[A-Za-z_]\w*", name):
        raise SignatureError(
            f"could not parse --function target {target!r}; expected `name` "
            "or `name(type, type, ...)`"
        )
    return name, split_top_level(inner[1:-1])


def find_function(source: str, target: str) -> Signature:
    """Recover the signature of the *definition* of `target` in `source`.

    `target` is a bare name, or `name(type, ...)` to pick one overload.
    """
    name, wanted = parse_target(target)
    scrubbed = scrub(source)
    classes = find_class_ranges(scrubbed)
    typedefs = collect_scalar_typedefs(source)

    candidates = []
    for m in re.finditer(rf"\b{re.escape(name)}\s*\(", scrubbed):
        lparen = scrubbed.index("(", m.end() - 1)
        try:
            rparen = match_bracket(scrubbed, lparen)
        except SignatureError:
            continue
        quals, brace = _scan_trailing(scrubbed, rparen + 1)
        if brace is None:
            continue  # a declaration or a call site, not a definition
        candidates.append((m.start(), lparen, rparen, quals, brace))

    if not candidates:
        raise SignatureError(
            f"no definition of `{name}` found in the file "
            "(only definitions can be harnessed; declarations have no body)"
        )
    if wanted is not None:
        wanted_types = [normalize_type(w, typedefs) for w in wanted]
        matching = []
        for cand in candidates:
            try:
                params = _parse_params(source[cand[1] + 1 : cand[2]])
            except SignatureError:
                continue
            if [normalize_type(p.type, typedefs) for p in params] == wanted_types:
                matching.append(cand)
        if not matching:
            raise SignatureError(
                f"no overload of `{name}` matches ({', '.join(wanted)}); "
                f"candidates:\n{_describe_candidates(source, scrubbed, name, candidates)}"
            )
        candidates = matching
    if len(candidates) > 1:
        raise SignatureError(
            f"`{name}` is defined {len(candidates)} times; pick one overload "
            "by parameter types, e.g. --function "
            f"'{name}(int, unsigned)'. Candidates:\n"
            f"{_describe_candidates(source, scrubbed, name, candidates)}"
        )

    name_start, lparen, rparen, quals, brace = candidates[0]

    head, after_colon = _decl_head(scrubbed, name_start)
    enclosing = _enclosing_class_range(classes, name_start)
    qualifier = _qualifier(scrubbed[head:name_start], name)
    class_name = qualifier or (enclosing.name if enclosing else None)
    if qualifier is not None:
        enclosing = next((c for c in classes if c.name == qualifier), enclosing)
    _reject_unmodellable(
        scrubbed, head, name_start, name, class_name, enclosing, after_colon
    )

    # Read the return type off the scrubbed text: comments must not leak into it.
    return_type, is_static = _return_type(scrubbed[head:name_start], name)
    params = _parse_params(source[lparen + 1 : rparen])
    body_end = match_bracket(scrubbed, brace)

    return Signature(
        name=name,
        return_type=return_type,
        params=params,
        class_name=class_name,
        is_static=is_static,
        is_const="const" in quals,
        body=source[brace + 1 : body_end],
    )


def _describe_candidates(source, scrubbed, name, candidates) -> str:
    lines = []
    for cand in candidates:
        line_no = scrubbed.count("\n", 0, cand[0]) + 1
        try:
            params = ", ".join(
                p.type for p in _parse_params(source[cand[1] + 1 : cand[2]])
            )
        except SignatureError:
            params = "?"
        lines.append(f"  line {line_no}: {name}({params})")
    return "\n".join(lines)


def _scan_trailing(scrubbed: str, pos: int) -> tuple[set[str], int | None]:
    """Read qualifiers after the parameter list. Returns (quals, brace offset).

    Strict on purpose. An earlier version skipped over `,`, which made every
    entry of a constructor initialiser list -- `: Base( 0 ), member_( x )` --
    look like a function definition whose body was the constructor's, and swept
    the whole list into the "return type". Anything not recognised here means
    "this is not a definition I understand", which is always the safe answer.
    """
    quals: set[str] = set()
    i, n = pos, len(scrubbed)
    trailing_return = False
    while i < n:
        ch = scrubbed[i]
        if ch.isspace():
            i += 1
        elif ch == "{":
            return quals, i
        elif scrubbed.startswith("->", i):
            trailing_return = True
            i += 2
        elif ch == "(":  # noexcept(...)
            i = match_bracket(scrubbed, i) + 1
        elif ch == "[":  # [[attribute]]
            i = match_bracket(scrubbed, i) + 1
        elif ch in "&*":
            i += 1
        elif trailing_return and ch in ":<>":
            i += 1
        else:
            word = re.match(r"[A-Za-z_]\w*", scrubbed[i:])
            if word is None:
                return quals, None  # ',' ':' '=' ';' ... : not a definition
            if word.group(0) in _TRAILING_QUALS:
                quals.add(word.group(0))
            elif not trailing_return:
                return quals, None  # an unknown token before the body
            i += word.end()
    return quals, None


def _reject_unmodellable(
    scrubbed: str,
    head: int,
    name_start: int,
    name: str,
    class_name: str | None,
    enclosing: "_ClassRange | None",
    after_colon: bool = False,
) -> None:
    """Refuse the shapes a harness cannot be built for, before guessing at one.

    Every check here started as something this scanner happily accepted while
    scanning real code: a destructor whose "return type" came out as `~`, and
    template members whose parameters and receiver are type variables, so the
    generated harness could not possibly compile.
    """
    before = scrubbed[head:name_start].rstrip()
    if before.endswith("~"):
        raise SignatureError(
            f"`~{name}` is a destructor; veripp harnesses ordinary functions. "
            "Verify the type's other operations instead."
        )
    if before.endswith("operator") or name == "operator":
        raise SignatureError(f"`{name}` is an operator; operators are not supported yet")
    if class_name == name:
        raise SignatureError(
            f"`{name}` is a constructor of `{class_name}`; constructors are not "
            "supported yet (a harness needs an already-constructed object)"
        )
    if enclosing is not None and enclosing.templated:
        raise SignatureError(
            f"`{class_name}::{name}` is a member of the class template "
            f"`{class_name}`: its parameter types are type variables, so no "
            "concrete harness exists. Wrap a concrete instantiation "
            f"(e.g. `{class_name}<int>`) in a non-template function and target that."
        )
    # Only text following a `:` can be a constructor initialiser list. Without
    # that check a macro-wrapped return type -- `CJSON_PUBLIC(cJSON *) f(...)`,
    # which is how most C libraries mark their exports -- looks like one
    # because of the parentheses, and the function is refused.
    if after_colon and _looks_like_an_initialiser_list(before):
        raise SignatureError(
            f"`{name}` here is an entry in a constructor initialiser list, not a "
            "function definition (the last entry is followed by the constructor "
            "body, which makes it look like one)"
        )
    if re.search(r"\btemplate\b", scrubbed[head:name_start]):
        raise SignatureError(
            f"`{name}` is a function template: its parameter types are type "
            "variables, so no concrete harness exists. Wrap a concrete "
            "instantiation in a non-template function and target that."
        )


_QUALIFIER_RE = re.compile(r"(?:^|[\s*&>])([A-Za-z_]\w*)\s*::\s*$")


def _qualifier(head: str, name: str) -> str | None:
    """Class name from an out-of-line definition head like `void Shape::area`."""
    if not head.rstrip().endswith("::"):
        return None
    m = _QUALIFIER_RE.search(head.rstrip() + "")
    if m is None:
        raise SignatureError(
            f"`{name}` is defined out of line under a qualifier this scanner "
            "cannot read (an explicit template specialisation, or a nested "
            "namespace-qualified name); it is not supported yet"
        )
    return m.group(1)


def _looks_like_an_initialiser_list(head: str) -> bool:
    """True if the text before the name cannot be a return type.

    A return type may contain identifiers, `::`, `*`, `&` and `<...>`, but
    never a bare parenthesis or comma. The last entry of a constructor
    initialiser list -- `: Base( 0 ), a_( 1 ), b_()` followed by the body --
    otherwise parses as a definition whose return type is the whole list.
    """
    head = re.sub(r"\bdecltype\s*\([^()]*\)", " ", head)  # a legitimate return type
    depth, flat = 0, []
    for ch in head:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            flat.append(ch)
    return "(" in flat or "," in flat


def _decl_head(scrubbed: str, name_start: int) -> tuple[int, bool]:
    """Offset where the declaration containing `name_start` begins.

    A single `:` ends the previous thing (an access specifier, a label, the
    start of a constructor initialiser list), but the `:` of a `::` does not:
    stopping there truncated `std::size_t` to `size_t` and made every
    out-of-line definition -- `void C::Clear() {}` -- look like it had no
    return type at all, which refused the most common shape in real C++.
    """
    i = name_start - 1
    after_colon = False
    while i >= 0:
        ch = scrubbed[i]
        if ch == ":":
            if not (scrubbed[i - 1 : i] == ":" or scrubbed[i + 1 : i + 2] == ":"):
                after_colon = True
                break
        elif ch in ";{}":
            break
        i -= 1
    start = i + 1
    # A preprocessor directive also ends whatever came before it: without this,
    # a file whose first function follows an #include has the directive text
    # swept into its return type.
    # Take the LAST directive in one pass: match offsets are relative to this
    # slice, so advancing `start` inside the loop would compound them and, with
    # two #includes, overshoot past the return type.
    region = scrubbed[start:name_start]
    last = None
    for m in re.finditer(r"^[ \t]*#.*$", region, re.M):
        last = m
    if last is not None:
        start += last.end()
        after_colon = False  # a directive intervened; no initialiser list here
    return start, after_colon


#: `CJSON_PUBLIC(cJSON *)` -- a macro wrapping the whole return type, which
#: is how a C library marks its exports. cJSON spells it CJSON_PUBLIC, zlib
#: ZEXTERN, miniz MZ_EXTERN, lwIP LWIP_DECLARE. veripp already knew not to
#: mistake the parentheses for a constructor's initialiser list; it kept the
#: wrapper in the return TYPE, so `CJSON_PUBLIC(cJSON *)` never matched
#: `cJSON *` and no constructor or destructor for the type was ever found.
_EXPORT_MACRO_RE = re.compile(r"^\s*[A-Za-z_]\w*\s*\((.*)\)\s*$", re.S)


def _unwrap_export_macro(ret: str) -> str:
    """`CJSON_PUBLIC(cJSON *)` -> `cJSON *`, leaving anything else alone."""
    match = _EXPORT_MACRO_RE.match(ret)
    if match is None:
        return ret
    inner = match.group(1).strip()
    # A function POINTER return type also ends in parens -- `void (*)(int)` --
    # and unwrapping that would produce nonsense. The wrapper's contents are
    # a type; a function pointer's are a parameter list preceded by `(*)`.
    if not inner or "(" in inner or "*" == inner:
        return ret
    return inner


def _return_type(head: str, name: str) -> tuple[str, bool]:
    tokens = head.replace("\n", " ").split()
    is_static = "static" in tokens
    kept = [t for t in tokens if t not in _LEADING_QUALS]
    if kept and kept[-1].endswith("::"):  # out-of-line definition: Class::name
        kept = kept[:-1]
    ret = " ".join(kept).strip()
    # `Class::name` written as one token leaves the qualifier glued to the type.
    ret = re.sub(r"\b[A-Za-z_]\w*::\s*$", "", ret).strip()
    ret = _unwrap_export_macro(ret)
    if not ret:
        raise SignatureError(
            f"could not determine the return type of `{name}`; constructors and "
            "operators are not supported yet"
        )
    return ret, is_static


def _enclosing_class_range(
    classes: list[_ClassRange], offset: int
) -> _ClassRange | None:
    inner = [c for c in classes if c.start < offset < c.end]
    return max(inner, key=lambda c: c.start) if inner else None


def _parse_params(text: str) -> list[Param]:
    params: list[Param] = []
    for idx, raw in enumerate(split_top_level(text)):
        if raw in ("void", ""):
            continue
        if raw == "...":
            raise SignatureError("variadic functions are not supported")
        decl = split_top_level(raw, "=")[0].strip()  # drop default argument
        params.append(_parse_param(decl, idx))
    return params


_ARRAY_SUFFIX_RE = re.compile(r"\[\s*(\w*)\s*\]\s*$")


def _parse_param(decl: str, idx: int) -> Param:
    array = _ARRAY_SUFFIX_RE.search(decl)
    if array:  # `int a[4]` decays to `int*` for our purposes
        decl = decl[: array.start()].strip()
        ptr_suffix = "*"
    else:
        ptr_suffix = ""

    m = re.search(r"([A-Za-z_]\w*)\s*$", decl)
    if m and not _is_type_word(m.group(1), decl):
        name = m.group(1)
        type_ = decl[: m.start()].strip()
    else:  # unnamed parameter
        name = f"_arg{idx}"
        type_ = decl.strip()
    if not type_:
        raise SignatureError(f"could not split type and name in parameter {decl!r}")
    return Param(type=(type_ + ptr_suffix).strip(), name=name)


_TYPE_WORDS = {
    "void", "bool", "char", "short", "int", "long", "float", "double", "signed",
    "unsigned", "size_t", "ssize_t", "auto",
}


def _is_type_word(word: str, decl: str) -> bool:
    """True if the trailing identifier is part of the type, not a parameter name."""
    if word in _TYPE_WORDS:
        return True
    # `const Foo&` / `Foo*`: the last identifier is the type when nothing follows it.
    return decl.strip() == word


# ------------------------------------------------------------- classes ----

_ACCESS_RE = re.compile(r"\b(public|private|protected)\s*:")


def find_class(source: str, name: str) -> ClassInfo:
    """The public method surface of `name`, for driving a call sequence.

    Only members reachable from outside are useful to a harness, so private
    and protected members are skipped, and so is anything `find_function`
    refuses (templates, operators, overloads it cannot disambiguate).
    """
    scrubbed = scrub(source)
    all_ranges = find_class_ranges(scrubbed)
    ranges = [c for c in all_ranges if c.name == name]
    if not ranges:
        # The names of the classes, not of the functions. Answering "class
        # Ringbuffer not found -- this file defines push, pop, size" points at
        # the wrong kind of thing entirely, and hides the one-letter fix.
        import difflib

        defined = sorted({c.name for c in all_ranges})
        close = difflib.get_close_matches(name, defined, n=1, cutoff=0.6)
        if close:
            hint = f"; did you mean `{close[0]}`?"
        elif defined:
            hint = "; this file defines: " + ", ".join(defined[:8])
            if len(defined) > 8:
                hint += f", and {len(defined) - 8} more"
        else:
            hint = "; this file defines no class, struct or union"
        raise SignatureError(
            f"no definition of class or struct `{name}` in the file{hint}"
        )
    if len(ranges) > 1:
        raise SignatureError(f"`{name}` is defined {len(ranges)} times")
    rng = ranges[0]
    if rng.templated:
        raise SignatureError(
            f"`{name}` is a class template; harness a concrete instantiation "
            "by wrapping it in a non-template type"
        )

    is_struct = _is_struct(scrubbed, rng)
    info = ClassInfo(name=name, is_struct=is_struct, templated=False)
    access = "public" if is_struct else "private"
    ctor_declared = False

    for member_name, member_start, member_access in _members(scrubbed, rng, access):
        if member_name == name:  # constructor
            ctor_declared = True
            if member_access == "public":
                try:
                    info.constructors.append(_signature_at(source, scrubbed, member_start, name))
                except SignatureError:
                    pass
            continue
        if member_access != "public":
            continue
        try:
            info.methods.append(find_function(source, member_name))
        except SignatureError as exc:
            info.skipped[member_name] = str(exc).split(";")[0].split("(")[0].strip()

    info.default_constructible = (
        not ctor_declared
        or any(not c.params for c in info.constructors)
    )
    return info


def _is_struct(scrubbed: str, rng: "_ClassRange") -> bool:
    head = scrubbed[max(0, rng.start - 200) : rng.start]
    m = list(re.finditer(r"\b(class|struct)\s+\w+", head))
    return bool(m) and m[-1].group(1) == "struct"


def _members(scrubbed: str, rng: "_ClassRange", access: str):
    """Yield (name, offset, access) for members declared directly in the body.

    Depth-aware: members of nested classes, and names inside method bodies,
    belong to those scopes and are not part of this class's surface.
    """
    i = rng.start + 1
    depth = 0
    while i < rng.end:
        ch = scrubbed[i]
        if ch in "{([":
            depth += 1
            i += 1
            continue
        if ch in "})]":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            acc = _ACCESS_RE.match(scrubbed, i)
            if acc:
                access = acc.group(1)
                i = acc.end()
                continue
            word = re.match(r"[A-Za-z_]\w*\s*\(", scrubbed[i:])
            if word:
                ident = word.group(0)[: word.group(0).index("(")].strip()
                if ident not in _NOT_A_MEMBER:
                    yield ident, i, access
                # Skip the parameter list so nested names are not mistaken
                # for members.
                try:
                    i = match_bracket(scrubbed, i + word.end() - 1) + 1
                except SignatureError:
                    i += word.end()
                continue
        i += 1


_NOT_A_MEMBER = {
    "if", "for", "while", "switch", "return", "sizeof", "operator", "catch",
    "throw", "new", "delete", "static_assert", "decltype", "noexcept",
    "alignof", "typeid", "explicit",
}


def _signature_at(source: str, scrubbed: str, offset: int, name: str) -> Signature:
    """Signature of the member whose name starts at `offset` (constructors)."""
    lparen = scrubbed.index("(", offset)
    rparen = match_bracket(scrubbed, lparen)
    return Signature(
        name=name,
        return_type="",
        params=_parse_params(source[lparen + 1 : rparen]),
        class_name=name,
    )


# -------------------------------------------------------------- fields ----

# `typedef`/`using`/`friend`/... never declare a data member. `struct`,
# `union`, `enum` and `class` do when they are elaborated type specifiers --
# `struct Node* next;` is a field, while `struct Inner { ... };` is a nested
# definition. Only the definition (which carries a brace) is skipped, or C
# code that spells its types the C way loses those fields silently.
_FIELD_SKIP = re.compile(r"^\s*(typedef|using|friend|static_assert|template)\b")
_NESTED_TYPE = re.compile(r"^\s*(enum|class|struct|union)\b[^;]*\{")
_ARRAY_FIELD_RE = re.compile(r"^(.*?)\s*\[\s*([^\]]*)\s*\]\s*$")
_BITFIELD_RE = re.compile(r":\s*\d+\s*$")


def find_struct(source: str, name: str) -> StructInfo:
    """Data members of `name`, in declaration order.

    Only what a harness has to initialise: methods, nested type definitions,
    and static members (which are not per-object) are skipped. Members the
    scanner cannot read are recorded in `unsupported` rather than dropped, so
    the harness can disclose them instead of silently leaving holes.
    """
    scrubbed = scrub(source)
    ranges = [c for c in find_class_ranges(scrubbed) if c.name == name]
    if not ranges:
        anon = _typedef_struct_range(scrubbed, name)
        if anon is not None:
            ranges = [anon]
    if not ranges:
        # `typedef struct json_value_t JSON_Value;` names an existing tag.
        # Looking up the alias finds nothing while the definition sits in the
        # same file under its tag -- the usual shape of a C API's handle type.
        tag = _struct_tag_for_alias(scrubbed, name)
        if tag is not None and tag != name:
            try:
                return find_struct(source, tag)
            except SignatureError as exc:
                # Report the name the caller wrote, not only the tag behind it.
                raise SignatureError(f"`{name}` is an alias for {exc}") from exc
    if not ranges:
        raise SignatureError(
            f"no definition of `{name}` is visible in this translation unit, "
            "so a harness cannot construct one (an opaque/forward-declared "
            "type: include the header that defines it, or harness a function "
            "that does not take one)"
        )
    rng = ranges[0]
    if rng.templated:
        raise SignatureError(f"`{name}` is a class template; harness a concrete instantiation")

    if _CONDITIONAL_MEMBER_RE.search(scrubbed[rng.start : rng.end]):
        # A struct whose members depend on #ifdef has more than one layout and
        # nothing here knows which the build selected. This refuses the whole
        # type rather than recording per-member holes, because the branches of
        # an #if/#else routinely declare the *same* member with different
        # types: modelling "the rest" would merge two mutually exclusive
        # layouts. Left unhandled, the directive text was absorbed into the
        # field type -- nanopb's pb_ostream_t came back with a member typed
        # `#endif void`, which the harness set to null and then blamed the
        # library for dereferencing.
        raise SignatureError(
            f"`{name}` has preprocessor-conditional members, so which fields "
            "exist depends on build configuration and the layout cannot be "
            "modelled; harness a function that does not take one"
        )

    info = StructInfo(name=name, is_union=_is_union(scrubbed, rng))
    access = "public" if _is_struct(scrubbed, rng) or info.is_union else "private"

    for statement, start in _field_statements(scrubbed, rng):
        acc = _ACCESS_RE.match(statement.strip())
        if acc:
            access = acc.group(1)
            continue
        if _FIELD_SKIP.match(statement) or _NESTED_TYPE.match(statement):
            continue  # an alias, or a nested type definition
        if "(" in statement:
            continue  # a method or a function-pointer member
        raw = statement.strip()
        if not raw or raw.startswith("static"):
            continue
        try:
            info.fields.extend(_parse_fields(statement, access))
        except SignatureError as exc:
            info.unsupported[raw[:40]] = str(exc)
    return info


_ALIAS_RE_TEMPLATE = (
    r"\btypedef\s+(?:struct|union)\s+([A-Za-z_]\w*)\s+{name}\s*;"
)


def _struct_tag_for_alias(scrubbed: str, name: str) -> str | None:
    """The struct tag an alias refers to, for `typedef struct TAG ALIAS;`.

    Only a plain alias counts: `typedef struct TAG *ALIAS;` names a pointer,
    which is a different type and must not be silently unwrapped.
    """
    m = re.search(_ALIAS_RE_TEMPLATE.format(name=re.escape(name)), scrubbed)
    return m.group(1) if m else None


def _typedef_struct_range(scrubbed: str, name: str) -> "_ClassRange | None":
    """Range of `typedef struct { ... } Name;` -- the usual C idiom.

    The struct itself has no name there, so `find_class_ranges` (which keys off
    `struct Name {`) never sees it. Most C libraries declare every type this
    way, so without this the generator refuses their entire API.
    """
    for m in re.finditer(rf"\}}\s*{re.escape(name)}\s*;", scrubbed):
        close = scrubbed.index("}", m.start())
        open_brace = _matching_open(scrubbed, close)
        if open_brace is None:
            continue
        head = scrubbed[:open_brace].rstrip()
        # Both spellings define the type here:
        #     typedef struct { ... } Name;              (anonymous)
        #     typedef struct name_s { ... } Name;       (tagged)
        # The tagged form is the commoner one in C headers, and matching only
        # the anonymous one refused every function taking such a type.
        if re.search(r"\b(struct|union)\s*(?:[A-Za-z_]\w*\s*)?$", head):
            return _ClassRange(name=name, start=open_brace, end=close)
    return None


def _matching_open(scrubbed: str, close: int) -> int | None:
    depth = 0
    for i in range(close, -1, -1):
        if scrubbed[i] == "}":
            depth += 1
        elif scrubbed[i] == "{":
            depth -= 1
            if depth == 0:
                return i
    return None


def _is_union(scrubbed: str, rng: "_ClassRange") -> bool:
    head = scrubbed[max(0, rng.start - 400) : rng.start]
    return bool(re.search(r"\bunion\b[^;{}]*$", head))


def _field_statements(scrubbed: str, rng: "_ClassRange"):
    """Yield (text, offset) for each `;`-terminated statement at body depth 0."""
    i = rng.start + 1
    depth = 0
    start = i
    while i < rng.end:
        ch = scrubbed[i]
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
        elif depth == 0 and ch == ";":
            yield scrubbed[start:i], start
            start = i + 1
        elif depth == 0 and ch == ":" and scrubbed[i - 1 : i] != ":" and scrubbed[i + 1 : i + 2] != ":":
            yield scrubbed[start : i + 1], start
            start = i + 1
        i += 1


_CONDITIONAL_MEMBER_RE = re.compile(r"^[ \t]*#\s*(?:if|ifdef|ifndef|else|elif|endif)\b", re.M)


def _parse_fields(text: str, access: str) -> list[Field]:
    """`int a, b[4];` -> two Fields. Declarators share the leading type."""
    text = re.sub(r"=\s*[^,]+", "", text)  # drop default member initialisers
    declarators = split_top_level(text)
    if not declarators:
        return []
    first = declarators[0].strip()
    if _BITFIELD_RE.search(first):
        raise SignatureError("bitfields are not supported")

    m = re.search(r"([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*$", first)
    if m is None:
        raise SignatureError(f"could not read a field name from {first!r}")
    base_type = first[: m.start()].strip()
    if not base_type:
        raise SignatureError(f"could not read a field type from {first!r}")

    fields: list[Field] = []
    for idx, decl in enumerate(declarators):
        decl = decl.strip()
        array = _ARRAY_FIELD_RE.match(decl)
        extent = None
        if array:
            decl, extent = array.group(1).strip(), array.group(2).strip()
        if idx == 0:
            name = decl[len(base_type) :].strip()
            type_ = base_type
        else:  # subsequent declarators reuse the base type; `*p` adds a star
            name = decl.lstrip("*& ")
            type_ = base_type + ("*" if decl.lstrip().startswith("*") else "")
        # `int *p` puts the star on the declarator, not the type
        while name.startswith(("*", "&")):
            type_ += name[0]
            name = name[1:].strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise SignatureError(f"could not read a field name from {decl!r}")
        fields.append(Field(
            type=" ".join(type_.split()),
            name=" ".join(name.split()),
            array_len=extent,
            access=access,
        ))
    return fields


_ENUM_RE = re.compile(
    r"\benum\s+(?:class\s+|struct\s+)?([A-Za-z_]\w*)?[^;{]*\{[^}]*\}\s*([A-Za-z_]\w*)?\s*;"
)


def collect_enum_types(source: str) -> set[str]:
    """Names of enumerations declared in `source`.

    An enum is an integer as far as a harness is concerned, so recognising one
    is the difference between filling a field and leaving a hole in the object.
    """
    scrubbed = scrub(source)
    names: set[str] = set()
    for m in _ENUM_RE.finditer(scrubbed):
        names.update(n for n in (m.group(1), m.group(2)) if n)
    for m in re.finditer(r"\benum\s+([A-Za-z_]\w*)\s*;", scrubbed):
        names.add(m.group(1))
    return names


# ----------------------------------------------------- unresolved callees ---

_DECLARED_RE_TEMPLATE = r"\b{name}\s*\([^;{{}}]*\)\s*(?:const\s*)?;"


#: `extern const struct protent* const protocols[];` -- an array declared
#: here and defined in another translation unit.
_EXTERN_ARRAY_RE = re.compile(
    r"^[ \t]*extern\b[^;=()]*?\b(\w+)\s*\[\s*\]\s*;", re.M
)


def unresolved_extern_arrays(source: str, body: str) -> list[str]:
    """Arrays `body` indexes that are declared here and defined elsewhere.

    Without the definition the checker has no size and no contents, so every
    index into one is out of bounds and every scan for a terminator runs
    forever. lwIP's `protocols[]` is declared in ppp_impl.h, defined in
    ppp.c, and ends in a NULL that stops the loops that walk it -- with
    ppp.c unlinked, lcp_rprotrej and lcp_extcode both reported an
    out-of-bounds read on a table they only ever walk to that NULL.

    veripp discloses unresolved CALLEES and always has. This is the same
    hole in the same wall: nothing about the data.
    """
    scrubbed_body = scrub(body)
    used = set(re.findall(r"\b([A-Za-z_]\w*)\s*\[", scrubbed_body))
    if not used:
        return []
    scrubbed_source = scrub(source)
    unresolved: list[str] = []
    for name in sorted(used):
        if not re.search(
            _EXTERN_ARRAY_RE.pattern.replace(r"(\w+)", re.escape(name)),
            scrubbed_source, re.M,
        ):
            continue
        # A definition is the same name followed by `[` and then either a
        # size or an initialiser, without `extern`.
        defined = re.search(
            r"^(?![ \t]*extern\b)[ \t]*[A-Za-z_][^;=]*?\b"
            + re.escape(name) + r"\s*\[[^\]]*\]\s*=",
            scrubbed_source, re.M,
        )
        if defined is None:
            unresolved.append(name)
    return unresolved


def unresolved_callees(source: str, body: str) -> list[str]:
    """Functions `body` calls that are declared here but defined elsewhere.

    ESBMC havocs such a call's return value but assumes it does not write
    through its pointer arguments, so an unresolved callee can turn a real
    proof into a false one -- or, as often, invent a counterexample. ESBMC
    reports these itself for C but not for C++, so veripp works them out
    rather than trusting the warning.
    """
    scrubbed_body = scrub(body)
    called: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", scrubbed_body):
        name = m.group(1)
        if name in seen or name in _NOT_A_CALL:
            continue
        seen.add(name)
        called.append(name)

    scrubbed_source = scrub(source)
    macros = set(re.findall(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)", source, re.M))

    unresolved: list[str] = []
    for name in called:
        if name in macros:
            continue  # a macro call site is not a function call
        try:
            find_function(source, name)
        except SignatureError as exc:
            # Only count it when the name really is a function here: declared
            # with no definition. A declaration ends in `;` and is not itself
            # preceded by a value context, which is what separates
            #     void normalize(Box *b);        <- declaration
            # from
            #     x = normalize(b);              <- a call
            if "no definition" not in str(exc):
                continue
            for m in re.finditer(
                _DECLARED_RE_TEMPLATE.format(name=re.escape(name)), scrubbed_source
            ):
                head = scrubbed_source[max(0, m.start() - 60) : m.start()].rstrip()
                if head and head[-1] in "=(,&|!+-*/<>?:{};" and not head.endswith(";"):
                    continue  # a call in an expression, not a declaration
                unresolved.append(name)
                break
    return unresolved


_NOT_A_CALL = {
    "if", "for", "while", "switch", "return", "sizeof", "catch", "throw",
    "new", "delete", "static_cast", "const_cast", "reinterpret_cast",
    "dynamic_cast", "decltype", "noexcept", "alignof", "typeid", "defined",
    "and", "or", "not", "assert",
}


# ---------------------------------------------------- enumerating targets ---

_TRAILING_WORDS = "|".join(sorted(_TRAILING_QUALS - {"&", "&&"}))


def _define_spans(source: str) -> list[tuple[int, int]]:
    """Character ranges covered by `#define` directives, continuations included.

    `scrub` blanks comments and string bodies but keeps offsets, so a span
    measured on the raw source lines up with the scrubbed copy.
    """
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"^[ \t]*#[ \t]*define\b", source, re.M):
        end = m.end()
        while True:
            newline = source.find("\n", end)
            if newline == -1:
                end = len(source)
                break
            end = newline + 1
            if not source[:newline].rstrip().endswith("\\"):
                break
        spans.append((m.start(), end))
    return spans


def function_definitions(source: str) -> list[str]:
    """Names of things that look like function definitions in `source`.

    A best-effort list of candidate targets for a whole-file scan. Each name is
    expected to go back through `find_function`, which is the part that refuses
    what it cannot model -- this only has to avoid missing real definitions.
    """
    scrubbed = scrub(source)
    # A function-like macro with a braced body reads exactly like a
    # definition. lwIP's vj.c defines five -- ENCODE, ENCODEZ, DECODEL,
    # DECODES, DECODEU -- and each was counted as a function and then
    # refused, which put the file's coverage at 30% when it is really 80%.
    # A wrong denominator is its own failure: it hides a surface by making
    # the tool look like it already tried.
    #
    # Excluding the NAME would be too much. mbedTLS defines
    # asn1_find_named_data as a real function in one branch of an #if and as
    # a macro in the other, and dropping the name lost the function. What is
    # not a definition is the macro BODY, so that is what gets skipped.
    macro_spans = _define_spans(source)
    names: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", scrubbed):
        name = m.group(1)
        if name in _NOT_A_CALL or name in _NOT_A_MEMBER:
            continue
        if any(start <= m.start() < end for start, end in macro_spans):
            continue
        lparen = scrubbed.index("(", m.end() - 1)
        try:
            rparen = match_bracket(scrubbed, lparen)
        except SignatureError:
            continue
        tail = scrubbed[rparen + 1 : rparen + 120]
        stripped = re.sub(rf"\b({_TRAILING_WORDS})\b|\s+", "", tail)
        if stripped[:1] != "{":
            continue  # a call or a declaration, not a definition
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


# ------------------------------------------------------------- includes ---

_QUOTED_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', re.M)
_ANGLE_INCLUDE_RE = re.compile(r"^[ \t]*#[ \t]*include[ \t]*<([^>]+)>", re.M)


def included_names(text: str, angle: bool = False) -> list[str]:
    """Header names `text` includes, in order, without duplicates.

    Deliberately reads the RAW text: `scrub` blanks string literals, which
    erases the filename in `#include "config.h"`. That mistake has been made
    twice in this codebase, which is why there is now one function for it.
    Following a commented-out include is harmless -- it only widens the set of
    headers considered.
    """
    names = list(_QUOTED_INCLUDE_RE.findall(text))
    if angle:
        names += _ANGLE_INCLUDE_RE.findall(text)
    return list(dict.fromkeys(names))
