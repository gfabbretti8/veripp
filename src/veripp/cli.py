"""veripp command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .agent import AgentReport, Budget, verify_with_agent
from .cppsig import SignatureError
from .esbmc import Outcome, VerifyConfig, check_soundness, find_esbmc
from .harness import (
    Harness,
    HarnessError,
    HarnessOptions,
    generate,
    generate_sequence,
)
from .llm import AnthropicLLM, NullLLM
from .paths import contracts_include_dir, scratch_dir
from .triage import TargetInfo

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
    target = p.add_mutually_exclusive_group(required=require_function)
    target.add_argument(
        "--function",
        help="target function; veripp generates a harness for it. Overloads "
        "are picked by parameter types: --function 'f(int, unsigned)'. "
        "(omit to verify the file's own main)",
    )
    target.add_argument(
        "--class",
        dest="cls",
        help="target class; veripp drives a nondeterministic sequence of its "
        "public methods, so states built up across calls are explored",
    )
    p.add_argument(
        "--max-calls",
        type=int,
        default=HarnessOptions.max_calls,
        help="length of the generated call sequence for --class",
    )
    p.add_argument(
        "--assert",
        dest="assertions",
        action="append",
        default=[],
        metavar="EXPR",
        help="property checked after every call in a --class sequence; the "
        "object under test is named `veripp_obj`. Repeatable.",
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
    p.add_argument(
        "--include-file",
        action="append",
        default=[],
        metavar="HEADER",
        help="force-include a header before the source (e.g. a libc/typedef shim "
        "for a symbol esbmclibc lacks); repeatable",
    )
    p.add_argument(
        "--no-overflow-check",
        action="store_true",
        help="disable arithmetic overflow checking (isolate other properties)",
    )
    p.add_argument(
        "--assume",
        action="append",
        default=[],
        metavar="EXPR",
        help="add a precondition over the target's parameters (e.g. --assume "
        "'x1 != x0'); this is what LLM triage proposes automatically, exposed "
        "for manual use. Repeatable. Requires --function.",
    )


def _harness_options(args) -> HarnessOptions:
    return HarnessOptions(
        max_array_len=args.max_array_len,
        max_calls=getattr(args, "max_calls", HarnessOptions.max_calls),
    )


def _build_harness(args) -> Harness:
    if getattr(args, "cls", None):
        return generate_sequence(
            args.source,
            args.cls,
            _harness_options(args),
            assertions=list(getattr(args, "assertions", []) or []),
        )
    return generate(
        args.source,
        args.function,
        _harness_options(args),
        extra_preconditions=list(getattr(args, "assume", []) or []),
    )


def _verify(args) -> int:
    harness: Harness | None = None
    target = args.source
    if args.function or getattr(args, "cls", None):
        try:
            harness = _build_harness(args)
        except (HarnessError, SignatureError) as exc:
            what = args.function or args.cls
            print(f"error: cannot harness `{what}`: {exc}", file=sys.stderr)
            return EXIT_USAGE
        target = harness.write(scratch_dir())

    extra_args: list[str] = []
    for header in args.include_file:
        extra_args += ["--include-file", str(header)]
    config = VerifyConfig(
        unwind=args.unwind,
        timeout_s=args.timeout,
        cpp_std=args.std,
        include_dirs=_include_dirs(args),
        defines=list(args.define),
        overflow_check=not args.no_overflow_check,
        extra_args=extra_args,
    )
    llm = NullLLM() if args.no_llm else _make_llm()
    target_info = (
        TargetInfo(
            source=args.source,
            function=args.function,
            options=_harness_options(args),
        )
        if harness and args.function
        else None
    )
    report = verify_with_agent(
        target,
        config,
        llm=llm,
        budget=Budget(),
        assumptions=harness.assumptions if harness else [],
        harness=target if harness else None,
        target=target_info,
    )
    if harness and getattr(args, "assume", None):
        report.accepted_preconditions = list(args.assume) + report.accepted_preconditions

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
        "accepted_preconditions": report.accepted_preconditions,
        "harness": str(report.harness) if report.harness else None,
        "function": harness.signature.qualified_name if harness else None,
        "sequence": bool(harness and harness.class_info),
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
    unsound: list[str] = []
    if esbmc:
        print("soundness self-check (known-failing programs must be rejected):")
        try:
            for name, ok in check_soundness(esbmc).items():
                print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
                if not ok:
                    unsound.append(name)
        except RuntimeError as exc:
            print(f"  could not run: {exc}")
    include = contracts_include_dir()
    print(f"contracts header: {include / 'veripp/contracts.hpp' if include else 'NOT FOUND'}")
    try:
        import anthropic  # noqa: F401

        print("anthropic sdk: ok")
    except ImportError:
        print("anthropic sdk: not installed (offline mode only)")
    import os

    print(f"ANTHROPIC_API_KEY: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}")
    if unsound:
        print(
            "\nWARNING: this esbmc silently misses "
            + ", ".join(unsound)
            + ".\n'verified' results covering that pattern are NOT trustworthy. "
            "Upgrade esbmc (the member-array hole is esbmc/esbmc#6508, fixed "
            "upstream but not in 8.4; `brew install --HEAD esbmc` builds a "
            "fixed one).",
            file=sys.stderr,
        )
        return EXIT_INCONCLUSIVE
    return EXIT_VERIFIED if esbmc else EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
