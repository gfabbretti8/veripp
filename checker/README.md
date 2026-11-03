# The bundled checker

`pip install veripp` is the entire installation on Linux because veripp's
platform wheels carry the [ESBMC](https://esbmc.org) model checker inside
the package, at `veripp/bin/esbmc`. This directory holds what makes those
wheels.

- `esbmc-linux.zip.sha256`, `esbmc-linux-armv8.zip.sha256`: the SHA-256 of
  the ESBMC release assets veripp pins -- one per Linux architecture. The
  release tag is `ESBMC_RELEASE` in `src/veripp/checker.py`, the same digests
  are in `PINNED_SHA256` there for `veripp install-checker`, and CI, the
  release workflow and the container image check downloads against these
  files. A test keeps all of them in step.
- `build_linux_payload.sh <x86_64|aarch64> <outdir>`: downloads the release
  zip for an architecture, checks it against the pin, refuses it if it
  carries MathSAT or Yices (ESBMC's own `COPYING` notes those may not be
  redistributed freely; the release builds ship Z3 and Boolector, both MIT),
  strips the static binary and keeps the release's licence notices beside it.
- `bundle_wheel.py`: takes the pure `py3-none-any` wheel that `uv build`
  makes and that payload, and writes `veripp-X.Y.Z-py3-none-manylinux_2_17_<arch>.whl`
  with the binary and its notices added, the WHEEL file retagged and RECORD
  rewritten. Nothing is rebuilt and no compiler is involved.

`.github/workflows/release.yml` does this on a native runner per
architecture, pip-installs each wheel in a clean container, runs
`veripp doctor` and a real verification there, and publishes the two platform
wheels together with the pure wheel and the sdist -- never a subset. pip
prefers the platform wheel where one matches and falls back to the pure one
elsewhere (macOS, other architectures, source installs), where
`veripp doctor` says what to run instead.

## Why inside veripp, and not a separate package

veripp 0.6 shipped the binary as `veripp-checker`, a dependency with its own
release tag, so that veripp's frequent releases did not re-upload a binary
that rarely changes. That meant two tags to sequence, a second package to
version, and a pipeline that copied the binary out of the container image.
The storage cost of bundling is about 105 MB per veripp release, well inside
PyPI's project quota, and in exchange there is one package, one version, and
no way to end up with a veripp and a checker that were not tested together.

## Building one locally

```bash
uv build --wheel --out-dir pure
./checker/build_linux_payload.sh x86_64 payload      # needs curl, unzip, strip
python3 checker/bundle_wheel.py --wheel pure/veripp-*-py3-none-any.whl \
  --payload payload --plat manylinux_2_17_x86_64 --outdir dist
pip install dist/veripp-*-manylinux*.whl && veripp doctor
```

Run the payload step on a machine of the target architecture, or with
multiarch binutils, since `strip` has to understand the binary. The wheels
measure about 61 MB for x86_64 and 43 MB for aarch64; the weight is Clang,
which ESBMC uses as its C/C++ frontend. PyPI's per-file default is 100 MB.

macOS and Windows wheels are not built. ESBMC's Windows release zip is
relocatable and `veripp install-checker` fetches it; the macOS zip links
Homebrew's libraries by absolute path, so there the route is
`brew install --HEAD esbmc`.
