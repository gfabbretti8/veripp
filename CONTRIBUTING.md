# Contributing

## Setup

```bash
uv sync
uv run veripp doctor     # checks ESBMC, the contracts header, and soundness
uv run pytest -q
```

You need ESBMC built from master or the rolling
[`weekly`](https://github.com/esbmc/esbmc/releases/tag/weekly) build — **not**
the v8.4 release, which silently misses a class of bug veripp targets
([esbmc#6508](https://github.com/esbmc/esbmc/issues/6508)). On macOS,
`brew install --HEAD esbmc`. `veripp doctor` fails loudly if yours is affected.

Tests that need ESBMC are marked `esbmc` and skip themselves when it is absent,
so `pytest` works without it — but the interesting ones will not run.

## The one rule

**The LLM proposes; the solver disposes.** No output may depend on a model
being right. Every proposal that can change a verdict is re-checked by ESBMC,
and anything a harness simplified is reported with the result. Concretely:

- A "verified" states its unwind bound, its assumptions, any stubbed callees,
  and any known-unsound behaviour of the checker that produced it.
- A proof resting on assumptions is re-run to prove it was reachable at all,
  because an unreachable program satisfies everything.
- When the generator cannot model a parameter, it refuses. It never guesses a
  harness that looks plausible.

A change that makes output *look* better by saying less about its own limits
is the one kind of change that will not be merged.

## Working on the harness generator

`cppsig.py` is not a C++ parser and does not aim to be — it recovers what a
harness needs and refuses the rest. If you find a construct it mishandles, the
fix is usually to refuse it more clearly, not to parse more heroically.

Bugs here are best found by running against a real library rather than by
writing fixtures: every serious defect so far
(`typedef struct { ... } name;`, comment newlines leaking into generated code,
`struct S *s`, a C standard forwarded to a C++ harness) came from
`veripp scan` on real code and none from hand-written tests.

```bash
uv run veripp scan path/to/library.c --timeout 45 -j 8
```

## Golden transcripts

`tests/golden/` holds pinned ESBMC output; the parser tests read it as input
and never re-run the checker, so they work anywhere. Re-capture with
`tests/golden/capture.sh` after an ESBMC upgrade and read the diff — a change
there is a change in the contract between veripp and the checker.

## Adding an LLM provider

Implement `_ask` on a `PromptedLLM` subclass and add an entry to `PROVIDERS`.
Prompts are shared by every provider on purpose, and a test enforces that, so
adding a vendor cannot change what is asked. Most providers need no code at
all: if they speak the OpenAI chat-completions API, `--llm-base-url` already
reaches them.
