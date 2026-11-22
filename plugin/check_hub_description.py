#!/usr/bin/env python3
"""Verify the Orca Cloud listing description fits the Hub limit.

Orca Cloud currently rejects descriptions above 10,000 characters. The API
also transports UTF-8, so a description with multi-byte characters can exceed
the byte limit while still fitting the character count. Enforce both.

Usage:
    python3 plugin/check_hub_description.py [plugin/HUB_DESCRIPTION.md]
"""
from __future__ import annotations

import sys
from pathlib import Path

MAX_CHARS = 10_000
MAX_BYTES = 10_000
DEFAULT = Path(__file__).resolve().parent / "HUB_DESCRIPTION.md"


def check(path: Path = DEFAULT) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8-sig")
    characters = len(text)
    byte_count = len(text.encode("utf-8"))
    problems = []
    if characters > MAX_CHARS:
        problems.append("%d characters (limit %d)" % (characters, MAX_CHARS))
    if byte_count > MAX_BYTES:
        problems.append("%d UTF-8 bytes (limit %d)" % (byte_count, MAX_BYTES))
    if problems:
        raise ValueError("%s is too large: %s" % (path, "; ".join(problems)))
    return characters, byte_count


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT
    try:
        characters, byte_count = check(path)
    except (OSError, ValueError) as error:
        print("Hub description check failed: %s" % error, file=sys.stderr)
        return 1
    print("%s: %d characters, %d UTF-8 bytes (limit %d/%d)" %
          (path, characters, byte_count, MAX_CHARS, MAX_BYTES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
