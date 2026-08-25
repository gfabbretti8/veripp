"""compile_commands.json: build a real project's flags into a verification run.

A single file is rarely self-contained. The build system already knows the
include paths, the defines and the language standard each translation unit
needs, and it publishes them in a clang compilation database. Reading that is
the difference between "works on the examples" and "works on your project".
"""

from __future__ import annotations

import json
import os
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


def normalise(path: Path) -> Path:
    """Tidy separators and `..` without inventing a drive.

    A database written on one platform and read on another mixes separators
    ("C:\\proj/include"), and two spellings of the same directory compare
    unequal. resolve() would fix that but also anchors a POSIX path to the
    current drive on Windows, turning "/usr/include" into "C:/usr/include" --
    a different place. normpath does the tidying and nothing else.
    """
    import os

    return Path(os.path.normpath(str(path)))


def looks_absolute(value: str | Path) -> bool:
    """Whether a compilation database means this path absolutely.

    A database is usually generated on the machine that built the project, and
    read wherever the code is checked out -- often not the same platform. On
    Windows, Path("/usr/include").is_absolute() is False, because it has no
    drive letter, so a POSIX path from a Linux-generated database was joined
    onto the database's own directory and produced a path like
    C:/build/UsersmeprojincludeInclude. Garbage, silently.
    """
    text = str(value)
    return (
        Path(text).is_absolute()
        or text.startswith(("/", "\\"))
        or (len(text) > 1 and text[1] == ":")  # C:\... on any platform
    )


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
        entries = json.loads(database.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompDBError(f"could not read {database}: {exc}") from exc
    if not isinstance(entries, list):
        raise CompDBError(f"{database} is not a list of compile commands")
    return entries


def _entry_path(raw: dict, database: Path) -> tuple[Path, Path]:
    directory = Path(raw.get("directory", database.parent))
    candidate = Path(raw.get("file", ""))
    if not looks_absolute(candidate):
        candidate = directory / candidate
    return directory, candidate


def _shared_tail(a: Path, b: Path) -> int:
    """How many trailing path components `a` and `b` have in common."""
    n = 0
    left, right = a.parts, b.parts
    while n < min(len(left), len(right)) and left[-1 - n] == right[-1 - n]:
        n += 1
    return n


def _relocated(entries: list[dict], target: Path, database: Path) -> CompileEntry | None:
    """Match a database written against a different absolute root.

    A compilation database records absolute paths from the machine that
    generated it. Mount that tree somewhere else -- /src in a container, a
    differently-named CI checkout -- and every path in it is wrong, including
    the -I flags. The tree itself is unchanged, though, so the entry can be
    found by its trailing components and the whole entry rebased onto wherever
    the tree now lives.

    Only an unambiguous match is accepted: if two entries tie, the database
    cannot tell us which file we were handed, and guessing would silently
    verify the wrong translation unit with the wrong flags.
    """
    best_n = 0
    best: list[tuple[dict, Path, Path]] = []
    for raw in entries:
        directory, candidate = _entry_path(raw, database)
        n = _shared_tail(candidate, target)
        if n == 0:
            continue
        if n > best_n:
            best_n, best = n, [(raw, directory, candidate)]
        elif n == best_n:
            best.append((raw, directory, candidate))

    if best_n == 0:
        return None
    if len(best) > 1:
        names = ", ".join(str(c) for _, _, c in best[:3])
        raise CompDBError(
            f"{target} matches {len(best)} entries in {database} equally well "
            f"({names}...). The database was written for a different directory "
            "layout and cannot be rebased unambiguously; pass -I/-D by hand."
        )

    raw, directory, candidate = best[0]
    old_root = Path(*candidate.parts[: len(candidate.parts) - best_n])
    new_root = Path(*target.parts[: len(target.parts) - best_n])

    def rebase(path: Path) -> Path:
        try:
            return new_root / path.relative_to(old_root)
        except ValueError:
            # Outside the moved tree (a system include, say). Leave it alone.
            return path

    entry = _parse(raw, rebase(directory), target)
    entry.include_dirs = [rebase(p) for p in entry.include_dirs]
    entry.force_includes = [rebase(p) for p in entry.force_includes]
    return entry


def entry_for(database: Path, source: Path) -> CompileEntry:
    """The compile command for `source`, parsed into flags ESBMC understands."""
    entries = load(database)
    target = source.resolve()
    for raw in entries:
        directory, candidate = _entry_path(raw, database)
        if candidate.resolve() == target:
            return _parse(raw, directory, target)

    relocated = _relocated(entries, target, database)
    if relocated is not None:
        return relocated

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
        # POSIX mode treats a backslash as an escape, so a Windows path in
        # the command string comes back with its separators eaten:
        # "-IC:\\proj\\include" becomes "-IC:projinclude". Silent, and every
        # include path is then wrong. Prefer the "arguments" array when a
        # database provides one; when only "command" is available, split it
        # the way the running platform quotes.
        tokens = shlex.split(raw.get("command", ""), posix=(os.name != "nt"))

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
        entry.include_dirs.append(
            normalise(path) if looks_absolute(path)
            else normalise(directory / path)
        )
    elif flag == "-D":
        entry.defines.append(value)
    elif flag == "-U":
        entry.undefines.append(value)
    elif flag == "-include":
        path = Path(value)
        entry.force_includes.append(
            normalise(path) if looks_absolute(path)
            else normalise(directory / path)
        )


def sources(database: Path) -> list[Path]:
    """Every translation unit in the database, absolute."""
    result: list[Path] = []
    for raw in load(database):
        directory = Path(raw.get("directory", database.parent))
        candidate = Path(raw.get("file", ""))
        result.append(
            normalise(candidate) if looks_absolute(candidate)
            else normalise(directory / candidate)
        )
    return result
