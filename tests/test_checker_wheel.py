"""Building the bundled-checker wheel.

The wheel exists so that `pip install veripp[checker]` is the whole install.
Two things about it are easy to get wrong and silent when wrong: the
executable bit, and the licence notices.
"""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILDER = ROOT / "checker" / "build_wheel.py"


@pytest.fixture
def payload(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "bin" / "esbmc").write_bytes(b"\x7fELF fake")
    (tmp_path / "lib" / "libz3.so.4").write_bytes(b"\x7fELF fake lib")
    return tmp_path


@pytest.fixture
def licence(tmp_path):
    path = tmp_path / "COPYING"
    path.write_text("ESBMC licence notices\n", encoding="utf-8")
    return path


def _build(payload, licence, outdir, plat="manylinux_2_38_x86_64"):
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--payload", str(payload),
         "--plat", plat, "--license", str(licence), "--outdir", str(outdir)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return next(Path(outdir).glob("*.whl"))


class TestWheelContents:
    def test_the_checker_keeps_its_executable_bit(self, payload, licence, tmp_path):
        """pip preserves the mode recorded in the archive. Without the file
        type bits in external_attr the checker installs as 0644, and veripp
        then reports no checker at all -- which is how this was found."""
        wheel = _build(payload, licence, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            mode = (z.getinfo("veripp_checker/bin/esbmc").external_attr >> 16)
        assert mode & 0o111, f"not executable: {oct(mode)}"
        assert mode & 0o170000 == 0o100000, "regular-file bits missing"

    def test_shared_libraries_travel_with_it(self, payload, licence, tmp_path):
        wheel = _build(payload, licence, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            assert "veripp_checker/lib/libz3.so.4" in z.namelist()

    def test_the_licence_notices_are_shipped(self, payload, licence, tmp_path):
        """ESBMC derives from CBMC under BSD-4-clause, whose advertising
        clause obliges the notice to travel with the binary."""
        wheel = _build(payload, licence, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
            notice = next(n for n in names if n.endswith("COPYING.esbmc"))
            assert "ESBMC licence notices" in z.read(notice).decode()

    def test_the_import_shim_is_included(self, payload, licence, tmp_path):
        wheel = _build(payload, licence, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            assert "veripp_checker/__init__.py" in z.namelist()

    def test_the_platform_tag_reaches_the_filename_and_metadata(
        self, payload, licence, tmp_path
    ):
        """A wheel tagged for the wrong platform installs on machines whose
        glibc cannot run the binary."""
        wheel = _build(payload, licence, tmp_path / "dist",
                       plat="manylinux_2_38_aarch64")
        assert wheel.name.endswith("manylinux_2_38_aarch64.whl")
        with zipfile.ZipFile(wheel) as z:
            meta = z.read("veripp_checker-0.1.0.dist-info/WHEEL").decode()
        assert "Tag: py3-none-manylinux_2_38_aarch64" in meta

    def test_a_record_is_written_for_every_file(self, payload, licence, tmp_path):
        wheel = _build(payload, licence, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            record = z.read("veripp_checker-0.1.0.dist-info/RECORD").decode()
            recorded = {line.split(",")[0] for line in record.splitlines()}
            missing = set(z.namelist()) - recorded
        assert not missing, f"absent from RECORD: {missing}"


class TestRefusals:
    def test_a_payload_without_a_checker_is_refused(self, tmp_path, licence):
        (tmp_path / "bin").mkdir()
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--payload", str(tmp_path),
             "--plat", "manylinux_2_38_x86_64", "--license", str(licence),
             "--outdir", str(tmp_path / "dist")],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode != 0
        assert "no bin/esbmc" in result.stderr

    def test_the_licence_is_not_optional(self, payload, tmp_path):
        """Building a redistributable binary without its notices should not
        be something you can do by forgetting a flag."""
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--payload", str(payload),
             "--plat", "manylinux_2_38_x86_64", "--outdir", str(tmp_path / "d")],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode != 0
        assert "--license" in result.stderr


def test_veripp_falls_back_gracefully_without_the_package():
    """veripp must stay usable where no wheel is published."""
    from veripp.checker import bundled_esbmc

    assert bundled_esbmc() is None or Path(bundled_esbmc()).exists()


class TestFallbackWheel:
    """The binary-free wheel is what lets `pip install veripp` resolve on a
    platform no checker wheel targets. Without it the dependency would have
    to be an extra, and batteries-included would stop being the default."""

    def _build(self, licence, outdir):
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--fallback",
             "--license", str(licence), "--outdir", str(outdir)],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode == 0, result.stderr
        return next(Path(outdir).glob("*.whl"))

    def test_it_is_tagged_any_so_every_platform_can_resolve(self, licence, tmp_path):
        wheel = self._build(licence, tmp_path / "dist")
        assert wheel.name.endswith("py3-none-any.whl")

    def test_it_carries_no_binary(self, licence, tmp_path):
        wheel = self._build(licence, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            payload = [n for n in z.namelist() if "/bin/" in n or ".so" in n]
        assert not payload, f"fallback wheel is not binary-free: {payload}"

    def test_it_still_ships_the_shim_and_the_notices(self, licence, tmp_path):
        wheel = self._build(licence, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
        assert "veripp_checker/__init__.py" in names
        assert any(n.endswith("COPYING.esbmc") for n in names)

    def test_a_platform_wheel_still_needs_its_payload(self, licence, tmp_path):
        """--fallback is the only way to build without one; forgetting
        --payload must not quietly produce an empty platform wheel."""
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--plat", "manylinux_2_38_x86_64",
             "--license", str(licence), "--outdir", str(tmp_path / "d")],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode != 0
        assert "--payload" in result.stderr
