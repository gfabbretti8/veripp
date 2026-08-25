"""LLM clients. Any provider, or none at all.

The rest of the codebase depends only on `LLMClient`, so veripp runs fully
offline with `NullLLM` and is not tied to a vendor.

Design rule: the LLM only ever *proposes* -- a classification, an explanation,
a precondition, a rewritten file. Every proposal that could change a verdict is
re-checked by ESBMC. A wrong proposal costs a retry, never soundness, which is
why a small cheap model is a reasonable choice here.

The prompts live in `PromptedLLM` and are identical for every provider; a
provider supplies only `_ask`. They encode what the triage pilot measured:
classification is decided by a function's REAL CALL SITES, not by the trace.
Without call sites all three pilot counterexamples were misread as real bugs;
with them, all three were triaged correctly.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
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

    Triage catches this and degrades to conservative offline behaviour; it must
    never abort a verification run.
    """


class LLMClient(Protocol):
    def classify(self, context: TriageContext) -> str: ...

    def explain(self, context: TriageContext) -> str: ...

    def propose_precondition(self, context: TriageContext) -> str | None: ...

    def propose_invariants(self, source: Path, result: VerifyResult) -> Path | None: ...

    def propose_frontend_fix(self, source: Path, result: VerifyResult) -> Path | None: ...


class NullLLM:
    """Offline mode: no proposals, conservative classifications."""

    PROVIDER = "none"

    def classify(self, context: TriageContext) -> str:
        return "real_bug"  # conservative default: surface it to the user

    def explain(self, context: TriageContext) -> str:
        return (
            f"{context.violated_property or 'Property violated'}. "
            "Offline mode: the counterexample was not analysed against the "
            "function's call sites; enable an LLM for triage."
        )

    def propose_precondition(self, context: TriageContext) -> str | None:
        return None

    def propose_invariants(self, source: Path, result: VerifyResult) -> Path | None:
        return None

    def propose_frontend_fix(self, source: Path, result: VerifyResult) -> Path | None:
        return None


# ------------------------------------------------------- shared prompting ---


class PromptedLLM:
    """Everything veripp asks an LLM, expressed through one primitive.

    Subclasses implement `_ask` and nothing else, so adding a provider cannot
    accidentally change what is asked.
    """

    MODEL = ""
    PROVIDER = "llm"

    def _ask(self, system: str, user: str, max_tokens: int = 16000) -> str:
        raise NotImplementedError

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
                "harness_issue - the harness models an input wrongly, so the "
                "counterexample is about the harness and not the code. This "
                "covers an object whose fields were all made independently "
                "nondeterministic and so hold a combination the type's own "
                "invariants forbid, an unterminated string, or a buffer of the "
                "wrong shape.\n\n"
                "Note: an input that merely looks extreme is not a real_bug "
                "unless a caller can actually produce it.\n\n"
                "Think it through if you need to, then end your reply with the "
                "chosen word on a line by itself."
            ),
            user=context.render(),
        )
        return _last_label(reply)

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
        )

    def propose_precondition(self, context: TriageContext) -> str | None:
        reply = self._ask(
            system=(
                "The counterexample uses inputs real callers never pass.\n"
                "Propose ONE C++ boolean expression that is FALSE for the "
                "counterexample inputs shown above -- so those inputs are "
                "excluded -- and TRUE at every call site shown.\n"
                "Do not restate the counterexample values: pinning a field to "
                "the value that broke it excludes nothing.\n"
                "Prefer the WEAKEST such condition the call sites support: an "
                "over-tight condition produces a proof that means nothing.\n"
                "You may use ONLY these parameter names:\n"
                f"{chr(10).join('  ' + n for n in context.parameters)}\n"
                "Reply with the bare expression on one line and nothing else, "
                "or NONE if no such precondition exists."
            ),
            user=context.render(),
        )
        return _expression_from(reply)

    # -- file-level proposals --------------------------------------------

    def propose_invariants(self, source: Path, result: VerifyResult) -> Path | None:
        reply = self._ask(
            system=(
                "You are a verification engineer operating the ESBMC model "
                "checker on C++ code. Given a program the checker could not "
                "conclude on, add loop invariants as __ESBMC_assert/"
                "__ESBMC_assume annotations, or strengthening assertions, that "
                "could make k-induction succeed. Return the complete modified "
                "file in one code block. Do not change program semantics."
            ),
            user=f"Verifier output (truncated):\n{result.raw_output[-4000:]}\n\n"
            f"Source:\n```cpp\n{source.read_text(encoding="utf-8")}\n```",
        )
        code = self._extract_code(reply)
        return self._write_variant(source, code, "inv") if code else None

    def propose_frontend_fix(self, source: Path, result: VerifyResult) -> Path | None:
        reply = self._ask(
            system=(
                "The ESBMC C++ frontend rejected this file. Produce a "
                "semantically equivalent version using constructs the frontend "
                "accepts (reduce template/STL usage, simplify). Return the full "
                "file in one code block."
            ),
            user=f"Frontend errors:\n{result.raw_output[-4000:]}\n\n"
            f"Source:\n```cpp\n{source.read_text(encoding="utf-8")}\n```",
        )
        code = self._extract_code(reply)
        return self._write_variant(source, code, "fix") if code else None

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _extract_code(reply: str) -> str | None:
        m = re.search(r"```(?:cpp|c\+\+|c)?\n(.*?)```", reply, re.S)
        return m.group(1) if m else None

    def _write_variant(self, source: Path, code: str, tag: str) -> Path:
        out = source.with_name(f"{source.stem}.{tag}{source.suffix}")
        out.write_text(code, encoding="utf-8")
        return out


