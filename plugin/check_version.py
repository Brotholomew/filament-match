#!/usr/bin/env python3
"""Check that release-facing version strings agree with the plugin source.

``plugin/filament_match.py`` is the single source of truth: its PEP 723
metadata ``version`` and its ``PLUGIN_VERSION`` constant must match, and the
user-facing documentation must not drift from them. This is deliberately a
check rather than a generator: generated docs would hide accidental edits.

Usage:
    python3 plugin/check_version.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENTRY = HERE / "filament_match.py"

HEADER_VERSION_RE = re.compile(r'^# version = "(.*)"$', re.MULTILINE)
PLUGIN_VERSION_RE = re.compile(r'^PLUGIN_VERSION = "(.*)"$', re.MULTILINE)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# (path relative to the repository root, description, regex with one group)
REFERENCES = [
    ("plugin/tests/test_entry_static.py", "static entry test",
     re.compile(r'version = "([^"]+)"')),
    ("plugin/HUB_PUBLISHING.md", "Hub listing version",
     re.compile(r"\|\s*Version\s*\|\s*`([^`]+)`\s*\|")),
    ("plugin/HUB_PUBLISHING.md", "Hub metadata example",
     re.compile(r'^# version = "([^"]+)"$', re.MULTILINE)),
]


def canonical_version(entry: Path = ENTRY) -> str:
    text = entry.read_text(encoding="utf-8")
    headers = HEADER_VERSION_RE.findall(text)
    constants = PLUGIN_VERSION_RE.findall(text)
    if len(headers) != 1:
        raise ValueError("%s must contain exactly one # version metadata line" % entry)
    if len(constants) != 1:
        raise ValueError("%s must contain exactly one PLUGIN_VERSION assignment" % entry)
    if headers[0] != constants[0]:
        raise ValueError(
            "%s version mismatch: metadata %r vs PLUGIN_VERSION %r" %
            (entry, headers[0], constants[0]))
    if not VERSION_RE.fullmatch(headers[0]):
        raise ValueError("%s has invalid version %r" % (entry, headers[0]))
    return headers[0]


def collect_references(version: str) -> list[tuple[str, str]]:
    """Return (label, found) for every release-facing version reference."""
    found: list[tuple[str, str]] = []
    for relative, label, pattern in REFERENCES:
        path = ROOT / relative
        if not path.exists():
            raise ValueError("missing version reference file: %s" % path)
        matches = pattern.findall(path.read_text(encoding="utf-8"))
        if len(matches) != 1:
            raise ValueError(
                "%s must contain exactly one %s version; found %d" %
                (relative, label, len(matches)))
        found.append((relative + ": " + label, matches[0]))
    return found


def check_artifacts(version: str) -> list[str]:
    checked: list[str] = []
    for relative in ("plugin/dist/filament_match.py",
                     "plugin/hub/filament_match_any.py"):
        path = ROOT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        headers = HEADER_VERSION_RE.findall(text)
        constants = PLUGIN_VERSION_RE.findall(text)
        if headers != [version] or constants != [version]:
            raise ValueError(
                "%s does not match the canonical version %r "
                "(header=%r, PLUGIN_VERSION=%r)" %
                (relative, version, headers, constants))
        checked.append(relative)
    return checked


def main() -> int:
    try:
        version = canonical_version()
        references = collect_references(version)
    except (OSError, ValueError) as error:
        print("version check failed: %s" % error, file=sys.stderr)
        return 1
    mismatches = [
        (label, found) for label, found in references if found != version
    ]
    if mismatches:
        print("version check failed: canonical version is %s" % version,
              file=sys.stderr)
        for label, found in mismatches:
            print("  %s: %s" % (label, found), file=sys.stderr)
        return 1
    try:
        artifacts = check_artifacts(version)
    except (OSError, ValueError) as error:
        print("version check failed: %s" % error, file=sys.stderr)
        return 1
    print("version %s is consistent across %d references%s" %
          (version, len(references),
           " and %d built artifact(s)" % len(artifacts) if artifacts else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
