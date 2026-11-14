#!/usr/bin/env python3
"""Render the README's terminal picture from a real run.

    python scripts/render-demo.py            # rewrites docs/demo.svg

The picture at the top of the README is the first thing a visitor reads, so
it is produced by running the command it shows rather than typed by hand:
what is in it is what veripp printed. tests/test_readme_promises.py re-runs
the command and fails if the committed picture says anything else.
"""
from __future__ import annotations

import html
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo.svg"

# The command, as the README gives it. Triage is off so the picture does not
# depend on which model, if any, the machine rendering it has configured.
COMMAND = ["verify", "examples/off_by_one.cpp", "--function", "sum_array", "--no-llm"]

COLUMNS = 96
CHAR_W, LINE_H, PAD, BAR = 7.8, 19, 18, 34

# One palette, legible on GitHub's light and dark pages alike because the
# window brings its own background.
BG, FG, DIM = "#0d1117", "#e6edf3", "#8b949e"
RED, GREEN, YELLOW, BLUE = "#ff7b72", "#7ee787", "#e3b341", "#79c0ff"


def run() -> str:
    """veripp's own output for COMMAND, run from the repository root."""
    result = subprocess.run(
        [sys.executable, "-m", "veripp.cli", *COMMAND],
        capture_output=True, text=True, cwd=ROOT, timeout=900,
    )
    return result.stdout


def colour(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("Result: counterexample"):
        return RED
    if stripped.startswith("Result:"):
        return GREEN
    if stripped.startswith(("Violated property", ">")) or stripped == "^":
        return RED
    if stripped.startswith(("Assumptions", "Counterexample inputs")):
        return YELLOW
    if stripped.startswith(("bounded,", "guard:", "CWE:", "Attempts:", "Not triaged")):
        return DIM
    return FG


def wrap(line: str) -> list[str]:
    """Fold a long line the way a terminal would show it, keeping its indent."""
    if len(line) <= COLUMNS:
        return [line]
    indent = " " * (len(line) - len(line.lstrip()))
    return textwrap.wrap(
        line, COLUMNS, initial_indent="", subsequent_indent=indent,
        break_long_words=False, break_on_hyphens=False,
    )


def render(output: str) -> str:
    rows: list[tuple[str, str]] = [("$ veripp " + " ".join(COMMAND[:-1]), BLUE)]
    for line in output.rstrip("\n").split("\n"):
        shade = colour(line)
        rows += [(piece, shade) for piece in wrap(line)]

    width = round(COLUMNS * CHAR_W + 2 * PAD)
    height = BAR + len(rows) * LINE_H + 2 * PAD - 6
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        'aria-label="veripp finding an off-by-one in examples/off_by_one.cpp">',
        f'<rect width="{width}" height="{height}" rx="8" fill="{BG}"/>',
        '<circle cx="20" cy="17" r="6" fill="#ff5f56"/>',
        '<circle cx="40" cy="17" r="6" fill="#ffbd2e"/>',
        '<circle cx="60" cy="17" r="6" fill="#27c93f"/>',
        '<g font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,'
        '\'Liberation Mono\',monospace" font-size="13">',
    ]
    for i, (text, shade) in enumerate(rows):
        y = BAR + PAD + i * LINE_H
        # No-break spaces: SVG text collapses ordinary ones, and the indent
        # and the caret's column are part of what veripp printed.
        cell = html.escape(text).replace(" ", "\u00a0")
        parts.append(f'<text x="{PAD}" y="{y}" fill="{shade}">{cell}</text>')
    parts += ["</g>", "</svg>", ""]
    return "\n".join(parts)


def main() -> int:
    output = run()
    if "Result:" not in output:
        print("veripp printed no result; is a checker installed? (veripp doctor)", file=sys.stderr)
        return 1
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(output), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
