# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

Two things are worth knowing when reading any entry here: a "verified" from
veripp is a **bounded** proof, and it is only as good as the checker underneath
it. `veripp doctor` probes that checker against known-failing programs on every
run, and refuses to back results from one that cannot detect a planted bug.

## 0.5.0

Fewer steps between a developer and a verified function.

### Added
- **`veripp-checker` wheels**, so that `pip install veripp` can be the whole
  installation, with no extra to remember. `checker/` holds the package, a wheel builder, and a
  script that assembles a relocatable payload from veripp's own image; CI
  builds Linux x86_64 and aarch64 and keeps a wheel only if a clean container
  that pip-installs it passes `veripp doctor`. What is bundled is the **slim,
  Z3-only** build -- not an optimisation but a licensing one: ESBMC's COPYING
  notes MathSAT is academic/non-commercial and Yices personal-use or GPL3,
  while Z3 is MIT, so the fat official release is the one that cannot be
  redistributed. Measured on arm64: an 87 MB wheel, under PyPI's 100 MB
  default. `find_esbmc` consults it after `$VERIPP_ESBMC` and
  `install-checker`, and veripp stays fully usable where no wheel exists.
  A binary-free `py3-none-any` wheel ships alongside the platform ones: pip
  ranks a platform wheel above `any`, so it is chosen only where nothing else
  fits, and it is what lets the dependency be unconditional instead of an
  extra -- `pip install veripp` resolves everywhere, and simply reports no
  bundled checker where there is none. **`pip install veripp` on Linux is now
  the whole installation**; macOS and Windows get the tool and are told to run
  `veripp install-checker`. Which solvers a binary actually contains is
  checked before packaging, by looking for their API symbols rather than
  their names: ESBMC names every solver it knows about in its own help text,
  and MathSAT and Yices may not be redistributed.
