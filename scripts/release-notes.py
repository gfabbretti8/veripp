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
    # The changelog is UTF-8 prose -- arrows, em dashes -- and this output is
    # piped straight into `gh release create`. Windows defaults stdout to
    # cp1252, which cannot encode all of it, so a release note would fail to
    # print for the character it happened to contain.
    sys.stdout.reconfigure(encoding="utf-8")
    print(section.group(1).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
