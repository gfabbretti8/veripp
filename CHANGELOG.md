# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

Two things are worth knowing when reading any entry here: a "verified" from
veripp is a **bounded** proof, and it is only as good as the checker underneath
it. `veripp doctor` probes that checker against known-failing programs on every
run, and refuses to back results from one that cannot detect a planted bug.

## 0.7.0

Results fail closed where they used to fail open, the checker arrives with
the install on Linux arm64 as well as x86_64, counterexamples say who can
reach them and when the harness had a hand in them, and an agent can drive
veripp over MCP. A counterexample can be replayed natively under the
sanitizers, real memory leaks are reported instead of verified, and a first
scan reaches the functions that take callbacks.

### Fixed: a leak was reported as verified
- **A function that leaks memory no longer verifies.** The checker reports
  memory that is never freed where the program ends, which in a generated
  harness is the last line of a `main` nobody wrote -- and a failure located
  in the harness was filed as the harness's own and discarded. So this
  verified, exit 0, with `memory-leak` in the list of checks it ran under:

  ```c
  int dup_len(const char *s) {
      char *copy = malloc(strlen(s) + 1);
      if (!copy) return -1;
      strcpy(copy, s);
      if (copy[0] == '#') return 0;      /* copy is lost here */
      ...
  ```

  Discarding them had a reason: "never freed" is the wrong question for one
  function. `char *dup(s)` is meant to return what it allocates and
  `init(obj)` is meant to leave it in the object, and both are "leaks" at
  the end of a harness. The right question is whether the function *lost*
  the memory, and the checker can answer it (`--no-reachable-memory-leak`)
  once what a caller still holds is reachable. A C harness now copies its
  objects and the return value into statics after the call, so memory that
  was returned, stored through an argument or left in a global is not
  reported, and memory that was dropped is -- at the line that allocated
  it, read back from the trace, rather than at the harness's closing brace.
  The harness of a file that cannot allocate is unchanged.

  Not yet for C++: copying an object there can run a copy constructor, so
  a C++ harness keeps nothing reachable and a leak in a C++ function is
  still filed as an artifact. The README's limits say so.

### Added: replaying a counterexample
- **`--replay` runs the counterexample.** On `verify` and `scan`, the
  inputs are built into a program, compiled with AddressSanitizer and
  UndefinedBehaviorSanitizer, and run. `CONFIRMED` means the compiled
  function fails on those inputs too, with what the sanitizer said and
  whether it was the line the checker named, so the failure is not an
  artifact of the checker's model. A clean run is printed as a reason to
  doubt the counterexample, not as a refutation. `scan --replay` lists
  confirmed findings first and clean ones last, ahead of any model's label,
  because what the compiled function did is not a judgement. Properties no
  sanitizer checks (NaN, comparing pointers into different objects,
  uninitialised reads) say that a clean run means nothing, and so does a
  trace that carries no input values. It is the one thing veripp does that
  executes the code under test, so it only happens when asked for.
- **The program `--repro` writes reproduces more.** Array contents the
  checker printed as an aggregate (`p_buf = { 56, 56, 56, 56 }`) were left
  in a comment, so a parser that over-reads on four digits was handed four
  zeroes and exited cleanly -- which the file's own header calls the sign
  of a false alarm. They are assigned now, values the trace does not
  mention are zero rather than whatever the stack held, and callback
  stand-ins and a set-aside `main` come along so the file compiles.

### Changed: reach
What a first `veripp scan` on someone else's code could not touch. Measured
on inih, three hundred lines and one of the most-copied C libraries there is:
9 of its 42 functions were harnessable and 3 proved; now 28 and 15, and one
of its two counterexamples, which no caller could produce, is gone.

- **Callback parameters are modelled instead of refused.** A function
  pointer parameter, through a typedef (`ini_handler handler`) or written in
  place (`int (*cmp)(const void *, const void *)`), gets a stand-in with its
  signature that returns any value its return type holds and touches none
  of its arguments, so both the "keep going" and the "stop" path of the
  caller are explored. A library's entry points are where callbacks live,
  so these were exactly the functions that receive outside input. What a
  real callback does with its arguments is not modelled, and the
  assumptions say so. One returning a pointer always returns NULL, and the
  assumption says the proof covers the NULL path only; one returning a
  struct by value is still refused. A typedef written two ways under `#if`
  is settled by running the preprocessor.
