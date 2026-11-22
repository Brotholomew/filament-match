#!/usr/bin/env python3
"""Validate a built Filament Match artifact without importing Orca.

Used by CI after a dry-run Hub build with a fake version. Checks the PEP 723
metadata, Hub fields, both version fields and that the file compiles.

Usage:
    python3 plugin/check_artifact.py PATH [--version X.Y.Z]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from build_single_file import parse_metadata  # noqa: E402

PLUGIN_VERSION_RE = re.compile(r'^PLUGIN_VERSION = "(.*)"$', re.MULTILINE)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def check(path: Path, expected_version: str = "") -> str:
    text = path.read_text(encoding="utf-8")
    _header, _body, metadata = parse_metadata(text)
    versions = PLUGIN_VERSION_RE.findall(text)
    if len(versions) != 1:
        raise ValueError(
            "%s must contain exactly one PLUGIN_VERSION assignment" % path)
    if versions[0] != metadata["version"]:
        raise ValueError(
            "%s version mismatch: metadata %r vs PLUGIN_VERSION %r" %
            (path, metadata["version"], versions[0]))
    if expected_version:
        if not VERSION_RE.fullmatch(expected_version):
            raise ValueError("invalid expected version %r" % expected_version)
        if metadata["version"] != expected_version:
            raise ValueError(
                "%s metadata version is %r, expected %r" %
                (path, metadata["version"], expected_version))
    try:
        compile(text, str(path), "exec")
    except SyntaxError as error:
        raise ValueError("%s is not valid Python: %s" % (path, error)) from error
    return metadata["version"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--version", default="")
    args = parser.parse_args(argv)
    try:
        version = check(args.path, args.version.strip())
    except (OSError, ValueError) as error:
        print("artifact check failed: %s" % error, file=sys.stderr)
        return 1
    print("artifact OK: %s (version %s)" % (args.path, version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
