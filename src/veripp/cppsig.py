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


_CLASS_RE = re.compile(r"\b(class|struct)\s+([A-Za-z_]\w*)\s*(?::[^;{]*)?\{")


def find_class_ranges(scrubbed: str) -> list[_ClassRange]:
    ranges = []
    for m in _CLASS_RE.finditer(scrubbed):
        brace = scrubbed.index("{", m.end() - 1)
        try:
            ranges.append(_ClassRange(m.group(2), brace, match_bracket(scrubbed, brace)))
        except SignatureError:
            continue
    return ranges


# ------------------------------------------------------------- signature ---

_DECL_STOP = ";{}:"
_LEADING_QUALS = {
    "static", "inline", "virtual", "explicit", "constexpr", "consteval",
    "friend", "extern", "template",
}
_TRAILING_QUALS = {"const", "noexcept", "override", "final", "volatile", "&", "&&"}


def find_function(source: str, name: str) -> Signature:
    """Recover the signature of the *definition* of `name` in `source`."""
    scrubbed = scrub(source)
    classes = find_class_ranges(scrubbed)

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
    if len(candidates) > 1:
        raise SignatureError(
            f"`{name}` is defined {len(candidates)} times (overloads are not "
            "supported yet); disambiguate by extracting the target into its own file"
        )

    name_start, lparen, rparen, quals, brace = candidates[0]

    head = _decl_head(scrubbed, name_start)
    # Read the return type off the scrubbed text: comments must not leak into it.
    return_type, is_static = _return_type(scrubbed[head:name_start], name)
    params = _parse_params(source[lparen + 1 : rparen])
    body_end = match_bracket(scrubbed, brace)
    class_name = _enclosing_class(classes, name_start)

    return Signature(
        name=name,
        return_type=return_type,
        params=params,
        class_name=class_name,
        is_static=is_static,
        is_const="const" in quals,
        body=source[brace + 1 : body_end],
    )


def _scan_trailing(scrubbed: str, pos: int) -> tuple[set[str], int | None]:
    """Read qualifiers after the parameter list. Returns (quals, brace offset)."""
    quals: set[str] = set()
    i = pos
    n = len(scrubbed)
    while i < n:
        ch = scrubbed[i]
        if ch.isspace():
            i += 1
        elif ch == "{":
            return quals, i
        elif ch == "(":  # noexcept(...) / attribute
            i = match_bracket(scrubbed, i) + 1
        elif scrubbed.startswith("->", i):  # trailing return type
            i += 2
        elif ch.isalnum() or ch in "_&:<>*,":
            word = re.match(r"[A-Za-z_]\w*", scrubbed[i:])
            if word:
                if word.group(0) in _TRAILING_QUALS:
                    quals.add(word.group(0))
                i += word.end()
            else:
                i += 1
        else:
            return quals, None  # ';' or anything else: not a definition
    return quals, None


def _decl_head(scrubbed: str, name_start: int) -> int:
    """Offset where the declaration containing `name_start` begins."""
    i = name_start - 1
    while i >= 0 and scrubbed[i] not in _DECL_STOP:
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
    ret = re.sub(rf"\b[A-Za-z_]\w*::\s*$", "", ret).strip()
    if not ret:
        raise SignatureError(
            f"could not determine the return type of `{name}`; constructors and "
            "operators are not supported yet"
        )
    return ret, is_static


def _enclosing_class(classes: list[_ClassRange], offset: int) -> str | None:
    inner = [c for c in classes if c.start < offset < c.end]
    return max(inner, key=lambda c: c.start).name if inner else None


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
