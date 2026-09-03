# veripp-checker

The [ESBMC](https://esbmc.org) model checker, packaged as a wheel so that

```bash
pip install veripp[checker]
```

is the entire installation of [veripp](https://github.com/gfabbretti8/veripp).

This is a **slim build: Z3 only**. Boolector, Bitwuzla, MathSAT and Yices are
not included. That keeps the wheel small, and it keeps redistribution simple —
ESBMC's own `COPYING` notes that MathSAT is academic/non-commercial and Yices
is personal-use or GPL3, while Z3 is MIT. ESBMC's own code is Apache-2.0 and
the CBMC base it derives from is BSD-4-clause. All of those notices ship
inside the wheel.

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
declare a `checker` extra: an extra naming an unresolvable package breaks
`uv sync` and `pip install veripp[checker]` alike. So:

1. build and check the wheels (`workflow_dispatch` on that workflow);
2. publish `veripp-checker`;
3. only then add to veripp's `pyproject.toml`:

   ```toml
   checker = ["veripp-checker>=0.1"]
   ```

4. release veripp.

## Building one locally

```bash
./checker/build_linux_payload.sh ghcr.io/gfabbretti8/veripp payload
curl -fsSL -o COPYING.esbmc \
  https://raw.githubusercontent.com/esbmc/esbmc/master/COPYING
python3 checker/build_wheel.py --payload payload \
  --plat manylinux_2_38_aarch64 --license COPYING.esbmc --outdir dist
```

Measured on arm64: a 16 MB binary plus 214 MB of LLVM, Clang and Z3, which
compresses to an **87 MB wheel** — under PyPI's 100 MB per-file default, but
not by much. The weight is Clang, which ESBMC uses as its C/C++ frontend, not
ESBMC itself.

macOS and Windows are not built yet. macOS should be the easiest of the three:
its release zip is 10 MB and is non-relocatable only because it links
Homebrew's dylibs, which is exactly what `delocate` exists to fix.
