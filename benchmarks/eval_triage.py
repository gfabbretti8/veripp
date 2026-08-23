#!/usr/bin/env python3
"""Evaluate live LLM triage against the benchmark findings' known answers.

The triage pilot (2026-08-23) established ground truth for these targets by
reading call sites and validating every proposal with ESBMC. This script asks
the production AnthropicLLM the same questions and grades it.

Needs Anthropic credentials (ANTHROPIC_API_KEY or `ant auth login`) and the
benchmark corpus:  ./benchmarks/eval_triage.py [corpus_dir]
A corpus dir given without lodepng/stb checkouts gets them cloned into it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from veripp.agent import Budget, verify_with_agent  # noqa: E402
from veripp.esbmc import Outcome, VerifyConfig  # noqa: E402
from veripp.harness import HarnessOptions, generate  # noqa: E402
from veripp.llm import AnthropicLLM  # noqa: E402
from veripp.paths import contracts_include_dir, scratch_dir  # noqa: E402
from veripp.triage import TargetInfo  # noqa: E402

# target -> (expected classification, precondition expected to be accepted)
EXPECTATIONS = {
    ("lodepng/lodepng.cpp", "reverseBits"): ("missing_assumption", True),
    ("stb/stb_image_write.h", "stbiw__zlib_bitrev"): ("missing_assumption", True),
}
DEFINES = {"stb/stb_image_write.h": ["STB_IMAGE_WRITE_IMPLEMENTATION"]}


def ensure_corpus(root: Path) -> None:
    for repo in ("lvandeve/lodepng", "nothings/stb"):
        dest = root / repo.split("/")[1]
        if not dest.is_dir():
            subprocess.run(
                ["git", "clone", "-q", "--depth", "1",
                 f"https://github.com/{repo}.git", str(dest)],
                check=True,
            )


def main() -> int:
    corpus = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
    ensure_corpus(corpus)
    try:
        llm = AnthropicLLM()
    except RuntimeError as exc:
        print(f"cannot run: {exc}")
        return 2

    failures = 0
    for (rel, function), (expected_kind, expect_accepted) in EXPECTATIONS.items():
        source = corpus / rel
        print(f"\n=== {rel} --function {function} ===")
        target = TargetInfo(source=source, function=function)
        harness = generate(source, function, target.options)
        path = harness.write(scratch_dir())
        config = VerifyConfig(
            timeout_s=120,
            include_dirs=[contracts_include_dir(), source.parent],
            defines=DEFINES.get(rel, []),
        )
        report = verify_with_agent(
            path, config, llm=llm, budget=Budget(),
            assumptions=harness.assumptions, harness=path, target=target,
        )
        kind = report.diagnosis.kind if report.diagnosis else "(none)"
        accepted = bool(report.accepted_preconditions)
        ok_kind = kind == expected_kind
        ok_loop = accepted == expect_accepted
        print(f"  classification: {kind}  (expected {expected_kind})"
              f"  {'OK' if ok_kind else 'WRONG'}")
        print(f"  solver accepted a proposal: {accepted}"
              f"  (expected {expect_accepted})  {'OK' if ok_loop else 'WRONG'}")
        if report.accepted_preconditions:
            print(f"  accepted precondition(s): {report.accepted_preconditions}")
        if report.diagnosis:
            print(f"  explanation: {report.diagnosis.explanation[:300]}")
        failures += (not ok_kind) + (not ok_loop)

    print(f"\n{'PASS' if failures == 0 else f'FAIL ({failures} mismatches)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
