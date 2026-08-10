#!/usr/bin/env python3
"""Canonicalize pg_dump text for deterministic lifecycle comparison.

PostgreSQL emits a random client-side ``\\restrict`` token and the matching
``\\unrestrict`` token in plain-text dumps.  The token is deliberately
nondeterministic and is not part of the database catalog, ACL, RLS, ownership,
or schema contract we are trying to compare.

This helper removes only that validated wrapper pair.  Every other byte is
preserved.  A malformed, duplicated, or mismatched wrapper fails closed rather
than broadening normalization and potentially hiding real schema drift.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re


_RESTRICT = re.compile(r"^\\\\restrict ([^\r\n]+)(\r?\n)?$")
_UNRESTRICT = re.compile(r"^\\\\unrestrict ([^\r\n]+)(\r?\n)?$")


def canonicalize_pg_dump(raw: str) -> str:
    """Return *raw* without pg_dump's validated random restriction wrapper."""
    restrict_tokens: list[str] = []
    unrestrict_tokens: list[str] = []
    kept: list[str] = []

    for line in raw.splitlines(keepends=True):
        restrict = _RESTRICT.match(line)
        if restrict:
            restrict_tokens.append(restrict.group(1))
            continue

        unrestrict = _UNRESTRICT.match(line)
        if unrestrict:
            unrestrict_tokens.append(unrestrict.group(1))
            continue

        kept.append(line)

    counts = (len(restrict_tokens), len(unrestrict_tokens))
    if counts == (0, 0):
        # Older pg_dump versions may not emit the wrapper.  No normalization is
        # needed and the dump remains byte-for-byte unchanged.
        return raw

    if counts != (1, 1):
        raise ValueError(
            "pg_dump restriction wrapper must contain exactly one "
            f"restrict/unrestrict pair; found {counts[0]}/{counts[1]}"
        )

    if restrict_tokens[0] != unrestrict_tokens[0]:
        raise ValueError("pg_dump restrict/unrestrict tokens do not match")

    return "".join(kept)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    raw = args.source.read_text(encoding="utf-8")
    canonical = canonicalize_pg_dump(raw)
    args.destination.write_text(canonical, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
