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
    # Take the LAST directive in one pass: match offsets are relative to this
    # slice, so advancing `start` inside the loop would compound them and, with
    # two #includes, overshoot past the return type.
    region = scrubbed[start:name_start]
    last = None
    for m in re.finditer(r"^[ \t]*#.*$", region, re.M):
        last = m
    if last is not None:
        start += last.end()
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


# ------------------------------------------------------------- classes ----

_ACCESS_RE = re.compile(r"\b(public|private|protected)\s*:")


def find_class(source: str, name: str) -> ClassInfo:
    """The public method surface of `name`, for driving a call sequence.

    Only members reachable from outside are useful to a harness, so private
    and protected members are skipped, and so is anything `find_function`
    refuses (templates, operators, overloads it cannot disambiguate).
    """
    scrubbed = scrub(source)
    ranges = [c for c in find_class_ranges(scrubbed) if c.name == name]
    if not ranges:
        raise SignatureError(f"no definition of class or struct `{name}` in the file")
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

_FIELD_SKIP = re.compile(
    r"^\s*(typedef|using|friend|static_assert|template|enum|class|struct|union)\b"
)
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
        raise SignatureError(
            f"no definition of `{name}` is visible in this translation unit, "
            "so a harness cannot construct one (an opaque/forward-declared "
            "type: include the header that defines it, or harness a function "
            "that does not take one)"
        )
    rng = ranges[0]
    if rng.templated:
        raise SignatureError(f"`{name}` is a class template; harness a concrete instantiation")

    info = StructInfo(name=name, is_union=_is_union(scrubbed, rng))
    access = "public" if _is_struct(scrubbed, rng) or info.is_union else "private"

    for statement, start in _field_statements(scrubbed, rng):
        acc = _ACCESS_RE.match(statement.strip())
        if acc:
            access = acc.group(1)
            continue
        if _FIELD_SKIP.match(statement) or "(" in statement:
            continue  # a method, a nested type, an alias
        raw = statement.strip()
        if not raw or raw.startswith("static"):
            continue
        try:
            info.fields.extend(_parse_fields(statement, access))
        except SignatureError as exc:
            info.unsupported[raw[:40]] = str(exc)
    return info


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
        if re.search(r"\b(struct|union)\s*$", head):
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
    head = scrubbed[max(0, rng.start - 200) : rng.start]
    return bool(re.search(r"\bunion\s+\w*\s*$", head))


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


def function_definitions(source: str) -> list[str]:
    """Names of things that look like function definitions in `source`.

    A best-effort list of candidate targets for a whole-file scan. Each name is
    expected to go back through `find_function`, which is the part that refuses
    what it cannot model -- this only has to avoid missing real definitions.
    """
    scrubbed = scrub(source)
    names: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", scrubbed):
        name = m.group(1)
        if name in _NOT_A_CALL or name in _NOT_A_MEMBER:
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
