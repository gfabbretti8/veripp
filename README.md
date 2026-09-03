# veripp

**AI-operated formal verification for real C and C++ code.**

veripp proves that a function is free of undefined behaviour — arithmetic
overflow, out-of-bounds access, null and invalid pointers, division by zero,
memory leaks, uninitialised reads, undefined shifts, NaN — or hands you a
concrete input that breaks it. You point it at a function; it generates the
verification harness, picks the properties, runs the [ESBMC](https://esbmc.org)
model checker, and widens the bounds until it can answer.

```bash
veripp verify src/parser.cpp --function parse_header
```

That returns one of three things:

1. **A proof** — the properties hold, with the bounds and assumptions stated
   explicitly.
2. **A bug** — a concrete counterexample trace, minimized and explained in
   plain language.
3. **A question** — e.g. *"this fails only when `len == 0`; is `len > 0` a
   precondition?"*

## Why

Model checkers like ESBMC and CBMC are sound and mature, but they need an
expert operator: someone to write the verification harness, guess loop
invariants, interpret counterexample traces, and decide how to escalate. That
labor is why formal verification is still a specialist activity.

veripp's bet is that an LLM can be that operator — and the solver keeps it
honest. The division of labor is strict: **the LLM only proposes, the solver
disposes.** Harnesses, loop invariants, and preconditions suggested by the
model are always checked by ESBMC before anything is reported. A hallucination
costs a retry, never soundness. The LLM is also optional: `--no-llm` runs the
plain verifier pipeline with no model at all.

## Examples

Find a real off-by-one:

```bash
$ veripp verify examples/off_by_one.cpp --function sum_array
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

Prove a postcondition:

```bash
veripp verify examples/ring_buffer.cpp --function push
```

Scan a whole file or project — every function veripp can harness:

```bash
$ veripp scan src/lodepng.cpp
  PROVED             99  no overflow, out-of-bounds, null deref or division by
                         zero, within the stated bounds and assumptions
  COUNTEREXAMPLE     61  a property fails for some input -- triage each one
  HARNESS ARTIFACT    3  failed because of how the harness was built, not the code
  INCONCLUSIVE       52  timed out, hit the unwind bound, or the frontend refused it
  NOT HARNESSABLE    47  veripp could not build inputs for the signature
```

Or watch it rediscover a real CVE on unmodified upstream source:

```bash
./demo/cve-2019-13223/run.sh        # a few seconds, clones stb for you
```

That run finds [CVE-2019-13223](https://nvd.nist.gov/vuln/detail/CVE-2019-13223)
— a division-by-zero in stb_vorbis's `predict_point()`, reachable from a
crafted Ogg Vorbis file — then proves that the precondition the official fix
enforces (`x1 != x0`) eliminates it. In agent mode the triage proposes that
precondition itself; the solver confirms it. Details in
[demo/cve-2019-13223](https://github.com/gfabbretti8/veripp/blob/main/demo/cve-2019-13223/README.md).

Exit codes are CI-friendly: `0` verified, `1` counterexample, `2` usage error,
`3` inconclusive. **An inconclusive run is not a pass.**

## Installation

```bash
pip install veripp
```

On Linux that is the whole installation: the ESBMC checker comes with it, as
a platform wheel. Confirm with `veripp doctor`.

Python 3.10+ is the only prerequisite. An LLM is optional, and used only for
triage.

### Where no checker wheel is published yet

macOS and Windows have no bundled checker, so `pip install veripp` gives you
the tool without one, and `veripp doctor` will say so. Fetch it with:

```bash
veripp install-checker
```

which downloads the
[`weekly`](https://github.com/esbmc/esbmc/releases/tag/weekly) build, runs
known-*failing* programs through it, and keeps it **only** if the checker
rejects every one. A binary that misses a planted bug is deleted rather than
installed, because every result built on it would be a false proof.

Where not even that is possible it names the alternative:

```bash
brew install --HEAD esbmc    # macOS. NOT `brew install esbmc`, which is 8.4.
```

**Not the v8.4 release**: it carries
[esbmc#6508](https://github.com/esbmc/esbmc/issues/6508) and silently misses
out-of-bounds writes in ordinary container code.

### Checking your setup

```bash
veripp doctor
```

`doctor` verifies everything is present, prints the right install command for
your machine and architecture, and — more importantly — probes your checker
for known soundness holes by running known-*failing* programs through it and
confirming it rejects them. A checker that answers "verified" on a program
that provably fails is worse than none.

### Or skip all of that: the container

The image bundles a checker that has already passed the soundness probe at
build time — an unsound image is never published:

```bash
docker run --rm -v "$PWD:/src" ghcr.io/gfabbretti8/veripp scan src/parser.c
```

It is multi-architecture (about 450 MB on amd64, 530 MB on arm64 — most of it
ESBMC), runs as a non-root user, and never writes to your tree, so the mount
can be read-only. See [Container notes](#container-notes) below.

### 3. An LLM (optional)

Only used for triage, and any provider works — see
[Bring your own model](#bring-your-own-model). Without one, `--no-llm` runs
the deterministic pipeline.

### Shell completions

Generated from the CLI itself, so they cannot fall out of step with it:

```bash
eval "$(veripp completion bash)"      # or zsh
veripp completion fish | source
```

## Usage

### Verify one function

```bash
veripp verify src/parser.cpp --function parse_header
```

Use `--assume 'len > 0'` to state what real callers guarantee, `--unwind N`
to widen the loop bound, `--link src/helper.cpp` to bring in callees defined
in other translation units.

### Turn a counterexample into a program that crashes

```bash
veripp verify src/parser.c --function parse --repro repro.c
```

writes a standalone file with the counterexample's own inputs, plus the build
line to compile it with AddressSanitizer and UBSan. A trace asks you to trust
that the harness modelled your function fairly; a program that crashes asks
nothing. It also checks itself — a repro that **exits cleanly** under the
sanitizers is what a harness artifact looks like from outside, an input no
real caller can construct.

### Verify only what you changed

```bash
veripp scan . --changed                  # vs HEAD: staged, unstaged, untracked
veripp scan . --changed origin/main      # everything this branch adds
```

Scanning a tree is a nightly job; scanning what a commit touches fits in front
of every commit. Finding nothing to verify exits 0, so this is safe as a gate.
As a [pre-commit](https://pre-commit.com) hook:

```yaml
repos:
  - repo: https://github.com/gfabbretti8/veripp
    rev: v0.5.0
    hooks:
      - id: veripp            # or: veripp-docker, which needs only Docker
```

### Scan a file or a whole project

```bash
veripp scan src/parser.cpp          # every function in one file
veripp scan src/                    # every .c/.cc/.cpp/.cxx underneath
veripp scan . --jobs 8
veripp scan src/ --only 'parse_*'   # just the ones you are working on
```

Build trees, vendored dependencies and dotted directories are skipped
(`build/`, `node_modules/`, `third_party/`, `.git/`, ...). Findings are
grouped by file, `--json` gives machine-readable output, and the exit code is
1 if anything anywhere failed.

Functions the first pass cannot settle are re-tried, cheapest first, under a
wall-clock budget (`--retry-budget`, default 120 seconds), with no LLM
involved: an exhausted bound restarts one widening past the widest already
tried and escalates to k-induction, and a timeout gets four times the time
with incremental BMC. Retries that the remaining budget cannot afford are
skipped rather than half-tried, and the summary says how many the second
attempt settled. On a file whose individual checker runs are expensive, the
default budget fits few retries — raise it when you can wait.

With an LLM configured (`--model`, or `$VERIPP_LLM_MODEL`), scan then triages
its counterexamples through the same agent loop `verify` uses: the model
classifies each one, may propose a precondition, and the solver re-checks
every proposal — vacuity probe included — before anything in the report
changes. A counterexample that disappears under a solver-accepted
precondition is reported as **PRECONDITIONED**, listed with the precondition
to confirm against your callers, and never folded into PROVED. The rest are
ranked with the model's verdict attached, likely real bugs first. The
mechanical pass stays LLM-free, so functions that prove outright never cost
an API call; `--no-llm` skips triage entirely.

Repeat runs are cached: a second scan reuses verdicts for files that have not
changed, which is what makes this affordable on every push rather than
nightly. The cache key covers the file, the local headers it includes, any
linked sources, the bounds, the harness options and the checker's own version
— so a stale verdict cannot be served. `--no-cache` verifies everything;
`--cache DIR` moves it.

### Adopting veripp on an existing codebase

The first run on existing code reports everything at once — cJSON gives 33
counterexamples. Record what is already there, then fail only on what appears
afterwards:

```bash
veripp accept src/ --baseline .veripp-baseline   # commit this
veripp scan   src/ --baseline .veripp-baseline   # exits 1 only on new findings
```

The baseline is sorted JSON, meant to be reviewed in the pull request that
adds it: each entry is a risk someone decided to carry, with the signature
and date recorded beside it. Findings are keyed on (file, function, property)
rather than line numbers, so moving code around does not resurrect an
accepted finding. When one stops occurring, veripp says so — an entry that
matches nothing still grants permission to whatever matches it later.

### Real build setups

veripp reads your build system rather than making you restate it. Point it at
a source file and it finds the nearest `compile_commands.json` (including in
`build/`) and takes include paths, defines and language standard from it:

```bash
veripp verify src/area.cpp --function area
# note: using build/compile_commands.json (1 include dirs, 1 defines, -std=c++17)
```

`--compile-commands PATH` chooses one explicitly; `--no-compile-commands`
ignores them.

**Linking matters for soundness, not convenience.** ESBMC gives an undefined
function a nondeterministic return value but assumes it does not write
through its pointer arguments — so a callee defined in another translation
unit is silently treated as side-effect-free. veripp detects those callees
itself (ESBMC reports them for C but not C++) and names them:

```
STUBBED CALLS (no body was available): normalize. Their effects were not
modelled, so this counterexample may be an artifact of the missing definition
rather than a real bug -- check it first.
```

Add the defining source with `--link src/helper.cpp` (repeatable). That can
be the difference between a false counterexample and a proof.

### Bring your own model

Triage works with any provider, and veripp needs no extra packages for most
of them — everything except Anthropic speaks the OpenAI-compatible HTTP API,
which veripp calls with the standard library.

```bash
veripp verify src/parser.cpp --function parse   --model openai:gpt-4o-mini
veripp verify src/parser.cpp --function parse   --model gemini:gemini-3.6-flash
veripp verify src/parser.cpp --function parse   --model groq:llama-3.3-70b-versatile
veripp verify src/parser.cpp --function parse   --model ollama:llama3.1   # local, no account
veripp verify src/parser.cpp --function parse   --model anthropic:claude-opus-5
```

Built-in: `anthropic`, `openai`, `gemini`, `groq`, `together`, `deepseek`,
`mistral`, `openrouter`, `ollama`, `lmstudio`. Anything else that speaks the
same API — a self-hosted gateway, vLLM, Azure — works with
`--llm-base-url https://…`. Defaults come from `$VERIPP_LLM_MODEL` and
`$VERIPP_LLM_BASE_URL`; `veripp doctor` lists which providers have
credentials.

A small model is a reasonable choice here: every proposal is re-checked by
ESBMC, so a wrong guess costs a retry, not a wrong answer.
`benchmarks/eval_triage.py --models a,b,c` scores providers against known
answers so you can pick on evidence. How well the triage works is measured,
not asserted — the path is validated end to end against a real model, a 7B
local model scores 0/2 on the benchmark, and no hosted model has been graded
here yet. [benchmarks/TRIAGE.md](https://github.com/gfabbretti8/veripp/blob/main/benchmarks/TRIAGE.md) states exactly what is
and is not evidenced.

## In CI

```yaml
- uses: gfabbretti8/veripp@main
  with:
    source: src/parser.c
    fail-on: never      # report findings without failing, for a first run
```

The action installs ESBMC (the `weekly` build, since the release is unsound
for a pattern veripp targets), runs `veripp doctor` so a broken checker fails
the job rather than producing quiet non-proofs, then scans. Set `function:`
to verify one target, `args:` for `-I`/`-D`/`--link`/`--unwind`.

A full workflow with a baseline and SARIF annotations on the pull-request
diff:

```yaml
name: verify
on: [pull_request]

permissions:
  contents: read
  security-events: write      # required to upload SARIF

jobs:
  veripp:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: gfabbretti8/veripp@main
        id: verify
        continue-on-error: true    # let the SARIF upload run either way
        with:
          source: src/
          baseline: .veripp-baseline
          sarif: veripp.sarif

      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: veripp.sarif

      - name: Fail on new findings
        if: steps.verify.outcome == 'failure'
        run: exit 1
```

`continue-on-error` plus the explicit final step is deliberate: without it a
finding fails the job before the SARIF is uploaded, and the annotations that
explain the failure never appear on the diff. Findings covered by the
baseline are uploaded as *suppressed* rather than omitted, so code scanning
shows them as accepted rather than pretending they are gone.

`fail-on: never` remains for a first look, but a check that can never fail is
a check nobody reads.

## As a skill for coding agents

The quickest way, needing nothing but Node:

```bash
npx veripp-skill            # this project
npx veripp-skill --global   # every project
```

That installs the skill only; the verifier is a Python program that needs
ESBMC, and the skill carries a script that works out how to get it and
reports what that would cost before touching anything.

In Claude Code you can also install it as a plugin:

```
/plugin marketplace add gfabbretti8/veripp
/plugin install veripp@veripp
```

For any other agent, copy [`skills/veripp`](https://github.com/gfabbretti8/veripp/blob/main/skills/veripp) wherever it keeps
skills.

The skill exists to correct one specific instinct. An agent that knows ESBMC
will try to hand-write a `main()` full of `__ESBMC_nondet_int()`, sprinkle
`__ESBMC_assume(...)`, and annotate loops with invariants. veripp already
generates all of that from the signature, and re-checks it. The skill's first
instruction is that the agent does not write the harness — plus how to read a
bounded proof, and why a vacuous result is not a pass.

## How it works

```
┌─────────────────────────────────────────────────────────┐
│ 4. Interface     CLI · JSON output · GitHub Action ·     │
│                  cache keyed on file + headers + bounds  │
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

### Honest reporting

Every "VERIFIED" result states its unwind bounds, stubbed calls, and harness
assumptions. Bounded results are labeled bounded. Overclaiming is how
verification tools lose trust permanently. Two places this bites in practice,
both handled explicitly:

- ESBMC reports an exhausted unwind bound as `VERIFICATION FAILED` with an
  "unwinding assertion" property. That means *the bound was too small*, not
  *your code is broken*. veripp classifies it as inconclusive and widens the
  bound instead of reporting a bug.
- A generated harness always simplifies something — a bounded array length, a
  default-constructed receiver, a non-null pointer. Every such simplification
  is recorded and printed with the result. When the generator cannot model a
  parameter soundly, it refuses to emit a harness rather than emit a
  plausible-looking wrong one.

### Vacuous proofs are rejected

A precondition that cannot be satisfied makes the target unreachable, and an
unreachable program satisfies every property. ESBMC answers *"does this hold
under these assumptions"* — it cannot notice the assumptions are impossible,
and neither can the model that proposed them. Since the design lets an LLM
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

### Container notes

The image is genuinely multi-architecture, and the two halves are not built
the same way. ESBMC publishes a prebuilt Linux binary for x86_64 only; the
one prebuilt arm64 Linux ESBMC that exists anywhere is the Homebrew bottle,
pinned to the unsound 8.4 release. So the arm64 image compiles ESBMC from
source, and arm64 users get the same soundness guarantee as everyone else.

The container never writes to your tree, so the mount can be read-only:

```bash
docker run --rm -v "$PWD:/src:ro" ghcr.io/gfabbretti8/veripp \
    verify src/img.c --function scale --assume 'w > 0 && h > 0'
```

`--compile-commands` works even though your `compile_commands.json` records
absolute paths from your machine and the tree is mounted at `/src` inside:
veripp finds the entry by its trailing path components and rebases the whole
thing — include dirs included — onto wherever the tree actually is. The same
applies to a CI checkout that does not sit where the database was generated.
If two entries match equally well, it says so rather than guess.

The container runs as a non-root user, which means it cannot read a project
directory that is not readable by others — a tree under a `0700` home
directory, typically. If `/src` comes up unreadable, veripp says so, and you
can run as yourself instead:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/src:ro" \
    ghcr.io/gfabbretti8/veripp scan src/parser.c
```

## Results on real libraries

Measured across nine popular C libraries: **40 of libpng's 70 functions
proved**, 99 of lodepng's 260, 32 of cJSON's 117 — free of overflow,
out-of-bounds, null dereference and division by zero for any input within the
stated bounds.

Those counts were taken with the four checks veripp ran at the time; it now
checks eight properties by default. Re-running cJSON with the same build and
bounds, changing only the check set: 32 proved under four checks, 31 under
eight. The stricter set cost exactly one proof — to `internal_malloc`, whose
body is `return malloc(size)` and which the memory-leak check flags because
the *harness* never frees. veripp labels that a harness artifact rather than
a bug.

The full table, per-check false-positive measurements, and the veripp bugs
each library exposed are in [benchmarks/CORPUS.md](https://github.com/gfabbretti8/veripp/blob/main/benchmarks/CORPUS.md).
What is not done yet is in [ROADMAP.md](https://github.com/gfabbretti8/veripp/blob/main/ROADMAP.md).

## Known limits

Read this before judging the output.

- **Counterexamples need triage; not all are bugs.** Where an object has no
  initialiser to build it from, the harness gives it every possible field
  value, including combinations no caller can construct — so it can report a
  failure that cannot happen in your program. veripp filters the mechanically
  decidable cases into a separate "harness artifact" count, but the rest are
  leads, not findings. The proofs are the trustworthy half. Use `--assume`
  (or an LLM) to state what real callers guarantee.
- **The released ESBMC is unsound for a common pattern.** v8.4 misses
  out-of-bounds writes to a member array indexed by another member of the
  same object ([esbmc#6508](https://github.com/esbmc/esbmc/issues/6508),
  fixed upstream, unreleased). `veripp doctor` detects it and every affected
  result says so. Use a master or `weekly` build.
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