- **A start and an end pointer are one buffer.** `ini_rstrip(char* s,
  char* end)` and every `(const char *p, const char *end)` parser take two
  pointers into the same memory. Built as two objects, the first `end > s`
  is undefined behaviour of the harness's own making, and it was reported.
  They are paired only on evidence from both the name and the body: the
  second is called an end, and the body compares or subtracts the two.
- **A source with its own `main()` is harnessed.** Command-line tools,
  examples and test drivers were refused whole, with advice to wrap their
  `main` in `#if defined(VERIPP_HAS_OWN_MAIN)` -- an edit to the code under
  test, before anything had run. Their `main` is now compiled under another
  name for the length of the `#include` and never called.
- **`veripp scan` and `veripp accept` default to the current directory.**

### Changed: reporting
- **A counterexample shows the line it fails on**, with the line before it
  and a caret at the column, and paths under the working directory are
  printed relative to it.
- **An untriaged counterexample is no longer labelled `real_bug`.** With no
  model configured, `verify` printed `Diagnosis: real_bug:` followed by the
  property a second time. Nothing had diagnosed anything: it was the
  conservative default. It now reads `Not triaged:` and says what to do
  about it. The JSON keeps `kind` and gains `"triaged": false`.
- **A scan of a directory accounts for every function.** The summary left
  out the functions that could not be harnessed, so inih's read 3 proved, 2
  counterexamples, 4 inconclusive, of 42. It now has the NOT HARNESSABLE row
  and the reasons, as a single-file scan always did, and lists the
  counterexamples themselves, ranked across files, each with its line --
  instead of file names and one command to run per finding.
- **The harness's own `main` is not a caller.** When the function under
  test assigns nothing before a failing library call, the last state in the
  trace outside the checker's model is the harness filling its inputs, and
  the finding was filed at that line of a temporary file. It now says the
  trace does not show the caller.
- **A harness refused for its own `main` is not counted as "overloaded".**
- **A library call after `return` is no longer listed as an unmodelled
  callee.** `return memcmp(a, buf, n);` was read as a declaration of
  `memcmp`, which has none in the unit and a body in the checker's model,
  so the harness said its side effects were not modelled. They are.
- **A failure inside the checker's library model is reported at the
  caller's line.** ESBMC locates a bad `memcmp` at
  `/esbmc-vfs/libc/library/string.c:371 in memcmp`; veripp now also says
  `called from vfs.c:3 in cmp`, recovered from the trace, and `scan`, the
  JSON and so SARIF file the finding at that line in the code under test
  instead of at a path that exists only inside the checker.

### Removed
- **Windows.** The CLI is no longer tested on Windows and
  `veripp install-checker` no longer fetches a Windows checker; on Windows
  it says to use WSL, where the Linux wheel applies, or the container.
  Nothing was ever packaged for Windows, and the Linux and macOS routes are
  the ones people use.

### Changed: harness modelling
- **Invariants are read from the library's own code.** A harness fills
  fields independently and so hands functions states the library never
  builds. Three of the commonest are now inferred, each quoted in the
  assumptions with the line it came from: a count within the capacity it
  is grown against (`if (object->count >= object->item_capacity)`), a
  field used as an `n - 1` mask being a nonzero power of two
  (`hash & (object->cell_capacity - 1)`), and the entries of an array that
  indexes another being below its count or the sentinel the code tests for
  (`cell = object->cells[ix]; if (cell == OBJECT_INVALID_IX) ...;
  object->hashes[cell]`). parson's hash lookups no longer fail on a table
  with capacity 0 or an arbitrary index. `--no-inferred-invariants` turns
  this off.
- **A null entry in a pointer array the harness filled is named as such**,
  also when the code reaches it through a local (`key = object->names[i];
  strlen(key)`).
- **The rest of `<stdint.h>` is scalar.** `uint_least8_t` (nanopb's
  `pb_byte_t`), the `int_fast*` and `int_least*` families, `intmax_t` and
  `intptr_t` get nondeterministic values like `uint8_t` always did.
