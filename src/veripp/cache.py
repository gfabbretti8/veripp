"""Skip re-verifying what has not changed.

Verifying a tree is minutes per commit, and most commits touch one file. A
cache turns the rest into seconds.

The whole design question is the key, and getting it wrong is worse than
having no cache: serving a stale "verified" is a false assurance, which is the
one failure a verification tool must never produce.

ROADMAP called for a key on the function body. That is unsound, and
demonstrably so:

    static int limit(void) { return 4; }
    int at(const int *a, int i) { if (i<0 || i>=limit()) return 0; return a[i]; }

`at` verifies. Change `limit` to return 99 and `at` -- byte-identical -- yields
a counterexample. A body hash would have answered "verified" from the cache.

So the key covers everything that feeds a verification: the translation unit,
every project header it reaches, any linked sources and force-included
headers, the harness options, the solver configuration, and veripp and ESBMC
themselves -- as bytes, not as version strings. That makes the unit of
caching the file rather than the function -- editing one function re-verifies
its file, and leaves every other file cached, which is the case CI actually
hits.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

CACHE_VERSION = 3
DEFAULT_DIR = ".veripp-cache"


def _digest_file(path: Path, into: hashlib._Hash) -> None:
    try:
        into.update(path.read_bytes())
    except OSError:
        # Unreadable input: fold the path in so it cannot collide with the
        # same file being readable later.
        into.update(f"<unreadable:{path}>".encode())


#: Checker digests already computed, by path, size, mtime and inode. The
#: binary runs to hundreds of megabytes -- the Linux `weekly` build is 650MB,
#: nearly two seconds to hash -- and a tree scan asks once per file, so each
#: is hashed once per process and remembered in the cache directory after.
_CHECKER_DIGESTS: dict[str, str] = {}
CHECKER_MEMO = "checkers.json"


def checker_digest(binary: str | None, memo: Path | None = None) -> str:
    """The checker's identity: the SHA-256 of the binary itself.

    A different checker can disagree about the same code, so a cache shared
    across one is not a cache. The version string cannot tell them apart: a
    build of master reports the release it descends from --
    tests/golden/esbmc-version.txt records a HEAD build calling itself 8.4.0
    -- so a sound build and the unsound 8.4 release would share one key.

    `memo`, a file in the cache directory, keeps digests between runs. A
    binary replaced in place keeping its size, mtime and inode would be
    missed; nothing that installs or builds a checker does that.
    """
    if not binary:
        return "none"
    try:
        path = Path(binary).resolve()
        stat = path.stat()
        identity = f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{stat.st_ino}"
        if identity not in _CHECKER_DIGESTS:
            remembered = _read_memo(memo).get(identity)
            if remembered is None:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while chunk := handle.read(1 << 20):
                        digest.update(chunk)
                remembered = digest.hexdigest()
                _write_memo(memo, identity, remembered)
            _CHECKER_DIGESTS[identity] = remembered
        return _CHECKER_DIGESTS[identity]
    except OSError:
        # A checker that cannot be read cannot be identified, and a key that
        # cannot tell it from the next one must not be reused: make this one
        # unique, so the cache misses rather than guesses.
        return f"unidentified:{os.urandom(16).hex()}"


def _read_memo(memo: Path | None) -> dict[str, str]:
    if memo is None:
        return {}
    try:
        table = json.loads(memo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return table if isinstance(table, dict) else {}


def _write_memo(memo: Path | None, identity: str, digest: str) -> None:
    if memo is None:
        return
    table = _read_memo(memo)
    table[identity] = digest
    try:
        memo.parent.mkdir(parents=True, exist_ok=True)
        temporary = memo.with_suffix(".tmp")
        temporary.write_text(json.dumps(table, indent=2), encoding="utf-8")
        temporary.replace(memo)
    except OSError:
        pass  # a memo that cannot be written only costs the next run a hash


_PACKAGE_ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def package_digest(root: Path = _PACKAGE_ROOT) -> str:
    """veripp's own code, as bytes: every module and the contracts header.

    The harness generator and the result classification decide what a cached
    verdict means, and `__version__` does not move with them -- it named 74
    commits of main in a row. A cache written by one of them must not answer
    for another.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.suffix in (".py", ".hpp") and path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def key_for(
    source: Path,
    *,
    config,
    options,
    veripp_version: str,
    checker_version: str,
    extra_files: list[Path] | None = None,
) -> str:
    """A digest of everything that could change this file's verdict."""
    digest = hashlib.sha256()
    digest.update(f"veripp-cache-v{CACHE_VERSION}\0".encode())
    digest.update(f"{veripp_version}\0{checker_version}\0".encode())

    _digest_file(source, digest)

    # Local headers and linked sources are part of the input, not context: a
    # change in either can flip a verdict without touching this file.
    for path in sorted(extra_files or []):
        digest.update(f"\0file:{path}\0".encode())
        _digest_file(path, digest)

    # The harness and the solver settings decide what was actually asked.
    for label, obj in (("config", config), ("options", options)):
        try:
            payload = json.dumps(asdict(obj), sort_keys=True, default=str)
        except (TypeError, ValueError):
            payload = repr(obj)
        digest.update(f"\0{label}:{payload}".encode())

    return digest.hexdigest()


class Cache:
    """A directory of verdicts, keyed as above."""

    def __init__(self, directory: Path):
        self.directory = directory

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict | None:
        try:
            payload = json.loads(self.path_for(key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        # A cache written by another version is not readable, not wrong: drop
        # it rather than interpret it.
        if payload.get("cache_version") != CACHE_VERSION:
            return None
        return payload

    def put(self, key: str, payload: dict) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            body = dict(payload)
            body["cache_version"] = CACHE_VERSION
            # Write then move, so an interrupted run cannot leave a truncated
            # entry that later reads as a verdict.
            temporary = self.path_for(key).with_suffix(".tmp")
            temporary.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
            temporary.replace(self.path_for(key))
        except OSError:
            # A cache that cannot be written must not cost anyone a result.
            pass
