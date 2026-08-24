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
│                  any LLM proposes preconditions; ESBMC   │
│                  checks every one, and vacuous proofs    │
│                  are rejected                            │
├─────────────────────────────────────────────────────────┤
│ 2. Specification implicit properties (overflow, bounds,  │
│                  UB, null deref — free) + explicit       │
│                  contracts via veripp/contracts.hpp      │
├─────────────────────────────────────────────────────────┤
│ 1. Ingestion     compile_commands.json for include paths │
│                  and defines; harnesses built from the   │
│                  signature; unlinked callees disclosed   │
└─────────────────────────────────────────────────────────┘
```

## Status

Working and used on real libraries. veripp harnesses a function, a whole class
(as a call sequence), or every function in a file; reads `compile_commands.json`;
triages counterexamples with any LLM; and refuses to call a vacuous or
unsoundly-obtained result a proof.

Measured on [lodepng](https://github.com/lvandeve/lodepng) (260 functions):
82% harnessable, 55 proved. See `ROADMAP.md` for what is not done, and
**Known limits** below for what to expect before you point it at your code.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python for you)
- [ESBMC](https://github.com/esbmc/esbmc/releases) built from master, or the
  rolling [`weekly`](https://github.com/esbmc/esbmc/releases/tag/weekly) build.
  **Not the v8.4 release** — it carries
  [esbmc#6508](https://github.com/esbmc/esbmc/issues/6508) and silently misses
  out-of-bounds writes in ordinary container code. `veripp doctor` checks this
  for you. On macOS: `brew install --HEAD esbmc`.
- An LLM, only for triage — see below. `--no-llm` runs the plain verifier
  pipeline with no model at all.

ESBMC is a C++ binary, not a Python package, so uv cannot install it — that
one is on you:

```bash
brew install esbmc          # macOS
# or download esbmc-linux.zip from the releases page above and put it on PATH
```

`veripp doctor` checks all of the above, tells you what is missing, and probes
your checker for known soundness holes.

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

Parameters that are structs or objects are built field by field with
nondeterministic values — nested structs recursively, array fields by loop,
pointer fields followed to `--max-struct-depth` (default 2) and then
null-terminated. Every simplification is reported as an assumption, including
the big one: an object with every field nondeterministic includes states no
real caller can produce, so a counterexample may be an unreachable object
state. Constrain it with `--assume 'w->count > 0'` and the solver checks the
property under that.

For a class, `--class` drives a bounded nondeterministic **sequence** of its
public methods, so states built up across calls are explored — not just the
first call on a fresh object:

```bash
veripp verify examples/ring_buffer.cpp --class RingBuffer --max-calls 6 \
    --assert 'veripp_obj.size() <= RingBuffer::capacity'
```

Without `--function` or `--class`, veripp verifies the file's own `main()` —
useful when you have written the harness yourself.

## Bring your own model

Triage works with any provider, and veripp needs no extra packages for most of
them — everything except Anthropic speaks the OpenAI-compatible HTTP API,
which veripp calls with the standard library.

```bash
veripp verify src/parser.cpp --function parse   --model openai:gpt-4o-mini
veripp verify src/parser.cpp --function parse   --model gemini:gemini-2.0-flash
veripp verify src/parser.cpp --function parse   --model groq:llama-3.3-70b-versatile
veripp verify src/parser.cpp --function parse   --model ollama:llama3.1   # local, no account
veripp verify src/parser.cpp --function parse   --model anthropic:claude-opus-5
```

Built-in: `anthropic`, `openai`, `gemini`, `groq`, `together`, `deepseek`,
`mistral`, `openrouter`, `ollama`, `lmstudio`. Anything else that speaks the
same API — a self-hosted gateway, vLLM, Azure — works with
`--llm-base-url https://…`. Defaults come from `$VERIPP_LLM_MODEL` and
`$VERIPP_LLM_BASE_URL`; `veripp doctor` lists which providers have credentials.

**A small model is a reasonable choice here.** Every proposal is re-checked by
ESBMC, so a wrong guess costs a retry, not a wrong answer.
`benchmarks/eval_triage.py --models a,b,c` scores providers against known
answers so you can pick on evidence.

## Scan a whole file

```bash
veripp scan src/parser.cpp
```

Harnesses and verifies every function veripp can model, then reports what it
proved, what produced counterexamples, and — importantly — what it could not
reach and why:

