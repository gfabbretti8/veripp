"""veripp command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .agent import Budget, verify_with_agent
from .esbmc import VerifyConfig, find_esbmc
from .llm import AnthropicLLM, NullLLM


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="veripp", description="AI-operated formal verification for C++")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="verify a self-contained C++ file")
    v.add_argument("source", type=Path)
    v.add_argument("--function", help="target function (used by the slicer; harness mode for now)")
    v.add_argument("--unwind", type=int, default=8)
    v.add_argument("--timeout", type=int, default=120, help="per-attempt timeout (s)")
    v.add_argument("--std", default="c++17")
    v.add_argument("-I", "--include", action="append", type=Path, default=[])
    v.add_argument("--no-llm", action="store_true", help="run the plain verifier pipeline offline")
    v.add_argument("--json", action="store_true", help="machine-readable output")

    d = sub.add_parser("doctor", help="check that dependencies are available")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor()

    if not args.source.exists():
        print(f"error: {args.source} not found", file=sys.stderr)
        return 2

    config = VerifyConfig(
        unwind=args.unwind,
        timeout_s=args.timeout,
        cpp_std=args.std,
        include_dirs=list(args.include),
    )
    llm = NullLLM() if args.no_llm else _make_llm()
    report = verify_with_agent(args.source, config, llm=llm, budget=Budget())

    if args.json:
        payload = {
            "outcome": report.final.outcome.value,
            "config": asdict(report.final.config),
            "violated_property": report.final.violated_property,
            "diagnosis": asdict(report.diagnosis) if report.diagnosis else None,
            "attempts": len(report.attempts),
        }
        payload = json.loads(json.dumps(payload, default=str))
        print(json.dumps(payload, indent=2))
    else:
        print(report.summary())

    return 0 if report.final.outcome.value == "verified" else 1


def _make_llm():
    try:
        return AnthropicLLM()
    except RuntimeError as exc:
        print(f"note: {exc}; falling back to offline mode", file=sys.stderr)
        return NullLLM()


def _doctor() -> int:
    esbmc = find_esbmc()
    print(f"esbmc: {esbmc or 'NOT FOUND — install from https://github.com/esbmc/esbmc/releases'}")
    try:
        import anthropic  # noqa: F401

        print("anthropic sdk: ok")
    except ImportError:
        print("anthropic sdk: not installed (offline mode only)")
    import os

    print(f"ANTHROPIC_API_KEY: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
