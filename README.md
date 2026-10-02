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

On Linux (x86_64 and aarch64) the ESBMC checker comes bundled with the
install. On macOS and Windows there is no bundled checker yet, so fetch one
with:

```bash
veripp install-checker
```

It downloads ESBMC's [`weekly`](https://github.com/esbmc/esbmc/releases/tag/weekly)
build and keeps it only if the checker rejects every one of a set of
known-failing programs. Avoid the ESBMC 8.4 release, including
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
for files that haven't changed. The cache key covers headers, linked sources,
bounds and the checker version. Counterexamples are grouped by file, with
writes listed before reads. Length parameters that a function never reads
are listed separately as leads.

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
fails the job) and then scans. This workflow uses a baseline and annotates
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
- **Vacuous proofs are rejected.** If a precondition makes the function
  unreachable, every property holds trivially. veripp re-runs any proof that
  rests on assumptions with an assertion that must fail. If that assertion
  doesn't fail, it reports `VACUOUS` and exits non-zero.
- **The LLM only proposes.** Harnesses and preconditions a model suggests
  are checked by ESBMC. A result that holds only under a proposed
  precondition is reported as `PRECONDITIONED`, never folded into `PROVED`.

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
