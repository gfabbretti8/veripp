"""Checking a counterexample in an internal function through its callers.

A harness calls a function with anything its signature admits. For an
entry point that is the right question. For a `static` helper it is not:
the only inputs it ever sees are the ones its callers pass, and a caller
that checks an index before handing it on has made the helper's
out-of-bounds read unreachable. Six of the false counterexamples in this
project's field report were exactly that, and nothing in a signature can
tell them from real ones.

The checker can. A caller's harness calls the real caller, which calls the
real helper, so the question "does the property on this line still fail
when the helper is reached through here" is one more run -- asked with
every unrelated check off and a verdict on every property, so that what the
caller trips over on the way does not hide the line being asked about.
A static caller that reaches it is asked the same of its own callers, until
an externally visible function is reached or none is left.

Three answers, and only the first two are verdicts:

- REACHED: an entry point gets there, with the inputs to *that* function.
- UNREACHED: every caller was checked, each one's harness was shown to get
  into the helper, and none makes the line fail. Bounded like everything
  else here: within the callers' own harness bounds.
- UNSETTLED: anything else. A caller that timed out, could not be
  harnessed, or whose harness never gets to the call; a call from a macro;
  the helper's address taken. Never taken as unreached.

LLM-free, like the checker runner: the callers are found in the text and
the verdicts are the solver's.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .cppsig import SignatureError, _define_spans, definition_spans, scrub
from .esbmc import (
    Outcome, VerifyConfig, VerifyResult, ViolatedProperty, parse_output, run,
)
from .harness import HarnessError, HarnessOptions, generate
from .triage import _is_declaration

REACHED = "reached"
UNREACHED = "unreached"
UNSETTLED = "unsettled"

#: How far up a chain of static callers to climb before giving up.
MAX_DEPTH = 4

#: How much longer than the function's own timeout a caller's run may take.
CALLER_TIMEOUT_FACTOR = 4

ENTERED_MESSAGE = "veripp: the function under question was entered"


@dataclass
class CallSites:
    """Who calls a function, as far as the text of its unit shows."""

    callers: list[str] = field(default_factory=list)
    #: Calls that sit in no function body: a macro's expansion text, an
    #: initialiser. Whoever uses that macro is a caller this cannot name.
    outside: int = 0
    #: The name is used without being called, so it may be reached through
    #: a pointer from anywhere.
    address_taken: bool = False


def call_sites(text: str, name: str, scrubbed: str | None = None) -> CallSites:
    """Functions in `text` whose bodies call `name`, by text.

    The same reading `scan` uses to count callers: a call through a pointer
    or a macro defined elsewhere is not seen. That is why `outside` and
    `address_taken` are reported -- they are the two ways the list can be
    known to be incomplete, and an incomplete list cannot clear a finding.
    """
    scrubbed = scrub(text) if scrubbed is None else scrubbed
    spans = definition_spans(text)
    macros = _define_spans(text)
    sites = CallSites()
    for m in re.finditer(rf"\b{re.escape(name)}\b", scrubbed):
        after = scrubbed[m.end():m.end() + 40].lstrip()
        if not after.startswith("("):
            before = scrubbed[:m.start()].rstrip()
            # `.name` / `->name` is a field that shares the spelling.
            if not before.endswith((".", "->")):
                sites.address_taken = True
            continue
        if _is_declaration(scrubbed, m.start()):
            continue
        owner = next((n for n, start, end in spans if start < m.start() < end),
                     None)
        if owner is None:
            # At file scope a name before a parenthesis is the definition
            # itself or a prototype -- `static const T *f(int n)` has
            # punctuation before the name and reads like a use. What is
            # left is a macro's body and an initialiser.
            in_macro = any(start <= m.start() < end for start, end in macros)
            statement = max(scrubbed.rfind(c, 0, m.start()) for c in ";{}")
            if in_macro or "=" in scrubbed[statement + 1:m.start()]:
                sites.outside += 1
        elif owner != name and owner not in sites.callers:
            sites.callers.append(owner)
    return sites


@dataclass
class CallerCheck:
    """What one caller's harness said about the line."""

    caller: str
    #: "reaches", "clear", "not_called" or "unsettled".
    status: str
    static: bool = False
    detail: str = ""
    inputs: list[str] = field(default_factory=list)
    signature: str = ""


@dataclass
class Escalation:
    verdict: str
    note: str
    #: For REACHED: the externally visible function, then each static
    #: function between it and the one the counterexample was found in.
    chain: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    checks: list[CallerCheck] = field(default_factory=list)

    @property
    def entry(self) -> str:
        return self.chain[0] if self.chain else ""


def _same_place(a, b) -> bool:
    return a.line == b.line and (
        a.file == b.file or Path(a.file).name == Path(b.file).name
    )


