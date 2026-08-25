#!/usr/bin/env python3
"""Print one version's section of CHANGELOG.md, for `gh release create`.

Keeping the release page and the changelog in sync by hand means they drift,
and the release page is the one clients read.

    scripts/release-notes.py 0.1.1 > notes.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} VERSION   (e.g. 0.1.1)")
    version = sys.argv[1].lstrip("v")
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = re.search(
        rf"^## {re.escape(version)}\n(.*?)(?=^## |\Z)", text, re.S | re.M
    )
    if not section:
        sys.exit(f"CHANGELOG.md has no section for {version}")
    print(section.group(1).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
