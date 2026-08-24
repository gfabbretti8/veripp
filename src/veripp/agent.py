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

import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .esbmc import Outcome, VerifyConfig, VerifyResult, run
from .harness import HarnessError, generate
from .llm import LLMClient, NullLLM
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

    @property
    def verified(self) -> bool:
        return self.final.outcome is Outcome.VERIFIED

    def summary(self) -> str:
        lines = [f"Result: {self.final.outcome.value}", f"  {self.final.config.describe()}"]
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
            return AgentReport(
                final=result,
                attempts=attempts,
                diagnosis=last_diagnosis,
                accepted_preconditions=preconditions,
                **context,
            )

        if result.outcome is Outcome.COUNTEREXAMPLE:
            diagnosis = triage_counterexample(target, source, result, llm)
            last_diagnosis = diagnosis
            if (
                diagnosis.kind == "missing_assumption"
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
            proposal = llm.propose_invariants(source, result)
            if proposal is not None:
                source = proposal
                config = replace(config, k_induction=True)
                continue
            return _inconclusive(
                attempts, "escalation ladder and LLM proposals exhausted", **context
            )

        if result.outcome is Outcome.PARSE_ERROR:
            fixed = llm.propose_frontend_fix(source, result)
            if fixed is not None:
                source = fixed
                continue
            return _inconclusive(
                attempts,
                f"ESBMC frontend rejected the input: {result.error or 'see raw output'}",
                **context,
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
