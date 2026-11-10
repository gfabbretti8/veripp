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

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_NONDET_INIT_RE = re.compile(r"\s*=\s*VERIPP_NONDET_\w*\s*\([^)]*\)\s*;")
_ASSUME_RE = re.compile(r"^\s*VERIPP_ASSUME\s*\(")
#: The reachability probe point: nothing outside ESBMC, and a repro includes
#: the source under test, which need not include contracts.hpp at all.
_REACHED_RE = re.compile(r"^\s*VERIPP_REACHED\s*\(\s*\)\s*;")
_NONDET_ANY_RE = re.compile(r"VERIPP_NONDET_\w*\s*\(")
_NONDET_CALL_RE = re.compile(r"VERIPP_NONDET_\w*\s*\(\s*\)")
#: `    int a_buf[4];` -- a harness buffer, filled by the loop that follows.
_BARE_ARRAY_RE = re.compile(r"^\s*[A-Za-z_][\w \t\*]*\b\w+\[\d+\];\s*$")
#: `{ 56, 56, 56, 56 }`: an array of plain numbers, nothing nested.
_FLAT_AGGREGATE_RE = re.compile(
    r"^\{\s*([-+]?[\w.]+(?:\s*,\s*[-+]?[\w.]+)*)\s*\}$")


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
        if _ASSUME_RE.match(line) or _REACHED_RE.match(line):
            continue
        if line.lstrip().startswith("for (") and "VERIPP_NONDET" not in line:
            skip_next = True
            continue
        if _NONDET_INIT_RE.search(line):
            # Zero, not nothing: a value the counterexample does not mention
            # was free to be anything, and a run that reads whatever the
            # stack held behaves differently every time it is built.
            kept.append(_NONDET_INIT_RE.sub(" = 0;", line))
            continue
        if _NONDET_ANY_RE.search(line):
            continue
        if _BARE_ARRAY_RE.match(line):
            kept.append(line.rstrip()[:-1] + " = {0};")
            continue
        kept.append(line)
    return kept


def _preamble(harness_code: str) -> list[str]:
    """What the harness defines before its main(): callback stand-ins and
    allocator hooks. main() names them, so a repro without them does not
    compile. A stand-in's nondeterministic return becomes 0."""
    guard = harness_code.find("#if !defined(__ESBMC__)")
    start = harness_code.find("#endif", guard) if guard != -1 else -1
    end = harness_code.find("int main(")
    if start == -1 or end == -1 or end < start:
        return []
    text = harness_code[start + len("#endif"): end].strip()
    if not text:
        return []
    return _NONDET_CALL_RE.sub("0", text).splitlines()


