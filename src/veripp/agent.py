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
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import term
from .esbmc import Outcome, VerifyConfig, VerifyResult, run
from .harness import HarnessError, generate, reachability_variant
from .llm import LLMClient, LLMError, NullLLM
from .triage import Diagnosis, TargetInfo, triage_counterexample


@dataclass
class Budget:
    max_attempts: int = 8
    max_llm_calls: int = 12
    max_precondition_rounds: int = 2  # LLM-proposed preconditions per run
    wall_time_s: int = 600


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
    #: True when the harness could not actually be reached under its own
    #: assumptions, which makes any "verified" meaningless.
    vacuous: bool = False

    #: Termination, kept separate from the safety verdict on purpose. It is a
    #: liveness property, and a safety proof says nothing about it: ESBMC
    #: reports SUCCESSFUL under k-induction for a function that loops forever,
    #: because an infinite loop violates no assertion. Folding the two would
    #: let "verified" mean "terminates" to a reader, which it does not.
    #: None -> not asked (no loop, or the safety check did not succeed).
    terminates: bool | None = None

    @property
    def verified(self) -> bool:
        return self.final.outcome is Outcome.VERIFIED and not self.vacuous

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
        else:
            headline = term.verdict(self.final.outcome.value)
        lines = [f"Result: {headline}", f"  {self.final.config.describe()}"]
        if self.vacuous:
            lines.append(
                "  The assumptions made the call unreachable, so every property "
                "held trivially. This is NOT a proof. Weaken the precondition(s) "
                "below until the harness can run."
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
            terminates = None
            if _might_not_terminate(target):
                terminates = _check_termination(source, config)
            return AgentReport(
                final=result,
                attempts=attempts,
                diagnosis=last_diagnosis,
                accepted_preconditions=preconditions,
                vacuous=_is_vacuous(source, config),
                terminates=terminates,
                **context,
            )

        if result.outcome is Outcome.COUNTEREXAMPLE:
            diagnosis = triage_counterexample(target, source, result, llm)
            last_diagnosis = diagnosis
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


def _is_vacuous(harness: Path, config: VerifyConfig) -> bool:
    """Did the harness's own assumptions make the call unreachable?

    An unreachable program satisfies everything, so a "verified" from one is
    worthless -- and neither ESBMC nor the LLM that proposed the precondition
    can notice. Only a harness carrying assumptions can be vacuous, so the
    extra run is skipped when there are none.
    """
    try:
        code = harness.read_text(encoding="utf-8")
    except OSError:
        return False
    if "VERIPP_ASSUME" not in code and "VERIPP_REQUIRES" not in code:
        return False
    probe = harness.with_name(f"{harness.stem}.reachable{harness.suffix}")
    try:
        probe.write_text(reachability_variant(code), encoding="utf-8")
        result = run(probe, config)
    except (OSError, RuntimeError):
        return False
    # The probe's trailing assertion is always false, so a reachable harness
    # must fail it. Verifying means nothing could reach it.
    return result.outcome is Outcome.VERIFIED


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
