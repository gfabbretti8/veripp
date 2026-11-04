#!/usr/bin/env python3
"""Turn veripp's pure wheel into a platform wheel that carries the checker.

`uv build` makes `veripp-X.Y.Z-py3-none-any.whl`: Python only, installable
anywhere, and without a checker. This takes that wheel and a payload from
checker/fetch_payload.sh -- ESBMC's own release binary for one platform,
digest-checked, stripped on Linux, with the DLL it needs on Windows -- and
writes `veripp-X.Y.Z-py3-none-<plat>.whl` with everything from the
payload's bin/ under `veripp/bin/` and the release's licence notices beside
it. pip prefers the platform wheel
where one matches and falls back to the pure one everywhere else, which is
what lets `pip install veripp` be the whole installation on Linux and still
resolve on macOS.

    python bundle_wheel.py --wheel dist/veripp-0.7.0-py3-none-any.whl \
                           --payload payload --plat manylinux_2_17_x86_64

Nothing is rebuilt: every entry of the pure wheel is copied as it is, the
checker and its notices are added with the executable bit set in the
archive (pip preserves what it finds, and a checker that arrives 0644 looks
to veripp like no checker), the WHEEL file is retagged, and RECORD is
rewritten to cover everything.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
import zipfile
from pathlib import Path

PACKAGE = "veripp"


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{name},sha256={digest.decode()},{len(data)}"


def bundle(wheel: Path, payload: Path, plat: str, outdir: Path) -> Path:
    match = re.fullmatch(r"(?P<dist>[^-]+)-(?P<ver>[^-]+)-py3-none-any\.whl", wheel.name)
    if match is None:
        raise SystemExit(f"{wheel.name} is not a py3-none-any wheel")
    if match["dist"] != PACKAGE:
        raise SystemExit(f"{wheel.name} is not a {PACKAGE} wheel")
    binaries = sorted(p for p in (payload / "bin").glob("*") if p.is_file()) if (payload / "bin").is_dir() else []
    if not any(p.name in ("esbmc", "esbmc.exe") for p in binaries):
        raise SystemExit(f"no bin/esbmc in {payload}: nothing to bundle")
    notices = sorted(p for p in (payload / "licenses").glob("*") if p.is_file())
    if not any(p.name == "COPYING" for p in notices):
        # ESBMC derives from CBMC under BSD-4-clause, whose advertising
        # clause obliges the notice to travel with the binary.
        raise SystemExit(f"no licenses/COPYING in {payload}: refusing to ship the binary without its notices")

    dist_info = f"{match['dist']}-{match['ver']}.dist-info"
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{match['dist']}-{match['ver']}-py3-none-{plat}.whl"
    records: list[str] = []

    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED, compresslevel=9
    ) as dst:

        def write(name: str, data: bytes, mode: int) -> None:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            # High 16 bits are the unix mode, and S_IFREG must be part of it:
            # without the file-type bits pip installs the file without its
            # executable bit.
            info.external_attr = (mode | 0o100000) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, data)
            records.append(_record_line(name, data))

        for info in src.infolist():
            if info.filename == f"{dist_info}/RECORD":
                continue
            data = src.read(info)
            if info.filename == f"{dist_info}/WHEEL":
                data = _retag(data.decode(), plat).encode()
            mode = (info.external_attr >> 16) & 0o7777 or 0o644
            write(info.filename, data, mode)

        # as_posix(), always: zip entry names are forward-slash separated by
        # specification, whatever the build host.
        # The checker, and on Windows the DLL beside it. Everything in bin/
        # is marked executable: pip preserves the mode, and a DLL's mode is
        # immaterial on Windows but harmless elsewhere.
        for binary in binaries:
            write(f"{PACKAGE}/bin/{binary.name}", binary.read_bytes(), 0o755)
        for notice in notices:
            write(f"{PACKAGE}/bin/licenses/{notice.name}", notice.read_bytes(), 0o644)

        records.append(f"{dist_info}/RECORD,,")
        dst.writestr(f"{dist_info}/RECORD", "\n".join(records) + "\n")
    return out


def _retag(wheel_file: str, plat: str) -> str:
    lines = []
    for line in wheel_file.splitlines():
        if line.startswith("Tag:"):
            line = f"Tag: py3-none-{plat}"
        elif line.startswith("Root-Is-Purelib:"):
            # The checker is a platform binary, so this is a platlib wheel.
            line = "Root-Is-Purelib: false"
        lines.append(line)
    if not any(line.startswith("Tag:") for line in lines):
        raise SystemExit("the pure wheel's WHEEL file names no Tag")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wheel", type=Path, required=True,
                    help="the py3-none-any wheel from `uv build`")
    ap.add_argument("--payload", type=Path, required=True,
                    help="tree from fetch_payload.sh: bin/esbmc and licenses/")
    ap.add_argument("--plat", required=True,
                    help="platform tag, e.g. manylinux_2_17_x86_64")
    ap.add_argument("--outdir", type=Path, default=Path("dist"))
    args = ap.parse_args()
    out = bundle(args.wheel, args.payload, args.plat, args.outdir)
    print(f"{out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