def _narrowed(config: VerifyConfig, description: str) -> VerifyConfig:
    """`config` with only the checks that can report `description`.

    The checks are assertions, not assumptions, so turning the others off
    cannot change whether this one fails. It only stops the caller's run
    from deciding every overflow in the caller on the way to an answer
    about one array index in the callee.
    """
    text = description.lower()
    on: dict[str, bool]
    off = dict(overflow_check=False, unsigned_overflow_check=False,
               bounds_check=False, pointer_check=False,
               div_by_zero_check=False, memory_leak_check=False,
               uninitialised_check=False, ub_shift_check=False,
               nan_check=False)
    if "nan on" in text:
        on = dict(nan_check=True)
    elif "division by zero" in text:
        on = dict(div_by_zero_check=True)
    elif "shift" in text:
        on = dict(ub_shift_check=True, overflow_check=True)
    elif "arithmetic overflow" in text:
        on = dict(overflow_check=config.overflow_check,
                  unsigned_overflow_check=config.unsigned_overflow_check)
    elif "uninitiali" in text:
        on = dict(uninitialised_check=True)
    elif any(word in text for word in (
            "dereference failure", "array bounds", "pointer", "out of bounds",
            "invalid", "free", "null")):
        on = dict(bounds_check=True, pointer_check=True)
    else:
        return config          # not one this knows how to isolate
    flags: dict[str, Any] = {**off, **on}
    return replace(config, **flags)


def _inputs_for(result: VerifyResult, target) -> list[str]:
    """The harness inputs of the one counterexample that ends at `target`.

    Asked about every property, the checker prints a trace per failure, and
    read as one transcript their inputs run together.
    """
    for block in result.raw_output.split("[Counterexample]")[1:]:
        one = parse_output(block + "\nVERIFICATION FAILED\n", result.config)
        if any(_same_place(p.user_loc, target) or _same_place(p.loc, target)
               for p in one.properties):
            return one.input_summary()
    return []


