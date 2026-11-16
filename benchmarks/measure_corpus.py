#!/usr/bin/env python3
"""Re-measure the table in CORPUS.md: one `veripp scan` per library.

    ./benchmarks/measure_corpus.py CORPUS OUT            # all of them
    ./benchmarks/measure_corpus.py CORPUS OUT --only lz4

Each library is cloned at its default branch into CORPUS, given the header
its own build would generate where it needs one, and scanned with veripp's
defaults: no flags beyond the include paths and defines the file cannot be
parsed without, no model. That is what a first `veripp scan` gives a user,
which is the number worth publishing. OUT gets the JSON report and the
readable output of each scan, and the script prints the table rows.

Slow: about two hours for the ten on four cores.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Library:
    name: str
    repo: str
    sources: list[str]                      # relative to the checkout
    includes: list[str] = field(default_factory=list)   # relative to CORPUS
    defines: list[str] = field(default_factory=list)
    prepare: str = ""                       # shell, run once in the checkout


LIBRARIES = [
    Library("libpng", "pnggroup/libpng", ["png.c"],
            includes=["libpng", "generated/libpng"],
            # The header libpng's build writes; it ships a default copy.
            prepare="mkdir -p ../generated/libpng && "
                    "cp pnglibconf.h.prebuilt ../generated/libpng/pnglibconf.h"),
    Library("lodepng", "lvandeve/lodepng", ["lodepng.cpp"]),
    Library("cJSON", "DaveGamble/cJSON", ["cJSON.c"]),
    Library("parson", "kgabis/parson", ["parson.c"]),
    Library("lz4", "lz4/lz4", ["lib/lz4.c"]),
    Library("tinyexpr", "codeplea/tinyexpr", ["tinyexpr.c"]),
    Library("zlib", "madler/zlib",
            ["adler32.c", "crc32.c", "compress.c", "uncompr.c",
             "deflate.c", "inflate.c", "trees.c", "zutil.c"]),
    Library("giflib", "mirrorer/giflib", ["lib/dgif_lib.c"],
            includes=["giflib/lib"]),
    Library("jansson", "akheron/jansson", ["src/value.c"],
            includes=["jansson/src", "jansson/build/include"],
            defines=["HAVE_STDINT_H"],
            prepare="mkdir -p build && cd build && "
                    "cmake .. -DJANSSON_BUILD_DOCS=OFF >/dev/null"),
    Library("libyaml", "yaml/libyaml", ["src/api.c"],
            includes=["libyaml/include", "libyaml/build/include"],
            defines=["HAVE_CONFIG_H"],
            prepare="mkdir -p build && cd build && cmake .. >/dev/null"),
]

COUNTED = ("proved", "preconditioned", "counterexamples", "no_caller_reaches",
           "artifacts", "inconclusive")


def checkout(lib: Library, corpus: Path) -> Path:
    dest = corpus / lib.repo.split("/")[1]
    if not dest.is_dir():
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        f"https://github.com/{lib.repo}.git", str(dest)], check=True)
        if lib.prepare:
            subprocess.run(lib.prepare, shell=True, cwd=dest, check=True)
    return dest


def scan(lib: Library, corpus: Path, out: Path) -> dict:
    """One library's totals, summed over its source files."""
    dest = checkout(lib, corpus)
    commit = subprocess.run(["git", "-C", str(dest), "log", "-1", "--format=%h"],
                            capture_output=True, text=True).stdout.strip()
    total = dict.fromkeys(COUNTED, 0)
    total.update(functions=0, refused=0, seconds=0.0, commit=commit, timeouts=0)
    for source in lib.sources:
        stem = f"{lib.name}.{Path(source).stem}"
        report = out / f"{stem}.json"
        command = [sys.executable, "-m", "veripp.cli", "scan", str(dest / source),
                   "--no-llm", "--no-cache", "--json-out", str(report)]
        for inc in lib.includes:
            command += ["-I", str(corpus / inc)]
        for define in lib.defines:
            command += ["-D", define]
        started = time.monotonic()
        with open(out / f"{stem}.txt", "w", encoding="utf-8") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        total["seconds"] += time.monotonic() - started
        if not report.is_file():
            print(f"  {stem}: no report; see {stem}.txt", flush=True)
            continue
        data = json.loads(report.read_text(encoding="utf-8"))
        total["functions"] += data["candidates"]
        for key in COUNTED:
            total[key] += len(data[key])
        total["refused"] += sum(data["not_harnessable"].values())
        total["timeouts"] += sum(
            1 for item in data["inconclusive"] if "timeout" in item["outcome"]
        )
    return total


def row(lib: Library, t: dict) -> str:
    harnessable = t["functions"] - t["refused"]
    share = f"{100 * harnessable / t['functions']:.0f}%" if t["functions"] else "n/a"
    return (
        f"| {lib.name} | {t['functions']} | {t['proved']} | {t['counterexamples']} | "
        f"{share} | {t['artifacts']} | {t['no_caller_reaches']} | "
        f"{t['inconclusive']} ({t['timeouts']} timeouts) | {t['preconditioned']} | "
        f"`{t['commit']}` | {t['seconds'] / 60:.0f} min |"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--only", help="a library name")
    args = ap.parse_args()
    if not shutil.which("git"):
        ap.error("git is needed to clone the libraries")
    args.corpus.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)

    print("| library | functions | proved | counterexamples | harnessable | "
          "artifacts | no caller reaches | inconclusive | preconditioned | "
          "commit | time |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|", flush=True)
    for lib in LIBRARIES:
        if args.only and args.only != lib.name:
            continue
        totals = scan(lib, args.corpus, args.out)
        (args.out / f"{lib.name}.totals.json").write_text(
            json.dumps(totals, indent=1), encoding="utf-8")
        print(row(lib, totals), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
