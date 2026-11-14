# Security

## Reporting a vulnerability

Report it privately through
[GitHub's vulnerability reporting](https://github.com/gfabbretti8/veripp/security/advisories/new),
not in a public issue. You should get a reply within a week. Fixes go into
the next release, and the changelog entry credits the reporter unless they
ask otherwise.

Only the latest release is supported.

## What counts

- **A wrong `verified`.** veripp exists to be trusted when it says a function
  is free of a class of bug, so a result that says `verified` for code that
  has one, within the bounds and assumptions it printed, is treated as a
  security bug. That includes a vacuous proof that was not caught, and a
  checker with a known soundness hole that `veripp doctor` accepts. If the
  miss is the checker's rather than veripp's, report it here anyway: veripp
  needs a probe for it, and the report is passed on to
  [ESBMC](https://github.com/esbmc/esbmc/issues).
- **Running or leaking what it should not.** veripp reads source code that
  may be hostile. It executes the code under test only when asked to
  (`--replay`), and sends code to a model only when one is configured
  (`--model`, `$VERIPP_LLM_MODEL`, or an endpoint). Anything that makes
  either happen without being asked is a vulnerability, and so is anything
  in the checker download, the GitHub Action or the container image that
  runs code other than the pinned one.

A false counterexample is a bug, not a vulnerability: open an ordinary issue.
