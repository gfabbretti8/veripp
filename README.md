# veripp

**AI-operated formal verification for real C++ code.**

`veripp` wraps the [ESBMC](https://esbmc.org) model checker in an LLM agent loop so that a
regular C++ developer can run:

```bash
veripp verify src/parser.cpp --function parse_header
```

and get back one of three things:

1. **A proof** — the property holds (with the bounds and assumptions stated explicitly).
2. **A bug** — a concrete counterexample trace, minimized and explained in plain language.
3. **A question** — e.g. *"this fails only when `len == 0`; is `len > 0` a precondition?"*

The division of labor is strict: **the LLM only proposes, the solver disposes.**
Harnesses, loop invariants, and preconditions suggested by the model are always
checked by ESBMC before anything is reported. A hallucination costs a retry,
never soundness.

## Why

Model checkers like ESBMC and CBMC are sound and mature, but they need an expert
operator: someone to write the verification harness, guess loop invariants,
interpret counterexample traces, and decide how to escalate. That labor is why
formal verification is still a specialist activity. `veripp`'s bet is that an
LLM can be that operator, and the solver keeps it honest.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ 4. Interface     CLI · JSON output · GitHub Action ·     │
│                  cache keyed on function-body hash       │
├─────────────────────────────────────────────────────────┤
│ 3. Agent loop    attempt → triage → escalate (budgeted)  │
│                  LLM proposes harnesses / invariants /   │
│                  assumptions; ESBMC checks every one     │
├─────────────────────────────────────────────────────────┤
│ 2. Specification implicit properties (overflow, bounds,  │
│                  UB, null deref — free) + explicit       │
│                  contracts via include/veripp/contracts  │
├─────────────────────────────────────────────────────────┤
│ 1. Ingestion     compile_commands.json + libclang slice  │
│                  of the target function and its deps;    │
│                  external calls get sound havoc stubs    │
└─────────────────────────────────────────────────────────┘
```

## Status

Early scaffold. The ESBMC runner, output parser, and agent state machine
skeleton are in place; the libclang slicer and LLM triage are stubs being
built next. See `ROADMAP.md`.

## Requirements

- Python ≥ 3.10
- [ESBMC](https://github.com/esbmc/esbmc/releases) ≥ 7.x on `PATH`
- An Anthropic API key in `ANTHROPIC_API_KEY` (only needed for agent mode;
  `--no-llm` runs the plain verifier pipeline)

## Quick start

```bash
pip install -e .
veripp verify examples/ring_buffer.cpp --function rb_push
veripp verify examples/off_by_one.cpp --function sum_array   # finds a real bug
```

## Honest-reporting policy

Every "VERIFIED" result states its unwind bounds, stubbed calls, and harness
assumptions. Bounded results are labeled bounded. Overclaiming is how
verification tools lose trust permanently; we don't.

## License

Apache-2.0
