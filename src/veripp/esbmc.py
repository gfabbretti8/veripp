"""Runner and output parser for the ESBMC model checker.

This module is deliberately LLM-free: it builds command lines, runs the
verifier, and parses its output into structured results. It is the sole
source of truth for verification outcomes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Outcome(Enum):
    VERIFIED = "verified"            # VERIFICATION SUCCESSFUL
    COUNTEREXAMPLE = "counterexample"  # VERIFICATION FAILED with trace
    UNWIND_LIMIT = "unwind_limit"    # bound too small to conclude
    PARSE_ERROR = "parse_error"      # frontend rejected the input
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class VerifyConfig:
    """Parameters of one verification attempt. Recorded verbatim in reports."""

    unwind: int = 8
    timeout_s: int = 120
    k_induction: bool = False
    incremental_bmc: bool = False
    overflow_check: bool = True
    bounds_check: bool = True
    pointer_check: bool = True
    div_by_zero_check: bool = True
    extra_args: list[str] = field(default_factory=list)
    include_dirs: list[Path] = field(default_factory=list)
    cpp_std: str = "c++17"

    def to_args(self) -> list[str]:
        args: list[str] = ["--std", self.cpp_std]
        if self.k_induction:
            args.append("--k-induction")
        elif self.incremental_bmc:
            args.append("--incremental-bmc")
        else:
            args += ["--unwind", str(self.unwind)]
        if self.overflow_check:
            args.append("--overflow-check")
        # bounds/pointer checks are on by default in ESBMC; flags below
        # exist so a config can disable them explicitly.
        if not self.bounds_check:
            args.append("--no-bounds-check")
        if not self.pointer_check:
            args.append("--no-pointer-check")
        if self.div_by_zero_check:
            args.append("--div-by-zero-check")
        for inc in self.include_dirs:
            args += ["-I", str(inc)]
        args += self.extra_args
        return args


@dataclass
class TraceStep:
    file: str
    line: int
    text: str


@dataclass
class VerifyResult:
    outcome: Outcome
    config: VerifyConfig
    violated_property: str | None = None
    trace: list[TraceStep] = field(default_factory=list)
    raw_output: str = ""
    duration_s: float | None = None

    @property
    def is_conclusive(self) -> bool:
        return self.outcome in (Outcome.VERIFIED, Outcome.COUNTEREXAMPLE)


_PROPERTY_RE = re.compile(r"Violated property:\s*\n(.*?)(?:\n\n|\Z)", re.S)
_STATE_LINE_RE = re.compile(
    r"^State \d+ file (?P<file>\S+) line (?P<line>\d+)", re.M
)


def find_esbmc() -> str | None:
    return shutil.which("esbmc")


def run(source: Path, config: VerifyConfig, esbmc_bin: str | None = None) -> VerifyResult:
    """Run one ESBMC invocation on a self-contained source file."""
    binary = esbmc_bin or find_esbmc()
    if binary is None:
        raise RuntimeError(
            "esbmc not found on PATH. Install from "
            "https://github.com/esbmc/esbmc/releases"
        )
    cmd = [binary, str(source), *config.to_args()]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return VerifyResult(Outcome.TIMEOUT, config, raw_output=partial)

    output = proc.stdout + "\n" + proc.stderr
    return _parse_output(output, config)


def _parse_output(output: str, config: VerifyConfig) -> VerifyResult:
    if "VERIFICATION SUCCESSFUL" in output:
        return VerifyResult(Outcome.VERIFIED, config, raw_output=output)

    if "VERIFICATION FAILED" in output:
        m = _PROPERTY_RE.search(output)
        violated = m.group(1).strip() if m else None
        trace = [
            TraceStep(file=s.group("file"), line=int(s.group("line")), text="")
            for s in _STATE_LINE_RE.finditer(output)
        ]
        return VerifyResult(
            Outcome.COUNTEREXAMPLE,
            config,
            violated_property=violated,
            trace=trace,
            raw_output=output,
        )

    if "unwinding assertion" in output.lower():
        return VerifyResult(Outcome.UNWIND_LIMIT, config, raw_output=output)

    if re.search(r"(PARSING ERROR|error: |Conversion failed)", output):
        return VerifyResult(Outcome.PARSE_ERROR, config, raw_output=output)

    return VerifyResult(Outcome.UNKNOWN, config, raw_output=output)
