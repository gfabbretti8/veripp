# Roadmap

## M0 — scaffold (done)
ESBMC runner + output parser, agent state machine, contracts header, CLI.

## M1 — single-file MVP
- Harness generator: given `--function`, emit a main() with nondet inputs
  constrained by VERIPP_REQUIRES.
- Golden tests against pinned ESBMC output on the examples.
- Counterexample trace parser: full variable assignments, not just lines.

## M2 — real projects
- libclang slicer: compile_commands.json -> self-contained TU per function.
- Sound havoc stubs for external calls.
- Cache keyed on function-body hash; `veripp verify-changed` for CI.

## M3 — sell it
- GitHub Action.
- Benchmarks: ETL containers, a JSON parser hot path, one reproduced CVE.
- CppCon lightning talk / Show HN.
