"""compile_commands.json: build a real project's flags into a verification run.

A single file is rarely self-contained. The build system already knows the
include paths, the defines and the language standard each translation unit
needs, and it publishes them in a clang compilation database. Reading that is
the difference between "works on the examples" and "works on your project".
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

#: Flags worth forwarding to ESBMC. Everything else a compiler is told
#: (-O2, -Wall, -fPIC, -c, -o, -MMD ...) is about producing an object file and
#: means nothing to a model checker; forwarding it only risks an error.
_VALUE_FLAGS = {"-I", "-isystem", "-iquote", "-D", "-U", "-include"}
_JOINED_PREFIXES = ("-I", "-D", "-U", "-isystem", "-iquote")
_STD_PREFIX = "-std="


class CompDBError(Exception):
    """The compilation database could not be used for this target."""


@dataclass
class CompileEntry:
    """One translation unit as the build system compiles it."""

    file: Path
    directory: Path
    include_dirs: list[Path] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    undefines: list[str] = field(default_factory=list)
    force_includes: list[Path] = field(default_factory=list)
    std: str | None = None

    def esbmc_args(self) -> list[str]:
        args: list[str] = []
        for inc in self.include_dirs:
            args += ["-I", str(inc)]
        for macro in self.defines:
            args += ["-D", macro]
        for macro in self.undefines:
            args += ["-U", macro]
        for header in self.force_includes:
            args += ["--include-file", str(header)]
        return args


def find_database(start: Path) -> Path | None:
    """Nearest compile_commands.json at or above `start`, or in ./build."""
    start = start.resolve()
    for directory in [start if start.is_dir() else start.parent, *start.parents]:
        for candidate in (
            directory / "compile_commands.json",
            directory / "build" / "compile_commands.json",
        ):
            if candidate.is_file():
                return candidate
    return None


def load(database: Path) -> list[dict]:
    try:
        entries = json.loads(database.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CompDBError(f"could not read {database}: {exc}") from exc
    if not isinstance(entries, list):
        raise CompDBError(f"{database} is not a list of compile commands")
    return entries


def entry_for(database: Path, source: Path) -> CompileEntry:
    """The compile command for `source`, parsed into flags ESBMC understands."""
    entries = load(database)
    target = source.resolve()
    for raw in entries:
        directory = Path(raw.get("directory", database.parent))
        candidate = Path(raw.get("file", ""))
        if not candidate.is_absolute():
            candidate = directory / candidate
        if candidate.resolve() == target:
            return _parse(raw, directory, target)
    raise CompDBError(
        f"{source} is not in {database} "
        f"({len(entries)} entries). Headers are usually absent from a "
        "compilation database: target the .cpp that includes it, or pass "
        "-I/-D by hand."
    )


def _parse(raw: dict, directory: Path, source: Path) -> CompileEntry:
    if "arguments" in raw:
        tokens = list(raw["arguments"])
    else:
        tokens = shlex.split(raw.get("command", ""))

    entry = CompileEntry(file=source, directory=directory)
    i = 1  # token 0 is the compiler
    while i < len(tokens):
        token = tokens[i]
        if token in _VALUE_FLAGS and i + 1 < len(tokens):
            _absorb(entry, token, tokens[i + 1], directory)
            i += 2
            continue
        if token.startswith(_STD_PREFIX):
            entry.std = token[len(_STD_PREFIX) :]
            i += 1
            continue
        matched = next((p for p in _JOINED_PREFIXES if token.startswith(p) and len(token) > len(p)), None)
        if matched:
            _absorb(entry, matched, token[len(matched) :], directory)
            i += 1
            continue
        i += 1
    return entry


def _absorb(entry: CompileEntry, flag: str, value: str, directory: Path) -> None:
    if flag in ("-I", "-isystem", "-iquote"):
        path = Path(value)
        entry.include_dirs.append(path if path.is_absolute() else (directory / path).resolve())
    elif flag == "-D":
        entry.defines.append(value)
    elif flag == "-U":
        entry.undefines.append(value)
    elif flag == "-include":
        path = Path(value)
        entry.force_includes.append(path if path.is_absolute() else (directory / path).resolve())


def sources(database: Path) -> list[Path]:
    """Every translation unit in the database, absolute."""
    result: list[Path] = []
    for raw in load(database):
        directory = Path(raw.get("directory", database.parent))
        candidate = Path(raw.get("file", ""))
        result.append(candidate if candidate.is_absolute() else (directory / candidate).resolve())
    return result
