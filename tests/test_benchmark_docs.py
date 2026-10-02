"""The benchmark documents agree with themselves and with each other.

CORPUS.md is where results are measured; benchmarks/README.md repeats some of
them. The README went on quoting the first probe -- lodepng 54/260 harnessable,
cJSON 4/117 -- a day after CORPUS.md measured 82% and 89%, and seven of
CORPUS.md's ten rows had drifted below a later section, where Markdown no
longer renders them as a table.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "benchmarks" / "CORPUS.md"
README = ROOT / "benchmarks" / "README.md"

_ROW = re.compile(r"^\| \[([^\]]+)\]")


def _rows(path: Path) -> dict[str, list[str]]:
    """Library name -> its table cells, for every row that links a library."""
    rows: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            rows[match.group(1)] = [cell.strip() for cell in line.strip("|").split("|")]
    return rows


def test_every_corpus_row_is_in_the_table():
    """A row only renders as part of a table when an unbroken run of table
    lines above it reaches the header's delimiter row."""
    lines = CORPUS.read_text(encoding="utf-8").splitlines()
    stranded = []
    for number, line in enumerate(lines):
        if not _ROW.match(line):
            continue
        above = number
        while above > 0 and lines[above - 1].startswith("|"):
            above -= 1
        if not any(re.match(r"^\|\s*:?-{3,}", lines[i]) for i in range(above, number)):
            stranded.append(_ROW.match(line).group(1))
    assert not stranded, f"CORPUS.md rows outside any table: {stranded}"


def test_the_readme_quotes_the_corpus_figures():
    corpus = _rows(CORPUS)
    readme = _rows(README)
    shared = sorted(set(corpus) & set(readme))
    assert shared, "no library appears in both documents"
    for library in shared:
        functions, proved, _, harnessable = (
            cell.strip("*") for cell in corpus[library][2:6]
        )
        quoted = " | ".join(readme[library])
        assert f"{harnessable} of {functions} functions" in quoted, (library, quoted)
        assert f"{proved} proved" in quoted, (library, quoted)
