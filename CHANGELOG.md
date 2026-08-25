# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

Two things are worth knowing when reading any entry here: a "verified" from
veripp is a **bounded** proof, and it is only as good as the checker underneath
it. `veripp doctor` probes that checker against known-failing programs on every
run, and refuses to back results from one that cannot detect a planted bug.

## Unreleased

### Added
- `npx veripp-skill` installs the agent skill with nothing but Node — into
  `./.claude/skills/veripp`, or `--global` for every project. It installs the
  skill, not the verifier, and says so: the verifier is a Python program
  needing ESBMC, and the skill's own `install.sh` reports what getting it
  would cost before doing anything.

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
