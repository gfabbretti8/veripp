"""LLM client abstraction.

The rest of the codebase depends only on this interface, so the tool runs
fully offline with NullLLM (plain verifier pipeline) and can swap providers.

Every method returns either a *proposal* (a new source file to verify) or
None. Proposals are never trusted; the agent re-runs ESBMC on them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .esbmc import VerifyResult


class LLMClient(Protocol):
    def propose_invariants(self, source: Path, result: VerifyResult) -> Path | None: ...

    def propose_frontend_fix(self, source: Path, result: VerifyResult) -> Path | None: ...

    def explain_trace(self, source: Path, result: VerifyResult) -> str: ...

    def classify_failure(self, source: Path, result: VerifyResult) -> str: ...


class NullLLM:
    """Offline mode: no proposals, generic explanations."""

    def propose_invariants(self, source: Path, result: VerifyResult) -> Path | None:
        return None

    def propose_frontend_fix(self, source: Path, result: VerifyResult) -> Path | None:
        return None

    def explain_trace(self, source: Path, result: VerifyResult) -> str:
        prop = result.violated_property
        where = f" at {prop.loc}" if prop else ""
        what = prop.description if prop else "unknown property"
        return (
            f"{what}{where}. Offline mode: no analysis of the trace was done; "
            "run without --no-llm for a plain-language explanation."
        )

    def classify_failure(self, source: Path, result: VerifyResult) -> str:
        return "real_bug"  # conservative default: surface it to the user


class AnthropicLLM:
    """Claude-backed client. Requires ANTHROPIC_API_KEY."""

    MODEL = "claude-sonnet-4-6"

    def __init__(self, model: str | None = None):
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("pip install anthropic, or use --no-llm") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set; use --no-llm for offline mode")
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = model or self.MODEL

    # -- prompt plumbing -------------------------------------------------

    def _ask(self, system: str, user: str, max_tokens: int = 2048) -> str:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")

    @staticmethod
    def _extract_code(reply: str) -> str | None:
        import re

        m = re.search(r"```(?:cpp|c\+\+|c)?\n(.*?)```", reply, re.S)
        return m.group(1) if m else None

    def _write_variant(self, source: Path, code: str, tag: str) -> Path:
        out = source.with_name(f"{source.stem}.{tag}{source.suffix}")
        out.write_text(code)
        return out

    # -- proposals -------------------------------------------------------

    def propose_invariants(self, source: Path, result: VerifyResult) -> Path | None:
        reply = self._ask(
            system=(
                "You are a verification engineer operating the ESBMC model checker "
                "on C++ code. Given a program the checker could not conclude on, add "
                "loop invariants as __ESBMC_assert/__ESBMC_assume annotations, or "
                "strengthening assertions, that could make k-induction succeed. "
                "Return the complete modified file in one code block. Do not change "
                "program semantics."
            ),
            user=f"Verifier output (truncated):\n{result.raw_output[-4000:]}\n\n"
            f"Source:\n```cpp\n{source.read_text()}\n```",
            max_tokens=4096,
        )
        code = self._extract_code(reply)
        return self._write_variant(source, code, "inv") if code else None

    def propose_frontend_fix(self, source: Path, result: VerifyResult) -> Path | None:
        reply = self._ask(
            system=(
                "The ESBMC C++ frontend rejected this file. Produce a semantically "
                "equivalent version using constructs the frontend accepts (reduce "
                "template/STL usage, simplify). Return the full file in one code block."
            ),
            user=f"Frontend errors:\n{result.raw_output[-4000:]}\n\n"
            f"Source:\n```cpp\n{source.read_text()}\n```",
            max_tokens=4096,
        )
        code = self._extract_code(reply)
        return self._write_variant(source, code, "fix") if code else None

    def explain_trace(self, source: Path, result: VerifyResult) -> str:
        return self._ask(
            system=(
                "Explain this model-checker counterexample to a C++ developer in "
                "3-5 sentences: what concrete input triggers it, which line fails, "
                "and why. No speculation beyond the trace."
            ),
            user=f"Violated property:\n{result.violated_property}\n\n"
            f"Trace (truncated):\n{result.raw_output[-6000:]}\n\n"
            f"Source:\n```cpp\n{source.read_text()}\n```",
        )

    def classify_failure(self, source: Path, result: VerifyResult) -> str:
        reply = self._ask(
            system=(
                "Classify this verification failure. Answer with exactly one word:\n"
                "real_bug        - the code misbehaves on a feasible input\n"
                "missing_assumption - the counterexample uses an input the caller "
                "would never pass (a missing precondition)\n"
                "harness_issue   - the failure is an artifact of the harness/stubs"
            ),
            user=f"Violated property:\n{result.violated_property}\n\n"
            f"Trace (truncated):\n{result.raw_output[-6000:]}\n\n"
            f"Source:\n```cpp\n{source.read_text()}\n```",
            max_tokens=16,
        )
        word = reply.strip().split()[0] if reply.strip() else "real_bug"
        return word if word in ("real_bug", "missing_assumption", "harness_issue") else "real_bug"
