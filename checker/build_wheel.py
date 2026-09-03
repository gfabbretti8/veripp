#!/usr/bin/env python3
"""Assemble a `veripp-checker` wheel from a built checker tree.

A wheel is a zip with three metadata files, so this writes one directly
rather than pulling in a build backend: the payload is a native binary that
no backend would compile anyway, and the parts that actually matter here --
the platform tag, the executable bit, and the licence texts -- are exactly
the parts a generic backend gets wrong.

    python build_wheel.py --payload out/ --plat manylinux_2_38_aarch64 \
                          --license path/to/COPYING

`--license` is required on purpose. ESBMC is a derivative of CBMC under
BSD-4-clause, whose advertising clause obliges the notice to travel with the
binary; a wheel that omits it should not be buildable by accident.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import zipfile
from pathlib import Path

DISTRIBUTION = "veripp_checker"

METADATA = """\
Metadata-Version: 2.1
Name: veripp-checker
Version: {version}
Summary: The ESBMC model checker, bundled for `pip install veripp[checker]`
License-Expression: Apache-2.0 AND BSD-4-Clause AND MIT
Requires-Python: >=3.10
Project-URL: Homepage, https://github.com/gfabbretti8/veripp
Project-URL: ESBMC, https://github.com/esbmc/esbmc
Classifier: Development Status :: 3 - Alpha
Classifier: Intended Audience :: Developers
Classifier: Topic :: Software Development :: Quality Assurance
Description-Content-Type: text/markdown

{readme}
"""

WHEEL = """\
Wheel-Version: 1.0
Generator: veripp build_wheel.py
Root-Is-Purelib: false
Tag: py3-none-{plat}
"""


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{name},sha256={digest.decode()},{len(data)}"


def build(payload: Path, plat: str, version: str, license_file: Path,
          outdir: Path, package_src: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    wheel_name = f"{DISTRIBUTION}-{version}-py3-none-{plat}.whl"
    dist_info = f"{DISTRIBUTION}-{version}.dist-info"
    records: list[str] = []

    readme = (package_src.parent.parent / "README.md")
    readme_text = readme.read_text(encoding="utf-8") if readme.is_file() else ""

    with zipfile.ZipFile(outdir / wheel_name, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as wheel:

        def write(name: str, data: bytes, mode: int = 0o644) -> None:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            # High 16 bits are the unix mode, and S_IFREG must be part of it:
            # without the file-type bits pip installs the checker without its
            # executable bit, and veripp then sees no checker at all.
            info.external_attr = (mode | 0o100000) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            wheel.writestr(info, data)
            records.append(_record_line(name, data))

        for source in sorted(package_src.rglob("*.py")):
            rel = source.relative_to(package_src.parent)
            write(str(rel), source.read_bytes())

        # The binary and the libraries it was linked against. The executable
        # bit has to be set in the archive: pip preserves what it finds, and a
        # checker that arrives non-executable looks to veripp like no checker.
        for source in sorted(payload.rglob("*")) if payload.is_dir() else ():
            if not source.is_file():
                continue
            rel = Path(DISTRIBUTION) / source.relative_to(payload)
            executable = source.parent.name == "bin" or ".so" in source.name
            write(str(rel), source.read_bytes(), 0o755 if executable else 0o644)

        write(f"{dist_info}/METADATA",
              METADATA.format(version=version, readme=readme_text).encode())
        write(f"{dist_info}/WHEEL", WHEEL.format(plat=plat).encode())
        write(f"{dist_info}/licenses/COPYING.esbmc",
              license_file.read_bytes())

        records.append(f"{dist_info}/RECORD,,")
        wheel.writestr(f"{dist_info}/RECORD", "\n".join(records) + "\n")

    return outdir / wheel_name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payload", type=Path,
                    help="tree containing bin/esbmc and lib/")
    ap.add_argument("--fallback", action="store_true",
                    help="build the binary-free py3-none-any wheel instead, so "
                         "that resolution succeeds on platforms no wheel "
                         "targets and veripp stays installable there")
    ap.add_argument("--plat",
                    help="wheel platform tag, e.g. manylinux_2_38_x86_64")
    ap.add_argument("--version", default="0.1.0")
    ap.add_argument("--license", type=Path, required=True,
                    help="ESBMC's COPYING; shipped inside the wheel")
    ap.add_argument("--outdir", type=Path, default=Path("dist"))
    args = ap.parse_args()

    here = Path(__file__).resolve().parent

    if args.fallback:
        # No binary, tagged `any`. pip ranks a platform wheel above it, so
        # this is chosen only where nothing else fits -- and there it keeps
        # `pip install veripp` working instead of failing to resolve.
        # esbmc_path() then returns None and veripp looks elsewhere.
        payload = Path(argparse.__file__).parent / "__nonexistent__"
        plat = "any"
    else:
        if args.payload is None or not args.plat:
            raise SystemExit("--payload and --plat are required "
                             "unless --fallback is given")
        payload = args.payload
        plat = args.plat
        if not any((payload / "bin").glob("esbmc*")):
            raise SystemExit(f"no bin/esbmc under {payload}")

    built = build(payload, plat, args.version, args.license,
                  args.outdir, here / "src" / DISTRIBUTION)
    size = built.stat().st_size / 1024 / 1024
    print(f"{built}  ({size:.1f} MB)")
    if size > 100:
        print("  note: over PyPI's 100 MB default per-file limit; publishing "
              "this needs a limit increase request")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
