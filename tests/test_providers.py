"""Any LLM provider, or none.

veripp must not be tied to one vendor. Everything it asks a model lives in
`PromptedLLM`; a provider supplies only transport. The OpenAI-compatible
client covers OpenAI, Gemini, Groq, Together, DeepSeek, Mistral, OpenRouter
and local runtimes (Ollama, vLLM, LM Studio) -- and is written over the
standard library so veripp stays dependency-free and works against a local
model with no account at all.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from veripp.llm import (
    PROVIDERS,
    LLMError,
    NullLLM,
    OpenAICompatibleLLM,
    PromptedLLM,
    TriageContext,
    make_llm,
)


@pytest.fixture
def context():
    return TriageContext(
        function="ratio", signature="unsigned ratio(unsigned total, unsigned count)",
        parameters=["total", "count"], function_source="return total / count;",
        call_sites=["line 3: ratio(10u, 2u)"], violated_property="division by zero",
    )


class _Server:
    """A real OpenAI-compatible endpoint, so the HTTP path is exercised."""

    def __init__(self, reply="missing_assumption", status=200, body=None):
        self.reply, self.status, self.body = reply, status, body
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"payload": payload, "headers": dict(self.headers)})
                raw = outer.body if outer.body is not None else json.dumps(
                    {"choices": [{"message": {"content": outer.reply}}]}
                )
                data = raw.encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()


class TestProviderSelection:
    def test_known_providers_have_endpoints(self):
        for name, entry in PROVIDERS.items():
            assert "class" in entry or entry["base_url"].startswith("http")

    def test_spec_picks_provider_and_model(self):
        llm = make_llm("ollama:llama3.1")
        assert isinstance(llm, OpenAICompatibleLLM)
        assert llm._model == "llama3.1"
        assert "11434" in llm._base_url

    def test_a_local_endpoint_needs_no_api_key(self):
        make_llm("lmstudio:any-model")  # must not raise

    def test_a_hosted_endpoint_without_a_key_says_which_variable(self):
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            make_llm("openai:gpt-4o-mini")

    def test_unknown_provider_lists_the_known_ones(self):
        with pytest.raises(RuntimeError, match="unknown LLM provider"):
            make_llm("nope:whatever")

    def test_any_endpoint_works_with_an_explicit_base_url(self):
        llm = make_llm("my-gateway:some-model", "http://127.0.0.1:9/v1")
        assert llm._base_url == "http://127.0.0.1:9/v1"


class TestOpenAICompatibleTransport:
    def _client(self, server):
        return OpenAICompatibleLLM(model="m", base_url=server.url, provider="test")

    def test_classification_round_trip(self, context):
        with _Server("missing_assumption") as server:
            assert self._client(server).classify(context) == "missing_assumption"
            sent = server.requests[0]["payload"]
            assert sent["model"] == "m"
            assert [m["role"] for m in sent["messages"]] == ["system", "user"]
            # the call sites are the evidence triage depends on
            assert "ratio(10u, 2u)" in sent["messages"][1]["content"]

    def test_api_key_is_sent_as_a_bearer_token(self, context):
        with _Server() as server:
            OpenAICompatibleLLM(model="m", base_url=server.url, api_key="sk-test").classify(context)
            assert server.requests[0]["headers"]["Authorization"] == "Bearer sk-test"

    def test_precondition_prompt_lists_parameters_unambiguously(self, context):
        with _Server("count != 0") as server:
            assert self._client(server).propose_precondition(context) == "count != 0"
            system = server.requests[0]["payload"]["messages"][0]["content"]
            block = system.split("ONLY these parameter names:\n")[1]
            assert [l.strip() for l in block.splitlines() if l.startswith("  ")] == [
                "total", "count",
            ]

    def test_none_means_no_proposal(self, context):
        with _Server("NONE") as server:
            assert self._client(server).propose_precondition(context) is None

    def test_an_unusable_classification_is_an_error_not_a_guess(self, context):
        with _Server("I think it might be a bug?") as server:
            with pytest.raises(LLMError, match="unusable classification"):
                self._client(server).classify(context)

    def test_http_errors_become_llm_errors(self, context):
        with _Server(status=429, body='{"error":"slow down"}') as server:
            with pytest.raises(LLMError, match="429"):
                self._client(server).classify(context)

    def test_a_malformed_response_is_reported_not_crashed(self, context):
        with _Server(body='{"unexpected": true}') as server:
            with pytest.raises(LLMError, match="unexpected response shape"):
                self._client(server).classify(context)

    def test_an_unreachable_endpoint_is_an_llm_error(self, context):
        client = OpenAICompatibleLLM(model="m", base_url="http://127.0.0.1:1/v1")
        with pytest.raises(LLMError, match="could not reach"):
            client.classify(context)


def test_every_provider_shares_one_set_of_prompts():
    """Adding a vendor must not be able to change what is asked."""
    for method in ("classify", "explain", "propose_precondition",
                   "propose_invariants", "propose_frontend_fix"):
        assert getattr(OpenAICompatibleLLM, method) is getattr(PromptedLLM, method)


def test_offline_mode_needs_no_provider(context):
    assert NullLLM().classify(context) == "real_bug"
    assert NullLLM().propose_precondition(context) is None


class TestClassificationParsing:
    """Most models reason inside the reply before answering.

    Only some providers split thinking into a separate field, and every
    candidate label is named in the question -- so an early mention is
    deliberation, not the answer. Taking the first word scored a local model
    0/2 on the benchmark; taking its conclusion scored 1/2 with no other
    change.
    """

    def _classify(self, reply, context):
        with _Server(reply) as server:
            return OpenAICompatibleLLM(model="m", base_url=server.url).classify(context)

    def test_a_bare_answer(self, context):
        assert self._classify("missing_assumption", context) == "missing_assumption"

    def test_an_answer_after_reasoning(self, context):
        reply = (
            "This could be real_bug, or a harness_issue if the buffer is wrong.\n"
            "But both call sites pass a non-zero count, so the caller upholds it.\n"
            "missing_assumption"
        )
        assert self._classify(reply, context) == "missing_assumption"

    def test_markdown_emphasis_does_not_hide_it(self, context):
        assert self._classify("**real_bug**", context) == "real_bug"

    def test_no_label_at_all_is_an_error(self, context):
        with pytest.raises(LLMError, match="unusable classification"):
            self._classify("I am not sure about this one.", context)


class TestPreconditionPrompt:
    def test_it_says_the_expression_must_exclude_the_counterexample(self, context):
        """A local model answered by pinning the field to the value that broke
        it -- which excludes nothing. The prompt now states the requirement."""
        with _Server("count > 0") as server:
            OpenAICompatibleLLM(model="m", base_url=server.url).propose_precondition(context)
            system = server.requests[0]["payload"]["messages"][0]["content"]
        assert "FALSE for the counterexample inputs" in system
        assert "TRUE at every call site" in system
        assert "pinning a field to the value that broke it" in system