- **A bounds failure on a length-less `void *` says the bound is the
  harness's.** lz4 indexes `void* tableBase` by a hash; the caller's table
  is 16 KB and the harness's is four bytes. Such counterexamples carry the
  note and rank after findings the harness had no hand in.
- **`scan` suggests `--preprocess` when the text is what blocks it.** When
  most functions are refused for a type veripp cannot construct from the
  source (a struct with `#if` members, a typedef written `} FAR name;`),
  the next-step line gives the command. nanopb goes from 21% to 86%
  harnessable with it, zlib from 52% to 74%; the refusal message for a
  struct with conditional members says the same.
- **`void *` parameters are modelled instead of refused.** Beside a length
  (`const void* src, int srcSize`) a void pointer is that many
  nondeterministic bytes, the shape of memcpy. When the body casts it
  (`(LZ4_stream_t_internal*)state`, or the implicit C conversion
  `struct ctx *c = p;`) the harness builds that object. With neither it
  gets a bounded run of bytes and the bound is stated. lz4's `lz4.c` goes
  from 62% to 92% of functions harnessable; 29 of its 99 were refused for a
  `void *`.
- **A comment inside a parameter list no longer becomes a parameter name.**
  lz4's `int* /* only written when outputDirective == fillOutput */
  inputConsumed` was refused for a parameter called `/* only written when`.
- **An uninitialised read inside the checker's library model is filed as an
  artifact.** ESBMC reports a failure in its own model of fread or fseek
  (`/esbmc-vfs/libc/library/io.c`) for a variable that exists in neither
  the code under test nor the harness; parson's `read_file` was listed as a
  counterexample for `nread`, a name that occurs nowhere in parson.c. A name
  the code declares is still the code's.
- **A plural array beside a bare count is paired.** `JSON_Value **items`
  next to `size_t count` used to be left null (its pointee not a type the
  harness constructs) with the count unbounded, and every getter was
  reported for the null the harness put there. An array-like pointer field
  (to pointers or scalars, not a link to one struct) now takes its
  singular-stem count (`cells` with `cell_capacity`) or the struct's one
  generic count, gets a bounded array, and the count is bounded to match.
  Where two generic counts compete nothing is paired. parson's
  `json_array_get_*` go from eight NULL-pointer counterexamples to proofs.

### Changed: scan and triage
- **Triage is told the mechanical facts first.** The context an LLM sees
  for a counterexample now states the function's linkage and caller count,
  and whether the null dereferenced is one the harness itself left, as facts
  to reason from rather than conclusions.
- **`scan` suggests `--constructors` when the findings call for it.** When
  most counterexamples are in functions handed an object the harness filled
  field by field, the next-step line says so and gives the command: those
  states are often ones no caller produces, and the library's constructors
  rule them out.
- **A null the harness left is named as such.** When a NULL dereference is
  of a pointer field the harness itself set to null (cut at
  `--max-struct-depth`, or a type it could not construct), `scan` tags the
  counterexample with that field and ranks it after nulls the code produced
  on its own, and `verify`'s note names the field the failing line
  dereferences instead of the first null field it finds. Still reported:
  only a caller can say whether that field is ever null.
- **An accepted precondition is checked against the callers.** Where a call
  site passes an integer literal for a parameter the precondition names,
  veripp evaluates the precondition on it; a caller that breaks it is
  reported under the conditional result ("line 6: d = 0 violates `d != 0`")
  in `verify` and `scan`, in text and in JSON, and `verify` exits 3 instead
  of 0 for it: that call reaches the counterexample the precondition was
  meant to exclude. Variable arguments are never judged either way.
- **`scan` says who can reach each counterexample.** Every finding carries
  its linkage (`external`, an entry point for any other translation unit, or
  `static`, reached only through this file) and the number of call sites
  found in the scanned unit and anything linked with it. Entry points are
  listed before static helpers; a static helper with no callers found is
  flagged as such. In the summary, the JSON report, the SARIF message (so a
  pull-request annotation says it) and the MCP result.

### Added
- **`veripp mcp`**: the CLI's `verify`, `scan`, `harness` and `doctor` as
  Model Context Protocol tools over stdio, for Claude Code and any other MCP
  client. Structured arguments in, the `--json` report plus the exit code and
  its meaning out; triage stays off unless the call names a model, since the
  agent on the other end is the one reading the result. Standard library
  only, like the LLM providers.

### Changed: what counts as a proof
- **Every proof is probed for reachability**, in `verify` and in `scan`. A
  harness that verifies is re-run with an always-failing assertion at its
  end: it must fail that (reachable), or it reached nothing (vacuous), or the
  probe did not settle (unconfirmed). Only the first is a proof; the other
  two exit 3, and `scan` lists them apart from timeouts. The probe no longer
  depends on the harness text mentioning an assumption, and a probe that
  times out no longer counts as reachable. `examples/off_by_one.reachable.cpp`,
  a leftover of the old probe, is gone.
- **An LLM's rewritten file is a proposal, not a proof.** When the escalation
  ladder runs out and the model proposes loop invariants, the proposal is
  accepted only if it is the original file plus side-effect-free assertions;
  anything else -- an assume, an edited or deleted line, an assertion that
  assigns or calls -- ends the run inconclusive with the reason. Accepted
  assertions are listed as `llm_invariants`. A frontend rewrite is never run
  as a proof: the result stays a parse error and names the file to review.
- **The scan retry pass never consults the model.** It widens bounds and
  switches strategy only; a counterexample that disappears under an accepted
  precondition is filed as `preconditioned`, never `proved`.
- **The scan cache keys on every input**: every project header reached at
  any depth along the real include path, linked sources, force-included
  headers, and the checker and veripp themselves identified by their bytes
  rather than a version string. `CACHE_VERSION` is 3; old entries are not
  served.
- **`doctor` probes every default check**, nine planted bugs instead of two,
  and passes a probe only when the planted property is the one that failed.
  `verify` remembers the result per checker binary instead of re-probing
  after every proof.
- **Triage runs only with a model you asked for** (`--model`,
  `$VERIPP_LLM_MODEL`, `--llm-base-url` or `$VERIPP_LLM_BASE_URL`). A
  provider's API key in the environment no longer turns it on by itself; the
  note says when a key is present and unused.

### Changed: the checker
- **ESBMC 8.6 everywhere**, pinned by tag and by digest, instead of the
  rolling `weekly` tag. `veripp install-checker`, the GitHub Action, CI, the
  release workflow and the container image all fetch the same release
  binaries, and `install-checker` refuses a download whose SHA-256 is not
  the one veripp pins.
- **The checker is inside veripp's own wheel.** `pip install veripp` on
  Linux x86_64 or arm64 gets a `manylinux` wheel carrying ESBMC at
  `veripp/bin/esbmc`; every other platform gets the pure wheel and
  `install-checker`. The separate `veripp-checker` package and its release
  tag are retired; veripp has no runtime dependencies.
- **Linux arm64 gets a prebuilt checker.** ESBMC 8.6 publishes a static
  arm64 Linux binary, so `install-checker` installs it, the Action runs on
  arm64 runners such as `ubuntu-24.04-arm`, and the `linux/arm64` image
  downloads it instead of compiling ESBMC from source. The from-source recipe
  is gone from the Dockerfile; it remains in the git history.
- The Action's checker cache is keyed on the runner's architecture as well
  as its OS, so an x86_64 cache can no longer be restored on an arm64 runner.
- A missing checker says where veripp looked and prints the install command
  for this machine, with exit 2 from `verify`, instead of a traceback
  recommending `brew install esbmc` (which is the unsound 8.4).

### Changed: the GitHub Action
- **A vacuous proof fails the job** under the default `fail-on: counterexample`,
  with a `vacuous` output for later steps. New `fail-on: inconclusive` fails
  on anything short of a proof; an unknown `fail-on` value is an error.
- On macOS the install step fails at once and says what works, instead of
  unpacking a release zip that cannot run outside Homebrew's prefix.

### Fixed
- `verify` and `scan` built their checker configuration separately and had
  drifted: include directories were passed twice, `-U` from the compilation
  database reached one and not the other, and a C standard never reached a
  C harness. One builder now, and the verdict names the standard that
  applied.
- `--preprocess` hands the preprocessor the build's `-D`, `-U` and `-include`
  flags, so a function behind `#if FEATURE` is no longer reported as "not in
  this build" under the very flags that enable it.
- Scan threads no longer empty each other's enum types: the type information
  is a value computed once per file, not a module-level set.
- `--keep-harness` keeps the harness; without it the scratch directory is
  removed, for `verify` and for every file a scan visits.
- Sources in any encoding are read; one function veripp trips over no longer
  costs the rest of its file.
- Each SARIF result states its own bounds (a tree scan stated none) and maps
  leaks, use after free, invalid frees, uninitialised reads, undefined shifts
  and NaN to rules of their own, with the CWEs ESBMC reports.
- The CLI parses on Python 3.10 and 3.11 again; CI tests 3.10, 3.12 and 3.14
  and lints for syntax newer than the floor.
- Checker-backed tests run whenever veripp can find a checker, not only when
  one is on PATH.
- The skill documents every flag, and a test holds it to that.

## 0.6.0

Fewer false counterexamples on real-world C, and new options for C APIs that
the harness could not model before.

### Added
- **`--constructors`** builds object parameters by calling the library's own
  constructors, and lets the solver choose between them, instead of filling
  fields with arbitrary values. Handle types that no constructor returns are
  reached through an owner that does. The object is freed with the library's
  own deallocator. This is off by default because it costs solver time.
- **`verify --sequence TYPE`** (with `--sequence-call GLOB`) constructs a C
  handle, drives a bounded nondeterministic sequence of the API calls that
  take it, and then frees it.
- **`--preprocess`** runs the C preprocessor, so structs with members inside
  `#if` blocks resolve to the configured build instead of being refused. When
  the target function is compiled out, veripp now says "not in this build".
- **`--setup CALL`** runs a linked module's initialiser before the function
  under test, and records it in the assumptions.
- **`--unterminated`** models `char *` parameters and fields as raw bytes
  with no NUL terminator, for parsers that are handed data off the wire.
- **`scan` lists length parameters a function never reads**, or reads only
  inside an assertion, as leads to look at by hand.
- **`scan` sorts writes before reads** within each triage verdict, and labels
  each counterexample with its access kind.
- **`doctor` warns on arm64 hosts** where ESBMC cannot parse ARM intrinsic
  headers, and points to the `linux/amd64` container.

### Changed: harness modelling
- C strings: `char *` and `unsigned char *` are modelled as NUL-terminated
  when the body treats them that way (including tests at any index and
  struct fields). `const unsigned char *` defaults to binary data. Bounded
  routines such as `strncmp` are no longer taken as evidence of a
  terminator.
- Lengths: the scan for a buffer's length stops at the next pointer
  parameter. Signed lengths are assumed non-negative, and this is disclosed.
  Every length field in an object is bounded, not only the first one.
  Run-together names like `userlen` are paired with their buffer.
- Output buffers with no length parameter are sized from fixed-size writes in
  the body, from how far the body advances through them, and from the
  requirements of callees they are passed to.
- Cursors: `(T **p, T *start)` writers are modelled, in both directions. A
  cursor struct's offset is kept within its own buffer.
- C structs: `struct` tags are kept in declarations, and enum tags are
  spelled out. A base struct placed first is modelled as the larger object
  when the file uses it that way. `void *` fields become byte buffers
  instead of null. Callback and vtable members get no-op stubs, and those
  stubs are disclosed. In/out `T **` parameters hold one object.
- Allocator hooks initialised to `malloc`/`free`, including struct tables of
  hooks, are pointed at wrappers the checker can see through. The rewrite is
  disclosed.
- Extern arrays with no definition in scope are disclosed as unmodelled.
- Function-like macros are no longer counted as function definitions, so
  coverage percentages are correct. Body braces are matched as the compiler
  sees them.

### Changed: triage and reporting
- A counterexample classified as a harness artifact is re-checked with
  `--multi-property`, so whatever the artifact was hiding is still reported.
- A counterexample that comes from an allocator with no body is classified as
  an artifact only when such an allocator really is missing from the run.
- A retried function no longer keeps its first attempt's read/write label.
- `verify FILE` without `--function` no longer calls findings in the user's
  own file harness artifacts.

### Fixed
- Wheels built on Windows now carry POSIX entry names. Also fixed: two
  Windows-only test failures.
- `scan` derives per-file facts once per file instead of once per function.

### Docs
- The README is shorter and restates only what the code does.

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
