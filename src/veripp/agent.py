"""The agent loop: attempt -> triage -> escalate, under a hard budget.

Design invariants:
  * The LLM never decides correctness. Every proposal (harness edit,
    invariant, assumption) is re-checked by ESBMC.
  * Every reported result carries the exact VerifyConfig it was obtained
    under, plus the harness assumptions, so "verified" always means
    "verified under these bounds and assumptions".
  * The loop terminates: bounded iterations, wall time, and LLM calls.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import term
from .esbmc import Outcome, VerifyConfig, VerifyResult, run
from .harness import (
    REACHABILITY_MESSAGE,
    REACHABILITY_PROBE_MACRO,
    HarnessError,
    generate,
    has_reachability_marker,
    reachability_variant,
)
from .llm import LLMClient, LLMError, NullLLM
from .triage import (
    Diagnosis, TargetInfo, real_failures, triage_counterexample,
)


@dataclass
class Budget:
    max_attempts: int = 8
    max_llm_calls: int = 12
    max_precondition_rounds: int = 2  # LLM-proposed preconditions per run
    wall_time_s: int = 600


#: What the reachability probe can say about a result that verified.
REACHABLE = "reachable"      # the harness runs to its end: something was checked
VACUOUS = "vacuous"          # it cannot: every property held trivially
UNCONFIRMED = "unconfirmed"  # the probe did not settle, so neither is shown


@dataclass
class AgentReport:
    final: VerifyResult
    attempts: list[VerifyResult] = field(default_factory=list)
    diagnosis: Diagnosis | None = None
    narrative: str = ""
    assumptions: list[str] = field(default_factory=list)
    harness: Path | None = None
    accepted_preconditions: list[str] = field(default_factory=list)
    #: Bug classes the checker that produced this result is known to miss.
    #: A "verified" is only as sound as the checker behind it.
    unsound_probes: list[str] = field(default_factory=list)
    #: The reachability probe's answer for a result that verified (None when
    #: it did not). Only REACHABLE makes a "verified" a proof: VACUOUS means
    #: the assumptions left no execution to check, and UNCONFIRMED means the
    #: probe could not tell -- which fails closed rather than open.
    reachability: str | None = None
    #: Why the probe did not settle, when it is UNCONFIRMED.
    reachability_note: str = ""

    #: Termination, kept separate from the safety verdict on purpose. It is a
    #: liveness property, and a safety proof says nothing about it: ESBMC
    #: reports SUCCESSFUL under k-induction for a function that loops forever,
    #: because an infinite loop violates no assertion. Folding the two would
    #: let "verified" mean "terminates" to a reader, which it does not.
    #: None -> not asked (no loop, or the safety check did not succeed).
    terminates: bool | None = None

    @property
    def vacuous(self) -> bool:
        """The harness could not be reached under its own assumptions, which
        makes any "verified" meaningless."""
        return self.reachability == VACUOUS

    @property
    def unconfirmed(self) -> bool:
        return (
            self.final.outcome is Outcome.VERIFIED
            and self.reachability != REACHABLE
            and not self.vacuous
        )

    @property
    def verified(self) -> bool:
        return (
            self.final.outcome is Outcome.VERIFIED
            and self.reachability == REACHABLE
        )

    def _depth_bound_hint(self) -> str | None:
        """Flag a null the harness itself introduced.

        Pointer fields are cut to null at --max-struct-depth, so a NULL
        dereference may be that cut rather than a missing check in the code.
        It is not safe to call it an artifact -- an unchecked pointer is a
        real bug class -- but the reader should know which nulls are ours.
        """
        prop = self.final.violated_property
        if prop is None or "NULL pointer" not in prop.description:
            return None
        nulled = [a for a in self.assumptions if "is null" in a]
        if not nulled:
            return None
        deeper = any("depth bound" in a for a in nulled)
        advice = (
            "re-run with a larger --max-struct-depth to tell the two apart"
            if deeper
            else "a caller would have set it; constrain it with --assume, or "
            "target a function that does not take it"
        )
        return (
            "  NOTE: the harness left a pointer field null "
            f"({nulled[0].split('`')[1] if '`' in nulled[0] else 'see assumptions'}"
            f"), so this null may be the harness's rather than something a "
            f"caller can produce. {advice.capitalize()}."
        )

    def summary(self) -> str:
        if self.vacuous:
            headline = term.style(
                "VACUOUS (nothing was actually checked)", "yellow", "bold"
            )
        elif self.unconfirmed:
            headline = term.style(
                "UNCONFIRMED (no violation found, but not shown to have "
                "checked anything)", "yellow", "bold",
            )
        else:
            headline = term.verdict(self.final.outcome.value)
        lines = [f"Result: {headline}", f"  {self.final.config.describe()}"]
        if self.vacuous:
            lines.append(
                "  The assumptions made the call unreachable, so every property "
                "held trivially. This is NOT a proof. The conflict may be "
                "between the precondition(s) below, or with a bound veripp's "
                "harness adds itself (a buffer length of at most "
                "--max-array-len, say). Weaken the precondition, or raise that "
                "bound, until the harness can run."
            )
        elif self.unconfirmed:
            lines.append(
                "  The solver found no violation, but the reachability probe "
                "could not show that the harness runs at all"
                + (f" ({self.reachability_note})" if self.reachability_note else "")
                + ", and an unreachable harness satisfies every property. This "
                "is NOT a proof. A longer --timeout usually settles it."
            )
        if self.harness:
            lines.append(f"  harness: {self.harness}")
        if self.assumptions:
            lines.append("Assumptions (a result is only as good as these):")
            lines += [f"  - {a}" for a in self.assumptions]
        if self.final.outcome is Outcome.VERIFIED and not self.final.config.k_induction:
            lines.append(
                "  This is a BOUNDED proof: it holds for executions within the "
                "unwind bound above, not for all executions."
            )
        # Termination gets its own line and its own words. "Verified" above
        # covers safety only; a reader should never have to know that to read
        # this report correctly.
        if self.terminates is True:
            lines.append("  Termination: proved -- this function always finishes.")
        elif self.terminates is False:
            lines.append(
                "  Termination: NOT PROVED. That is not the same as "
                "'loops forever' -- ESBMC proves termination but cannot refute "
                "it, so this is an open question, not a bug."
            )
        stubbed = self.final.stubbed_calls
        if stubbed:
            names = ", ".join(stubbed[:8]) + ("..." if len(stubbed) > 8 else "")
            if self.verified:
                lines.append(
                    f"  STUBBED CALLS (no body was available): {names}. ESBMC "
                    "havocs their return values but assumes they do not write "
                    "through pointer arguments -- if any of them does, this "
                    "result does not account for it."
                )
            else:
                lines.append(
                    f"  STUBBED CALLS (no body was available): {names}. Their "
                    "effects were not modelled, so this counterexample may be "
                    "an artifact of the missing definition rather than a real "
                    "bug -- check it first."
                )
            lines.append(
                "  Link the defining source with --link, or point veripp at "
                "compile_commands.json."
            )
        if self.verified and self.unsound_probes:
            lines.append(
                "  CHECKER IS KNOWN-UNSOUND for: "
                + ", ".join(self.unsound_probes)
                + ". This 'verified' does NOT cover that class of bug; "
                "upgrade esbmc and re-run (see `veripp doctor`)."
            )
        if self.verified and self.accepted_preconditions:
            lines.append(
                "  CONDITIONAL: verified only under triage-proposed "
                "precondition(s) the solver confirmed sufficient. Nothing "
                "checks that real callers satisfy them - review before trusting:"
            )
            lines += [f"    requires {p}" for p in self.accepted_preconditions]
        prop = self.final.violated_property
        if prop:
            lines.append(f"Violated property: {prop.description}")
            lines.append(f"  at {prop.loc}")
            if prop.expression:
                lines.append(f"  guard: {prop.expression}")
            if prop.cwes:
                lines.append(f"  CWE: {', '.join(prop.cwes)}")
            hint = self._depth_bound_hint()
            if hint:
                lines.append(hint)
            inputs = self.final.input_summary()
            if inputs:
                lines.append("Counterexample inputs:")
                lines += [f"  {line}" for line in inputs]
        if self.final.error:
            lines.append(f"Error: {self.final.error}")
        if self.diagnosis:
            lines.append(f"Diagnosis: {self.diagnosis.kind}: {self.diagnosis.explanation}")
        if self.narrative:
            lines.append(self.narrative)
        lines.append(f"Attempts: {len(self.attempts)}")
        return "\n".join(lines)


# Escalation ladder for "not conclusive yet": widen the bound, then try to
# escape boundedness entirely.
_UNWIND_ESCALATIONS = [
    lambda c: replace(c, unwind=c.unwind * 4),
    lambda c: replace(c, unwind=c.unwind * 4),
    lambda c: replace(c, k_induction=True),
]

# A timeout means the search was too expensive, so widening the bound is the
# wrong move: switch to incremental BMC, which reports shallow bugs early.
_TIMEOUT_ESCALATIONS = [
    lambda c: replace(c, incremental_bmc=True, k_induction=False),
]


_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _strip_comments(text: str) -> str:
    """Drop comments before scanning for loop keywords.

    Prose says "for" and "while" constantly ("loop for each element"), and a
    match there would buy an extra verification run for nothing.
    """
    return _COMMENT_RE.sub(" ", text)


#: A function with no loop and no recursion terminates trivially, and asking
#: the checker costs a whole extra verification run. Cheap syntactic test:
#: only ask when there is something that could fail to terminate.
_LOOP_RE = re.compile(r"\b(while|for|goto)\b")


def _might_not_terminate(target: "TargetInfo | None") -> bool:
    """Whether termination is worth asking about for this target.

    Scans the original translation unit, not the harness: the harness only
    `#include`s the source, so its own text has no loop in it even when the
    code under test loops. Scanning the whole TU over-approximates -- a loop
    in an unrelated function also triggers the question -- but a callee's loop
    is just as able to hang the target, and the only cost of guessing yes is
    one extra run. Guessing no would silently drop the question.
    """
    if target is None:
        return False
    try:
        text = target.source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_LOOP_RE.search(_strip_comments(text)))


def _check_termination(harness: Path, config: VerifyConfig) -> bool | None:
    """True if termination is proved, False if the checker could not, None if
    it could not be asked.

    ESBMC proves termination but does not refute it: a function that may loop
    forever comes back UNKNOWN, not FAILED. So False here means "not proved",
    never "proved not to terminate", and the reporting says so.
    """
    from dataclasses import replace as _replace

    try:
        result = run(harness, _replace(config, termination=True))
    except (OSError, RuntimeError):
        return None
    return result.outcome is Outcome.VERIFIED


def verify_with_agent(
    source: Path,
    base_config: VerifyConfig | None = None,
    llm: LLMClient | None = None,
    budget: Budget | None = None,
    assumptions: list[str] | None = None,
    harness: Path | None = None,
    target: TargetInfo | None = None,
) -> AgentReport:
    """Main entry point: drive ESBMC to a conclusive answer if possible.

    `target` (set when --function generated the harness) enables the
    propose->check loop: triage may propose a precondition, the harness is
    regenerated with it, and ESBMC re-runs. The solver, never the LLM,
    decides whether the proposal stands.
    """
    llm = llm or NullLLM()
    budget = budget or Budget()
    config = base_config or VerifyConfig()
    started = time.monotonic()
    context = dict(assumptions=list(assumptions or []), harness=harness)
    preconditions: list[str] = []
    last_diagnosis: Diagnosis | None = None

    attempts: list[VerifyResult] = []
    unwind_idx = 0
    timeout_idx = 0
    looked_past_artifact = False

    while True:
        if len(attempts) >= budget.max_attempts:
            return _inconclusive(attempts, "attempt budget exhausted", **context)
        if time.monotonic() - started > budget.wall_time_s:
            return _inconclusive(attempts, "wall-time budget exhausted", **context)

        result = run(source, config)
        attempts.append(result)

        if result.outcome is Outcome.VERIFIED:
            # Safety holds. Termination is a separate question, and the tool
            # asks it rather than making the user find a flag: only when there
            # is a loop to worry about, and only once safety succeeded, since
            # proving that buggy code terminates helps nobody.
            # Reachability first: termination of a harness that runs nothing
            # is not worth asking, or printing.
            reachability, note = probe_reachability(source, config)
            terminates = None
            if reachability == REACHABLE and _might_not_terminate(target):
                terminates = _check_termination(source, config)
            return AgentReport(
                final=result,
                attempts=attempts,
                diagnosis=last_diagnosis,
                accepted_preconditions=preconditions,
                reachability=reachability,
                reachability_note=note,
                terminates=terminates,
                **context,
            )

        if result.outcome is Outcome.COUNTEREXAMPLE:
            diagnosis = triage_counterexample(target, source, result, llm)
            last_diagnosis = diagnosis
            if diagnosis.kind == "harness_issue" and not looked_past_artifact:
                # ESBMC stops at the first violation, so an artifact means
                # nothing else in the function was checked at all. Ask again
                # for a verdict on every property: either something real is
                # behind it, or the artifact was the only thing wrong.
                looked_past_artifact = True
                wider = run(source, replace(config, multi_property=True))
                attempts.append(wider)
                if wider.outcome is Outcome.COUNTEREXAMPLE:
                    real = real_failures(wider, source)
                    if real:
                        result = replace(wider, properties=real)
                        diagnosis = triage_counterexample(
                            target, source, result, llm
                        )
                        last_diagnosis = diagnosis
                    else:
                        # Every failure was the harness's. Nothing in the
                        # code under test failed, which is worth saying --
                        # it is not the same as not having looked.
                        result = replace(
                            wider, outcome=Outcome.VERIFIED, properties=[]
                        )
                        attempts.append(result)
                        reachability, note = probe_reachability(source, config)
                        return AgentReport(
                            final=result,
                            attempts=attempts,
                            diagnosis=diagnosis,
                            accepted_preconditions=preconditions,
                            reachability=reachability,
                            reachability_note=note,
                            **context,
                        )
            if (
                diagnosis.kind in ("missing_assumption", "harness_issue")
                and diagnosis.proposed_precondition
                and target is not None
                and len(preconditions) < budget.max_precondition_rounds
            ):
                # Regenerate the harness with the proposal; the re-run is the
                # solver's verdict on it. Unwind may need widening once the
                # precondition admits longer loops, so reset the ladder.
                candidate = preconditions + [diagnosis.proposed_precondition]
                try:
                    regenerated = generate(
                        target.source,
                        target.function,
                        target.options,
                        extra_preconditions=candidate,
                    )
                except HarnessError:
                    # Proposal out of scope (guardrail refused it): report the
                    # counterexample as triaged, without the proposal.
                    return AgentReport(
                        final=result, attempts=attempts, diagnosis=diagnosis, **context
                    )
                preconditions = candidate
                source = regenerated.write(source.parent, tag=f"pre{len(preconditions)}")
                context["assumptions"] = list(regenerated.assumptions)
                context["harness"] = source
                unwind_idx = 0
                continue
            return AgentReport(
                final=result,
                attempts=attempts,
                diagnosis=diagnosis,
                accepted_preconditions=[],
                **context,
            )

        if result.outcome is Outcome.TOOL_ERROR:
            # Escalating cannot fix a broken invocation; surface it immediately.
            return _inconclusive(
                attempts, f"esbmc could not be run: {result.error}", **context
            )

        if result.outcome is Outcome.TIMEOUT:
            if timeout_idx < len(_TIMEOUT_ESCALATIONS):
                config = _TIMEOUT_ESCALATIONS[timeout_idx](config)
                timeout_idx += 1
                continue
            return _inconclusive(attempts, "esbmc timed out at every setting", **context)

        if result.outcome in (Outcome.UNWIND_LIMIT, Outcome.UNKNOWN):
            if unwind_idx < len(_UNWIND_ESCALATIONS):
                config = _UNWIND_ESCALATIONS[unwind_idx](config)
                unwind_idx += 1
                continue
            # Ladder exhausted: ask the LLM for loop invariants / lemmas.
            # An unreachable LLM means no proposal, not an aborted run.
            try:
                proposal = llm.propose_invariants(source, result)
            except LLMError:
                proposal = None
            if proposal is not None:
                source = proposal
                config = replace(config, k_induction=True)
                continue
            return _inconclusive(
                attempts, "escalation ladder and LLM proposals exhausted", **context
            )

        if result.outcome is Outcome.PARSE_ERROR:
            try:
                fixed = llm.propose_frontend_fix(source, result)
            except LLMError:
                fixed = None
            if fixed is not None:
                source = fixed
                continue
            return _inconclusive(
                attempts,
                f"ESBMC frontend rejected the input: {result.error or 'see raw output'}",
                **context,
            )


def _reached(result: VerifyResult) -> bool:
    """Whether the counterexample is the probe's own assertion failing."""
    return any(REACHABILITY_MESSAGE in p.description for p in result.properties)