```
  PROVED             39  no overflow, out-of-bounds, null deref or division by
                         zero, within the stated bounds and assumptions
  COUNTEREXAMPLE     53  a property fails for some input -- triage each one
  INCONCLUSIVE       87  timed out, hit the unwind bound, or the frontend refused it
  NOT HARNESSABLE    81  veripp could not build inputs for the signature
```

`--json` for machine-readable output, `-j` for parallelism.

## Real projects

veripp reads your build system rather than making you restate it. Point it at
a source file and it finds the nearest `compile_commands.json` (including in
`build/`), and takes that file's include paths, defines and language standard
from it:

```bash
veripp verify src/area.cpp --function area
# note: using build/compile_commands.json (1 include dirs, 1 defines, -std=c++17)
```

Use `--compile-commands PATH` to choose one, `--no-compile-commands` to ignore
them.

**Linking matters for soundness, not convenience.** ESBMC gives an undefined
function a nondeterministic return value, but assumes it does not write
through its pointer arguments. So a callee whose definition is in another
translation unit is silently treated as side-effect-free. veripp detects those
callees itself — ESBMC reports them for C but not for C++ — and names them:

```
STUBBED CALLS (no body was available): normalize. Their effects were not
modelled, so this counterexample may be an artifact of the missing definition
rather than a real bug -- check it first.
```

Add the defining source with `--link src/helper.cpp` (repeatable) and the run
accounts for it. In the example above that is the difference between a false
counterexample and a proof.

## Vacuous proofs

A precondition that cannot be satisfied makes the target unreachable, and an
unreachable program satisfies every property. ESBMC answers *"does this hold
under these assumptions"* — it cannot notice the assumptions are impossible,
and neither can a model that proposed them. Since the whole design lets an LLM
suggest preconditions, that hole matters: a weak model fails toward
over-constraining, and the solver applauds.

So whenever a proof rests on assumptions, veripp re-runs the harness with a
deliberately false assertion at the end. A reachable harness must fail it; if
it verifies instead, nothing was checked:

```
Result: VACUOUS (nothing was actually checked)
  The assumptions made the call unreachable, so every property held trivially.
  This is NOT a proof. Weaken the precondition(s) below until the harness can run.
```

It exits non-zero, so a vacuous proof can never pass CI.

## Check your checker

```bash
veripp doctor
```

runs known-*failing* programs through your ESBMC and confirms it rejects them.
A model checker that answers "verified" on a program that provably fails is
worse than none, because every result built on it is a false proof. ESBMC 8.4
has one such hole ([esbmc#6508](https://github.com/esbmc/esbmc/issues/6508),
fixed upstream but unreleased): an out-of-bounds write to a member array is
missed when the index is another member of the same object reached through
`this` or a pointer — the ordinary container idiom. `doctor` fails loudly
rather than letting you build proofs on it.

## Killer example: a real CVE, found and fixed

```bash
./demo/cve-2019-13223/run.sh
```

veripp rediscovers [CVE-2019-13223](https://nvd.nist.gov/vuln/detail/CVE-2019-13223)
— a division-by-zero in stb_vorbis's `predict_point()`, reachable from a crafted
Ogg Vorbis file — on the real, unmodified upstream source, then proves that the
precondition the official fix enforces (`x1 != x0`) eliminates it. In agent mode
the triage proposes that precondition itself; the solver confirms it. See
[demo/cve-2019-13223](demo/cve-2019-13223/README.md).

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

## Known limits

Read this before judging the output.

- **Counterexamples need triage, and many are not bugs.** A generated harness
  gives a struct every possible field value, including combinations no caller
  can construct, so it will report failures that cannot happen in your program.
  On lodepng most counterexamples were artifacts of exactly this. The proofs
  are the trustworthy half; treat findings as leads, and use `--assume` (or an
  LLM) to state what real callers guarantee.
- **The released ESBMC is unsound for a common pattern.** v8.4 misses
  out-of-bounds writes to a member array indexed by another member of the same
  object ([esbmc#6508](https://github.com/esbmc/esbmc/issues/6508), fixed
  upstream, unreleased). `veripp doctor` detects it and every affected result
  says so. Use a master or `weekly` build.
- **C and C-like C++ only.** ESBMC's C++ frontend does not digest STL-heavy
  code; tinyxml2 crashes it and jsoncpp will not parse. Codecs, parsers and
  embedded-style code work well.
- **Bounded by default.** A proof covers executions within the unwind bound,
  which is stated with every result.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: the LLM proposes,
the solver disposes, and every result states what it assumed.

## License

Apache-2.0
