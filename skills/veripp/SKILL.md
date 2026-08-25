---
name: veripp
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/install.sh)
description: Prove a C or C++ function cannot overflow, read or write out of bounds, divide by zero, or dereference null - or get a concrete input that makes it happen. Use when the user asks to verify, prove, formally check, model-check, or find memory-safety and arithmetic bugs in C/C++ code, when reviewing security-sensitive parsing or buffer code, or when a fuzzer found nothing and they want a stronger guarantee than testing.
---

# veripp

veripp runs a bounded model checker (ESBMC) over a C/C++ function and returns
either a proof or a concrete counterexample. Your job is to point it at the
right function and interpret what comes back honestly.

## The one thing to understand first

**You do not write the verification harness. veripp writes it.**

If you have used ESBMC directly, you will be tempted to hand-write a `main()`
full of `__ESBMC_nondet_int()`, add `__ESBMC_assume(...)` lines, and annotate
loops with invariants. Do not do that here. veripp reads the function
signature, builds the nondeterministic harness itself - including struct
parameters field by field, array bounds, pointer depth, and enum ranges - and
re-checks every assumption it makes.

Write a harness by hand only when veripp explicitly refuses a parameter type
and you have read its stated reason.

## Start here

```bash
veripp doctor                    # is the checker present, and is it sound?
veripp scan path/to/file.c       # every function in the file
veripp verify file.c --function parse_header
```

Always run `veripp doctor` first on an unfamiliar machine. It plants known bugs
and checks the installed ESBMC actually finds them. The published ESBMC 8.4
release silently misses out-of-bounds writes to a member array
(esbmc/esbmc#6508), so a "verified" from that build is not worth having, and
doctor is what tells you.

## Reading the result

| Exit | Meaning | What to tell the user |
|---|---|---|
| 0 | VERIFIED | Proved **within the stated bounds**. Quote the bounds. |
| 1 | COUNTEREXAMPLE | A concrete input reaches the fault. This is a real lead. |
| 2 | Usage error | Your invocation was wrong. |
| 3 | Inconclusive / vacuous | Proved nothing. Do not report it as a pass. |

Three failure modes to name out loud rather than paper over:

- **Bounded, not total.** A pass means "no counterexample within N loop
  unwindings and arrays of length M". Say the numbers. `--unwind` and
  `--max-array-len` change them.
- **Vacuous proofs.** If preconditions contradict each other, everything is
  provable. veripp re-runs the harness with a deliberately false assertion to
  catch this and reports VACUOUS. Never report a vacuous run as verified.
- **Stubbed callees.** If a called function has no body in the translation
  unit, the checker invents a return value. veripp lists those calls. Pass
  `--link other.c` to give it the real ones, or say which were stubbed.

## Counterexamples need triage before you report them

A counterexample proves the fault is reachable **in the generated harness**.
That is not the same as reachable from real calling code. Before telling the
user they have a bug:

1. Read the input assignment veripp printed. Is it a value a caller could
   actually pass?
2. If the function is internal and callers always pass, say, a non-zero length,
   the finding is an unenforced precondition, not a bug. Re-run with
   `--assume 'len > 0'` and see if it proves.
3. Only call it a bug when you can name a public entry point that reaches it.

veripp will propose a precondition itself if an LLM is configured
(`--model`), but the solver re-checks every proposal, so treat the proposal as
a hypothesis, not an answer.

## Working on an existing codebase

Pointed at code that already exists, veripp reports everything at once. cJSON
gives 33 counterexamples on a first run. Failing a build on all of them gets
the check deleted, so record what is already there and fail only on what
appears afterwards:

```bash
veripp accept src/ --baseline .veripp-baseline   # commit this file
veripp scan   src/ --baseline .veripp-baseline   # now fails only on new findings
```

The baseline is JSON and meant to be read in the pull request that adds it:
each entry is a risk somebody decided to carry. Findings are keyed on
(file, function, property), never line numbers, so moving code does not
resurrect an accepted finding. When an accepted finding stops occurring veripp
says so, because an entry matching nothing still grants permission to whatever
matches it later.

**Suggest this whenever someone is adding veripp to a project that already has
code.** Recommending a bare `veripp scan` there produces a wall of findings and
a bad first impression of the tool.

## Common invocations

```bash
# a whole project: every .c/.cc/.cpp/.cxx underneath, skipping build and
# vendor trees. Prefer this to scanning files one at a time.
veripp scan src/ --jobs 8

# only what you are working on -- seconds instead of minutes on a big file
veripp scan src/ --only 'parse_*'

# an inconclusive result usually means it ran out of budget, not that the
# code is wrong. More of both is the first thing to try.
veripp verify src/parser.c --function parse --unwind 64 --timeout 300

# findings as SARIF, so CI can put them on the pull request diff rather than
# in a log (github/codeql-action/upload-sarif reads this)
veripp scan src/ --sarif veripp.sarif --baseline .veripp-baseline

# unchanged files are reused from cache automatically
veripp scan src/ --cache .veripp-cache   # or --no-cache to verify everything

# a whole file, in parallel, machine-readable
veripp scan src/parser.c --jobs 4 --json

# both at once: a readable log AND a report for later, from one run
veripp scan src/parser.c --json-out report.json

# a function that needs its build flags
veripp verify src/parser.c --function parse --compile-commands build/compile_commands.json

# a precondition the callers guarantee but the signature does not
veripp verify src/img.c --function scale --assume 'w > 0 && h > 0'

# a class: bounded sequences of public method calls, not a single call
veripp verify src/Buf.cpp --class Buf --max-calls 4 --assert 'b.size() <= b.capacity()'

# see what it generated before trusting it
veripp harness src/parser.c --function parse
```

`veripp harness` is the honesty check. If a result surprises you, read the
harness before believing either the proof or the counterexample.

## Where it works and where it does not

ESBMC handles C and C-like C++ well. Heavy STL and template metaprogramming
hit real frontend gaps - some inputs crash the checker outright. That is a
tool limit, not a proof of anything. If `veripp scan` reports a large number
of refusals, run `veripp harness` on one of them and report the stated reason
rather than guessing.

## If veripp is not installed

Run the bundled script to find out what this machine needs. It changes
nothing and prints the plan and its cost:

```bash
${CLAUDE_SKILL_DIR}/install.sh
```

`${CLAUDE_SKILL_DIR}` resolves wherever the skill is installed; a bare
`./install.sh` only works if you happen to be sitting in the skill directory,
which you will not be.

**Show the user that plan and get agreement before running it with `--yes`.**
The dry report above needs no permission; the `--yes` form deliberately does.
Every route is expensive in a way worth consenting to: the image is a
450-540 MB pull, the Linux x86_64 ESBMC is a 235 MB download, and on macOS
`brew install --HEAD esbmc` is a source build measured in tens of minutes that
may drag in LLVM. None of that should start because an agent decided to be
helpful.

Two things the script will tell you that are easy to get wrong on your own:

- `brew install esbmc` (without `--HEAD`) gives you 8.4, which silently misses
  out-of-bounds writes (esbmc/esbmc#6508). A "verified" from it is worthless,
  which is why `veripp doctor` probes for it.
- Linux/arm64 has no prebuilt ESBMC at all. Use the image there.

If the user would rather not install anything, the image needs nothing beyond
docker:

```bash
docker run --rm -v "$PWD:/src" ghcr.io/gfabbretti8/veripp scan file.c
```