def probe_reachability(harness: Path, config: VerifyConfig) -> tuple[str, str]:
    """Can the harness that just verified actually run? And if not, why not.

    An unreachable program satisfies everything, so a "verified" from one is
    worthless -- and neither ESBMC nor the LLM that proposed a precondition
    can notice. So every proof is re-run with an assertion at its end that
    always fails: a harness that can get there must fail it (REACHABLE), and
    one that verifies again reached nothing (VACUOUS).

    Anything else -- a timeout, a tool error, a counterexample for some other
    property -- settles nothing, and is UNCONFIRMED rather than taken as
    reachable: a proof not shown to have checked anything is not a proof.
    The run is asked even when the harness text holds no assumption, because
    the assumption that empties it can live anywhere -- a VERIPP_REQUIRES in
    the code under test, an __ESBMC_assume in a header, a bound veripp added.
    """
    try:
        code = harness.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return UNCONFIRMED, f"could not read {harness}: {exc}"
    probe_config = replace(
        config, defines=[*config.defines, REACHABILITY_PROBE_MACRO]
    )
    workdir: Path | None = None
    if has_reachability_marker(code):
        # A generated harness: the same file, with the marker switched on.
        target = harness
    else:
        # A file checked through its own main. The probe is written into a
        # scratch directory rather than beside it, so it never lands in the
        # user's tree; its directory goes first on the include path, where a
        # quoted include in the original would have looked first.
        variant = reachability_variant(code)
        if variant is None:
            return UNCONFIRMED, "there is no main() to probe"
        workdir = Path(tempfile.mkdtemp(prefix="veripp-probe-"))
        target = workdir / harness.name
        target.write_text(variant, encoding="utf-8")
        probe_config = replace(
            probe_config,
            include_dirs=[harness.resolve().parent, *probe_config.include_dirs],
        )
    try:
        result = run(target, probe_config)
        if result.outcome is Outcome.COUNTEREXAMPLE and not _reached(result):
            # Something else failed first. That happens when every failure
            # was the harness's own and the proof is what is left after
            # them: the checker stopped at an artifact before it got to the
            # probe. Ask about every property instead.
            result = run(target, replace(probe_config, multi_property=True))
    except (OSError, RuntimeError) as exc:
        return UNCONFIRMED, f"the probe could not run: {exc}"
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
    if result.outcome is Outcome.VERIFIED:
        return VACUOUS, ""
    if result.outcome is Outcome.COUNTEREXAMPLE and _reached(result):
        return REACHABLE, ""
    return UNCONFIRMED, (
        f"the probe ended in {result.outcome.value}"
        + (f": {result.error}" if result.error else "")
    )


def _inconclusive(
    attempts: list[VerifyResult],
    reason: str,
    assumptions: list[str],
    harness: Path | None,
) -> AgentReport:
    return AgentReport(
        final=attempts[-1],
        attempts=attempts,
        narrative=f"Inconclusive: {reason}. No claim is made about this code.",
        assumptions=assumptions,
        harness=harness,
    )
