"""Scanning only what changed.

Verifying a whole tree is a nightly job; verifying what a commit touches is
something you can put in front of every commit. The selection has to be
exactly right in one direction: missing a changed file means a check that
silently passed on unverified code.
"""

import subprocess
from pathlib import Path

import pytest

from veripp.cli import changed_sources


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "base.c").write_text("int base(void){return 0;}\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


class TestAgainstHead:
    def test_an_untracked_file_counts(self, repo):
        (repo / "new.c").write_text("int f(void){return 1;}\n")
        found, described = changed_sources(repo)
        assert [p.name for p in found] == ["new.c"]
        assert described == "changed since HEAD"

    def test_a_modified_tracked_file_counts(self, repo):
        (repo / "base.c").write_text("int base(void){return 1;}\n")
        found, _ = changed_sources(repo)
        assert [p.name for p in found] == ["base.c"]

    def test_a_staged_file_counts(self, repo):
        """pre-commit stashes unstaged changes, so during a hook run the
        working tree is exactly the staged content."""
        (repo / "staged.c").write_text("int f(void){return 1;}\n")
        git(repo, "add", "staged.c")
        found, _ = changed_sources(repo)
        assert [p.name for p in found] == ["staged.c"]

    def test_an_untouched_file_does_not_count(self, repo):
        (repo / "notes.txt").write_text("hello\n")
        assert changed_sources(repo)[0] == []

    def test_a_deleted_file_is_not_scanned(self, repo):
        """It cannot be scanned, and listing it as skipped is noise."""
        (repo / "base.c").unlink()
        assert changed_sources(repo)[0] == []

    def test_non_source_files_are_ignored(self, repo):
        (repo / "README.md").write_text("# hi\n")
        (repo / "header.h").write_text("int f(void);\n")
        assert changed_sources(repo)[0] == []

    def test_build_and_vendor_directories_are_skipped(self, repo):
        for junk in ("build", "third_party", ".hidden"):
            (repo / junk).mkdir()
            (repo / junk / "x.c").write_text("int x(void){return 0;}\n")
        assert changed_sources(repo)[0] == []


class TestAgainstARef:
    def test_only_what_this_branch_added(self, repo):
        git(repo, "checkout", "-qb", "feature")
        (repo / "feature.c").write_text("int g(void){return 2;}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "feature work")
        found, described = changed_sources(repo, "main")
        assert [p.name for p in found] == ["feature.c"]
        assert described == "changed since main"

    def test_commits_that_landed_on_the_base_meanwhile_are_excluded(self, repo):
        """Three-dot: compare against where the branch diverged, not against
        every commit that reached main since."""
        git(repo, "checkout", "-qb", "feature")
        (repo / "feature.c").write_text("int g(void){return 2;}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "feature work")
        git(repo, "checkout", "-q", "main")
        (repo / "unrelated.c").write_text("int u(void){return 3;}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "meanwhile")
        git(repo, "checkout", "-q", "feature")
        assert [p.name for p in changed_sources(repo, "main")[0]] == ["feature.c"]


class TestScoping:
    def test_only_files_under_the_directory_asked_about(self, repo):
        (repo / "src").mkdir()
        (repo / "src" / "in.c").write_text("int a(void){return 0;}\n")
        (repo / "out.c").write_text("int b(void){return 0;}\n")
        found, _ = changed_sources(repo / "src")
        assert [p.name for p in found] == ["in.c"]

    def test_outside_a_git_repository_it_raises(self, tmp_path):
        with pytest.raises((RuntimeError, IndexError, OSError)):
            changed_sources(tmp_path)
