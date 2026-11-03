# Roadmap

## M0 — scaffold (done)
ESBMC runner + output parser, agent state machine, contracts header, CLI.

## M1 — single-file MVP (done, ESBMC 8.4)
- [x] Harness generator: given `--function`, emit a main() with nondet inputs
      constrained by VERIPP_REQUIRES (`veripp harness` prints it for review).
- [x] Golden tests against pinned ESBMC output on the examples
      (`tests/golden/`, re-captured by `tests/golden/capture.sh`).
- [x] Counterexample trace parser: full variable assignments, not just lines.
- [x] Parser calibrated against the real checker: an exhausted unwind bound is
      no longer misread as a counterexample, `VERIFICATION UNKNOWN` and tool
      errors are distinguished, and `-D__ESBMC__` is passed (ESBMC does not
      predefine it).

Known M1 limits, all disclosed at runtime rather than papered over:
- One call from a default-constructed receiver for member functions; call
  sequences need a hand-written harness.
- Scalars, and pointers/references to scalars, are the only parameter types the
  generator will model; anything else is refused.
- Buffer lengths are bounded (`--max-array-len`, default 4).
- Overloaded targets are refused rather than guessed at.

## M1.5 — C++ objects (done)
- [x] `--class`: bounded nondeterministic sequences over a class's public
      methods, replacing the "exactly one call on a fresh object" limitation.
      Catches state-dependent bugs a single-call harness proves "verified".
- [x] `--assert`: properties checked after every call in a sequence.
- [x] `veripp doctor` soundness self-check: known-failing programs must be
      rejected, so a checker with a false-negative hole cannot silently back
      a "verified" result (esbmc#6508 is one such hole in 8.4).

## M1.6 — objects as inputs (done)
- [x] Struct/class parameters built field by field: nested structs, array
      fields, pointer fields to a depth bound, enums, and honest refusal for
      opaque types. Struct definitions are resolved through the TU's own
      `#include "..."` headers, where real libraries keep them.
- [x] Preconditions may talk about fields (`--assume 'w->count > 0'`).
- [x] Counterexample rendering for objects: array writes collapsed, one line
      per location, long struct dumps truncated.
- Measured on lodepng: harnessable functions went 20.8% -> 65.8%, and the
  "pointer to non-scalar" refusal went 66.2% -> 23.1%.

## M2 — real projects
- [x] compile_commands.json: include paths, defines and -std taken from the
      build system, auto-discovered near the source.
- [x] `--link`: compile other translation units alongside the harness, and
      detect/disclose callees that are declared but never defined (ESBMC
      reports these for C but not C++, so veripp works them out itself).
- [x] `veripp scan`: harness and verify every function in a file, and report
      what could not be reached and why.
- [x] Incremental verification: unchanged files are reused. Deliberately NOT
      keyed on the function body, which this milestone originally called for
      and which is unsound — a function can be byte-identical and change
      verdict when a callee changes, so a body hash serves a stale "verified".
      The key covers the translation unit, its local headers, linked sources,
      the bounds, the harness options and the checker's version.
- Note: the libclang slicer this milestone originally called for was dropped.
  The measured blocker was never TU assembly -- tinyxml2 is a single .cpp and
  still failed -- it was type visibility and frontend gaps, addressed by
  following the compilation database's include paths instead.

## M1.7 — trusting the proposals (done)
- [x] Vacuity check: a proof resting on assumptions is re-run with a false
      assertion, so unsatisfiable preconditions are reported as VACUOUS
      instead of passing. The solver cannot catch this on its own.
- [x] `benchmarks/eval_triage.py --models a,b,c` scores triage per model
      against the pilot's ground truth. Because the solver rejects wrong
      proposals, the question is hit rate against price, not correctness.
- [ ] Call-site validation of a proposed precondition (mechanical for literal
      arguments), to catch over-tight but satisfiable invariants.

## M1.8 — provider independence (done)
- [x] Any OpenAI-compatible endpoint (OpenAI, Gemini, Groq, Together,
      DeepSeek, Mistral, OpenRouter, Ollama, vLLM, LM Studio, self-hosted),
      implemented over the standard library so there is no new dependency and
      a local model needs no account.
- [x] Prompts live in one place shared by every provider, so adding a vendor
      cannot change what is asked.

## M3 — delivery (done, except the talk)
- [x] GitHub Action (`action.yml`), with a self-test workflow that runs it
      three ways. It previously used `uv run --directory`, which resolves the
      caller's source against the action's own checkout and so broke every
      relative path; nothing caught that because nothing ran the action.
- [x] Multi-architecture container image. amd64 uses the published binary;
      arm64 compiles ESBMC from source, because the only prebuilt arm64 Linux
      ESBMC is the Homebrew 8.4 bottle and 8.4 carries esbmc#6508. The build
      runs `veripp doctor`, so an image whose checker misses a planted bug
      fails to build. 446 MB / 528 MB, 13/13 on `tests/image_smoketest.sh`.
- [x] Agent skill, installable as a Claude Code plugin from this repo. Its
      first instruction is the differentiator: the agent does not write the
      harness.
- [x] Benchmarks across nine libraries plus a reproduced CVE
      (`benchmarks/CORPUS.md`, `demo/cve-2019-13223/`).
- [x] Both delivery workflows verified on real runners: the action self-test
      (three jobs, including a relative path from a subdirectory — the case
      the `--directory` bug broke) and `image.yml` with `push: false`.
- [x] Published, from the public repo: the image on ghcr.io
      (`ghcr.io/gfabbretti8/veripp`, 0.1.0 to 0.5.0) and the packages on PyPI
      (`veripp` 0.2.0, 0.4.0, 0.5.0 and 0.6.0; `veripp-checker` 0.1.0, which
      0.7.0 folded back into veripp's own platform wheels). RELEASING.md is
      the procedure.
- [ ] CppCon lightning talk / Show HN.

Two things worth knowing before touching delivery:
- Everything pins one ESBMC release, 8.6, by tag and by digest: `ESBMC_RELEASE`
  and `PINNED_SHA256` in `src/veripp/checker.py`, `checker/*.sha256` for the
  workflows and the image, and a test that keeps them in step. veripp used
  to track the `weekly` tag, which is re-cut in place under one name, so
  nothing recorded which build a run used; a release's assets do not move.
- Since 8.6 ESBMC publishes a static arm64 Linux binary
  (`esbmc-linux-armv8.zip`), so the image, the Action, the checker wheel and
  `veripp install-checker` all download it like the x86_64 one. Before that
  the arm64 image compiled ESBMC from source with a hand-tuned cmake
  configuration and the wheel was copied out of that image; both are in the
  git history if upstream ever stops publishing. The image is now only a
  distribution channel, nothing else is built from it.
