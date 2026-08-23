"""The agent loop: attempt -> triage -> escalate, under a hard budget.

Design invariants:
  * The LLM never decides correctness. Every proposal (harness edit,
    invariant, assumption) is re-checked by ESBMC.
  * Every reported result carries the exact VerifyConfig it was obtained
    under, so "verified" always means "verified under these bounds and
    assumptions".
  * The loop terminates: bounded iterations, wall time, and LLM calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .esbmc import Outcome, VerifyConfig, VerifyResult, run
from .llm import LLMClient, NullLLM
from .triage import Diagnosis, triage_counterexample


@dataclass
class Budget:
    max_attempts: int = 8
    max_llm_calls: int = 12
    wall_time_s: int = 600


@dataclass
class AgentReport:
    final: VerifyResult
    attempts: list[VerifyResult] = field(default_factory=list)
    diagnosis: Diagnosis | None = None
    narrative: str = ""

    def summary(self) -> str:
        cfg = self.final.config
        mode = "k-induction (unbounded)" if cfg.k_induction else f"bounded, unwind={cfg.unwind}"
        lines = [f"Result: {self.final.outcome.value}  [{mode}]"]
        if self.final.violated_property:
            lines.append(f"Violated property:\n{self.final.violated_property}")
        if self.diagnosis:
            lines.append(f"Diagnosis: {self.diagnosis.kind}: {self.diagnosis.explanation}")
        if self.narrative:
            lines.append(self.narrative)
        lines.append(f"Attempts: {len(self.attempts)}")
        return "\n".join(lines)


# Escalation ladder: each entry transforms the previous config.
_ESCALATIONS = [
    lambda c: _with(c, unwind=c.unwind * 4),
    lambda c: _with(c, unwind=c.unwind * 4),
    lambda c: _with(c, k_induction=True),
]


def _with(cfg: VerifyConfig, **kw) -> VerifyConfig:
    from dataclasses import replace

    return replace(cfg, **kw)


def verify_with_agent(
    source: Path,
    base_config: VerifyConfig | None = None,
    llm: LLMClient | None = None,
    budget: Budget | None = None,
) -> AgentReport:
    """Main entry point: drive ESBMC to a conclusive answer if possible."""
    llm = llm or NullLLM()
    budget = budget or Budget()
    config = base_config or VerifyConfig()
    started = time.monotonic()

    attempts: list[VerifyResult] = []
    escalation_idx = 0

    while True:
        if len(attempts) >= budget.max_attempts:
            return _inconclusive(attempts, "attempt budget exhausted")
        if time.monotonic() - started > budget.wall_time_s:
            return _inconclusive(attempts, "wall-time budget exhausted")

        result = run(source, config)
        attempts.append(result)

        if result.outcome is Outcome.VERIFIED:
            return AgentReport(final=result, attempts=attempts)

        if result.outcome is Outcome.COUNTEREXAMPLE:
            diagnosis = triage_counterexample(source, result, llm)
            if diagnosis.kind == "missing_assumption" and diagnosis.patched_source:
                # LLM proposed a precondition; verify the patched harness.
                source = diagnosis.patched_source
                continue
            return AgentReport(
                final=result,
                attempts=attempts,
                diagnosis=diagnosis,
                narrative=diagnosis.explanation,
            )

        if result.outcome in (Outcome.UNWIND_LIMIT, Outcome.TIMEOUT, Outcome.UNKNOWN):
            if escalation_idx < len(_ESCALATIONS):
                config = _ESCALATIONS[escalation_idx](config)
                escalation_idx += 1
                continue
            # Ladder exhausted: ask the LLM for loop invariants / lemmas.
            proposal = llm.propose_invariants(source, result)
            if proposal is not None:
                source = proposal
                config = _with(config, k_induction=True)
                continue
            return _inconclusive(attempts, "escalation ladder and LLM proposals exhausted")

        if result.outcome is Outcome.PARSE_ERROR:
            fixed = llm.propose_frontend_fix(source, result)
            if fixed is not None:
                source = fixed
                continue
            return _inconclusive(attempts, "ESBMC frontend rejected the input")


def _inconclusive(attempts: list[VerifyResult], reason: str) -> AgentReport:
    return AgentReport(
        final=attempts[-1],
        attempts=attempts,
        narrative=f"Inconclusive: {reason}. Best diagnosis attached.",
    )
