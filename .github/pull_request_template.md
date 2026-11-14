## What this changes

## Why

## How it was checked

<!-- The command you ran and what it printed, a test, or the library you
     scanned. For a harness change, a real library beats a fixture. -->

## Checklist

- [ ] `uv run pytest -q` passes, with ESBMC installed (`uv run veripp doctor`)
- [ ] `ruff check .` and `mypy src` are clean, at the versions `ci.yml` pins
- [ ] Anything the harness now assumes is printed with the result
- [ ] User-visible changes have a `CHANGELOG.md` entry under *Unreleased*
