#!/usr/bin/env python3
"""
Parse and validate a multiline Google Maps search plan.

Each non-blank, non-comment line of the plan has the form:

    keyword | location | depth

`depth` is optional; when omitted, --default-depth is used. Lines starting
with '#' (after stripping whitespace) are treated as comments and skipped.
Blank lines are skipped.

This module is pure/deterministic: no network calls, no randomness. It is
the single source of truth for what counts as a valid search line so the
workflow, the tests, and any future caller agree on the same rules.

Usage:
    python3 search_plan.py --plan-file plan.txt --out plan.json
    python3 search_plan.py --plan-file plan.txt --default-depth 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys

DEFAULT_DEPTH = 5


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return value.strip("-")


def _parse_depth(raw_depth: str, default_depth: int) -> tuple[int | None, str | None]:
    raw_depth = raw_depth.strip()
    if not raw_depth:
        return default_depth, None
    if not re.match(r"^\d+$", raw_depth):
        return None, f"depth must be a positive integer, got {raw_depth!r}"
    depth = int(raw_depth)
    if depth < 1:
        return None, "depth must be >= 1"
    return depth, None


def parse_line(raw_line: str, line_no: int, default_depth: int = DEFAULT_DEPTH) -> dict:
    """Parse and validate a single search-plan line.

    Returns a dict with `valid` (bool). Valid entries additionally carry
    `keyword`, `location`, `depth`. Invalid entries carry `error` describing
    what's wrong, and never carry a search_id (assigned later, only for
    valid entries, so ids stay deterministic regardless of how many
    invalid lines surround them).
    """
    entry = {"line_no": line_no, "raw": raw_line}

    parts = [p.strip() for p in raw_line.split("|")]
    if len(parts) not in (2, 3):
        entry["valid"] = False
        entry["error"] = (
            "expected 'keyword | location' or 'keyword | location | depth', "
            f"got {len(parts)} field(s)"
        )
        return entry

    keyword = parts[0]
    location = parts[1]
    raw_depth = parts[2] if len(parts) == 3 else ""

    if not keyword:
        entry["valid"] = False
        entry["error"] = "keyword must not be empty"
        return entry
    if not location:
        entry["valid"] = False
        entry["error"] = "location must not be empty"
        return entry

    depth, depth_error = _parse_depth(raw_depth, default_depth)
    if depth_error:
        entry["valid"] = False
        entry["error"] = depth_error
        return entry

    entry.update(valid=True, keyword=keyword, location=location, depth=depth, error=None)
    return entry


def assign_search_ids(entries: list[dict]) -> None:
    """Assign deterministic, collision-safe search_id to each valid entry.

    The id is built from location+keyword slugs, in the order they appear
    in the plan. A duplicate keyword/location pair (or a slug collision
    between two different pairs) gets a numeric suffix so two searches
    never share an id and therefore never share an output directory.
    """
    seen: dict[str, int] = {}
    for entry in entries:
        if not entry.get("valid"):
            continue
        base = f"{slugify(entry['location'])}_{slugify(entry['keyword'])}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        entry["search_id"] = base if count == 0 else f"{base}-{count + 1}"


def parse_plan(text: str, default_depth: int = DEFAULT_DEPTH) -> list[dict]:
    entries = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(parse_line(stripped, line_no, default_depth))
    assign_search_ids(entries)
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--default-depth", type=int, default=DEFAULT_DEPTH)
    parser.add_argument("--out", default=None, help="Write JSON array here; defaults to stdout")
    args = parser.parse_args(argv)

    with open(args.plan_file, encoding="utf-8") as f:
        text = f.read()

    entries = parse_plan(text, args.default_depth)
    output = json.dumps(entries, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output + "\n")
    else:
        print(output)

    if not entries:
        print("error: search plan contains no usable lines", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
