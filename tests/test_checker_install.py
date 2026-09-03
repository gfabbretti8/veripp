"""Installing a checker, and refusing to install an unsound one.

The install path downloads a binary from the internet and then runs it over
your source, so two things are load-bearing: the archive cannot write outside
the install directory, and nothing is kept that fails the soundness probes.
A checker that verifies a program which provably fails turns every result
built on it into a false proof, which is worse than having no checker.
"""

import os
import sys
import zipfile
from pathlib import Path

#: What the checker is called here. managed_esbmc() looks for esbmc.exe on
#: Windows, so a payload named `esbmc` there is invisible to it -- which is
#: correct behaviour, and made this test fail on Windows alone.
EXE = "esbmc.exe" if sys.platform == "win32" else "esbmc"

import pytest

from veripp import checker
from veripp.checker import Source, install, managed_esbmc, source_for


class TestPlatformSelection:
    def test_linux_x86_64_gets_the_weekly_build(self):
        src = source_for("Linux", "x86_64")
        assert src.available and src.url.endswith("esbmc-linux.zip")
        assert "weekly" in src.url  # the release is 8.4 and unsound

    def test_windows_gets_an_exe(self):
        src = source_for("Windows", "AMD64")
        assert src.available and src.binary_name == "esbmc.exe"

    def test_macos_refuses_rather_than_shipping_a_broken_unzip(self):
        src = source_for("Darwin", "arm64")
        assert not src.available
        assert "brew install --HEAD" in src.unavailable_reason
        assert "NOT `brew install esbmc`" in src.unavailable_reason

    def test_linux_arm64_refuses_rather_than_installing_8_4(self):
        """The only prebuilt arm64 Linux ESBMC is the Homebrew bottle, pinned
        to the release that misses member-array writes."""
        src = source_for("Linux", "aarch64")
        assert not src.available
        assert "docker" in src.unavailable_reason.lower()


def _release_zip(path: Path, script: str, name: str = None) -> Path:
    name = name or f"esbmc-linux/bin/{EXE}"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, script)
    return path


def _opener_for(archive: Path):
    return lambda url: archive.open("rb")


class TestInstall:
    def test_a_checker_that_misses_a_planted_bug_is_deleted(
        self, tmp_path, monkeypatch
    ):
        archive = _release_zip(tmp_path / "rel.zip", "#!/bin/sh\nexit 0\n")
        monkeypatch.setattr(
            "veripp.esbmc.check_soundness",
            lambda binary, **kw: {"member-array bounds (esbmc#6508)": False,
                                  "local-array bounds": True},
        )
        dest = tmp_path / "install"
        result = install(dest=dest, source=Source(url="http://x/rel.zip", binary_name=EXE),
                         opener=_opener_for(archive))
        assert not result.ok
        assert "MISSES" in result.error
        assert "member-array bounds" in result.error
        assert not (dest / "bin" / EXE).exists(), "unsound binary was kept"

    def test_a_sound_checker_is_installed_and_found(self, tmp_path, monkeypatch):
        archive = _release_zip(tmp_path / "rel.zip", "#!/bin/sh\nexit 0\n")
        monkeypatch.setattr(
            "veripp.esbmc.check_soundness",
            lambda binary, **kw: {"member-array bounds (esbmc#6508)": True,
                                  "local-array bounds": True},
        )
        dest = tmp_path / "install"
        result = install(dest=dest, source=Source(url="http://x/rel.zip", binary_name=EXE),
                         opener=_opener_for(archive))
        assert result.ok, result.error
        assert Path(result.path).is_file()
        assert os.access(result.path, os.X_OK), "installed checker is not executable"
        assert len(result.sha256) == 64
        # What was installed, recorded for the question asked afterwards.
        assert result.sha256 in (dest / "INSTALLED").read_text()

        monkeypatch.setenv("VERIPP_CHECKER_DIR", str(dest))
        assert managed_esbmc() == result.path

    def test_an_archive_cannot_write_outside_the_install_directory(self, tmp_path):
        """A zip may name `../../etc/whatever`. This is a download from the
        internet unpacked with the user's own permissions."""
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../../escaped", "pwned")
        result = install(dest=tmp_path / "install",
                         source=Source(url="http://x/evil.zip", binary_name=EXE),
                         opener=_opener_for(archive))
        assert not result.ok
        assert "outside the install directory" in result.error
        assert not (tmp_path.parent / "escaped").exists()

    def test_an_archive_without_a_checker_is_reported(self, tmp_path):
        archive = _release_zip(tmp_path / "rel.zip", "nope", name="README")
        result = install(dest=tmp_path / "install",
                         source=Source(url="http://x/rel.zip", binary_name=EXE),
                         opener=_opener_for(archive))
        assert not result.ok and "no esbmc" in result.error

    def test_an_unavailable_platform_installs_nothing(self, tmp_path):
        result = install(dest=tmp_path / "install",
                         source=Source(url=None, unavailable_reason="use brew"))
        assert not result.ok and result.error == "use brew"


