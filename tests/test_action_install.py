"""The action's checker install step, run per platform.

Linux gets the release binary for its architecture: x86_64, and since ESBMC
8.6 arm64 too, which the step used to refuse because the only prebuilt arm64
build was the unsound Homebrew 8.4 bottle.

On a macOS runner it downloaded esbmc-macos.zip -- a build that links against
Homebrew's libraries by absolute path, which is why `veripp install-checker`
refuses to install it: unzipped anywhere else it does not start. The job then
failed two steps later, on `veripp doctor`, with nothing pointing at the
cause. This runs the step's own script, with `runner.os` filled in and
stand-ins for `uname` and `curl`, and looks at what it tried to fetch.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the step is bash, run on Linux and macOS runners"
)


def _install(tmp_path, runner_os: str, machine: str):
    """The step's exit code, what it printed, and the URLs it fetched."""
    steps = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))["runs"]["steps"]
    script = next(step for step in steps if step.get("name") == "Install ESBMC")["run"]
    script = script.replace("${{ runner.os }}", runner_os)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fetched = tmp_path / "fetched"
    (bin_dir / "uname").write_text(f"#!/bin/sh\necho {machine}\n", encoding="utf-8")
    # Record the URL, then fail as a download that never happened would.
    (bin_dir / "curl").write_text(
        f'#!/bin/sh\nfor a in "$@"; do case "$a" in http*) echo "$a" >> "{fetched}";; esac; done\n'
        "exit 22\n",
        encoding="utf-8",
    )
    for tool in ("uname", "curl"):
        (bin_dir / tool).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "ESBMC_VERSION": "v8.6",
    }
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        env=env, capture_output=True, text=True, timeout=120,
    )
    urls = fetched.read_text(encoding="utf-8").split() if fetched.exists() else []
    return done.returncode, done.stdout + done.stderr, urls


@pytest.mark.parametrize("machine", ["arm64", "x86_64"])
def test_macos_fails_with_directions_instead_of_fetching_a_dead_binary(tmp_path, machine):
    code, printed, urls = _install(tmp_path, "macOS", machine)
    assert code != 0
    assert urls == [], f"fetched {urls}, which cannot run outside Homebrew's prefix"
    assert "brew install --HEAD esbmc" in printed
    assert "already on PATH" in printed


def test_linux_x86_64_fetches_the_x86_64_release_binary(tmp_path):
    _, _, urls = _install(tmp_path, "Linux", "x86_64")
    assert urls == ["https://github.com/esbmc/esbmc/releases/download/v8.6/esbmc-linux.zip"]


@pytest.mark.parametrize("machine", ["aarch64", "arm64"])
def test_linux_arm64_fetches_the_arm64_release_binary(tmp_path, machine):
    _, _, urls = _install(tmp_path, "Linux", machine)
    assert urls == ["https://github.com/esbmc/esbmc/releases/download/v8.6/esbmc-linux-armv8.zip"]


def test_the_default_version_is_the_one_veripp_pins(tmp_path):
    from veripp.checker import ESBMC_RELEASE

    steps = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    assert steps["inputs"]["esbmc-version"]["default"] == ESBMC_RELEASE


def test_the_cache_key_tells_the_architectures_apart():
    """x86_64 and arm64 runners share an OS; a cache keyed on the OS alone
    would restore a binary the other cannot execute."""
    steps = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))["runs"]["steps"]
    cache = next(step for step in steps if step.get("id") == "cache-esbmc")
    assert "runner.arch" in cache["with"]["key"]