- **`veripp install-checker`.** Installing ESBMC was the worst step in getting
  started: not pip-installable, and the release people reach for first
  silently misses out-of-bounds writes to a member array
  ([esbmc#6508](https://github.com/esbmc/esbmc/issues/6508)). This downloads
  the `weekly` build where a relocatable one is published and keeps it **only**
  if it passes the same soundness probes `doctor` runs -- an unsound download
  is deleted, not reported, because every result built on it would be a false
  proof. Where no relocatable build exists (macOS, Linux arm64) it says so and
  names the alternative rather than installing something that will not run.
  `find_esbmc` resolves most-deliberate-first: `$VERIPP_ESBMC`, a checker
  installed by this command, `PATH`, and only then the bundled wheel. PATH
  beats the wheel on purpose -- putting an esbmc there is a decision, and
  overriding it would stop an ESBMC developer testing their own build. A bad
  checker chosen that way is not a hazard, because `doctor` probes whichever
  one is selected.
- **`veripp scan --changed [REF]`** verifies only the files git reports as new
  or modified: against `HEAD` alone (staged, unstaged and untracked), or
  `REF...HEAD` with a ref, so a long-lived branch is compared against where it
  diverged rather than everything that landed on main meanwhile. Deletions are
  dropped and finding nothing exits 0, since a gate that fails when you touched
  no C is a gate people remove. `.pre-commit-hooks.yaml` ships `veripp` and
  `veripp-docker` hooks built on it.
- **`veripp verify --repro PATH`** writes a standalone C/C++ file that
  reproduces a counterexample with its own concrete inputs, and prints the
  build line to compile it under AddressSanitizer and UBSan, include paths
  carried over. A trace asks the reader to trust the harness; a program that
  crashes asks nothing. It is also self-checking: a repro that exits cleanly
  under the sanitizers is what a harness artifact looks like from outside.
  Only written for counterexamples -- a proof has no failing input, and a file
  for an inconclusive result would read as a finding.

## 0.4.0

A first scan you can read.

### Added
- **`scan` re-tries its inconclusives.** Functions the mechanical pass could
  not settle get a second attempt under a wall-clock budget
  (`--retry-budget`, default 120s, 0 disables), cheapest first, with no LLM
  needed. Each retry starts past what the first pass already established: a
  bound that ran out is seeded one widening beyond the widest tried (then
  k-induction), and a timeout gets four times the time with incremental
  BMC — measured on cJSON, a timeout retried under the same limit settles
  exactly nothing. A candidate the remaining budget provably cannot afford
  is skipped rather than half-tried. The INCONCLUSIVE line reports how many
  the second pass settled, and `accept` runs the same passes so a baseline
  records what `scan` would find. Measured on lodepng: 51 inconclusive
  before; at `--retry-budget 900`, two became proofs. The default budget is
  a real help only on files whose checker runs are cheap — the summary
  tells you what it did either way.
- **`scan` triages its counterexamples.** With an LLM configured (`--model`,
  or `$VERIPP_LLM_MODEL` — same default as `verify`), each counterexample
  goes through the agent loop `verify` uses: the model classifies it and may
  propose a precondition, which the solver re-checks, vacuity probe included,
  before the report changes. A counterexample that disappears under a
  solver-accepted precondition is reported as **PRECONDITIONED** with the
  precondition listed — a conditional result, never folded into PROVED. The
  rest are ranked with the model's verdict attached, likely real bugs first;
  a triage the model could not perform says "triage unavailable" rather than
  pretending an opinion. The mechanical pass stays parallel and LLM-free, so
  functions that prove outright never cost an API call. `--no-llm`, formerly
  a no-op accepted for symmetry, now actually turns something off. Cached
  untriaged scans are re-verified rather than served when triage is asked
  for.

### Fixed
- **Endpoints that refuse a token cap no longer break the provider.** Gemini's
  OpenAI-compatible layer drops the connection — no HTTP status at all — for
  models newer than `gemini-3.6-flash` whenever the request carries
  `max_tokens` (or `max_completion_tokens`; observed 2026-08-30). The client
  now retries such a request once without the cap, and a connection dropped
  for any other reason becomes an `LLMError` instead of an unhandled
  `RemoteDisconnected` that aborted the run.
- **An unreachable LLM during escalation no longer aborts a verification.**
  The agent loop's invariant and frontend-fix proposals now degrade to "no
  proposal" on `LLMError`, honouring the same contract triage always had.
- **`eval_triage.py` no longer grades a model that never answered.** An API
  failure (quota, dropped connection) previously scored the pipeline's
  conservative offline fallback as the model's answer — 0/2 for a model that
  was never asked. Such cases now report `UNAVAILABLE` and are excluded from
  the score, and a model with any unavailable case cannot count as perfect.

### Changed
- README rewritten around why → examples → installation → usage; duplicated
  install, Docker and exit-code sections consolidated. The architecture
  diagram no longer claims the cache is keyed on a function-body hash — it
  never was, and the cache section beside it said so.

## 0.3.0

Ask for a verdict, not a configuration.

### Added
- **Termination proving.** For a function with a loop, once safety holds veripp
  asks whether it also terminates, and reports that on its own line — never
  folded into "verified", because a safety proof says nothing about liveness.
  A negative reads as "not proved", not "loops forever": ESBMC proves
  termination but cannot refute it. Measured guard: raw ESBMC reports
  SUCCESSFUL under `--k-induction` for a loop that never finishes, so the
  termination question always forces `--termination` rather than trusting a
  k-induction verdict. Reported by both `verify` and `scan`.
- **k-induction is reached on its own.** When a bounded result stays
  inconclusive, veripp widens the unwind bound and finally switches to
  k-induction to escape boundedness — no flag to find. Every result still
  states the mode it was obtained under.
- **`--esbmc-arg`** forwards any flag straight to ESBMC, so every checker
  feature stays reachable without a veripp flag per check. Anything passed this
  way is named in the result line, since a raw flag can weaken a proof as
  easily as strengthen it.
- **Offline CVE demo.** `demo/cve-2019-13223/predict_point.c` carries the
  vulnerable function verbatim (with its upstream commit), so the CVE — and its
  test — reproduce with no network. `run.sh` still proves it on the whole
  unmodified `stb_vorbis.c`.

### Changed
- **Eight undefined-behaviour checks on by default**, up from four: added
  memory leaks, uninitialised reads, undefined shifts, and NaN. Each was
  measured on real code before being turned on; on cJSON the stricter set cost
  exactly one proof out of 32, and that one is a correctly-labelled harness
  artifact, not a finding to triage. `--nan-check` is viable only because
  veripp writes the harness and constrains float inputs to finite values.
  Unsigned overflow stays off: wraparound is defined behaviour.
- **The default `--help` asks for intent, not tuning.** `verify` dropped from
  26 flags to 13, `scan` from 31 to 15. Nothing was removed — the bounds and
  build knobs moved behind `veripp <command> --help-all`. A bare path
  (`veripp src/`) scans it.
- `veripp doctor` no longer suggests repo-relative example paths when run from
  a `pip install`, where those files are not on disk.

### Release
- Pushing a `vX.Y.Z` tag now re-tests on that commit (with a real ESBMC and the
  soundness probe), then publishes to PyPI and confirms the released version
  installs from the index. See `RELEASING.md`.

## 0.2.0

Usable on a codebase that already has findings.

### Added
- `veripp accept` records current findings to a `.veripp-baseline`, and
  `veripp scan --baseline` fails only on findings absent from it. Pointed at an
  existing codebase a verifier reports everything at once — cJSON gives 33
  counterexamples on the first run — so a blocking check gets removed on day
  two and a non-blocking one gets ignored. Findings are keyed on
  (file, function, property), never on line numbers, so moving code does not
  resurrect accepted findings. Accepted entries that stop occurring are
  reported, since an entry matching nothing still grants permission to whatever
  matches it later. The GitHub Action takes a `baseline:` input.
- `veripp scan --sarif PATH` writes SARIF 2.1.0, so the Action can hand results
  to GitHub code scanning and each finding lands on the pull request diff
  rather than in a job log. Validated against the published schema. Findings
  covered by a baseline are emitted as suppressed rather than dropped, so code
  scanning shows them as accepted instead of pretending they are gone, and
  fingerprints are keyed like the baseline so a finding survives code moving.
- `veripp scan --only 'parse_*'` verifies just the functions matching a glob
  (repeatable). A glob matching nothing is an error rather than a silent
  full scan.
- Counterexamples of the form "Incorrect alignment when accessing data object"
  are classified as harness artifacts. They come from allocating through a
  function pointer the checker could not resolve — 14 of cJSON's 33
  counterexamples were this one pattern, and none of them said anything about
  cJSON.
- Repeat scans reuse verdicts for unchanged files, turning minutes per commit
  into seconds. The key covers the translation unit, its local headers, linked
  sources, bounds, harness options and the checker version. Deliberately not
  keyed on the function body, which is unsound: a function can be
  byte-identical and change verdict when a callee changes, and a body hash
  would serve the stale "verified".
- The default unwind bound is 32, was 8. Measured on 37 cJSON functions:
  29% decided before, 62% after, in the same wall time (488s vs 489s). The
  old default was tuned for loops people write, but library code constantly
  memsets and memcpys structs, and those loops need a bound near the struct's
  size — `cJSON_CreateArray` has no loop of its own and was inconclusive at 8
  and at 32, needing 128, which the old ladder never reached. Pass `--unwind`
  to override.
- `benchmarks/TRIAGE.md` records how far the LLM triage path is actually
  evidenced: the path runs end to end against a real model, a 7B local model
  scores 0/2 and over-reports real bugs, and no hosted model has been graded
  because nothing here has an API key. That last gap sits under the half of
  the product the name advertises, and one command closes it.
- `npx veripp-skill` installs the agent skill with nothing but Node.

- `npx veripp-skill` installs the agent skill with nothing but Node — into
  `./.claude/skills/veripp`, or `--global` for every project. It installs the
  skill, not the verifier, and says so: the verifier is a Python program
  needing ESBMC, and the skill's own `install.sh` reports what getting it
  would cost before doing anything.

### Changed
- The README leads with something runnable. The first command a reader could
  run used to be 109 lines in, behind four sections of context; the three
  install routes and the worked examples are now at the top, with the exit-code
  contract beside them.

### Fixed
- Every file read and write is explicitly UTF-8. Without an encoding Python
  uses the platform default, which on Windows is cp1252: veripp then crashed
  on any source file containing a non-ASCII byte, or silently mis-decoded it
  where `errors="replace"` was set. Found by running the suite on
  windows-latest.

## 0.1.3

### Fixed
- `veripp scan DIR --compile-commands ...` died with a usage error before
  verifying anything, and an auto-discovered database silently gave every file
  no include paths. A compilation database is keyed by translation unit and the
  harness's include path starts at the file's own directory, so both are now
  resolved per file rather than once against the tree root. A file the database
  does not cover — a test, a fuzzer, generated code — is scanned without its
  flags instead of aborting the run; naming such a file directly still fails
  loudly.

## 0.1.2

Scanning a project rather than a file.

### Added
- `veripp scan DIR` scans every C/C++ file under a directory, which is what
  every neighbouring tool does (ripgrep, fd, clang-tidy) and the difference
  between working on a file and working on a project. Build trees, vendored
  dependencies and dotted directories are skipped; headers are left alone
  because definitions live in the source file. Findings are grouped by file,
  and one unreadable file does not discard the work already done.

## 0.1.1

First contact with the CLI, which is where a tool is judged.

### Added
- `--version` / `-V`, which was missing entirely.
- `veripp` with no arguments prints an overview and exits 0, instead of an
  argparse error announcing that you failed to supply a command.
- Did-you-mean for mistyped commands (`veripp scna` → `veripp scan`) and for
  mistyped class names — the latter used to answer a `--class` query with a
  list of *functions*, hiding a one-letter fix behind the wrong kind of thing.
- Colour on the verdict, on `doctor`'s soundness probes, and on the `scan`
  markers. Respects `NO_COLOR`, `FORCE_COLOR`, non-TTY stdout and `TERM=dumb`,
  so it never leaks into a pipe, a log or a redirect.
- Shell completions for bash, zsh and fish, generated from the parser so they
  cannot drift from the flags that actually exist: `veripp completion bash`.
- `--json-out PATH`, which writes the machine-readable report to a file while
  leaving the readable output on stdout. CI previously had to verify
  everything twice to get both.
- A scan now ends with the command to investigate its first finding, and
  repeats the caveat: a counterexample holds in the generated harness, which
  is not the same as a caller being able to reach it.

### Fixed
- `veripp verify .` crashed with a traceback. Pointing at a directory is an
  ordinary slip and now gets a usage error naming a file inside it to try.
- An empty file and a non-C file both reported "no definition found", which
  describes neither problem. They now say what is actually wrong.

### GitHub Action
- Outputs `outcome`, `exit-code` and `report`, so a later step can branch
  without re-deriving the verdict from an exit code.
- A job summary on the run page: the verdict, a scan table or a formatted
  violation with location and CWE, and explicit labels for bounded proofs,
  vacuous results and inconclusive runs.
- Reuses an ESBMC already on `PATH` instead of downloading 235 MB over a
  working checker.

## 0.1.0

First release. Bounded formal verification for C and C++ on top of ESBMC,
where **the harness is generated from the function signature rather than
written by hand**.

- Four delivery channels: a multi-architecture container image, a GitHub
  Action, a Claude Code plugin/skill, and the CLI.
- Honest reporting throughout: proofs labelled as bounded, assumptions listed,
  stubbed calls disclosed, vacuous proofs detected, and the checker itself
  probed for known soundness holes before any result is trusted.
- The `linux/arm64` image builds ESBMC from source, because the only prebuilt
  arm64 Linux build available anywhere carries
  [esbmc#6508](https://github.com/esbmc/esbmc/issues/6508) and silently misses
  out-of-bounds writes.
