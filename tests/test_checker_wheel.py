"""Bundling the checker into veripp's own platform wheel.

`uv build` makes a pure wheel; checker/bundle_wheel.py adds ESBMC's release
binary to a copy of it and retags it for the platform. Two things about that
are easy to get wrong and silent when wrong: the executable bit, and the
licence notices. A third is RECORD: pip refuses a wheel whose RECORD does not
cover every file.
"""

import base64
import hashlib
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUNDLER = ROOT / "checker" / "bundle_wheel.py"


@pytest.fixture
def pure_wheel(tmp_path):
    """A stand-in for what `uv build` produces: the package, and a dist-info
    with METADATA, WHEEL and RECORD."""
    path = tmp_path / "veripp-9.9.9-py3-none-any.whl"
    files = {
        "veripp/__init__.py": b'__version__ = "9.9.9"\n',
        "veripp/cli.py": b"def main(): return 0\n",
        "veripp/include/veripp/contracts.hpp": b"// header\n",
        "veripp-9.9.9.dist-info/METADATA": b"Metadata-Version: 2.4\nName: veripp\nVersion: 9.9.9\n",
        "veripp-9.9.9.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    with zipfile.ZipFile(path, "w") as z:
        records = []
        for name, data in files.items():
            z.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            records.append(f"{name},sha256={digest.decode()},{len(data)}")
        records.append("veripp-9.9.9.dist-info/RECORD,,")
        z.writestr("veripp-9.9.9.dist-info/RECORD", "\n".join(records) + "\n")
    return path


@pytest.fixture
def payload(tmp_path):
    """What build_linux_payload.sh leaves behind."""
    root = tmp_path / "payload"
    (root / "bin").mkdir(parents=True)
    (root / "licenses").mkdir()
    (root / "bin" / "esbmc").write_bytes(b"\x7fELF fake")
    (root / "licenses" / "COPYING").write_text("ESBMC licence notices\n", encoding="utf-8")
    (root / "licenses" / "Z3_LICENSE.txt").write_text("MIT\n", encoding="utf-8")
    return root


def _bundle(wheel, payload, outdir, plat="manylinux_2_17_x86_64"):
    result = subprocess.run(
        [sys.executable, str(BUNDLER), "--wheel", str(wheel), "--payload", str(payload),
         "--plat", plat, "--outdir", str(outdir)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return next(Path(outdir).glob("*.whl"))


class TestWheelContents:
    def test_it_is_named_and_tagged_for_the_platform(self, pure_wheel, payload, tmp_path):
        wheel = _bundle(pure_wheel, payload, tmp_path / "dist", plat="manylinux_2_17_aarch64")
        assert wheel.name == "veripp-9.9.9-py3-none-manylinux_2_17_aarch64.whl"
        with zipfile.ZipFile(wheel) as z:
            meta = z.read("veripp-9.9.9.dist-info/WHEEL").decode()
        assert "Tag: py3-none-manylinux_2_17_aarch64" in meta
        assert "Root-Is-Purelib: false" in meta

    def test_the_checker_keeps_its_executable_bit(self, pure_wheel, payload, tmp_path):
        """pip preserves the mode recorded in the archive. Without the file
        type bits in external_attr the checker installs as 0644, and veripp
        then reports no checker at all."""
        wheel = _bundle(pure_wheel, payload, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            mode = z.getinfo("veripp/bin/esbmc").external_attr >> 16
        assert mode & 0o111, f"not executable: {oct(mode)}"
        assert mode & 0o170000 == 0o100000, "regular-file bits missing"

    def test_everything_from_the_pure_wheel_is_still_there(self, pure_wheel, payload, tmp_path):
        wheel = _bundle(pure_wheel, payload, tmp_path / "dist")
        with zipfile.ZipFile(pure_wheel) as before, zipfile.ZipFile(wheel) as after:
            for name in before.namelist():
                if name.endswith(("/RECORD", "/WHEEL")):
                    continue
                assert after.read(name) == before.read(name), name

    def test_the_licence_notices_travel_beside_the_binary(self, pure_wheel, payload, tmp_path):
        wheel = _bundle(pure_wheel, payload, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
        assert "veripp/bin/licenses/COPYING" in names
        assert "veripp/bin/licenses/Z3_LICENSE.txt" in names

    def test_a_record_is_written_for_every_file(self, pure_wheel, payload, tmp_path):
        """pip verifies RECORD on install; a file it does not list, or a
        hash that does not match, is an install error."""
        wheel = _bundle(pure_wheel, payload, tmp_path / "dist")
        with zipfile.ZipFile(wheel) as z:
            names = set(z.namelist())
            record = z.read("veripp-9.9.9.dist-info/RECORD").decode()
            listed = {}
            for line in record.strip().splitlines():
                name, digest, _size = line.split(",")
                listed[name] = digest
            assert set(listed) == names
            for name, digest in listed.items():
                if name.endswith("/RECORD"):
                    assert digest == ""
                    continue
                expect = base64.urlsafe_b64encode(
                    hashlib.sha256(z.read(name)).digest()).rstrip(b"=").decode()
                assert digest == f"sha256={expect}", name

    @pytest.mark.skipif(os.name == "nt", reason="unix modes")
    def test_the_wheel_installs_and_veripp_finds_the_checker(self, pure_wheel, payload, tmp_path):
        """The real thing: unpack the wheel the way pip does and ask veripp's
        own lookup whether it sees a checker."""
        from veripp.checker import bundled_esbmc

        wheel = _bundle(pure_wheel, payload, tmp_path / "dist")
        site = tmp_path / "site"
        with zipfile.ZipFile(wheel) as z:
            for info in z.infolist():
                target = site / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
                mode = (info.external_attr >> 16) & 0o7777
                if mode:
                    target.chmod(mode)
        assert bundled_esbmc(site / "veripp" / "bin") == str(site / "veripp" / "bin" / "esbmc")
        assert site.joinpath("veripp", "bin", "esbmc").stat().st_mode & stat.S_IXUSR


class TestRefusals:
    def _run(self, *args):
        return subprocess.run([sys.executable, str(BUNDLER), *args],
                              capture_output=True, text=True, cwd=ROOT)

    def test_a_payload_without_a_checker_is_refused(self, pure_wheel, tmp_path):
        (tmp_path / "empty" / "bin").mkdir(parents=True)
        result = self._run("--wheel", str(pure_wheel), "--payload", str(tmp_path / "empty"),
                           "--plat", "manylinux_2_17_x86_64", "--outdir", str(tmp_path / "d"))
        assert result.returncode != 0
        assert "no bin/esbmc" in result.stderr

    def test_a_payload_without_the_notices_is_refused(self, pure_wheel, payload, tmp_path):
        """Shipping the binary without ESBMC's COPYING should not be
        something you can do by forgetting a file."""
        (payload / "licenses" / "COPYING").unlink()
        result = self._run("--wheel", str(pure_wheel), "--payload", str(payload),
                           "--plat", "manylinux_2_17_x86_64", "--outdir", str(tmp_path / "d"))
        assert result.returncode != 0
        assert "COPYING" in result.stderr

    def test_only_the_pure_wheel_is_accepted_as_input(self, payload, tmp_path):
        already = tmp_path / "veripp-9.9.9-py3-none-manylinux_2_17_x86_64.whl"
        already.write_bytes(b"PK")
        result = self._run("--wheel", str(already), "--payload", str(payload),
                           "--plat", "manylinux_2_17_x86_64", "--outdir", str(tmp_path / "d"))
        assert result.returncode != 0
        assert "py3-none-any" in result.stderr


class TestLookup:
    def test_a_source_checkout_has_no_bundled_checker(self):
        """veripp must stay usable where no platform wheel is published: a
        checkout, the pure wheel, the sdist."""
        from veripp.checker import bundled_esbmc

        found = bundled_esbmc()
        assert found is None or Path(found).exists()

    @pytest.mark.skipif(os.name == "nt", reason="unix modes")
    def test_a_binary_without_the_executable_bit_is_not_a_checker(self, tmp_path):
        from veripp.checker import bundled_esbmc

        (tmp_path / "esbmc").write_bytes(b"\x7fELF")
        (tmp_path / "esbmc").chmod(0o644)
        assert bundled_esbmc(tmp_path) is None
        (tmp_path / "esbmc").chmod(0o755)
        assert bundled_esbmc(tmp_path) == str(tmp_path / "esbmc")

    def test_the_bundled_checker_comes_last(self):
        """$VERIPP_ESBMC, then install-checker's, then PATH, then the wheel's:
        the default for people who have not chosen, not an override."""
        import inspect

        from veripp import esbmc

        source = inspect.getsource(esbmc.find_esbmc)
        assert source.index("managed_esbmc()") < source.index('which("esbmc")') < source.index("bundled_esbmc()")
