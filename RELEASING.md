# Releasing

Everything below has been validated except the upload itself, which needs
credentials this repository does not carry.

## Before tagging

```bash
uv run pytest -q                 # all tests, including the ESBMC-backed ones
uv run veripp doctor             # must not report a soundness hole
./demo/cve-2019-13223/run.sh     # the demo the README leads with
```

The soundness check matters most: a release built against an ESBMC that
silently misses a class of bug would ship proofs that are not proofs. `doctor`
exits non-zero in that case for exactly this reason.

## Build and check

```bash
uv build
uv run --with twine twine check dist/*
```

Both artifacts must pass. `twine check` catches the failure that is otherwise
invisible until the project page is live: a README that does not render.

## Upload

```bash
uv run --with twine twine upload dist/*
```

Use a PyPI API token (`__token__` as the username). Then confirm the thing
users will actually do works:

```bash
uvx veripp doctor
```

## Version

`pyproject.toml` is the only place the version lives. Bump it before building;
PyPI refuses a re-upload of an existing version, so a mistake costs a new
number rather than a fix.

## What the README promises

The README is the PyPI project page, so its links must be absolute — relative
ones resolve on GitHub and 404 on PyPI. It also tells people to run
`examples/*.cpp`, so those ship in the sdist; check they are still there after
any packaging change:

```bash
tar tzf dist/*.tar.gz | grep examples/
```
