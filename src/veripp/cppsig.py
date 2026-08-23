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
    """Canonical spelling of a scalar type: no cv-qualifiers, no namespaces.

    `typedefs` maps project-local aliases (`mz_ulong`) to their underlying
    types; chains are resolved by `collect_scalar_typedefs`.
    """
    t = re.sub(r"\b(const|volatile|constexpr)\b", " ", type_)
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
_USING_RE = re.compile(r"\busing\s+([A-Za-z_]\w*)\s*=\s*([^;<>{}]+);")


def collect_scalar_typedefs(source: str) -> dict[str, str]:
    """Project-local aliases of scalar types: `typedef unsigned long mz_ulong;`.

    Only aliases that bottom out at a plain scalar are kept -- a typedef of a
    struct or a function pointer is not something a harness can nondet-fill,
    so resolving it would only produce a better-looking wrong answer.
    """
    scrubbed = scrub(source)
    raw: dict[str, str] = {}
    for m in _TYPEDEF_RE.finditer(scrubbed):
        raw[m.group(2)] = m.group(1).strip()
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


@dataclass
class Param:
    type: str
    name: str

    @property
    def is_pointer(self) -> bool:
        return self.type.rstrip().endswith("*")

    @property
    def is_reference(self) -> bool:
        return self.type.rstrip().endswith("&")

    @property
    def is_const(self) -> bool:
        return bool(re.match(r"^\s*const\b", self.type))

    def pointee(self) -> str:
        """Type pointed/referred to, with the outer `*`/`&` and `const` removed."""
        t = self.type.rstrip()
        if t.endswith("*") or t.endswith("&"):
            t = t[:-1].rstrip()
        return re.sub(r"^\s*const\b\s*", "", t).strip()


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


_CLASS_RE = re.compile(r"\b(class|struct)\s+([A-Za-z_]\w*)\s*(?::[^;{]*)?\{")


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

    head = _decl_head(scrubbed, name_start)
    enclosing = _enclosing_class_range(classes, name_start)
    qualifier = _qualifier(scrubbed[head:name_start], name)
    class_name = qualifier or (enclosing.name if enclosing else None)
    if qualifier is not None:
        enclosing = next((c for c in classes if c.name == qualifier), enclosing)
    _reject_unmodellable(scrubbed, head, name_start, name, class_name, enclosing)

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
    if _looks_like_an_initialiser_list(before):
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


def _decl_head(scrubbed: str, name_start: int) -> int:
    """Offset where the declaration containing `name_start` begins.

    A single `:` ends the previous thing (an access specifier, a label, the
    start of a constructor initialiser list), but the `:` of a `::` does not:
    stopping there truncated `std::size_t` to `size_t` and made every
    out-of-line definition -- `void C::Clear() {}` -- look like it had no
    return type at all, which refused the most common shape in real C++.
    """
    i = name_start - 1
    while i >= 0:
        ch = scrubbed[i]
        if ch == ":":
            if not (scrubbed[i - 1 : i] == ":" or scrubbed[i + 1 : i + 2] == ":"):
                break
        elif ch in ";{}":
            break
        i -= 1
    start = i + 1
    # A preprocessor directive also ends whatever came before it: without this,
    # a file whose first function follows an #include has the directive text
    # swept into its return type.
    for m in re.finditer(r"^[ \t]*#.*$", scrubbed[start:name_start], re.M):
        start += m.end()
    return start


def _return_type(head: str, name: str) -> tuple[str, bool]:
    tokens = head.replace("\n", " ").split()
    is_static = "static" in tokens
    kept = [t for t in tokens if t not in _LEADING_QUALS]
    if kept and kept[-1].endswith("::"):  # out-of-line definition: Class::name
        kept = kept[:-1]
    ret = " ".join(kept).strip()
    # `Class::name` written as one token leaves the qualifier glued to the type.
    ret = re.sub(r"\b[A-Za-z_]\w*::\s*$", "", ret).strip()
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
