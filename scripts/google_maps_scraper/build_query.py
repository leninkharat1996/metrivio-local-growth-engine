#!/usr/bin/env python3
"""
Build a gosom/google-maps-scraper input query file from a keyword + location.

Deterministic string composition only - no AI usage.

Usage:
    python3 build_query.py --keyword "commercial HVAC contractors" \
        --location "Dallas, TX" --out queries.txt
"""
from __future__ import annotations

import argparse


def build_query_line(keyword: str, location: str) -> str:
    keyword = keyword.strip()
    location = location.strip()
    if not keyword:
        raise ValueError("keyword must not be empty")
    if location:
        return f"{keyword} in {location}"
    return keyword


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--location", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    line = build_query_line(args.keyword, args.location)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
