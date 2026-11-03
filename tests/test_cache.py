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


def run(*args: str, cwd=None, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True, text=True, cwd=cwd, timeout=1800, env=env,
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
        f.write_text("int f(void){return 1;}\n", encoding="utf-8")
        first = _key(source=f)
        f.write_text("int f(void){return 2;}\n", encoding="utf-8")
        assert _key(source=f) != first

    def test_the_bounds(self, tmp_path) -> None:
        from veripp.esbmc import VerifyConfig

        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n", encoding="utf-8")
        assert _key(source=f) != _key(source=f, config=VerifyConfig(unwind=99))

    def test_the_harness_options(self, tmp_path) -> None:
        """A different array bound is a different question."""
        from veripp.harness import HarnessOptions

        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n", encoding="utf-8")
        assert _key(source=f) != _key(source=f, options=HarnessOptions(max_array_len=16))

    def test_the_checker_version(self, tmp_path) -> None:
        """A different checker can disagree about the same code, so a cache
        shared across one is not a cache."""
        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n", encoding="utf-8")
        assert _key(source=f) != _key(source=f, checker_version="esbmc-y")

    def test_veripps_own_version(self, tmp_path) -> None:
        """The harness generator changes what is asked."""
        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n", encoding="utf-8")
        assert _key(source=f) != _key(source=f, veripp_version="2.0")

    def test_a_header_or_linked_source(self, tmp_path) -> None:
        """These are inputs, not context: changing one can flip a verdict
        without the file under test changing at all."""
        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n", encoding="utf-8")
        h = tmp_path / "h.h"; h.write_text("#define N 4\n", encoding="utf-8")
        first = _key(source=f, extra_files=[h])
        h.write_text("#define N 99\n", encoding="utf-8")
        assert _key(source=f, extra_files=[h]) != first

    def test_an_unchanged_input_keeps_its_key(self, tmp_path) -> None:
        f = tmp_path / "a.c"; f.write_text("int f(void){return 1;}\n", encoding="utf-8")
        assert _key(source=f) == _key(source=f)


@pytest.mark.esbmc
class TestBehaviour:
    def test_an_unchanged_file_is_reused(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text(CLEAN, encoding="utf-8")
        run("scan", ".", cwd=tmp_path)
        second = run("scan", ".", cwd=tmp_path)
        assert "cached" in second.stderr

    def test_editing_one_file_reverifies_only_it(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text(CLEAN, encoding="utf-8")
        (tmp_path / "b.c").write_text("int twice(int x){ if(x>99||x<-99) return 0; return x*2; }\n", encoding="utf-8")
        run("scan", ".", cwd=tmp_path)
        (tmp_path / "a.c").write_text(CLEAN.replace("100", "50"), encoding="utf-8")
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
        source.write_text(source.read_text(encoding="utf-8").replace("return 4;", "return 99;"))
        after = run("scan", "t.c", cwd=tmp_path)
        assert after.returncode == 1, (
            "a stale 'verified' was served after a callee changed:\n"
            + after.stdout[-500:]
        )

    def test_no_cache_disables_it(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text(CLEAN, encoding="utf-8")
        run("scan", ".", cwd=tmp_path)
        assert "cached" not in run("scan", ".", "--no-cache", cwd=tmp_path).stderr

    def test_only_results_are_not_cached_as_the_files_verdict(self, tmp_path) -> None:
        """A subset scan answers a different question; storing it as the
        file's verdict would hide every function it skipped."""
        (tmp_path / "a.c").write_text(CLEAN + "int mean(int a,int b){return (a+b)/2;}\n", encoding="utf-8")
        run("scan", "a.c", "--only", "clamp", cwd=tmp_path)
        full = run("scan", "a.c", cwd=tmp_path)
        assert full.returncode == 1, "the skipped buggy function was masked"


@pytest.mark.esbmc
class TestEveryInputMisses:
    """The key hashed only the headers a file includes directly with quotes,
    found next to it or on -I. Each input below reached the checker without
    reaching the key, so editing it served the old verdict."""

    @staticmethod
    def _twice(tmp_path, edit, *args, env=None):
        first = run("scan", "a.c", *args, cwd=tmp_path, env=env)
        assert first.returncode in (0, 1), first.stderr[-400:]
        edit()
        return run("scan", "a.c", *args, cwd=tmp_path, env=env)

    def test_a_header_included_by_a_header(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text('#include "a.h"\nint f(void){ return LIMIT; }\n', encoding="utf-8")
        (tmp_path / "a.h").write_text('#include "b.h"\n', encoding="utf-8")
        deep = tmp_path / "b.h"
        deep.write_text("#define LIMIT 1\n", encoding="utf-8")
        again = self._twice(tmp_path, lambda: deep.write_text("#define LIMIT 2\n", encoding="utf-8"))
        assert "cached" not in again.stderr

    def test_an_angle_bracket_project_header(self, tmp_path) -> None:
        (tmp_path / "inc").mkdir()
        (tmp_path / "a.c").write_text("#include <proj.h>\nint f(void){ return LIMIT; }\n", encoding="utf-8")
        header = tmp_path / "inc" / "proj.h"
        header.write_text("#define LIMIT 1\n", encoding="utf-8")
        again = self._twice(
            tmp_path, lambda: header.write_text("#define LIMIT 2\n", encoding="utf-8"), "-I", "inc",
        )
        assert "cached" not in again.stderr

    def test_a_header_found_through_compile_commands(self, tmp_path) -> None:
        (tmp_path / "cfg").mkdir()
        (tmp_path / "a.c").write_text('#include "cfg.h"\nint f(void){ return LIMIT; }\n', encoding="utf-8")
        header = tmp_path / "cfg" / "cfg.h"
        header.write_text("#define LIMIT 1\n", encoding="utf-8")
        (tmp_path / "compile_commands.json").write_text(json.dumps([{
            "directory": str(tmp_path), "file": "a.c",
            "arguments": ["cc", "-Icfg", "-c", "a.c"],
        }]), encoding="utf-8")
        again = self._twice(tmp_path, lambda: header.write_text("#define LIMIT 2\n", encoding="utf-8"))
        assert "cached" not in again.stderr

    def test_a_force_included_header(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text("int f(void){ return LIMIT; }\n", encoding="utf-8")
        shim = tmp_path / "shim.h"
        shim.write_text("#define LIMIT 1\n", encoding="utf-8")
        again = self._twice(
            tmp_path, lambda: shim.write_text("#define LIMIT 2\n", encoding="utf-8"),
            "--include-file", str(shim),
        )
        assert "cached" not in again.stderr

    def test_a_different_checker_that_reports_the_same_version(self, tmp_path) -> None:
        """What a master build and the release it descends from look like."""
        import os
        import shutil

        from veripp.esbmc import find_esbmc

        real = shutil.which("esbmc") or find_esbmc()
        builds = []
        for name in ("build-a", "build-b"):
            wrapper = tmp_path / name
            wrapper.write_text(f'#!/bin/sh\n# {name}\nexec "{real}" "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            builds.append({**os.environ, "VERIPP_ESBMC": str(wrapper)})
        (tmp_path / "a.c").write_text(CLEAN, encoding="utf-8")
        assert run("scan", "a.c", cwd=tmp_path, env=builds[0]).returncode == 0
        again = run("scan", "a.c", cwd=tmp_path, env=builds[1])
        assert "cached" not in again.stderr


class TestVeripsOwnCode:
    """`__version__` stayed 0.5.0 across 74 commits of main, so it cannot say
    which veripp wrote a cached verdict."""

    def test_editing_a_module_changes_the_digest(self, tmp_path) -> None:
        import shutil

        from veripp.cache import _PACKAGE_ROOT, package_digest

        copy = tmp_path / "veripp"
        shutil.copytree(_PACKAGE_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
        before = package_digest(copy)
        scan = copy / "scan.py"
        scan.write_text(scan.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        package_digest.cache_clear()
        assert package_digest(copy) != before

    def test_the_contracts_header_counts(self, tmp_path) -> None:
        import shutil

        from veripp.cache import _PACKAGE_ROOT, package_digest

        copy = tmp_path / "veripp"
        shutil.copytree(_PACKAGE_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
        before = package_digest(copy)
        header = copy / "include" / "veripp" / "contracts.hpp"
        header.write_text(header.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
        package_digest.cache_clear()
        assert package_digest(copy) != before

    def test_the_key_follows_it(self, tmp_path, monkeypatch) -> None:
        from veripp import cache
        from veripp.cli import _cache_key
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions

        f = tmp_path / "a.c"
        f.write_text(CLEAN, encoding="utf-8")
        monkeypatch.setattr(cache, "package_digest", lambda: "one")
        first = _cache_key(f, VerifyConfig(), HarnessOptions())
        monkeypatch.setattr(cache, "package_digest", lambda: "two")
        assert _cache_key(f, VerifyConfig(), HarnessOptions()) != first


def test_the_checker_is_identified_by_its_bytes(tmp_path) -> None:
    from veripp.cache import checker_digest

    one, two = tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"checker")
    two.write_bytes(b"checker")
    assert checker_digest(str(one)) == checker_digest(str(two))
    two.write_bytes(b"checker, rebuilt")
    assert checker_digest(str(one)) != checker_digest(str(two))


def test_a_checker_digest_is_remembered_between_runs(tmp_path, monkeypatch) -> None:
    """The Linux checker is ~650MB; hashing it on every cached run would cost
    more than the cache saves on a small tree."""
    from veripp import cache

    binary = tmp_path / "esbmc"
    binary.write_bytes(b"checker")
    memo = tmp_path / "cache" / cache.CHECKER_MEMO
    first = cache.checker_digest(str(binary), memo=memo)
    assert memo.is_file()
    monkeypatch.setattr(cache, "_CHECKER_DIGESTS", {})  # a new process
    monkeypatch.setattr(cache.hashlib, "sha256", None)  # hashing would fail
    assert cache.checker_digest(str(binary), memo=memo) == first


class TestCacheStore:
    def test_an_entry_from_another_version_is_ignored(self, tmp_path) -> None:
        from veripp.cache import Cache

        cache = Cache(tmp_path)
        cache.directory.mkdir(exist_ok=True)
        cache.path_for("k").write_text(json.dumps({"cache_version": 0, "results": []}), encoding="utf-8")
        assert cache.get("k") is None

    def test_corrupt_entries_are_ignored_not_fatal(self, tmp_path) -> None:
        from veripp.cache import Cache

        cache = Cache(tmp_path)
        cache.path_for("k").write_text("{ truncated", encoding="utf-8")
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
