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
the local headers it pulls in, any linked sources, the harness options, the
solver configuration, and the versions of veripp and ESBMC themselves. That
makes the unit of caching the file rather than the function -- editing one
function re-verifies its file, and leaves every other file cached, which is
the case CI actually hits.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

CACHE_VERSION = 2
DEFAULT_DIR = ".veripp-cache"


def _digest_file(path: Path, into: hashlib._Hash) -> None:
    try:
        into.update(path.read_bytes())
    except OSError:
        # Unreadable input: fold the path in so it cannot collide with the
        # same file being readable later.
        into.update(f"<unreadable:{path}>".encode())


def esbmc_version(binary: str | None) -> str:
    """The checker's own version. A different checker can disagree about the
    same code, so a cache shared across one is not a cache."""
    if not binary:
        return "none"
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=60)
        return (out.stdout or out.stderr).strip()[:120]
    except (OSError, subprocess.SubprocessError):
        return "unknown"


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
