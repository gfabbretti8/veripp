"""veripp command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .agent import AgentReport, Budget, verify_with_agent
from .cppsig import SignatureError
from .esbmc import Outcome, VerifyConfig, find_esbmc
from .harness import Harness, HarnessError, HarnessOptions, generate
from .llm import AnthropicLLM, NullLLM
from .paths import contracts_include_dir, scratch_dir

EXIT_VERIFIED = 0
EXIT_COUNTEREXAMPLE = 1
EXIT_USAGE = 2
EXIT_INCONCLUSIVE = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="veripp", description="AI-operated formal verification for C++"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="verify a self-contained C++ file")
    _add_common_args(v)
    v.add_argument("--no-llm", action="store_true", help="run the plain verifier pipeline offline")
    v.add_argument("--json", action="store_true", help="machine-readable output")
    v.add_argument("--keep-harness", action="store_true", help="print where the harness was written")

    h = sub.add_parser("harness", help="print the generated harness without verifying")
    _add_common_args(h, require_function=True)

    sub.add_parser("doctor", help="check that dependencies are available")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor()
    if not args.source.exists():
        print(f"error: {args.source} not found", file=sys.stderr)
        return EXIT_USAGE

    if args.command == "harness":
        try:
            print(_build_harness(args).code, end="")
        except (HarnessError, SignatureError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_VERIFIED

    return _verify(args)


def _add_common_args(p: argparse.ArgumentParser, require_function: bool = False) -> None:
    p.add_argument("source", type=Path)
    p.add_argument(
        "--function",
        required=require_function,
        help="target function; veripp generates a harness for it "
        "(omit to verify the file's own main)",
    )
    p.add_argument("--unwind", type=int, default=8)
    p.add_argument("--timeout", type=int, default=120, help="per-attempt timeout (s)")
    p.add_argument("--std", default="c++17")
    p.add_argument(
        "--max-array-len",
        type=int,
        default=HarnessOptions.max_array_len,
        help="harness bound on generated buffer lengths",
    )
    p.add_argument("-I", "--include", action="append", type=Path, default=[])
    p.add_argument("-D", "--define", action="append", default=[], help="preprocessor macro")


def _build_harness(args) -> Harness:
    return generate(
        args.source,
        args.function,
        HarnessOptions(max_array_len=args.max_array_len),
    )


def _verify(args) -> int:
    harness: Harness | None = None
    target = args.source
    if args.function:
        try:
            harness = _build_harness(args)
        except (HarnessError, SignatureError) as exc:
            print(f"error: cannot harness `{args.function}`: {exc}", file=sys.stderr)
            return EXIT_USAGE
        target = harness.write(scratch_dir())

    config = VerifyConfig(
        unwind=args.unwind,
        timeout_s=args.timeout,
        cpp_std=args.std,
        include_dirs=_include_dirs(args),
        defines=list(args.define),
    )
    llm = NullLLM() if args.no_llm else _make_llm()
    report = verify_with_agent(
        target,
        config,
        llm=llm,
        budget=Budget(),
        assumptions=harness.assumptions if harness else [],
        harness=target if harness else None,
    )

    if args.json:
        print(json.dumps(_payload(report, harness), indent=2, default=str))
    else:
        print(report.summary())
        if harness and not args.keep_harness:
            print(f"(harness kept at {target})")

    return _exit_code(report)


def _include_dirs(args) -> list[Path]:
    dirs = list(args.include)
    bundled = contracts_include_dir()
    if bundled is not None and bundled not in dirs:
        dirs.append(bundled)
    parent = args.source.resolve().parent
    if parent not in dirs:
        dirs.append(parent)
    return dirs


def _payload(report: AgentReport, harness: Harness | None) -> dict:
    prop = report.final.violated_property
    return {
        "outcome": report.final.outcome.value,
        "bounded": not report.final.config.k_induction,
        "config": asdict(report.final.config),
        "assumptions": report.assumptions,
        "harness": str(report.harness) if report.harness else None,
        "function": harness.signature.qualified_name if harness else None,
        "violated_property": asdict(prop) if prop else None,
        "trace": [asdict(step) for step in report.final.trace],
        "diagnosis": asdict(report.diagnosis) if report.diagnosis else None,
        "narrative": report.narrative,
        "error": report.final.error,
        "attempts": len(report.attempts),
        "duration_s": report.final.duration_s,
    }


def _exit_code(report: AgentReport) -> int:
    outcome = report.final.outcome
    if outcome is Outcome.VERIFIED:
        return EXIT_VERIFIED
    if outcome is Outcome.COUNTEREXAMPLE:
        return EXIT_COUNTEREXAMPLE
    return EXIT_INCONCLUSIVE


def _make_llm():
    try:
        return AnthropicLLM()
    except RuntimeError as exc:
        print(f"note: {exc}; falling back to offline mode", file=sys.stderr)
        return NullLLM()


def _doctor() -> int:
    esbmc = find_esbmc()
    print(f"esbmc: {esbmc or 'NOT FOUND — brew install esbmc, or see esbmc.org'}")
    if esbmc:
        import subprocess

        version = subprocess.run([esbmc, "--version"], capture_output=True, text=True)
        print(f"  {version.stdout.strip() or version.stderr.strip()}")
    include = contracts_include_dir()
    print(f"contracts header: {include / 'veripp/contracts.hpp' if include else 'NOT FOUND'}")
    try:
        import anthropic  # noqa: F401

        print("anthropic sdk: ok")
    except ImportError:
        print("anthropic sdk: not installed (offline mode only)")
    import os

    print(f"ANTHROPIC_API_KEY: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}")
    return EXIT_VERIFIED if esbmc else EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
