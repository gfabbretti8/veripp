#!/usr/bin/env bash
# Re-capture the pinned ESBMC transcripts the parser tests run against.
# Run from the repository root after upgrading ESBMC, then read the diff:
# a change here is a change in the contract between veripp and the checker.
set -euo pipefail
cd "$(dirname "$0")/../.."
G=tests/golden
COMMON=(--std c++17 -I src/veripp/include -D__ESBMC__)

esbmc examples/ring_buffer.cpp "${COMMON[@]}" --unwind 8 --overflow-check > "$G/verified.txt" 2>&1 || true
esbmc examples/off_by_one.cpp  "${COMMON[@]}" --unwind 8 --overflow-check > "$G/counterexample.txt" 2>&1 || true
esbmc examples/ring_buffer.cpp "${COMMON[@]}" --unwind 3 --overflow-check > "$G/unwind_limit.txt" 2>&1 || true
# No -D__ESBMC__: the file's own main is compiled out, so there is no entry point.
esbmc examples/off_by_one.cpp  --std c++17 -I src/veripp/include --unwind 8          > "$G/conversion_error.txt" 2>&1 || true
# No -I: the contracts header cannot be found.
esbmc examples/off_by_one.cpp  --std c++17 --unwind 8                     > "$G/missing_include.txt" 2>&1 || true
esbmc "$G/syntax_error.cpp"    --std c++17 --unwind 8                     > "$G/parse_error.txt" 2>&1 || true
esbmc "$G/unknown.cpp"         --std c++17 --k-induction --max-k-step 3   > "$G/unknown.txt" 2>&1 || true

esbmc --version > "$G/esbmc-version.txt" 2>&1
