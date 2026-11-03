"""The action's checker install step, run per platform.

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
        "ESBMC_VERSION": "weekly",
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


def test_linux_x86_64_still_fetches_the_weekly_build(tmp_path):
    _, _, urls = _install(tmp_path, "Linux", "x86_64")
    assert urls == ["https://github.com/esbmc/esbmc/releases/download/weekly/esbmc-linux.zip"]


def test_linux_arm64_still_says_why_it_cannot(tmp_path):
    code, printed, urls = _install(tmp_path, "Linux", "aarch64")
    assert code != 0 and urls == []
    assert "esbmc#6508" in printed
