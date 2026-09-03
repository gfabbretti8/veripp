"""Turning a counterexample into a program you can compile and run.

A trace is evidence, but it is evidence in the checker's language: to act on
it a developer still has to believe the harness modelled their function
fairly. A file that compiles, runs and crashes needs no such belief -- and it
checks itself, because a repro that exits cleanly under the sanitizers is the
signature of a lead that was an artifact of the harness rather than a bug in
the code.

The generated file is the harness with the nondeterminism removed: same
includes, same declarations, the counterexample's own values, same call.
Nothing is invented here, which is the point -- it must fail for the reason
the checker said it would, or say nothing at all.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_NONDET_INIT_RE = re.compile(r"\s*=\s*VERIPP_NONDET_\w*\s*\([^)]*\)\s*;")
_ASSUME_RE = re.compile(r"^\s*VERIPP_ASSUME\s*\(")
_NONDET_ANY_RE = re.compile(r"VERIPP_NONDET_\w*\s*\(")


def _body_lines(harness_code: str) -> list[str]:
    """The statements inside the harness's main(), in order."""
    start = harness_code.find("int main(")
    if start == -1:
        return []
    brace = harness_code.find("{", start)
    depth, end = 0, len(harness_code)
    for index in range(brace, len(harness_code)):
        if harness_code[index] == "{":
            depth += 1
        elif harness_code[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    return harness_code[brace + 1 : end].splitlines()


def _strip_nondeterminism(lines: list[str]) -> list[str]:
    """Keep the declarations, drop everything that only ESBMC can execute."""
    kept: list[str] = []
    skip_next = False
    for line in lines:
        if skip_next:
            # The second half of a `for (...) buf[i] = VERIPP_NONDET_*();`
            # fill, whose values the counterexample supplies element by
            # element instead.
            skip_next = False
            continue
        if _ASSUME_RE.match(line):
            continue
        if line.lstrip().startswith("for (") and "VERIPP_NONDET" not in line:
            skip_next = True
            continue
        if _NONDET_INIT_RE.search(line):
            kept.append(_NONDET_INIT_RE.sub(";", line))
            continue
        if _NONDET_ANY_RE.search(line):
            continue
        kept.append(line)
    return kept


def _concrete_assignments(assignments) -> tuple[list[str], list[str]]:
    """Counterexample values as C statements, plus the ones left as comments.

    An aggregate (`a_buf = { 0, 0, 0, 0 }`) is the variable's initial state,
    not a legal assignment statement, and ESBMC prints the element writes
    that supersede it. Recording it as a comment keeps the trace honest
    without emitting something that will not compile.
    """
    statements: list[str] = []
    notes: list[str] = []
    for item in assignments:
        value = item.value.strip()
        if not value or value.startswith("{"):
            notes.append(f"{item.lvalue} = {value}")
            continue
        statements.append(f"    {item.lvalue} = {value};")
    return statements, notes


def render(
    harness_code: str,
    source: Path,
    function: str,
    assignments,
    violated_property: str = "",
    repro_path: Path | None = None,
    include_dirs=(),
) -> str:
    """A standalone C/C++ file that reproduces one counterexample."""
    body = _strip_nondeterminism(_body_lines(harness_code))

    call_at = next(
        (i for i, line in enumerate(body) if re.search(rf"\b{re.escape(function)}\s*\(", line)),
        len(body),
    )
    statements, notes = _concrete_assignments(assignments)
    if statements:
        statements = ["", "    // counterexample inputs"] + statements
    body = body[:call_at] + statements + [""] + body[call_at:]

    header = [
        "// Reproduction of a veripp counterexample -- generated, safe to edit.",
        f"//   function: {function}",
        f"//   source:   {source}",
    ]
    if violated_property:
        # The property spans several lines (location, guard, CWEs). Every one
        # of them has to carry its own `//` or the file will not compile.
        first, *rest = violated_property.strip().splitlines()
        header.append(f"//   property: {first}")
        header += [f"//             {line.strip()}" for line in rest]
    header += [
        "//",
        "// Build and run it with the sanitizers on:",
        "//",
        f"//   {build_command(repro_path or Path('veripp_repro' + source.suffix), include_dirs=include_dirs)}",
        "//   ./veripp_repro",
        "//",
        "// If it crashes, the counterexample is reachable with these inputs.",
        "// If it exits cleanly, that is informative too: the failure needed a",
        "// state the harness allowed and this concrete run did not reach --",
        "// most often an input no real caller can construct.",
    ]
    if notes:
        header += ["//", "// Initial aggregate state reported by the checker:"]
        header += [f"//   {note}" for note in notes]

    return "\n".join([
        *header,
        "",
        f'#include "{source}"',
        "",
        "int main(void) {",
        *body,
        "}",
        "",
    ])


def build_command(
    repro: Path, out: str = "veripp_repro", include_dirs=()
) -> str:
    """A compile line that turns undefined behaviour into a visible crash.

    Carries the same include paths the verification used, `veripp/contracts.hpp`
    among them: a build line that does not compile is worse than none, because
    the reader concludes the repro is broken rather than their include path.
    """
    compiler = "c++" if repro.suffix in (".cpp", ".cc", ".cxx") else "cc"
    includes = " ".join(f"-I {shlex.quote(str(d))}" for d in include_dirs if d)
    return (
        f"{compiler} -g -fsanitize=address,undefined -fno-omit-frame-pointer "
        + (f"{includes} " if includes else "")
        + f"{shlex.quote(str(repro))} -o {out}"
    )
