# Roadmap

## M0 — scaffold (done)
ESBMC runner + output parser, agent state machine, contracts header, CLI.

## M1 — single-file MVP (done, ESBMC 8.4)
- [x] Harness generator: given `--function`, emit a main() with nondet inputs
      constrained by VERIPP_REQUIRES (`veripp harness` prints it for review).
- [x] Golden tests against pinned ESBMC output on the examples
      (`tests/golden/`, re-captured by `tests/golden/capture.sh`).
- [x] Counterexample trace parser: full variable assignments, not just lines.
- [x] Parser calibrated against the real checker: an exhausted unwind bound is
      no longer misread as a counterexample, `VERIFICATION UNKNOWN` and tool
      errors are distinguished, and `-D__ESBMC__` is passed (ESBMC does not
      predefine it).

Known M1 limits, all disclosed at runtime rather than papered over:
- One call from a default-constructed receiver for member functions; call
  sequences need a hand-written harness.
- Scalars, and pointers/references to scalars, are the only parameter types the
  generator will model; anything else is refused.
- Buffer lengths are bounded (`--max-array-len`, default 4).
- Overloaded targets are refused rather than guessed at.

## M1.5 — C++ objects (done)
- [x] `--class`: bounded nondeterministic sequences over a class's public
      methods, replacing the "exactly one call on a fresh object" limitation.
      Catches state-dependent bugs a single-call harness proves "verified".
- [x] `--assert`: properties checked after every call in a sequence.
- [x] `veripp doctor` soundness self-check: known-failing programs must be
      rejected, so a checker with a false-negative hole cannot silently back
      a "verified" result (esbmc#6508 is one such hole in 8.4).

## M1.6 — objects as inputs (done)
- [x] Struct/class parameters built field by field: nested structs, array
      fields, pointer fields to a depth bound, enums, and honest refusal for
      opaque types. Struct definitions are resolved through the TU's own
      `#include "..."` headers, where real libraries keep them.
- [x] Preconditions may talk about fields (`--assume 'w->count > 0'`).
- [x] Counterexample rendering for objects: array writes collapsed, one line
      per location, long struct dumps truncated.
- Measured on lodepng: harnessable functions went 20.8% -> 65.8%, and the
  "pointer to non-scalar" refusal went 66.2% -> 23.1%.

## M2 — real projects
- [x] compile_commands.json: include paths, defines and -std taken from the
      build system, auto-discovered near the source.
- [x] `--link`: compile other translation units alongside the harness, and
      detect/disclose callees that are declared but never defined (ESBMC
      reports these for C but not C++, so veripp works them out itself).
- [x] `veripp scan`: harness and verify every function in a file, and report
      what could not be reached and why.
- [ ] Cache keyed on function-body hash; `veripp verify-changed` for CI.
- Note: the libclang slicer this milestone originally called for was dropped.
  The measured blocker was never TU assembly -- tinyxml2 is a single .cpp and
  still failed -- it was type visibility and frontend gaps, addressed by
  following the compilation database's include paths instead.

## M1.7 — trusting the proposals (done)
- [x] Vacuity check: a proof resting on assumptions is re-run with a false
      assertion, so unsatisfiable preconditions are reported as VACUOUS
      instead of passing. The solver cannot catch this on its own.
- [x] `benchmarks/eval_triage.py --models a,b,c` scores triage per model
      against the pilot's ground truth. Because the solver rejects wrong
      proposals, the question is hit rate against price, not correctness.
- [ ] Call-site validation of a proposed precondition (mechanical for literal
      arguments), to catch over-tight but satisfiable invariants.

## M3 — sell it
- GitHub Action.
- Benchmarks: grown from `benchmarks/` (lodepng, stb_image_write today;
  tinyxml2/jsoncpp once the upstream ESBMC defects there are fixed),
  plus one reproduced CVE.
- CppCon lightning talk / Show HN.
