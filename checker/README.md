# veripp-checker

The [ESBMC](https://esbmc.org) model checker, packaged as a wheel so that

```bash
pip install veripp
```

is the entire installation of [veripp](https://github.com/gfabbretti8/veripp).

The binary is ESBMC's own static Linux release build (8.6, for x86_64 and
arm64), fetched straight from the release and checked against the digest
veripp pins — the same bytes CI tests against and the container image ships.
It carries Z3 and Boolector and **no MathSAT or Yices**;
`build_linux_payload.sh` checks that before packaging, since ESBMC's own
`COPYING` notes that MathSAT is academic/non-commercial and Yices is
personal-use or GPL3, while Z3 and Boolector are MIT. ESBMC's own code is
Apache-2.0 and the CBMC base it derives from is BSD-4-clause. All of those
notices ship inside the wheel.

Every wheel is built only from a checker that passes veripp's soundness
probes: known-failing programs that the checker must reject. A build that
misses a planted bug is never published, because every result obtained from it
would be a false proof.

```python
import veripp_checker
veripp_checker.esbmc_path()   # -> "/.../site-packages/veripp_checker/bin/esbmc"
```

## Publishing

The wheels are built by `.github/workflows/checker-wheels.yml`, which keeps a
wheel only if a clean container that `pip install`s it passes `veripp doctor`.
Nothing is uploaded automatically.

Order matters. Until `veripp-checker` exists on an index, veripp **must not**
depend on it: a dependency naming an unresolvable package breaks
`uv sync` and `pip install veripp` alike. So:

1. build and check the wheels (`workflow_dispatch` on that workflow);
2. publish `veripp-checker`;
3. only then add to veripp's `pyproject.toml`, as a real dependency rather
   than an extra:

   ```toml
   dependencies = ["veripp-checker>=0.1"]
   ```

4. release veripp.

Publish the `py3-none-any` wheel **together with** the platform wheels, never
after. pip ranks a platform wheel above `any`, so the fallback is chosen only
where nothing else fits — and it is the reason the dependency can be
unconditional. Without it, `pip install veripp` would fail outright on every
platform no checker is built for.

## Building one locally

```bash
./checker/build_linux_payload.sh aarch64 payload   # or x86_64; needs curl, unzip, strip
python3 checker/build_wheel.py --payload payload \
  --plat manylinux_2_17_aarch64 --license payload/licenses/COPYING --outdir dist
```

Nothing here needs Docker or a compiler: the script downloads the release
zip for the architecture, checks it against `checker/<asset>.sha256`,
refuses it if it carries MathSAT or Yices, and strips it. Run it on a
machine of the target architecture (or with multiarch binutils), since
`strip` has to understand the binary.

The payload is one static binary; the weight is Clang, which ESBMC uses as
its C/C++ frontend, not ESBMC itself. The 0.1.0 arm64 wheel, built from
source with Z3 only, measured 87 MB; PyPI's per-file default is 100 MB, so
check the size before tagging a checker release.

macOS and Windows are not built yet. macOS should be the easiest of the three:
its release zip is 10 MB and is non-relocatable only because it links
Homebrew's dylibs, which is exactly what `delocate` exists to fix.
