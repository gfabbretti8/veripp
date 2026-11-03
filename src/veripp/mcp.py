"""veripp as an MCP server: the CLI's commands offered to an agent as tools.

The Model Context Protocol is JSON-RPC 2.0, one message per line, over stdin
and stdout. An agent (Claude Code, Cursor, anything that speaks MCP) starts
`veripp mcp` as a subprocess and calls `verify`, `scan`, `harness` and
`doctor` with structured arguments, getting back the same JSON the `--json`
flag prints, plus the exit code and what it means.

Why this is worth having: an agent driving veripp through a shell has to
parse prose and remember flags. Here it gets a schema, and the result it
gets back is the report veripp already produces for CI -- including, every
time, the assumptions a verdict rests on. Nothing in this module decides
anything about code; it is the CLI with a different front door.

Implemented over the standard library, like the LLM providers: no new
dependency, and the protocol subset needed here is small.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Any, TextIO

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

EXIT_MEANING = {
    0: "verified within the stated bounds and assumptions",
    1: "counterexample: a property fails in the generated harness",
    2: "usage error: nothing was checked",
    3: "inconclusive: a bound, a timeout, a vacuous proof, or a refused input",
}

_ASSUMPTIONS_NOTE = (
    "A result is only as good as the assumptions and bounds it states. Read "
    "`assumptions` and the bounds line before repeating a verdict, and treat "
    "a counterexample as holding in the generated harness until a caller is "
    "shown to reach it."
)

_SOURCE = {"type": "string", "description": "path to the C or C++ source file"}
_STRINGS = {"type": "array", "items": {"type": "string"}}


def _build_flags(description: str) -> dict[str, Any]:
    return {
        "include": {**_STRINGS, "description": "include directories (-I)"},
        "define": {**_STRINGS, "description": "preprocessor defines (-D NAME or NAME=VALUE)"},
        "link": {**_STRINGS, "description": "other translation units to compile alongside, "
                                            "so callees are defined rather than stubbed"},
        "compile_commands": {"type": "string",
                             "description": "compile_commands.json (or its directory) to take "
                                            "include paths, defines and -std from"},
        "std": {"type": "string", "description": "language standard, e.g. c11 or c++17"},
        "timeout": {"type": "integer", "description": "seconds per checker attempt"},
        "unwind": {"type": "integer", "description": "loop unwind bound"},
        "extra_args": {**_STRINGS,
                       "description": f"any other `veripp {description}` flags, verbatim, "
                                      "as `veripp --help-all` lists them"},
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "verify",
        "description": (
            "Prove one function free of undefined behaviour within stated bounds "
            "(overflow, out-of-bounds, null and invalid pointers, division by zero, "
            "leaks, uninitialised reads, undefined shifts, NaN), or get a concrete "
            "counterexample. veripp generates a harness that calls the function "
            "with unconstrained inputs, runs the ESBMC model checker, and re-runs "
            "every proof to make sure the harness could execute at all. "
            + _ASSUMPTIONS_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": _SOURCE,
                "function": {"type": "string",
                             "description": "the function to verify; omit for a file with its own main()"},
                "assume": {**_STRINGS,
                           "description": "preconditions on the parameters, e.g. \"n > 0\"; each "
                                          "is checked for satisfiability and reported with the result"},
                "sequence": {"type": "string",
                             "description": "for a C API: the handle type to construct and drive a "
                                            "bounded sequence of calls on"},
                "sequence_call": {**_STRINGS, "description": "globs choosing the functions a sequence may call"},
                "setup": {"type": "string", "description": "a call to run first, e.g. \"mem_init()\""},
                "preprocess": {"type": "boolean", "description": "read the source as the compiler sees it"},
                "constructors": {"type": "boolean",
                                 "description": "build object parameters with the library's own constructors"},
                "unterminated": {"type": "boolean",
                                 "description": "model char* parameters as raw bytes without a NUL terminator"},
                "max_array_len": {"type": "integer", "description": "bound on generated buffer lengths (default 4)"},
                "repro": {"type": "string",
                          "description": "write a standalone C program reproducing the counterexample here"},
                "model": {"type": "string",
                          "description": "an LLM for triage, e.g. \"anthropic:claude-sonnet-5\"; "
                                         "by default none, since the caller of this tool is the agent"},
                **_build_flags("verify"),
            },
            "required": ["source"],
        },
    },
    {
        "name": "scan",
        "description": (
            "Harness and verify every function in a file or a directory tree, and "
            "report what was proved, what has a counterexample, what could not be "
            "harnessed and why. Inconclusive results are retried with wider bounds, "
            "with no LLM involved. " + _ASSUMPTIONS_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "a source file, or a directory to scan"},
                "only": {**_STRINGS, "description": "verify only these functions"},
                "jobs": {"type": "integer", "description": "parallel checker runs"},
                "changed": {"type": "string",
                            "description": "scan only files git reports as changed: the "
                                           "uncommitted work when \"\" (empty), or everything "
                                           "since a ref such as \"origin/main\""},
                "retry_budget": {"type": "integer",
                                 "description": "seconds to spend re-trying inconclusive functions"},
                "baseline": {"type": "string", "description": "a baseline file of accepted findings"},
                "sarif": {"type": "string", "description": "write a SARIF report here"},
                **_build_flags("scan"),
            },
            "required": ["source"],
        },
    },
    {
        "name": "harness",
        "description": (
            "Print the harness veripp would verify for a function, without running "
            "the checker: the exact program whose properties a verdict is about, "
            "with the assumptions it bakes in. Read it when a result surprises you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": _SOURCE,
                "function": {"type": "string", "description": "the function to harness"},
                "sequence": {"type": "string", "description": "handle type for a C API sequence harness"},
                "preprocess": {"type": "boolean"},
                "constructors": {"type": "boolean"},
                "unterminated": {"type": "boolean"},
                **{k: v for k, v in _build_flags("harness").items()
                   if k in ("include", "define", "link", "compile_commands", "std", "extra_args")},
            },
            "required": ["source", "function"],
        },
    },
    {
        "name": "doctor",
        "description": (
            "Check that a checker is installed and sound: runs planted-bug programs "
            "the checker must reject, one per default check. Call this first if a "
            "verify or scan reports a tool error."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_TOOL_NAMES = {tool["name"] for tool in TOOLS}


def argv_for(tool: str, arguments: dict[str, Any]) -> list[str]:
    """The `veripp` command line a tool call stands for."""
    args = dict(arguments)
    argv: list[str] = [tool]
    if tool != "doctor":
        source = args.pop("source", None)
        if not isinstance(source, str) or not source:
            raise ValueError("`source` is required")
        argv.append(source)

    def flag(name: str, value: Any) -> None:
        option = "--" + name.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(option)
        elif isinstance(value, list):
            for item in value:
                argv.extend([option, str(item)])
        elif value is not None:
            argv.extend([option, str(value)])

    extra = args.pop("extra_args", None) or []
    for name, value in args.items():
        if name == "include":
            for item in value or []:
                argv += ["-I", str(item)]
        elif name == "define":
            for item in value or []:
                argv += ["-D", str(item)]
        elif name == "sequence_call":
            flag("sequence-call", value)
        elif name == "model" and value:
            argv += ["--model", str(value)]
        elif name == "changed" and value is not None:
            # Bare --changed means "against HEAD"; a ref means REF...HEAD.
            argv += ["--changed"] + ([str(value)] if value else [])
        else:
            flag(name, value)
    if tool in ("verify", "scan"):
        if "model" not in arguments or not arguments.get("model"):
            argv.append("--no-llm")
        argv.append("--json")
    # Last, so a deliberate flag wins over anything inferred above.
    argv += [str(item) for item in extra]
    return argv


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Run one tool and shape the outcome the way MCP wants it."""
    if name not in _TOOL_NAMES:
        return _error_result(f"unknown tool {name!r}; this server offers {sorted(_TOOL_NAMES)}")
    try:
        argv = argv_for(name, arguments or {})
    except ValueError as exc:
        return _error_result(str(exc))

    from .cli import main

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
    except SystemExit as exc:  # argparse rejected a flag
        code = exc.code if isinstance(exc.code, int) else 2
    except Exception as exc:  # a tool error is a result, not a protocol error
        return _error_result(f"veripp failed: {type(exc).__name__}: {exc}\n{err.getvalue()}")

    stdout, stderr = out.getvalue(), err.getvalue()
    structured: dict[str, Any] = {
        "command": " ".join(["veripp", *argv]),
        "exit_code": code,
        "meaning": EXIT_MEANING.get(code, "unknown exit code"),
    }
    if stderr.strip():
        structured["stderr"] = stderr.strip()
    if name in ("verify", "scan"):
        try:
            structured["report"] = json.loads(stdout)
        except json.JSONDecodeError:
            structured["stdout"] = stdout.strip()
    elif name == "harness":
        structured["harness"] = stdout
    else:
        structured["stdout"] = stdout.strip()

    text = json.dumps(structured, indent=2, default=str) if name != "harness" else (
        stdout if code == 0 else f"{stderr.strip()}\n(exit {code})"
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        # A counterexample or an inconclusive result is an answer, not an error;
        # only a usage error (nothing was checked) is.
        "isError": code == 2,
    }


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out (None for a notification)."""
    method = message.get("method")
    identifier = message.get("id")
    params = message.get("params") or {}
    if "id" not in message:
        return None  # notifications/initialized, notifications/cancelled, ...
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(identifier, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "veripp", "version": __version__},
            "instructions": (
                "veripp proves C and C++ functions free of undefined behaviour with "
                "the ESBMC model checker, or produces a counterexample. Every verdict "
                "is bounded and conditional on the assumptions it lists; quote them. "
                "Start with `doctor` if unsure a checker is installed."
            ),
        })
    if method == "ping":
        return _result(identifier, {})
    if method == "tools/list":
        return _result(identifier, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(identifier, -32602, "tools/call needs a tool `name`")
        return _result(identifier, call_tool(name, params.get("arguments") or {}))
    return _error(identifier, -32601, f"method not found: {method}")


def _result(identifier: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Answer messages until stdin closes. One JSON object per line, each way."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = _error(None, -32700, "parse error: not JSON")
        else:
            if not isinstance(message, dict):
                response = _error(None, -32600, "invalid request: expected an object")
            else:
                response = handle(message)
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()
    return 0