class TestDiscoveryPrecedence:
    def test_an_explicit_binary_wins(self, tmp_path, monkeypatch):
        from veripp.esbmc import find_esbmc

        monkeypatch.setenv("VERIPP_ESBMC", "/somewhere/mine/esbmc")
        assert find_esbmc() == "/somewhere/mine/esbmc"

    def test_a_managed_checker_beats_path(self, tmp_path, monkeypatch):
        """`brew install esbmc` puts the unsound 8.4 on PATH. A checker the
        user asked veripp to install passed the probes; PATH promises nothing."""
        from veripp.esbmc import find_esbmc

        monkeypatch.delenv("VERIPP_ESBMC", raising=False)
        managed = tmp_path / "bin" / EXE
        managed.parent.mkdir(parents=True)
        managed.write_text("#!/bin/sh\n")
        managed.chmod(0o755)
        monkeypatch.setenv("VERIPP_CHECKER_DIR", str(tmp_path))
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/esbmc")
        assert find_esbmc() == str(managed)

    def test_path_is_still_the_fallback(self, tmp_path, monkeypatch):
        from veripp.esbmc import find_esbmc

        monkeypatch.delenv("VERIPP_ESBMC", raising=False)
        monkeypatch.setenv("VERIPP_CHECKER_DIR", str(tmp_path / "empty"))
        # veripp depends on veripp-checker, so on any machine with a wheel
        # for it there IS a bundled checker; PATH is only reached when there
        # is not one. Stand that case up rather than assume it.
        monkeypatch.setattr("veripp.checker.bundled_esbmc", lambda: None)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/esbmc")
        assert find_esbmc() == "/usr/bin/esbmc"

    def test_a_checker_on_path_beats_the_bundled_wheel(
        self, tmp_path, monkeypatch
    ):
        """Putting an esbmc on PATH is a decision. Overriding it with the
        wheel that arrived as a dependency would mean an ESBMC developer
        could not test their own build -- and it would defeat the action's
        own selftest, which plants a deliberately unsound checker there."""
        from veripp.esbmc import find_esbmc

        monkeypatch.delenv("VERIPP_ESBMC", raising=False)
        monkeypatch.setenv("VERIPP_CHECKER_DIR", str(tmp_path / "empty"))
        monkeypatch.setattr("veripp.checker.bundled_esbmc",
                            lambda: "/wheel/esbmc")
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/esbmc")
        assert find_esbmc() == "/usr/bin/esbmc"

    def test_the_bundled_wheel_is_used_when_nothing_else_is_there(
        self, tmp_path, monkeypatch
    ):
        from veripp.esbmc import find_esbmc

        monkeypatch.delenv("VERIPP_ESBMC", raising=False)
        monkeypatch.setenv("VERIPP_CHECKER_DIR", str(tmp_path / "empty"))
        monkeypatch.setattr("veripp.checker.bundled_esbmc",
                            lambda: "/wheel/esbmc")
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert find_esbmc() == "/wheel/esbmc"