# ------------------------------------------------------------- providers ---


class AnthropicLLM(PromptedLLM):
    """Claude, through the official SDK (`pip install 'veripp[anthropic]'`)."""

    MODEL = "claude-opus-5"
    PROVIDER = "anthropic"

    def __init__(self, model: str | None = None):
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "the `anthropic` package is not installed "
                "(pip install 'veripp[anthropic]'), or use --no-llm"
            ) from exc
        self._anthropic = anthropic
        # The SDK resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an
        # `ant auth login` profile. It constructs happily with none and only
        # fails when a request is sent, so check now and let the caller fall
        # back to offline mode rather than dying mid-run.
        self._client = anthropic.Anthropic()
        if not (getattr(self._client, "api_key", None)
                or getattr(self._client, "auth_token", None)):
            raise RuntimeError(
                "no Anthropic credentials found (set ANTHROPIC_API_KEY, or run "
                "`ant auth login`); use --no-llm to silence this"
            )
        self._model = model or self.MODEL

    def _ask(self, system: str, user: str, max_tokens: int = 16000) -> str:
        anthropic = self._anthropic
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the credentials") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic API rate limit hit") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("could not reach the Anthropic API") from exc
        except TypeError as exc:  # SDK raises this when auth resolves to nothing
            raise LLMError(f"Anthropic client is not usable: {exc}") from exc
        return "".join(b.text for b in msg.content if b.type == "text")


class OpenAICompatibleLLM(PromptedLLM):
    """Any provider speaking the OpenAI chat-completions API.

    That is most of them: OpenAI itself, and -- by pointing `base_url` at their
    endpoint -- Google Gemini, Groq, Together, Fireworks, DeepSeek, Mistral,
    OpenRouter, Azure OpenAI, and local runtimes like Ollama, vLLM and
    LM Studio. Deliberately implemented over the standard library so veripp
    stays dependency-free and works against a local model with no account at
    all.
    """

    MODEL = "gpt-4o-mini"
    PROVIDER = "openai"
    BASE_URL = "https://api.openai.com/v1"
    API_KEY_ENV = "OPENAI_API_KEY"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        provider: str | None = None,
        timeout: int = 120,
    ):
        self._model = model or self.MODEL
        self._base_url = (base_url or os.environ.get("VERIPP_LLM_BASE_URL")
                          or self.BASE_URL).rstrip("/")
        env = api_key_env or self.API_KEY_ENV
        self._api_key = api_key or os.environ.get(env) or os.environ.get("VERIPP_LLM_API_KEY")
        self._timeout = timeout
        if provider:
            self.PROVIDER = provider
        # A local runtime needs no key; a hosted one does. Only complain when
        # the endpoint looks remote.
        if not self._api_key and not _is_local(self._base_url):
            raise RuntimeError(
                f"no API key for {self._base_url} (set {env} or "
                "VERIPP_LLM_API_KEY); use --no-llm to run without an LLM"
            )

    def _ask(self, system: str, user: str, max_tokens: int = 16000) -> str:
        payload = json.dumps({
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions", data=payload, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise LLMError(f"{self.PROVIDER} API error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"could not reach {self._base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.PROVIDER} returned a non-JSON body") from exc

        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape from {self.PROVIDER}: "
                           f"{str(body)[:200]}") from exc


