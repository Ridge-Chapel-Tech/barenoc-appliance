#!/usr/bin/env python3
"""Print the CHANGELOG section for a version (used by the release workflow).

Usage: python3 scripts/changelog_section.py <version>   # e.g. 0.1.1
Prints everything between '## [<version>]' and the next '## [' (or EOF).
"""

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: changelog_section.py <version>", file=sys.stderr)
        return 2
    version = sys.argv[1]
    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text()
    pattern = re.compile(rf"^## \[{re.escape(version)}\](.*?)(?=^## \[|\Z)", re.M | re.S)
    m = pattern.search(text)
    if not m:
        print(f"no CHANGELOG section for [{version}]", file=sys.stderr)
        return 1
    print(m.group(1).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
