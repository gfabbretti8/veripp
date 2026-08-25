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

Measured across nine popular C libraries — **40 of libpng's 70 functions
proved**, 99 of lodepng's 260, 32 of cJSON's 117 — free of overflow,
out-of-bounds, null dereference and division by zero for any input within the
stated bounds. Full table, and the veripp bugs each library exposed, in
[benchmarks/CORPUS.md](https://github.com/gfabbretti8/veripp/blob/main/benchmarks/CORPUS.md). See `ROADMAP.md` for what is not done, and
**Known limits** below for what to expect before you point it at your code.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python for you)
- [ESBMC](https://github.com/esbmc/esbmc/releases) built from master, or the
  [`weekly`](https://github.com/esbmc/esbmc/releases/tag/weekly) build (which,
  despite the name, is cut infrequently — check its date).
  **Not the v8.4 release** — it carries
  [esbmc#6508](https://github.com/esbmc/esbmc/issues/6508) and silently misses
  out-of-bounds writes in ordinary container code. `veripp doctor` checks this
  for you. On macOS: `brew install --HEAD esbmc`.
- An LLM, only for triage — see below. `--no-llm` runs the plain verifier
  pipeline with no model at all.

ESBMC is a C++ binary, not a Python package, so uv cannot install it. If you
would rather not think about that at all, use the image — it carries a checker
that has already passed the soundness probe at build time:

```bash
docker run --rm -v "$PWD:/src" ghcr.io/gfabbretti8/veripp scan src/parser.c
```

Otherwise install it yourself:

```bash
brew install --HEAD esbmc    # macOS. NOT `brew install esbmc`, which is 8.4.
# Linux x86_64: download esbmc-linux.zip from the `weekly` release above,
# unzip it, and put the binary on PATH.
# Linux arm64: no prebuilt ESBMC is published; use the image.
```

`veripp doctor` checks all of the above, tells you what is missing, prints the
right command for your machine and architecture, and probes your checker for
known soundness holes.

Shell completions are generated from the CLI itself, so they cannot fall out
of step with it:

```bash
eval "$(veripp completion bash)"      # or zsh
veripp completion fish | source
```

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

Parameters that are structs or objects are built by calling the library's own
initialiser when one can be found (`vec_init`, `HuffmanTree_init`), so the
object starts in a state that genuinely occurs. That is a narrower question
than "any object at all", and it is stated with the result.

With `--no-initializers`, or when no initialiser exists, they are built field
by field with nondeterministic values — nested structs recursively, array fields by loop,
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

## See it find a real CVE

```bash
./demo/cve-2019-13223/run.sh        # a few seconds, clones stb for you
```

veripp rediscovers [CVE-2019-13223](https://nvd.nist.gov/vuln/detail/CVE-2019-13223)
— a division-by-zero in stb_vorbis's `predict_point()`, reachable from a crafted
Ogg Vorbis file — on the real, unmodified upstream source, then proves that the
precondition the official fix enforces (`x1 != x0`) eliminates it. In agent mode
the triage proposes that precondition itself; the solver confirms it. See
[demo/cve-2019-13223](https://github.com/gfabbretti8/veripp/blob/main/demo/cve-2019-13223/README.md).

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

## In CI

```yaml
- uses: gfabbretti8/veripp@main
  with:
    source: src/parser.c
    fail-on: never      # report findings without failing, for a first run
```

It installs ESBMC (the `weekly` build, since the release is unsound for a
pattern veripp targets), runs `veripp doctor` so a broken checker fails the job
rather than producing quiet non-proofs, then scans. Set `function:` to verify
one target, `args:` for `-I`/`-D`/`--link`/`--unwind`.

Start with `fail-on: never`. A generated harness produces leads that still need
triage, and a verifier that reddens the build on its first day gets removed on
its second.

## In a container

```bash
docker run --rm -v "$PWD:/src" ghcr.io/gfabbretti8/veripp scan src/parser.c
```

Nothing to install, and the checker inside has already been probed: the image
build runs `veripp doctor` and fails if the bundled ESBMC cannot find a planted
bug, so an unsound image is never published.

The image is genuinely multi-architecture, and the two halves are not built the
same way. ESBMC publishes a prebuilt Linux binary for x86_64 only. The one
prebuilt arm64 Linux ESBMC that exists anywhere is the Homebrew bottle, pinned
to the 8.4 release that silently misses out-of-bounds writes
([esbmc#6508](https://github.com/esbmc/esbmc/issues/6508)) — shipping that
would trade a loud failure for a quiet one. So the arm64 image compiles ESBMC
from source, and arm64 users get the same soundness guarantee as everyone else.

The container runs as a non-root user and never writes to your tree, so the
mount can be read-only:

```bash
docker run --rm -v "$PWD:/src:ro" ghcr.io/gfabbretti8/veripp \
    verify src/img.c --function scale --assume 'w > 0 && h > 0'
```

`--compile-commands` works too, even though your `compile_commands.json`
records absolute paths from your machine and the tree is mounted at `/src`
inside. veripp finds the entry by its trailing path components and rebases the
whole thing — include dirs included — onto wherever the tree actually is. The
same applies to a CI checkout that does not sit where the database was
generated. If two entries match equally well it says so rather than guess.

The container runs as a non-root user, which means it cannot read a project
directory that is not readable by others — a tree under a `0700` home
directory, typically. If `/src` comes up unreadable, veripp says so and you can
run as yourself instead:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/src:ro" \
    ghcr.io/gfabbretti8/veripp scan src/parser.c
```

Roughly 450 MB (amd64) and 530 MB (arm64), most of it ESBMC itself.

## As a skill for coding agents

In Claude Code, install it from this repository:

```
/plugin marketplace add gfabbretti8/veripp
/plugin install veripp@veripp
```

For any other agent, copy [`skills/veripp`](https://github.com/gfabbretti8/veripp/blob/main/skills/veripp) wherever it keeps
skills. Either way the agent will reach for veripp when it is asked to verify
or prove something about C/C++.

The skill exists to correct one specific instinct. An agent that knows ESBMC
will try to hand-write a `main()` full of `__ESBMC_nondet_int()`, sprinkle
`__ESBMC_assume(...)`, and annotate loops with invariants. veripp already
generates all of that from the signature, and re-checks it. The skill's first
instruction is that the agent does not write the harness — plus how to read a
bounded proof, and why a vacuous result is not a pass.

## Scan a whole project

```bash
veripp scan src/            # every .c/.cc/.cpp/.cxx underneath
veripp scan . --jobs 8
```

Build trees, vendored dependencies and dotted directories are skipped
(`build/`, `node_modules/`, `third_party/`, `.git/`, ...), and headers are
left alone because definitions live in the source file. The file count is
printed before the work starts, findings are grouped by file, and the exit
code is 1 if anything anywhere failed.

## Scan a whole file

```bash
veripp scan src/parser.cpp
```

Harnesses and verifies every function veripp can model, then reports what it
proved, what produced counterexamples, and — importantly — what it could not
reach and why:

```
  PROVED             99  no overflow, out-of-bounds, null deref or division by
                         zero, within the stated bounds and assumptions
  COUNTEREXAMPLE     61  a property fails for some input -- triage each one
  HARNESS ARTIFACT    3  failed because of how the harness was built, not the code
  INCONCLUSIVE       52  timed out, hit the unwind bound, or the frontend refused it
  NOT HARNESSABLE    47  veripp could not build inputs for the signature
```

(That is real output from `veripp scan` on lodepng.cpp.)

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

- **Counterexamples need triage; not all are bugs.** Where an object has no
  initialiser to build it from, the harness gives it every possible field
  value, including combinations no caller can construct — so it can report a
  failure that cannot happen in your program. veripp filters the mechanically
  decidable cases into a separate "harness artifact" count, but the rest are
  leads, not findings. The proofs are the trustworthy half. Use `--assume` (or
  an LLM) to state what real callers guarantee.
- **The released ESBMC is unsound for a common pattern.** v8.4 misses
  out-of-bounds writes to a member array indexed by another member of the same
  object ([esbmc#6508](https://github.com/esbmc/esbmc/issues/6508), fixed
  upstream, unreleased). `veripp doctor` detects it and every affected result
  says so. Use a master or `weekly` build.
- **C and C-like C++ only.** ESBMC's C++ frontend does not digest STL-heavy
  code; tinyxml2 crashes it and jsoncpp will not parse. Codecs, parsers and
  embedded-style code work well.
- **How much of a file veripp can reach varies.** lodepng 82%, cJSON 89%,
  parson 91%, tinyexpr 96%. Types whose definition is not in the translation
  unit cannot be constructed, and those functions are refused with the reason
  given. Run `veripp scan` on your own code to find out.
- **Bounded by default.** A proof covers executions within the unwind bound,
  which is stated with every result.

## Contributing

See [CONTRIBUTING.md](https://github.com/gfabbretti8/veripp/blob/main/CONTRIBUTING.md). The short version: the LLM proposes,
the solver disposes, and every result states what it assumed.

## License

Apache-2.0
