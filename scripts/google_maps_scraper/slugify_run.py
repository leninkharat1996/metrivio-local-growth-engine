#!/usr/bin/env python3
"""Build a filesystem-safe slug from a keyword + location for run filenames."""
from __future__ import annotations

import re
import sys


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return value.strip("-")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print("usage: slugify_run.py <keyword> <location>", file=sys.stderr)
        return 1
    keyword, location = argv
    print(f"{slugify(location)}_{slugify(keyword)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
