"""`veripp mcp`: the CLI's commands as MCP tools over stdio.

An agent gets a schema instead of prose, and the --json report back with the
exit code and its meaning. Everything here goes through the same `main()` the
shell does, so there is no second code path to drift.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from veripp import mcp

ROOT = Path(__file__).resolve().parent.parent


def _rpc(method, params=None, identifier=1):
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


class TestHandshake:
    def test_initialize_offers_tools_and_names_itself(self):
        response = mcp.handle(_rpc("initialize", {"protocolVersion": "2025-06-18",
                                                  "capabilities": {}, "clientInfo": {"name": "t"}}))
        result = response["result"]
        assert result["protocolVersion"] == "2025-06-18"
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "veripp"
        assert "assumptions" in result["instructions"]

    def test_an_unknown_protocol_version_gets_the_newest_we_speak(self):
        response = mcp.handle(_rpc("initialize", {"protocolVersion": "1999-01-01"}))
        assert response["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]

    def test_notifications_get_no_response(self):
        assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_ping(self):
        assert mcp.handle(_rpc("ping"))["result"] == {}

    def test_unknown_method_is_a_jsonrpc_error(self):
        response = mcp.handle(_rpc("resources/list"))
        assert response["error"]["code"] == -32601


class TestToolList:
    def test_the_four_commands_are_offered_with_schemas(self):
        tools = mcp.handle(_rpc("tools/list"))["result"]["tools"]
        names = {tool["name"] for tool in tools}
        assert names == {"verify", "scan", "harness", "doctor"}
        for tool in tools:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"

    def test_every_documented_option_is_a_real_flag(self):
        """The schema's property names become `--flags`; each must exist on
        the parser, or an agent following the schema gets a usage error."""
        import subprocess
        import sys

        help_all = subprocess.run(
            [sys.executable, "-m", "veripp.cli", "verify", "--help-all"],
            capture_output=True, text=True, cwd=ROOT, timeout=300,
        ).stdout
        scan_help = subprocess.run(
            [sys.executable, "-m", "veripp.cli", "scan", "--help-all"],
            capture_output=True, text=True, cwd=ROOT, timeout=300,
        ).stdout
        special = {"source", "extra_args", "include", "define", "model", "function"}
        for tool in mcp.TOOLS:
            text = scan_help if tool["name"] == "scan" else help_all
            for prop in tool["inputSchema"]["properties"]:
                if prop in special:
                    continue
                flag = "--" + prop.replace("_", "-")
                assert flag in text, f"{tool['name']} offers {prop!r} but `{flag}` is not a flag"

    def test_the_descriptions_carry_the_one_warning_that_matters(self):
        for tool in mcp.TOOLS:
            if tool["name"] in ("verify", "scan"):
                assert "assumptions" in tool["description"]


class TestArgv:
    def test_verify_is_offline_and_json_by_default(self):
        argv = mcp.argv_for("verify", {"source": "f.c", "function": "f"})
        assert argv[:4] == ["verify", "f.c", "--function", "f"]
        assert "--no-llm" in argv and "--json" in argv

    def test_a_model_turns_triage_on(self):
        argv = mcp.argv_for("verify", {"source": "f.c", "function": "f", "model": "ollama:x"})
        assert "--model" in argv and "--no-llm" not in argv

    def test_lists_repeat_their_flag_and_includes_use_short_options(self):
        argv = mcp.argv_for("verify", {
            "source": "f.c", "function": "f", "assume": ["n > 0", "d != 0"],
            "include": ["inc", "inc2"], "define": ["A=1"], "link": ["g.c"],
            "sequence_call": ["list_*"], "preprocess": True, "constructors": False,
        })
        assert argv.count("--assume") == 2 and "d != 0" in argv
        assert argv[argv.index("-I") + 1] == "inc" and argv.count("-I") == 2
        assert argv[argv.index("-D") + 1] == "A=1"
        assert "--link" in argv and "--sequence-call" in argv
        assert "--preprocess" in argv and "--constructors" not in argv

    def test_extra_args_come_last_so_they_win(self):
        argv = mcp.argv_for("scan", {"source": ".", "timeout": 5, "extra_args": ["--timeout", "9"]})
        assert argv[-2:] == ["--timeout", "9"]
        assert argv.index("--json") < len(argv) - 2

    def test_changed_alone_means_against_head(self):
        assert mcp.argv_for("scan", {"source": ".", "changed": ""})[2:4] == ["--changed", "--no-llm"]
        assert "origin/main" in mcp.argv_for("scan", {"source": ".", "changed": "origin/main"})

    def test_harness_and_doctor_are_not_json(self):
        assert "--json" not in mcp.argv_for("harness", {"source": "f.c", "function": "f"})
        assert mcp.argv_for("doctor", {}) == ["doctor"]

    def test_source_is_required(self):
        with pytest.raises(ValueError):
            mcp.argv_for("verify", {"function": "f"})


class TestToolCall:
    @pytest.fixture
    def fake_main(self, monkeypatch):
        calls = []

        def main(argv):
            calls.append(argv)
            print(json.dumps({"outcome": "counterexample", "assumptions": ["x"]}))
            print("note: something", file=__import__("sys").stderr)
            return 1

        monkeypatch.setattr("veripp.cli.main", main)
        return calls

    def test_the_report_comes_back_structured_with_the_exit_meaning(self, fake_main):
        response = mcp.handle(_rpc("tools/call", {"name": "verify",
                                                  "arguments": {"source": "f.c", "function": "f"}}))
        result = response["result"]
        assert result["isError"] is False, "a counterexample is an answer, not an error"
        structured = result["structuredContent"]
        assert structured["exit_code"] == 1 and "counterexample" in structured["meaning"]
        assert structured["report"] == {"outcome": "counterexample", "assumptions": ["x"]}
        assert structured["stderr"] == "note: something"
        assert structured["command"].startswith("veripp verify f.c --function f")
        assert json.loads(result["content"][0]["text"])["exit_code"] == 1
        assert fake_main == [["verify", "f.c", "--function", "f", "--no-llm", "--json"]]

    def test_a_usage_error_is_an_error_result(self, monkeypatch):
        monkeypatch.setattr("veripp.cli.main", lambda argv: (print("error: no", file=__import__("sys").stderr), 2)[1])
        result = mcp.handle(_rpc("tools/call", {"name": "scan", "arguments": {"source": "x"}}))["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["exit_code"] == 2

    def test_argparse_rejecting_a_flag_is_a_result_not_a_crash(self, monkeypatch):
        def main(argv):
            raise SystemExit(2)

        monkeypatch.setattr("veripp.cli.main", main)
        result = mcp.handle(_rpc("tools/call", {"name": "verify", "arguments": {
            "source": "f.c", "extra_args": ["--no-such-flag"]}}))["result"]
        assert result["isError"] is True and result["structuredContent"]["exit_code"] == 2

    def test_an_exception_inside_veripp_is_reported_not_raised(self, monkeypatch):
        def main(argv):
            raise RuntimeError("boom")

        monkeypatch.setattr("veripp.cli.main", main)
        result = mcp.handle(_rpc("tools/call", {"name": "doctor", "arguments": {}}))["result"]
        assert result["isError"] is True and "boom" in result["content"][0]["text"]

    def test_an_unknown_tool(self):
        result = mcp.handle(_rpc("tools/call", {"name": "fix_my_code", "arguments": {}}))["result"]
        assert result["isError"] is True and "unknown tool" in result["content"][0]["text"]

    def test_a_missing_source_is_an_error_result(self):
        result = mcp.handle(_rpc("tools/call", {"name": "verify", "arguments": {}}))["result"]
        assert result["isError"] is True and "source" in result["content"][0]["text"]

    def test_harness_returns_the_program_as_text(self, monkeypatch):
        monkeypatch.setattr("veripp.cli.main", lambda argv: (print("int main() {}"), 0)[1])
        result = mcp.handle(_rpc("tools/call", {"name": "harness",
                                                "arguments": {"source": "f.c", "function": "f"}}))["result"]
        assert result["content"][0]["text"].startswith("int main() {}")
        assert result["structuredContent"]["harness"].startswith("int main() {}")


class TestServe:
    def test_one_json_object_per_line_each_way(self, monkeypatch):
        monkeypatch.setattr("veripp.cli.main", lambda argv: (print("{}"), 0)[1])
        lines = [
            json.dumps(_rpc("initialize", {"protocolVersion": "2025-06-18"}, 1)),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "",
            "this is not json",
            json.dumps([1, 2, 3]),
            json.dumps(_rpc("tools/list", None, 2)),
            json.dumps(_rpc("tools/call", {"name": "scan", "arguments": {"source": "x"}}, 3)),
        ]
        out = io.StringIO()
        assert mcp.serve(io.StringIO("\n".join(lines) + "\n"), out) == 0
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        assert [r.get("id") for r in responses] == [1, None, None, 2, 3]
        assert responses[1]["error"]["code"] == -32700
        assert responses[2]["error"]["code"] == -32600
        assert {t["name"] for t in responses[3]["result"]["tools"]} >= {"verify", "scan"}
        assert responses[4]["result"]["structuredContent"]["exit_code"] == 0

    def test_the_cli_exposes_it(self):
        import subprocess
        import sys

        out = subprocess.run([sys.executable, "-m", "veripp.cli", "mcp", "--help"],
                             capture_output=True, text=True, cwd=ROOT, timeout=300)
        assert out.returncode == 0 and "Model Context Protocol" in out.stdout


@pytest.mark.esbmc
def test_end_to_end_verify_finds_the_example_bug():
    """The real thing: the off-by-one example through the MCP front door."""
    result = mcp.handle(_rpc("tools/call", {"name": "verify", "arguments": {
        "source": str(ROOT / "examples" / "off_by_one.cpp"), "function": "sum_array"}}))["result"]
    structured = result["structuredContent"]
    assert structured["exit_code"] == 1, structured
    assert structured["report"]["outcome"] == "counterexample"
    assert structured["report"]["assumptions"], "the assumptions must travel with the verdict"
    assert result["isError"] is False
