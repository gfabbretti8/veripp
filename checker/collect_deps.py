#!/usr/bin/env python3
"""Collect a binary and the shared libraries it needs, without running it.

`ldd` resolves dependencies by invoking the dynamic loader, which means it
cannot be used on a binary for a foreign architecture -- under emulation it
crashes rather than answering. This reads DT_NEEDED straight out of the ELF
dynamic section instead, so the same script works whether the build is
native or being assembled from a cross-architecture image.

    python3 collect_deps.py <binary> <outdir>
"""

from __future__ import annotations

import shutil
import struct
import sys
from pathlib import Path

#: Provided by the host system on any glibc target, and bundling them is how
#: you get a wheel that segfaults on a machine with a different loader.
SYSTEM = ("libc.so", "libm.so", "libpthread", "libdl.so", "librt.so",
          "ld-linux", "libresolv", "libnsl")

SEARCH = ("/opt/esbmc/lib", "/usr/lib", "/lib", "/usr/local/lib")


def needed(path: Path) -> list[str]:
    """The DT_NEEDED sonames recorded in an ELF file."""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2:
        return []                                    # not 64-bit ELF
    little = data[5] == 1
    end = "<" if little else ">"
    e_shoff, = struct.unpack_from(f"{end}Q", data, 0x28)
    e_shentsize, e_shnum = struct.unpack_from(f"{end}HH", data, 0x3A)

    dynamic = dynstr = None
    for index in range(e_shnum):
        base = e_shoff + index * e_shentsize
        sh_type, = struct.unpack_from(f"{end}I", data, base + 4)
        sh_offset, = struct.unpack_from(f"{end}Q", data, base + 0x18)
        sh_size, = struct.unpack_from(f"{end}Q", data, base + 0x20)
        sh_link, = struct.unpack_from(f"{end}I", data, base + 0x28)
        if sh_type == 6:                             # SHT_DYNAMIC
            dynamic = (sh_offset, sh_size, sh_link)
        elif sh_type == 3 and dynstr is None:        # SHT_STRTAB candidate
            dynstr = (sh_offset, sh_size)
    if dynamic is None:
        return []

    offset, size, link = dynamic
    base = e_shoff + link * e_shentsize
    str_offset, = struct.unpack_from(f"{end}Q", data, base + 0x18)
    str_size, = struct.unpack_from(f"{end}Q", data, base + 0x20)
    strtab = data[str_offset:str_offset + str_size]

    names = []
    for position in range(offset, offset + size, 16):
        tag, value = struct.unpack_from(f"{end}QQ", data, position)
        if tag == 0:
            break
        if tag == 1:                                 # DT_NEEDED
            name = strtab[value:strtab.index(b"\0", value)].decode()
            names.append(name)
    return names


def locate(soname: str) -> Path | None:
    for root in SEARCH:
        for candidate in Path(root).rglob(soname):
            if candidate.is_file():
                return candidate
    return None


def main() -> int:
    binary, outdir = Path(sys.argv[1]), Path(sys.argv[2])
    (outdir / "bin").mkdir(parents=True, exist_ok=True)
    (outdir / "lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, outdir / "bin" / binary.name)

    seen: set[str] = set()
    queue = list(needed(binary))
    while queue:
        soname = queue.pop()
        if soname in seen or any(s in soname for s in SYSTEM):
            continue
        seen.add(soname)
        found = locate(soname)
        if found is None:
            print(f"warning: {soname} not found", file=sys.stderr)
            continue
        shutil.copy2(found, outdir / "lib" / soname)
        queue.extend(needed(found))

    print(f"{len(seen)} libraries collected", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