#: Ready-made endpoints, so `--model groq:llama-3.3-70b-versatile` just works.
#: Anything absent is still reachable with --llm-base-url.
PROVIDERS: dict[str, dict] = {
    "anthropic": {"class": AnthropicLLM},
    "openai": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
    },
    "groq": {"base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"},
    "together": {"base_url": "https://api.together.xyz/v1", "api_key_env": "TOGETHER_API_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "api_key_env": "MISTRAL_API_KEY"},
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"
    },
    "ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": "OLLAMA_API_KEY"},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "api_key_env": "LMSTUDIO_API_KEY"},
}


_FENCE_RE = re.compile(r"```[A-Za-z+]*\n(.*?)```", re.S)
_PROSE = re.compile(r"\b(the|this|because|since|should|would|note|here|we|I)\b", re.I)


def _expression_from(reply: str) -> str | None:
    """The C++ expression a model settled on, however it chose to wrap it.

    Models fence code, prefix it with a sentence, or do both. Taking the first
    line yielded the literal string "cpp" from a ```cpp fence -- a proposal the
    solver then dutifully rejected.
    """
    if not reply or not reply.strip():
        return None
    fenced = _FENCE_RE.search(reply)
    body = fenced.group(1) if fenced else reply
    candidates = [
        line.strip().strip("`").rstrip(";")
        for line in body.strip().splitlines()
        if line.strip().strip("`")
    ]
    for line in reversed(candidates):          # the conclusion comes last
        if line.upper() == "NONE":
            return None
        if len(line) > 200 or not re.search(r"[<>=!&|+\-*/]", line):
            continue                            # prose, or a bare word
        if _PROSE.search(line) and not fenced:
            continue
        return line
    return None


_LABEL_RE = re.compile(r"\b(" + "|".join(KINDS) + r")\b")


def _last_label(reply: str) -> str:
    """The classification a model settled on.

    Taking the first word only works for a model that answers bare. Most
    reason first -- in the reply itself, since only some providers split
    thinking into a separate field -- and every candidate label is named in
    the question, so an early mention is deliberation, not the answer. The
    last one is the conclusion.
    """
    matches = _LABEL_RE.findall(reply or "")
    if not matches:
        raise LLMError(f"unusable classification reply: {(reply or '')[:120]!r}")
    return matches[-1]


def _is_local(base_url: str) -> bool:
    return any(h in base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))


def detect_provider() -> str | None:
    """The first provider this machine actually has credentials for.

    Guessing a vendor the user never mentioned produces a confusing first run
    ("no API key for api.openai.com" when they never said OpenAI), so the
    default is whatever is configured -- and nothing, if nothing is.
    """
    if os.environ.get("VERIPP_LLM_BASE_URL"):
        return "custom"
    for name, entry in PROVIDERS.items():
        env = entry.get("api_key_env", "ANTHROPIC_API_KEY")
        if os.environ.get(env):
            return name
    return None


def make_llm(spec: str | None = None, base_url: str | None = None) -> LLMClient:
    """Build a client from a `provider:model` string.

    Examples:
        anthropic:claude-opus-5
        openai:gpt-4o-mini
        ollama:llama3.1            (no account needed)
        groq:llama-3.3-70b-versatile
        my-gateway:some-model      with --llm-base-url https://...

    A bare model name uses VERIPP_LLM_PROVIDER, else openai. Raises
    RuntimeError when the provider cannot be used, so callers can fall back to
    offline mode with a note.
    """
    spec = spec or os.environ.get("VERIPP_LLM_MODEL") or ""
    if not spec and base_url is None:
        detected = detect_provider()
        if detected is None:
            raise RuntimeError(
                "no LLM configured, so counterexamples will not be triaged. "
                "Set one with --model (e.g. ollama:llama3.1 for a local model, "
                "needing no account), or pass --no-llm to say so explicitly"
            )
        spec = detected
    provider, _, model = spec.partition(":")
    if not model:  # bare provider or bare model name
        if provider.lower() in PROVIDERS:
            provider, model = provider, ""
        else:
            provider, model = os.environ.get("VERIPP_LLM_PROVIDER", "openai"), provider
    provider = provider.lower()

    if provider == "anthropic":
        return AnthropicLLM(model or None)

    entry = PROVIDERS.get(provider)
    if entry is None and base_url is None:
        known = ", ".join(sorted(PROVIDERS))
        raise RuntimeError(
            f"unknown LLM provider {provider!r}. Known: {known}. Any other "
            "OpenAI-compatible endpoint works with --llm-base-url."
        )
    entry = entry or {}
    if entry.get("class") is AnthropicLLM:
        return AnthropicLLM(model or None)
    return OpenAICompatibleLLM(
        model=model or None,
        base_url=base_url or entry.get("base_url"),
        api_key_env=entry.get("api_key_env"),
        provider=provider,
    )
