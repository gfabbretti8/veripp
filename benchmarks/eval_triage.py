#!/usr/bin/env python3
"""Grade live LLM triage against known answers -- and compare models.

The triage pilot (2026-08-23) established ground truth for these targets by
reading call sites and validating every proposal with ESBMC. This asks the
production AnthropicLLM the same questions and scores it.

Because the solver checks every proposal, a wrong answer costs a retry rather
than soundness -- so the question is not "is this model reliable" but "how
often is it right, and is that worth its price". Run several models to find
out for your codebase:

    ./benchmarks/eval_triage.py --models claude-haiku-4-5,claude-sonnet-5,claude-opus-5

Works with any provider veripp supports, including a local model:

    ./benchmarks/eval_triage.py --models ollama:llama3.1        # no account needed
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from veripp.agent import Budget, verify_with_agent  # noqa: E402
from veripp.esbmc import VerifyConfig  # noqa: E402
from veripp.harness import HarnessOptions, generate  # noqa: E402
from veripp.llm import make_llm  # noqa: E402
from veripp.paths import contracts_include_dir, scratch_dir  # noqa: E402
from veripp.triage import TargetInfo  # noqa: E402

DEFAULT_MODELS = ["anthropic:claude-opus-5"]


@dataclass
class Case:
    repo: str
    rel: str
    function: str
    expected_kind: str
    expect_accepted: bool
    defines: list[str] = field(default_factory=list)
    note: str = ""


CASES = [
    Case(
        repo="lvandeve/lodepng", rel="lodepng/lodepng.cpp", function="reverseBits",
        expected_kind="missing_assumption", expect_accepted=True,
        note="callers pass FIRSTBITS=9 or a code length bounded by maxbitlen<=15",
    ),
    Case(
        repo="nothings/stb", rel="stb/stb_image_write.h", function="stbiw__zlib_bitrev",
        expected_kind="missing_assumption", expect_accepted=True,
        defines=["STB_IMAGE_WRITE_IMPLEMENTATION"],
        note="only ever called with the constants 5, 7, 8 and 9",
    ),
]


@dataclass
class Score:
    model: str
    correct_kind: int = 0
    correct_loop: int = 0
    total: int = 0
    seconds: float = 0.0
    detail: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"{self.model:22} classification {self.correct_kind}/{self.total}   "
            f"solver accepted {self.correct_loop}/{self.total}   "
            f"{self.seconds:5.1f}s"
        )


def ensure_corpus(root: Path) -> None:
    for repo in {c.repo for c in CASES}:
        dest = root / repo.split("/")[1]
        if not dest.is_dir():
            subprocess.run(
                ["git", "clone", "-q", "--depth", "1",
                 f"https://github.com/{repo}.git", str(dest)],
                check=True,
            )


def evaluate(model: str, corpus: Path, timeout: int, base_url: str | None) -> Score:
    score = Score(model=model)
    llm = make_llm(model, base_url)
    for case in CASES:
        source = corpus / case.rel
        if not source.is_file():
            score.detail.append(f"  {case.function}: SKIPPED (missing {source})")
            continue
        score.total += 1
        started = time.monotonic()
        target = TargetInfo(source=source, function=case.function)
        harness = generate(source, case.function, target.options)
        path = harness.write(scratch_dir())
        config = VerifyConfig(
            timeout_s=timeout,
            include_dirs=[contracts_include_dir(), source.parent],
            defines=case.defines,
        )
        report = verify_with_agent(
            path, config, llm=llm, budget=Budget(),
            assumptions=harness.assumptions, harness=path, target=target,
        )
        score.seconds += time.monotonic() - started

        kind = report.diagnosis.kind if report.diagnosis else "(none)"
        accepted = bool(report.accepted_preconditions)
        score.correct_kind += kind == case.expected_kind
        score.correct_loop += accepted == case.expect_accepted
        score.detail.append(
            f"  {case.function}: kind={kind} (want {case.expected_kind})"
            f"  accepted={accepted} (want {case.expect_accepted})"
            + (f"  -> {report.accepted_preconditions}" if accepted else "")
        )
        if report.vacuous:
            score.detail.append("    WARNING: the accepted precondition was vacuous")
    return score


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus", nargs="?", type=Path, help="where to clone the libraries")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS),
                    help="comma-separated model ids to compare")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--llm-base-url", help="endpoint for a provider not built in")
    args = ap.parse_args()

    corpus = args.corpus or Path(tempfile.mkdtemp())
    ensure_corpus(corpus)

    scores: list[Score] = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"\n=== {model} ===", flush=True)
        try:
            score = evaluate(model, corpus, args.timeout, args.llm_base_url)
        except RuntimeError as exc:  # no credentials, bad model id
            print(f"  cannot run: {exc}")
            continue
        for line in score.detail:
            print(line)
        scores.append(score)

    if not scores:
        print("\nNo model ran. Set ANTHROPIC_API_KEY or run `ant auth login`.")
        return 2

    print("\n" + "=" * 72)
    for score in scores:
        print(score.line())
    print(
        "\nA wrong proposal costs a retry, not soundness -- the solver rejects it.\n"
        "So compare hit rate against price, not correctness against perfection."
    )
    perfect = [s for s in scores if s.correct_kind == s.total == s.correct_loop]
    return 0 if perfect else 1


if __name__ == "__main__":
    raise SystemExit(main())
