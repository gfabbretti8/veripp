"""LLM client abstraction.

The rest of the codebase depends only on this interface, so the tool runs
fully offline with NullLLM (plain verifier pipeline) and can swap providers.

Design rule: the LLM only ever *proposes* -- a classification, an explanation,
a precondition expression, a rewritten file. Every proposal that affects a
verification verdict is re-checked by ESBMC before anything is reported.

The prompts here encode what the triage pilot measured: classification is
decided by the function's REAL CALL SITES, not by the trace alone. Without
call sites, all three pilot counterexamples were misread as real bugs; with
them, all three were correctly triaged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .esbmc import VerifyResult

KINDS = ("real_bug", "missing_assumption", "harness_issue")


@dataclass
class TriageContext:
    """Everything the triage needs to judge one counterexample."""

    function: str                 # qualified name
    signature: str                # printable signature
    parameters: list[str]         # parameter names, for precondition scoping
    function_source: str          # the definition, verbatim
    call_sites: list[str] = field(default_factory=list)  # "line N: <code>"
    harness_code: str = ""
    violated_property: str = ""
    counterexample_inputs: str = ""
    raw_output_tail: str = ""

    def render(self) -> str:
        sites = "\n".join(self.call_sites) or "(no call sites found in this file)"
        return (
            f"Function under verification: {self.signature}\n\n"
            f"Definition:\n```cpp\n{self.function_source}\n```\n\n"
            f"Call sites in the same file (what real callers pass):\n{sites}\n\n"
            f"Verification harness (machine-generated):\n"
            f"```cpp\n{self.harness_code}\n```\n\n"
            f"Violated property:\n{self.violated_property}\n\n"
            f"Concrete counterexample inputs:\n{self.counterexample_inputs}\n\n"
            f"Verifier output (tail):\n{self.raw_output_tail}"
        )


class LLMError(Exception):
    """The LLM could not be reached or answered unusably.

    Triage catches this and degrades to the conservative offline behaviour;
    it must never abort a verification run.
    """


class LLMClient(Protocol):
    def classify(self, context: TriageContext) -> str: ...

    def explain(self, context: TriageContext) -> str: ...

    def propose_precondition(self, context: TriageContext) -> str | None: ...

    def propose_invariants(self, source: Path, result: VerifyResult) -> Path | None: ...

    def propose_frontend_fix(self, source: Path, result: VerifyResult) -> Path | None: ...


class NullLLM:
    """Offline mode: no proposals, conservative classifications."""

    def classify(self, context: TriageContext) -> str:
        return "real_bug"  # conservative default: surface it to the user

    def explain(self, context: TriageContext) -> str:
        return (
            f"{context.violated_property or 'Property violated'}. "
            "Offline mode: the counterexample was not analysed against the "
            "function's call sites; run with an LLM enabled for triage."
        )

    def propose_precondition(self, context: TriageContext) -> str | None:
        return None

    def propose_invariants(self, source: Path, result: VerifyResult) -> Path | None:
        return None

    def propose_frontend_fix(self, source: Path, result: VerifyResult) -> Path | None:
        return None


class AnthropicLLM:
    """Claude-backed client.

    Credentials resolve through the SDK's normal chain (ANTHROPIC_API_KEY,
    ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile) -- an unset env var
    alone does not mean offline.
    """

    MODEL = "claude-opus-5"

    def __init__(self, model: str | None = None):
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "the `anthropic` package is not installed "
                "(pip install 'veripp[llm]'), or use --no-llm"
            ) from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self._model = model or self.MODEL

    # -- prompt plumbing -------------------------------------------------

    def _ask(self, system: str, user: str, max_tokens: int = 16000,
             effort: str = "high") -> str:
        anthropic = self._anthropic
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                output_config={"effort": effort},
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                "no usable Anthropic credentials (set ANTHROPIC_API_KEY or "
                "run `ant auth login`), or use --no-llm"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic API rate limit hit") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("could not reach the Anthropic API") from exc
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

    # -- triage ----------------------------------------------------------

    def classify(self, context: TriageContext) -> str:
        reply = self._ask(
            system=(
                "You triage counterexamples from the ESBMC model checker. The "
                "harness feeds a function fully nondeterministic inputs, so a "
                "counterexample may use inputs no real caller produces.\n"
                "Decide from the CALL SITES what real callers actually pass.\n"
                "Answer with exactly one word:\n"
                "real_bug - a feasible input (one real callers can produce) "
                "misbehaves\n"
                "missing_assumption - every shown call site respects a "
                "precondition the counterexample violates\n"
                "harness_issue - the harness models an input wrongly (e.g. an "
                "unterminated string, a buffer of the wrong shape), violating "
                "the function's documented or obvious contract"
            ),
            user=context.render(),
            effort="high",
        )
        word = reply.strip().split()[0].strip(".:,") if reply.strip() else ""
        if word not in KINDS:
            raise LLMError(f"unusable classification reply: {reply[:80]!r}")
        return word

    def explain(self, context: TriageContext) -> str:
        return self._ask(
            system=(
                "Explain this model-checker counterexample to the maintainer "
                "of the code, in 3-5 sentences: the concrete input that "
                "triggers it, the line that fails, why it fails, and what the "
                "call sites imply about whether it is reachable in practice. "
                "No speculation beyond the trace and the shown code."
            ),
            user=context.render(),
            effort="high",
        )

    def propose_precondition(self, context: TriageContext) -> str | None:
        reply = self._ask(
            system=(
                "The counterexample uses inputs real callers never pass. "
                "Propose ONE C++ boolean expression, over ONLY these parameter "
                f"names: {', '.join(context.parameters)}, that rules the "
                "counterexample out while admitting every shown call site. "
                "Prefer the weakest such condition the call sites support. "
                "Reply with the bare expression on one line and nothing else, "
                "or NONE if no such precondition exists."
            ),
            user=context.render(),
            effort="high",
        )
        line = reply.strip().splitlines()[0].strip().strip("`") if reply.strip() else ""
        if not line or line.upper() == "NONE" or len(line) > 200:
            return None
        return line

    # -- file-level proposals (unchanged interface) ----------------------

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
        )
        code = self._extract_code(reply)
        return self._write_variant(source, code, "fix") if code else None