def _concrete_assignments(assignments, arrays=frozenset()) -> tuple[list[str], list[str]]:
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
            flat = _FLAT_AGGREGATE_RE.match(value)
            if flat and item.lvalue in arrays:
                # The contents ARE the input: with `p_buf = { 56, 56, 56,
                # 56 }` left as a comment, a parser that over-reads on four
                # digits was handed four zeroes and stopped at the first.
                statements += [
                    f"    {item.lvalue}[{index}] = {element.strip()};"
                    for index, element in enumerate(flat.group(1).split(","))
                ]
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
    arrays = frozenset(
        m.group(1) for line in body
        if (m := re.match(r"^\s*[A-Za-z_][\w \t\*]*\b(\w+)\[\d+\] = \{0\};", line))
    )
    statements, notes = _concrete_assignments(assignments, arrays)
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

    preamble = _preamble(harness_code)
    include = [f'#include "{source}"']
    if "#define main " in harness_code:
        # The source is a program with a main() of its own; the harness
        # compiled it under another name, and so must this.
        include = ["#define main veripp_original_main", *include, "#undef main"]
    return "\n".join([
        *header,
        "",
        *include,
        *(["", *preamble] if preamble else []),
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


# ------------------------------------------------------------- replay ----
#
# The repro above is something to build by hand. Building and running it is
# mechanical, and the answer is the most useful single fact about a
# counterexample: whether the compiled function, handed these inputs, really
# does what the checker said. It needs no model and it cannot be argued with.
#
# It is also the one thing veripp does that EXECUTES the code under test, so
# it happens only when asked for.

#: Properties the sanitizers have no check for. A clean run of one of these
#: says nothing, and must not be read as "the checker was wrong".
_NOT_SANITIZER_CHECKED = (
    ("NaN", "producing NaN is defined behaviour to a compiler"),
    ("Relational comparison", "comparing pointers into different objects "
                              "compiles to an ordinary comparison"),
    ("Same object", "subtracting pointers into different objects compiles "
                    "to an ordinary subtraction"),
    ("uninitiali", "AddressSanitizer does not track uninitialised reads"),
    ("use of uninit", "AddressSanitizer does not track uninitialised reads"),
    ("assertion", "an assertion veripp checks is compiled out of a normal "
                  "build"),
    ("postcondition", "a postcondition veripp checks is compiled out of a "
                      "normal build"),
)

_ASAN_RE = re.compile(r"ERROR: (AddressSanitizer|LeakSanitizer): ([^\n]+)")
_ASAN_SUMMARY_RE = re.compile(
    r"SUMMARY: \w+Sanitizer: (\S+) (\S+?):(\d+)(?::\d+)? in (\S+)")
_FRAME_RE = re.compile(r"^\s*#\d+ 0x[0-9a-f]+ in \S+ (\S+?):(\d+)", re.M)
_UBSAN_RE = re.compile(r"^(\S+?):(\d+):\d+: runtime error: ([^\n]+)", re.M)


@dataclass
class Replay:
    """What happened when the counterexample was compiled and run."""

    #: "crashed": a sanitizer reported an error, or the process died.
    #: "clean": it ran to the end with nothing reported.
    #: "unchecked": it ran clean, but for a property sanitizers cannot see.
    #: "timeout", "not_built", "no_compiler": no answer.
    status: str
    detail: str = ""
    file: str = ""
    line: int = 0
    #: Whether the harness stands in for anything a real run would do
    #: differently (a callback returning 0 where the checker chose a value).
    stand_ins: bool = False

    def summary(self, property_line: int = 0) -> list[str]:
        if self.status == "crashed":
            where = f" at {os.path.basename(self.file)}:{self.line}" if self.line else ""
            same = (" -- the line the checker named"
                    if self.line and self.line == property_line else "")
            return [
                f"Replayed natively: CONFIRMED. {self.detail}{where}{same}.",
                "  The compiled function fails on these inputs too, so this "
                "is not an artifact of the checker's model. Whether a real "
                "caller can pass them is still the question the assumptions "
                "above answer.",
            ]
        if self.status == "clean":
            lines = [
                "Replayed natively: ran clean under AddressSanitizer and "
                "UndefinedBehaviorSanitizer.",
                "  The checker's failure needed something this concrete run "
                "did not have -- most often a state the harness allows and "
                "real memory does not. Treat the counterexample as doubtful, "
                "not as refuted: a clean run is one execution.",
            ]
            if self.stand_ins:
                lines.append(
                    "  The harness's callback stand-ins returned 0 here; the "
                    "checker was free to choose their results, so a failure "
                    "that depends on one would not show.")
            return lines
        if self.status == "unchecked":
            return [
                "Replayed natively: ran to the end, which says nothing here: "
                f"{self.detail}."]
        if self.status == "timeout":
            return ["Replayed natively: no answer -- the program was still "
                    f"running after {self.detail}."]
        if self.status == "no_compiler":
            return ["Not replayed: no C or C++ compiler was found "
                    "(--replay builds the counterexample with cc or c++)."]
        return ["Not replayed: the counterexample did not compile as a "
                "program, so there is no native answer.",
                *(f"    {line}" for line in self.detail.splitlines()[:3])]

    def as_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail,
                "file": self.file, "line": self.line,
                "stand_ins": self.stand_ins}


def _compiler(cxx: bool) -> str | None:
    named = os.environ.get("CXX" if cxx else "CC")
    for candidate in ([named] if named else []) + (
            ["c++", "g++", "clang++"] if cxx else ["cc", "gcc", "clang"]):
        if shutil.which(candidate):
            return candidate
    return None


def replay(
    code: str,
    suffix: str,
    violated_property: str = "",
    include_dirs=(),
    defines=(),
    link_sources=(),
    std: str | None = None,
    timeout_s: int = 20,
) -> Replay:
    """Compile `code` (from `render`) with the sanitizers on, and run it.

    This runs the function under test, on this machine, with the
    counterexample's inputs. Everything else veripp does only reads code.
    """
    cxx = suffix in (".cpp", ".cc", ".cxx")
    compiler = _compiler(cxx)
    if compiler is None:
        return Replay("no_compiler")
    stand_ins = "veripp_stub_" in code
    with tempfile.TemporaryDirectory(prefix="veripp-replay-") as tmp:
        source = Path(tmp) / f"replay{suffix}"
        source.write_text(code, encoding="utf-8")
        binary = Path(tmp) / "replay"
        command = [
            compiler, "-g", "-O0", "-w", "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            *([f"-std={std}"] if std else []),
            *(f"-I{d}" for d in include_dirs if d),
            *(f"-D{d}" for d in defines),
            str(source), *(str(s) for s in link_sources), "-o", str(binary),
        ]
        try:
            built = subprocess.run(command, capture_output=True, text=True,
                                   timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return Replay("not_built", str(exc))
        if built.returncode != 0:
            errors = [line for line in built.stderr.splitlines()
                      if "error" in line] or built.stderr.splitlines()
            return Replay("not_built", "\n".join(errors[:3]))
        env = dict(os.environ,
                   ASAN_OPTIONS="detect_leaks=1:abort_on_error=0:color=never",
                   UBSAN_OPTIONS="print_stacktrace=0:color=never")
        try:
            ran = subprocess.run([str(binary)], capture_output=True, text=True,
                                 errors="replace", timeout=timeout_s, cwd=tmp,
                                 env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return Replay("timeout", f"{timeout_s} seconds", stand_ins=stand_ins)
        except OSError as exc:
            return Replay("not_built", str(exc))
    report = ran.stderr
    asan = _ASAN_RE.search(report)
    if asan:
        summary = _ASAN_SUMMARY_RE.search(report)
        kind = summary.group(1) if summary else asan.group(2).split()[0]
        detail = (f"{asan.group(1)}: {kind}" if asan.group(1) != "LeakSanitizer"
                  else "LeakSanitizer: detected memory leaks")
        # The innermost frame is often the sanitizer's own interceptor
        # (an over-long memcmp is caught inside memcmp). The first frame in
        # a file that exists here is the line in the code under test; the
        # replayed program itself is gone by now, so it never matches.
        frame = next(
            (m for m in _FRAME_RE.finditer(report) if os.path.isfile(m.group(1))),
            None,
        )
        file, line = (frame.group(1), int(frame.group(2))) if frame else (
            (summary.group(2), int(summary.group(3))) if summary else ("", 0))
        return Replay("crashed", detail, file=file, line=line,
                      stand_ins=stand_ins)
    ubsan = _UBSAN_RE.search(report)
    if ubsan:
        return Replay("crashed",
                      f"UndefinedBehaviorSanitizer: {ubsan.group(3).strip()}",
                      file=ubsan.group(1), line=int(ubsan.group(2)),
                      stand_ins=stand_ins)
    if ran.returncode < 0:
        return Replay("crashed", f"the process was killed by signal "
                                 f"{-ran.returncode}", stand_ins=stand_ins)
    for needle, why in _NOT_SANITIZER_CHECKED:
        if needle.lower() in violated_property.lower():
            return Replay("unchecked", why, stand_ins=stand_ins)
    return Replay("clean", stand_ins=stand_ins)


def replay_counterexample(
    harness_code: str, source: Path, function: str, assignments,
    violated_property: str = "", **build,
) -> Replay:
    """Render one counterexample as a program and replay it.

    The checker prints the assignments that bear on the property, which is
    not always the inputs: for a leak it names the allocation and nothing
    else. A harness with nondeterministic inputs and a trace with no values
    for them leaves nothing concrete to run -- every input would be zero,
    and a clean run of that would be reported as doubt about a finding it
    never exercised.
    """
    if "veripp_step" in harness_code or "veripp_choice" in harness_code:
        # A sequence harness picks its calls with nondeterministic choices
        # inside loops, and stripping them leaves no sequence at all.
        return Replay("not_built", "call-sequence harnesses (--class, "
                                   "--sequence) cannot be replayed yet")
    if not list(assignments) and _NONDET_ANY_RE.search(harness_code):
        return Replay("unchecked", "the checker's trace gives no values for "
                                   "the inputs, so there is nothing concrete "
                                   "to run")
    code = render(harness_code, source, function, assignments, violated_property)
    return replay(code, source.suffix, violated_property, **build)
