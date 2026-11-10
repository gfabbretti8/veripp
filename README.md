# veripp

**AI-assisted bounded verification for C and C++ functions.**

Point veripp at a function. It generates a verification harness from the
signature and runs the [ESBMC](https://esbmc.org) model checker for eight
kinds of undefined behaviour: arithmetic overflow, out-of-bounds access,
null and invalid pointers, division by zero, memory leaks, uninitialised
reads, undefined shifts and NaN. If the checker can't settle the function, it
retries with wider bounds.

```bash
veripp verify src/parser.cpp --function parse_header
```

Every run gives one of these results:

- **verified**: no property fails within the stated bounds and assumptions.
  This is a bounded proof, not a proof for every possible input.
- **counterexample**: concrete inputs that break a property, with the trace.
  A counterexample is a lead to triage, not automatically a bug.
- **inconclusive**: the checker timed out or hit the unwind bound. This is
  never reported as a pass.

Each result lists the assumptions it rests on, because those are the part to
check:

```
Assumptions (a result is only as good as these):
  - `a` points to exactly `n` valid elements, with n <= 4 (harness bound)
  - `uri` is a NUL-terminated string of at most 4 characters
  - these callees are declared but not defined here, so their side effects
    are NOT modelled: strlen, fs_open (link the source with --link)
```

An LLM is optional. When you configure one, it triages counterexamples and
may propose preconditions, and ESBMC re-checks every proposal before anything
in the report changes. `--no-llm` runs the same pipeline without a model.

## Install

```bash
pip install veripp
veripp doctor          # checks the setup and probes the checker for known soundness holes
```

On Linux (x86_64 and arm64) that is the whole installation: the wheel
carries the [ESBMC 8.6](https://github.com/esbmc/esbmc/releases/tag/v8.6)
checker inside it. When installing from source, fetch one with:

```bash
veripp install-checker
```

It downloads the same release binary for your architecture, checks it
against the digest veripp pins, and keeps it only if the checker rejects
every one of a set of known-failing programs. On macOS the only sound route
is `brew install --HEAD esbmc`. Windows is not supported; use WSL or the
container. Avoid the ESBMC 8.4 release, including
`brew install esbmc`: it silently misses some out-of-bounds writes
([esbmc#6508](https://github.com/esbmc/esbmc/issues/6508)). `doctor` detects
this.

To skip the setup entirely, use the container. The image is only published
after its checker passes the same probe:

```bash
docker run --rm -v "$PWD:/src:ro" ghcr.io/gfabbretti8/veripp scan src/parser.c
```

Python 3.10+ is the only prerequisite for the pip install.

## Examples

This finds an off-by-one:

```bash
$ veripp verify examples/off_by_one.cpp --function sum_array
Result: counterexample
  bounded, unwind=32; checks: overflow, bounds, pointer, div-by-zero, memory-leak, uninitialised, ub-shift, nan; std=c++17
Assumptions (a result is only as good as these):
  - `a` points to exactly `n` valid elements, with n <= 4 (harness bound on array length)
Violated property: dereference failure: array bounds violated
  at examples/off_by_one.cpp:7:9 in sum_array
    6 |     for (unsigned i = 0; i <= n; ++i)  // BUG: <= reads one past the end
  > 7 |         s += a[i];
                ^
Counterexample inputs:
  n = 4
  ...
```

This proves a postcondition, then checks a class over call sequences:

```bash
veripp verify examples/ring_buffer.cpp --function push
veripp verify examples/ring_buffer.cpp --class RingBuffer --max-calls 6
```

Scan a whole file or a whole tree. The summary counts functions that were
proved, gave a counterexample, were inconclusive, or could not be harnessed:

```bash
veripp scan                  # everything under the current directory
veripp scan src/lodepng.cpp
veripp scan src/
```

Rediscover a known CVE on unmodified upstream source:

```bash
./demo/cve-2019-13223/run.sh        # a few seconds, clones stb for you
```

The demo finds [CVE-2019-13223](https://nvd.nist.gov/vuln/detail/CVE-2019-13223),
a division by zero in stb_vorbis's `predict_point()`. It then proves that the
precondition enforced by the official fix (`x1 != x0`) removes it
([details](https://github.com/gfabbretti8/veripp/blob/main/demo/cve-2019-13223/README.md)).

Exit codes: `0` verified, `1` counterexample, `2` usage error, `3`
inconclusive.

## Usage

### Verify one function

```bash
veripp verify src/parser.c --function parse --assume 'len > 0' --link src/helper.c --repro repro.c
```

`--assume` states what real callers guarantee. `--link` brings in callees
defined in other files. `--repro` is used only when there is a counterexample.

### Is the counterexample real? Run it.

```bash
$ veripp verify src/parser.c --function parse_u8 --replay
...
Violated property: dereference failure: array bounds violated
  at src/parser.c:9:5 in parse_u8
    8 |     int v = 0;
  > 9 |     while (*p >= '0' && *p <= '9' && p < end) {
          ^
Counterexample inputs:
  p_buf = { 56, 56, 56, 56 }
  end = &p_buf[0] + 4
Replayed natively: CONFIRMED. AddressSanitizer: stack-buffer-overflow at parser.c:9 -- the line the checker named.
```

`--replay` builds the counterexample's inputs into a program, compiles it
with AddressSanitizer and UBSan, and runs it. `CONFIRMED` means the compiled
function fails on those inputs too, so the failure is not an artifact of the
checker's model; whether a real caller can pass them is still the question
the assumptions answer. A clean run is a reason to doubt the counterexample,
not a refutation. `veripp scan --replay` does it for every finding and lists
the confirmed ones first. This is the only thing veripp does that **executes
the code under test**, which is why it is a flag.

`--repro` writes a standalone program built from the counterexample's inputs,
plus the command to compile it under AddressSanitizer and UBSan. If that
program runs cleanly under the sanitizers, the counterexample is most likely
an artifact of the harness rather than a bug.

**Link what you can.** ESBMC treats a function it has no body for as having
no side effects on its pointer arguments. veripp names every such callee in
the result so you can decide whether that matters.

veripp picks up include paths, defines and `-std` from the nearest
`compile_commands.json`. `--compile-commands PATH` names one explicitly.

### Harder C code (`--help-all`)

These options are off by default. Each one changes what the harness assumes,
and the result says so.

| option | use it when |
|---|---|
| `--constructors` | object parameters should come from the library's own constructors, not from arbitrary field values |
| `--sequence TYPE` | you want to exercise a C handle type: construct it, make a bounded sequence of API calls on it, then free it |
| `--preprocess` | structs have members inside `#if` blocks; runs the C preprocessor so they resolve as the compiler sees them |
| `--setup 'init()'` | a linked module needs its initialiser to run first |
| `--unterminated` | `char *` inputs are raw bytes (for example off the wire), not NUL-terminated strings |

### Scan a project, or only what changed

```bash
veripp scan src/ --jobs 8
veripp scan src/ --only 'parse_*'
veripp scan . --changed origin/main      # only files this branch touches
```

`scan` skips build and vendored directories. It retries unsettled functions
within a time budget (`--retry-budget`, default 120 s) and caches verdicts
for files that haven't changed. The cache key covers every project header
the file reaches (through `-I` and `compile_commands.json` alike), linked
sources and force-included headers, the bounds and harness options, and the
checker and veripp themselves, identified by their bytes rather than by a
version string. Counterexamples are grouped by file and ordered by who can
reach them: externally visible functions before `static` helpers, each
tagged with the call sites found in the scanned units, and writes before
reads. Length parameters that a function never reads are listed separately
as leads.

To adopt veripp on existing code, record what is already there and fail
only on new findings:

```bash
veripp accept src/ --baseline .veripp-baseline   # commit this, review it
veripp scan   src/ --baseline .veripp-baseline   # exits 1 only on new findings
```

As a [pre-commit](https://pre-commit.com) hook:

```yaml
repos:
  - repo: https://github.com/gfabbretti8/veripp
    rev: v0.6.0
    hooks:
      - id: veripp            # or: veripp-docker, which needs only Docker
```

### Bring your own model

```bash
veripp scan src/ --model ollama:llama3.1          # local, no account
veripp scan src/ --model openai:gpt-4o-mini
```

Built-in providers are `anthropic`, `openai`, `gemini`, `groq`, `together`,
`deepseek`, `mistral`, `openrouter`, `ollama` and `lmstudio`. Anything else
that speaks the OpenAI-compatible API works with `--llm-base-url`. Every
provider except Anthropic is called through the standard library, so they
need no extra packages.

Triage runs only when asked for: `--model`, `$VERIPP_LLM_MODEL`, or an
endpoint (`--llm-base-url` or `$VERIPP_LLM_BASE_URL`). A provider's API key
in the environment, set for some other tool perhaps, does not send your code
anywhere on its own.

How good the triage is has only partly been measured. The path works end to
end, a 7B local model scored 0 of 2 on the benchmark, and no hosted model has
been graded yet. [benchmarks/TRIAGE.md](https://github.com/gfabbretti8/veripp/blob/main/benchmarks/TRIAGE.md)
has the details.

## In CI

The action installs a checker, runs `veripp doctor` (so a broken checker
fails the job) and then scans. It installs one on Linux runners, x86_64 and
arm64 alike; anywhere else it uses an `esbmc` already on PATH. On macOS that
means an earlier `brew install --HEAD esbmc` step, since the only sound macOS
build is compiled from source. This workflow uses a baseline and annotates
the pull-request diff through SARIF:

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

Findings covered by the baseline are uploaded as *suppressed*, not dropped.

The job fails on a counterexample and on a vacuous proof. Other inconclusive
results (a bound, a timeout, a file the checker's frontend refused) only
warn; `fail-on: inconclusive` fails on those too, and `fail-on: never` is for
a first look.

## As an MCP server

```bash
claude mcp add veripp -- veripp mcp      # Claude Code; any MCP client works the same way
```

`veripp mcp` speaks the Model Context Protocol over stdio and offers
`verify`, `scan`, `harness` and `doctor` as tools with structured arguments.
A call returns the same JSON report `--json` prints, with the exit code and
what it means, so an agent reads a verdict and the assumptions it rests on
instead of parsing prose. Triage is off unless the call names a model: the
agent on the other end is the one doing the reading.

With nothing installed, the image serves the same tools; paths in tool calls
are then relative to the mounted directory:

```bash
claude mcp add veripp -- docker run -i --rm -v "$PWD:/src" ghcr.io/gfabbretti8/veripp mcp
```

## As a skill for coding agents

```bash
npx veripp-skill            # this project; --global for every project
```

In Claude Code you can install it as a plugin instead:

```
/plugin marketplace add gfabbretti8/veripp
/plugin install veripp@veripp
```

For any other agent, copy [`skills/veripp`](https://github.com/gfabbretti8/veripp/blob/main/skills/veripp)
to wherever it keeps skills. The skill teaches the agent to let veripp
generate the harness instead of writing one by hand, and how to read a
bounded result.

Shell completions: `eval "$(veripp completion bash)"` (or `zsh`, or
`veripp completion fish | source`).

## How results stay honest

- **Bounds are stated.** Every result shows its unwind bound, array bounds
  and checks. An exhausted unwind bound is reported as inconclusive (and
  retried wider), not as a bug.
- **Assumptions are stated.** Every simplification the harness makes is
  printed: a bounded length, a non-null pointer, an unlinked callee, an
  undefined extern array. Where a parameter can't be modelled soundly, veripp
  refuses to generate a harness rather than guess.
- **Vacuous proofs are rejected.** If the assumptions make the function
  unreachable, every property holds trivially. veripp re-runs every proof, in
  `verify` and `scan` alike, with an assertion that must fail: the assumption
  that empties a harness can sit in the code under test, or be a bound the
  harness adds itself. If that assertion doesn't fail, the result is
  `VACUOUS`; if the re-run can't settle (a timeout, a checker error), it is
  `UNCONFIRMED`. Neither is a proof, and both exit 3.
- **Inferred invariants are quoted.** Where the library's own code states
  an invariant about a struct (a count kept within a capacity, a capacity
  used as a power-of-two mask, an array of indices into another), the
  harness assumes it and prints the line it read it from.
  `--no-inferred-invariants` explores the states they rule out.
- **Stand-ins are named.** A callback parameter gets a function that
  returns any value and touches nothing, two pointers the body compares are
  placed in one buffer, and a file's own `main()` is set aside so the
  harness can have one. Each is an assumption printed with the result, and
  a callback that can only return NULL says its proof covers that path only.
- **Nothing is called a bug that nobody looked at.** Without a model, a
  counterexample is printed as not triaged, with the line it fails on.
- **The LLM only proposes.** Harnesses and preconditions a model suggests
  are checked by ESBMC. A result that holds only under a proposed
  precondition is reported as `PRECONDITIONED`, never folded into `PROVED`,
  and the precondition is checked against every caller that passes a
  literal: one that breaks it is reported, and `verify` exits 3 for it.

## Results on real libraries

Measured with `veripp scan`: libpng `png.c` 40 of 70 functions proved,
lodepng 99 of 260, cJSON 32 of 117. These are bounded proofs, under the
assumptions printed with each result. They were measured with an earlier,
four-check version and are observations at a point in time. The full table,
false-positive triage and reproduction commands are in
[benchmarks/CORPUS.md](https://github.com/gfabbretti8/veripp/blob/main/benchmarks/CORPUS.md).

## Known limits

- **Counterexamples need triage.** A harness can build inputs no real caller
  can, especially object fields. The proofs are the trustworthy half. The
  counterexamples are leads, and `--assume`, `--constructors` and `--repro`
  help sort them.
- **Verification is bounded.** A proof covers executions within the stated
  bounds only.
- **Leaks are reported for C, not yet for C++.** A leak is memory the
  function allocated and can no longer be reached through its return value,
  its arguments or a global when it returns. A C++ harness cannot yet keep
  those reachable, so a leak in a C++ function is filed as a harness
  artifact rather than reported.
- **Only C and C-like C++ work.** ESBMC's C++ frontend doesn't handle
  STL-heavy code. For example, tinyxml2 crashes it and jsoncpp won't parse.
- **Coverage varies by file.** Types defined outside the translation unit
  can't be constructed, and those functions are refused with the reason
  given. Run `veripp scan` to see how much of your code veripp reaches.
- **There's a frontend issue on arm64.** ESBMC can't parse some ARM
  intrinsic headers there. `doctor` warns about this, and the `linux/amd64`
  container works around it.

What is planned is in [ROADMAP.md](https://github.com/gfabbretti8/veripp/blob/main/ROADMAP.md).
Contributions are welcome, see
[CONTRIBUTING.md](https://github.com/gfabbretti8/veripp/blob/main/CONTRIBUTING.md).

## License

Apache-2.0
