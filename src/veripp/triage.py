"""Counterexample triage: real bug vs missing assumption vs harness issue."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .esbmc import VerifyResult
from .llm import LLMClient


@dataclass
class Diagnosis:
    kind: str  # "real_bug" | "missing_assumption" | "harness_issue"
    explanation: str
    patched_source: Path | None = None  # only for missing_assumption proposals


def triage_counterexample(
    source: Path, result: VerifyResult, llm: LLMClient
) -> Diagnosis:
    kind = llm.classify_failure(source, result)
    explanation = llm.explain_trace(source, result)

    # For now we do not auto-patch preconditions; we surface the suggestion
    # and let the user confirm. Auto-patching (with a re-verify) lands once
    # the slicer can regenerate harnesses deterministically.
    return Diagnosis(kind=kind, explanation=explanation, patched_source=None)
