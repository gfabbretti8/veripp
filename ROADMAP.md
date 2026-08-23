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

## M2 — real projects
- libclang slicer: compile_commands.json -> self-contained TU per function.
- Sound havoc stubs for external calls.
- Cache keyed on function-body hash; `veripp verify-changed` for CI.

## M3 — sell it
- GitHub Action.
- Benchmarks: grown from `benchmarks/` (lodepng, stb_image_write today;
  tinyxml2/jsoncpp once the upstream ESBMC defects there are fixed),
  plus one reproduced CVE.
- CppCon lightning talk / Show HN.