class _Climb:
    def __init__(self, source: Path, name: str, prop: ViolatedProperty,
                 config: VerifyConfig, options: HarnessOptions, workdir: Path,
                 types, harness_paths: dict[str, Path], deadline: float | None,
                 progress):
        self.source = source
        self.name = name
        self.prop = prop
        self.target = prop.user_loc
        self.config = config
        self.options = options
        self.workdir = workdir
        self.types = types
        self.harness_paths = harness_paths
        self.deadline = deadline
        self.progress = progress
        self.text = source.read_text(encoding="utf-8", errors="replace")
        self.scrubbed = scrub(self.text)
        self.checks: dict[str, CallerCheck] = {}
        self._probe_source: Path | None = None

    # -------------------------------------------------------- one caller ---

    def _timeout(self) -> int:
        # A caller is a larger function than the one it calls, and it is
        # asked for a verdict on every property: the time that settled the
        # helper is not the time that settles this. Four times it, as the
        # retry pass gives a timeout, and never past the shared budget.
        limit = self.config.timeout_s * CALLER_TIMEOUT_FACTOR
        if self.deadline is None:
            return limit
        return int(min(limit, self.deadline - time.monotonic()))

    def check(self, caller: str) -> CallerCheck:
        if caller not in self.checks:
            self.checks[caller] = self._check(caller)
            if self.progress is not None:
                self.progress(self.name, self.checks[caller])
        return self.checks[caller]

    def _check(self, caller: str) -> CallerCheck:
        if self._timeout() < 2:
            return CallerCheck(caller, "unsettled", detail="out of time")
        try:
            harness = generate(self.source, caller, self.options,
                               types=self.types)
        except (HarnessError, SignatureError) as exc:
            return CallerCheck(caller, "unsettled",
                               detail=f"could not be harnessed ({exc})")
        sig = harness.signature
        static = sig.is_static
        printable = "{}({})".format(
            sig.qualified_name, ", ".join(f"{p.type} {p.name}" for p in sig.params))
        path = self.harness_paths.get(caller)
        if path is None or not path.exists():
            path = harness.write(self.workdir, tag=f"{caller}.via")
        asked = replace(_narrowed(self.config, self.prop.description),
                        multi_property=True, timeout_s=self._timeout())
        try:
            result = run(path, asked)
        except (OSError, RuntimeError) as exc:
            return CallerCheck(caller, "unsettled", static, str(exc))
        if any(_same_place(p.user_loc, self.target)
               or _same_place(p.loc, self.target) for p in result.properties):
            return CallerCheck(caller, "reaches", static,
                               inputs=_inputs_for(result, self.target),
                               signature=printable)
        if result.outcome not in (Outcome.VERIFIED, Outcome.COUNTEREXAMPLE):
            why = {Outcome.TIMEOUT: "timed out",
                   Outcome.UNWIND_LIMIT: "hit the unwind bound"}.get(
                       result.outcome, result.outcome.value)
            return CallerCheck(caller, "unsettled", static, why)
        # Nothing fails on the line. That clears the caller only if its
        # harness gets into the function at all: one that returns early on
        # every input it is given proves nothing about the call it skips.
        entered = self._enters(path, asked)
        if entered is None:
            return CallerCheck(caller, "unsettled", static,
                               "could not confirm its harness gets to the call")
        if not entered:
            return CallerCheck(
                caller, "not_called", static,
                "its harness never gets to the call within these bounds")
        return CallerCheck(caller, "clear", static, signature=printable)

    def _enters(self, harness: Path, config: VerifyConfig) -> bool | None:
        """Does this harness ever enter the function under question?

        A copy of the unit is given an assertion that always fails as the
        function's first statement, on the same line so nothing moves, and
        the caller's harness is pointed at the copy.
        """
        if self._probe_source is None:
            marks = sorted(
                (start for n, start, _ in definition_spans(self.text)
                 if n == self.name), reverse=True)
            if not marks:
                return None
            text = self.text
            for brace in marks:
                text = (text[:brace + 1]
                        + f' __ESBMC_assert(0, "{ENTERED_MESSAGE}");'
                        + text[brace + 1:])
            probe_dir = self.workdir / "entered"
            probe_dir.mkdir(parents=True, exist_ok=True)
            self._probe_source = probe_dir / self.source.name
            self._probe_source.write_text(text, encoding="utf-8")
        code = harness.read_text(encoding="utf-8")
        include = f'#include "{self.source.resolve()}"'
        if include not in code:
            include = f'#include "{self.source}"'
            if include not in code:
                return None
        variant = self._probe_source.parent / harness.name
        pointed = code.replace(include, f'#include "{self._probe_source}"')
        variant.write_text(pointed, encoding="utf-8")
        probe = replace(
            config,
            overflow_check=False, unsigned_overflow_check=False,
            bounds_check=False, pointer_check=False, div_by_zero_check=False,
            memory_leak_check=False, uninitialised_check=False,
            ub_shift_check=False, nan_check=False, multi_property=True,
            # A quoted include in the unit looked beside the unit first.
            include_dirs=[self.source.resolve().parent, *config.include_dirs],
            timeout_s=max(2, self._timeout()),
        )
        try:
            result = run(variant, probe)
        except (OSError, RuntimeError):
            return None
        if any(ENTERED_MESSAGE in p.description for p in result.properties):
            return True
        return False if result.outcome is Outcome.VERIFIED else None

    # ------------------------------------------------------------ climb ---

    def climb(self, fn: str, trail: tuple[str, ...]) -> Escalation:
        """Whether an entry point reaches the line through `fn`'s callers.

        `trail` is the static functions already on the way down to the
        line, nearest caller first.
        """
        sites = call_sites(self.text, fn, self.scrubbed)
        callers = [c for c in sites.callers if c not in trail and c != self.name]
        open_ends: list[str] = []
        if not callers:
            open_ends.append(f"no callers of `{fn}` found in this file")
        if len(trail) >= MAX_DEPTH:
            return Escalation(UNSETTLED, f"more than {MAX_DEPTH} static callers deep")
        cleared: list[str] = []
        for caller in callers:
            check = self.check(caller)
            if check.status == "reaches" and not check.static:
                chain = [caller, *trail]
                return Escalation(
                    REACHED,
                    "reached from " + " -> ".join([*chain, self.name]),
                    chain=chain, inputs=check.inputs)
            if check.status == "reaches":
                above = self.climb(caller, (caller, *trail))
                if above.verdict == REACHED:
                    return above
                if above.verdict == UNREACHED:
                    cleared.append(caller)
                else:
                    open_ends.append(
                        f"`{caller}` (static) reaches it, and its own "
                        f"callers are not settled: {above.note}")
            elif check.status == "clear":
                cleared.append(caller)
            else:
                open_ends.append(f"`{caller}`: {check.detail}")
        if sites.outside:
            open_ends.append(
                f"`{fn}` is also called from outside any function body "
                "(a macro, most likely)")
        if sites.address_taken:
            open_ends.append(
                f"`{fn}` is also used without being called, so it may be "
                "reached through a pointer")
        if open_ends:
            return Escalation(UNSETTLED, "; ".join(open_ends))
        return Escalation(
            UNREACHED,
            "not through " + ", ".join(f"`{c}`" for c in cleared))


def escalate(
    source: Path,
    name: str,
    prop: ViolatedProperty,
    config: VerifyConfig,
    options: HarnessOptions,
    workdir: Path,
    types=None,
    harness_paths: dict[str, Path] | None = None,
    budget_s: float | None = None,
    progress=None,
) -> Escalation | None:
    """Ask the callers of static function `name` whether `prop` fails
    through them. None when the question does not apply.

    It does not apply to a leak, which is reported where the program ends
    rather than on a line of the function, or to a failure located in the
    harness itself.
    """
    if prop.allocated_at is not None or "forgotten memory" in prop.description:
        return None
    if Path(prop.user_loc.file).name.startswith("veripp_harness_"):
        return None
    deadline = None if budget_s is None else time.monotonic() + budget_s
    climb = _Climb(source, name, prop, config, options, workdir, types,
                   harness_paths or {}, deadline, progress)
    result = climb.climb(name, ())
    result.checks = list(climb.checks.values())
    return result
