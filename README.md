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
│                  contracts via veripp/contracts.hpp      │
├─────────────────────────────────────────────────────────┤
│ 1. Ingestion     compile_commands.json + libclang slice  │
│                  of the target function and its deps;    │
│                  external calls get sound havoc stubs    │
└─────────────────────────────────────────────────────────┘
```

## Status

M1 (single-file MVP) works: the ESBMC runner and output parser are calibrated
against real ESBMC 8.4 output, and `--function` generates the verification
harness for you. The libclang slicer (multi-file projects) and LLM triage are
next. See `ROADMAP.md`.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python for you)
- [ESBMC](https://github.com/esbmc/esbmc/releases) ≥ 8.x on `PATH`
- An Anthropic API key in `ANTHROPIC_API_KEY` (only needed for agent mode;
  `--no-llm` runs the plain verifier pipeline)

ESBMC is a C++ binary, not a Python package, so uv cannot install it — that
one is on you:

```bash
brew install esbmc          # macOS
# or download esbmc-linux.zip from the releases page above and put it on PATH
```

`veripp doctor` checks all of the above and tells you what is missing.

## Quick start

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you do not have uv yet
git clone https://github.com/gfabbretti8/veripp && cd veripp

uv run veripp doctor
uv run veripp verify examples/ring_buffer.cpp --function push       # proves a postcondition
uv run veripp verify examples/off_by_one.cpp --function sum_array   # finds a real bug
```

`uv run` creates the environment on first use; there is no install step and
nothing to activate.

To put `veripp` on your `PATH` and use it on your own code:

```bash
uv tool install git+https://github.com/gfabbretti8/veripp
veripp verify src/parser.cpp --function parse_header
```

or run it once without installing anything:

```bash
uvx --from git+https://github.com/gfabbretti8/veripp veripp verify mycode.cpp --function f
```

Plain pip works too (`pip install -e .`); uv is a convenience, not a
dependency of the tool.

### Working on veripp

```bash
uv sync             # exact environment from uv.lock
uv run pytest -q    # tests needing esbmc skip themselves when it is absent
```

`--function f` generates a harness: nondeterministic values for every
parameter, a bound on any buffer length, and `f`'s own `VERIPP_REQUIRES`
preconditions hoisted in front of the call. Inspect it before you trust it:

```bash
veripp harness examples/off_by_one.cpp --function sum_array
```

Without `--function`, veripp verifies the file's own `main()` — useful when you
have written the harness yourself.

## What a result looks like

```
Result: counterexample
  bounded, unwind=8; checks: overflow, bounds, pointer, div-by-zero; std=c++17
Assumptions (a result is only as good as these):
  - `a` points to exactly `n` valid elements, with n <= 4 (harness bound on array length)
Violated property: dereference failure: array bounds violated
  at examples/off_by_one.cpp:7:9 in sum_array
Counterexample inputs:
  n = 4
  a_buf[0] = -1879048911
  ...
```

Exit codes: `0` verified, `1` counterexample, `2` usage error, `3` inconclusive.

## Honest-reporting policy

Every "VERIFIED" result states its unwind bounds, stubbed calls, and harness
assumptions. Bounded results are labeled bounded. Overclaiming is how
verification tools lose trust permanently; we don't.

Two places this bites in practice, both handled explicitly:

- ESBMC reports an exhausted unwind bound as `VERIFICATION FAILED` with an
  "unwinding assertion" property. That means *the bound was too small*, not
  *your code is broken*. veripp classifies it as inconclusive and widens the
  bound instead of reporting a bug.
- A generated harness always simplifies something — a bounded array length, a
  default-constructed receiver, a non-null pointer. Every such simplification
  is recorded and printed with the result. When the generator cannot model a
  parameter soundly it refuses to emit a harness rather than emit a
  plausible-looking wrong one.

## License

Apache-2.0
