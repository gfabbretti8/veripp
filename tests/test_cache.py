"""Incremental verification.

Verifying a tree is minutes per commit and most commits touch one file, so a
cache is the difference between a check that runs on every push and one that
gets moved to nightly.

Every test here is about the same thing: a cache that serves a stale
"verified" is a false assurance, which is the one failure a verification tool
must never produce. Speed is the easy half.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLEAN = "int clamp(int x){ if(x<0) return 0; if(x>100) return 100; return x; }\n"


def run(*args: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True, text=True, cwd=cwd, timeout=1800,
    )


def _key(**overrides):
    from veripp.cache import key_for
    from veripp.esbmc import VerifyConfig
    from veripp.harness import HarnessOptions

    base = dict(
        config=VerifyConfig(), options=HarnessOptions(),
        veripp_version="1.0", checker_version="esbmc-x",
    )
    source = overrides.pop("source")
    base.update(overrides)
    return key_for(source, **base)


class TestKeyCoversEverythingThatMatters:
    """Anything that can change a verdict has to change the key."""

    def test_the_file_itself(self, tmp_path) -> None:
        f = tmp_path / "a.c"
        f.write_text("int f(void){return 1;}\n")
        first = _key(source=f)
        f.write_text("int f(void){return 2;}\n")
        assert _key(source=f) != first

    def test_the_bounds(self, tmp_path) -> None:
        from veripp.esbmc import VerifyConfig

        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n")
        assert _key(source=f) != _key(source=f, config=VerifyConfig(unwind=99))

    def test_the_harness_options(self, tmp_path) -> None:
        """A different array bound is a different question."""
        from veripp.harness import HarnessOptions

        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n")
        assert _key(source=f) != _key(source=f, options=HarnessOptions(max_array_len=16))

    def test_the_checker_version(self, tmp_path) -> None:
        """A different checker can disagree about the same code, so a cache
        shared across one is not a cache."""
        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n")
        assert _key(source=f) != _key(source=f, checker_version="esbmc-y")

    def test_veripps_own_version(self, tmp_path) -> None:
        """The harness generator changes what is asked."""
        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n")
        assert _key(source=f) != _key(source=f, veripp_version="2.0")

    def test_a_header_or_linked_source(self, tmp_path) -> None:
        """These are inputs, not context: changing one can flip a verdict
        without the file under test changing at all."""
        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n")
        h = tmp_path / "h.h"; h.write_text("#define N 4\n")
        first = _key(source=f, extra_files=[h])
        h.write_text("#define N 99\n")
        assert _key(source=f, extra_files=[h]) != first

    def test_an_unchanged_input_keeps_its_key(self, tmp_path) -> None:
        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n")
        assert _key(source=f) == _key(source=f)


@pytest.mark.esbmc
class TestBehaviour:
    def test_an_unchanged_file_is_reused(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text(CLEAN)
        run("scan", ".", cwd=tmp_path)
        second = run("scan", ".", cwd=tmp_path)
        assert "cached" in second.stderr

    def test_editing_one_file_reverifies_only_it(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text(CLEAN)
        (tmp_path / "b.c").write_text("int twice(int x){ if(x>99||x<-99) return 0; return x*2; }\n")
        run("scan", ".", cwd=tmp_path)
        (tmp_path / "a.c").write_text(CLEAN.replace("100", "50"))
        again = run("scan", ".", cwd=tmp_path)
        assert "1 of 2 files" in again.stderr, again.stderr[-400:]

    def test_a_changed_callee_is_not_served_from_cache(self, tmp_path) -> None:
        """The exact trap a function-body key falls into: `at` is
        byte-identical and its verdict flips from verified to counterexample
        when the callee changes. ROADMAP asked for a body hash; this is why
        the key is the whole translation unit and its inputs instead."""
        source = tmp_path / "t.c"
        source.write_text(
            "static int limit(void){ return 4; }\n"
            "int at(const int*a,int i){ if(i<0||i>=limit()) return 0; return a[i]; }\n"
        )
        assert run("scan", "t.c", cwd=tmp_path).returncode == 0
        source.write_text(source.read_text().replace("return 4;", "return 99;"))
        after = run("scan", "t.c", cwd=tmp_path)
        assert after.returncode == 1, (
            "a stale 'verified' was served after a callee changed:\n"
            + after.stdout[-500:]
        )

    def test_no_cache_disables_it(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text(CLEAN)
        run("scan", ".", cwd=tmp_path)
        assert "cached" not in run("scan", ".", "--no-cache", cwd=tmp_path).stderr

    def test_only_results_are_not_cached_as_the_files_verdict(self, tmp_path) -> None:
        """A subset scan answers a different question; storing it as the
        file's verdict would hide every function it skipped."""
        (tmp_path / "a.c").write_text(CLEAN + "int mean(int a,int b){return (a+b)/2;}\n")
        run("scan", "a.c", "--only", "clamp", cwd=tmp_path)
        full = run("scan", "a.c", cwd=tmp_path)
        assert full.returncode == 1, "the skipped buggy function was masked"


class TestCacheStore:
    def test_an_entry_from_another_version_is_ignored(self, tmp_path) -> None:
        from veripp.cache import Cache

        cache = Cache(tmp_path)
        cache.directory.mkdir(exist_ok=True)
        cache.path_for("k").write_text(json.dumps({"cache_version": 0, "results": []}))
        assert cache.get("k") is None

    def test_corrupt_entries_are_ignored_not_fatal(self, tmp_path) -> None:
        from veripp.cache import Cache

        cache = Cache(tmp_path)
        cache.path_for("k").write_text("{ truncated")
        assert cache.get("k") is None

    def test_an_unwritable_cache_does_not_raise(self, tmp_path) -> None:
        """A cache that cannot be written must not cost anyone a result."""
        from veripp.cache import Cache

        Cache(tmp_path / "nope" / "deeper").put("k", {"results": []})

    def test_a_round_trip_returns_what_was_stored(self, tmp_path) -> None:
        from veripp.cache import Cache

        cache = Cache(tmp_path)
        cache.put("k", {"candidates": 3, "results": []})
        assert cache.get("k")["candidates"] == 3
