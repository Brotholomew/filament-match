#!/usr/bin/env python3
"""Build a single-file Filament Match plugin from the development modules.

OrcaSlicer local plugins are loaded from one entry file. During development the
plugin is split into three modules; this script flattens them into
dist/filament_match.py for users who prefer one file.

The build refuses to run when the PEP 723 metadata is missing or invalid, when
two modules define the same top-level name, or when the entry point's metadata
version and ``PLUGIN_VERSION`` disagree. A flattened module shares one global
namespace, so a silent duplicate would change behavior without an import error.

Usage:
    python3 plugin/build_single_file.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    raise SystemExit("build_single_file.py requires Python 3.12 or newer")

try:
    import tomllib
except ImportError as error:  # Python < 3.11
    raise SystemExit(
        "build_single_file.py requires Python 3.12 or newer") from error

HERE = Path(__file__).resolve().parent
FILES = [
    "filament_match_core.py",
    "filament_match_sources.py",
    "filament_match.py",
]
OUT = HERE / "dist" / "filament_match.py"

IMPORT_RE = re.compile(
    r"^\s*(?:from\s+filament_match_(?:core|sources)\s+import\s+.*|"
    r"import\s+filament_match_(?:core|sources)\s*)$"
)
SYSPATH_RE = re.compile(r"^\s*sys\.path\.insert\(0,\s*os\.path\.dirname\(__file__\)\)\s*$")
FUTURE_RE = re.compile(r"^\s*from\s+__future__\s+import\s+.*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
PLUGIN_VERSION_RE = re.compile(r'^PLUGIN_VERSION = "(.*)"$', re.MULTILINE)
METADATA_FIELDS = ("id", "name", "description", "author", "version")


def parse_metadata(text: str) -> tuple[str, str, dict[str, str]]:
    """Return (PEP 723 header, body, plugin metadata), validating the header.

    PEP 723 requires the script block to be the first thing in the file. The
    block is TOML once its leading ``#`` comments are removed, so parse it with
    ``tomllib`` instead of trusting a hand-written regex.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "# /// script":
        raise SystemExit(
            "filament_match.py is missing its PEP 723 '# /// script' header at the top")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "# ///":
            end = index
            break
    if end is None:
        raise SystemExit("filament_match.py has an unterminated PEP 723 header")
    block_lines = []
    for index, line in enumerate(lines[1:end], start=2):
        if not line.startswith("#"):
            raise SystemExit(
                "PEP 723 header line %d is not a comment: %r" % (index, line))
        content = line[1:]
        if content.startswith(" "):
            content = content[1:]
        block_lines.append(content)
    try:
        data = tomllib.loads("\n".join(block_lines))
    except tomllib.TOMLDecodeError as error:
        raise SystemExit("invalid PEP 723 TOML header: %s" % error) from error

    requires_python = data.get("requires-python")
    if not isinstance(requires_python, str) or not requires_python.strip():
        raise SystemExit("PEP 723 header is missing requires-python")
    dependencies = data.get("dependencies")
    if not isinstance(dependencies, list):
        raise SystemExit("PEP 723 header is missing the dependencies array")

    try:
        plugin = data["tool"]["orcaslicer"]["plugin"]
    except (KeyError, TypeError):
        raise SystemExit("PEP 723 header is missing [tool.orcaslicer.plugin]") from None
    if not isinstance(plugin, dict):
        raise SystemExit("[tool.orcaslicer.plugin] is not a TOML table")
    for field in METADATA_FIELDS:
        value = plugin.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(
                "[tool.orcaslicer.plugin] is missing a non-empty %s field" % field)
    if not VERSION_RE.fullmatch(plugin["version"]):
        raise SystemExit(
            "invalid plugin version %r; expected a semantic version" % plugin["version"])
    if plugin["name"] != "Filament Match":
        raise SystemExit("unexpected plugin name %r" % plugin["name"])

    header = "\n".join(lines[:end + 1]) + "\n"
    body = "\n".join(lines[end + 1:])
    return header, body, {field: plugin[field] for field in METADATA_FIELDS}


def split_metadata(text: str) -> tuple[str, str]:
    """Backwards-compatible wrapper returning only (header, body)."""
    header, body, _ = parse_metadata(text)
    return header, body


def strip_module_boundaries(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if IMPORT_RE.match(line):
            # Keep the enclosing try/if block syntactically valid. The flattened
            # file already has the sibling symbols in the global namespace.
            indent = re.match(r"^(\s*)", line).group(1)
            lines.append(indent + "pass")
            continue
        if SYSPATH_RE.match(line) or FUTURE_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in target.elts:
            names.extend(_target_names(element))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _guard_names(test: ast.AST) -> set[str]:
    """Names a ``"name" not in globals()`` condition protects."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return set()
    if not isinstance(test.ops[0], ast.NotIn):
        return set()
    left = test.left
    right = test.comparators[0]
    if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
        return set()
    if (isinstance(right, ast.Call) and isinstance(right.func, ast.Name)
            and right.func.id == "globals" and not right.args):
        return {left.value}
    return set()


def module_bindings(source: str, filename: str = "<unknown>") -> list[tuple[str, int, bool]]:
    """Return module-level names bound by definitions or assignments.

    The third item is true when the binding only runs under an explicit
    ``"name" not in globals()`` guard, which is safe in the flattened build.

    Module-level ``if``/``try``/``with``/``for``/``match`` bodies still execute
    in the module namespace. The old checker only looked at ``tree.body``, so a
    function defined under ``if TYPE_CHECKING`` or a fallback ``try`` block
    could collide with another module in the flattened build unnoticed.
    Nested function/class bodies are local and are deliberately not scanned.
    """
    tree = ast.parse(source, filename=filename)
    found: list[tuple[str, int, bool]] = []
    try_nodes = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
    type_alias = getattr(ast, "TypeAlias", None)

    def collect(target: ast.AST, guarded: bool) -> None:
        for name in _target_names(target):
            found.append((name, target.lineno, guarded))

    def walk(statements: list[ast.stmt], guarded: bool = False) -> None:
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.append((node.name, node.lineno, guarded))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    collect(target, guarded)
            elif isinstance(node, ast.AnnAssign):
                collect(node.target, guarded)
            elif type_alias is not None and isinstance(node, type_alias):
                found.append((node.name.id, node.lineno, guarded))
            elif isinstance(node, ast.If):
                protected = _guard_names(node.test)
                walk(node.body, guarded or bool(protected))
                walk(node.orelse, guarded)
            elif isinstance(node, ast.While):
                walk(node.body, guarded)
                walk(node.orelse, guarded)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                walk(node.body, guarded)
                walk(node.orelse, guarded)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                walk(node.body, guarded)
            elif isinstance(node, try_nodes):
                walk(node.body, guarded)
                walk(node.orelse, guarded)
                walk(node.finalbody, guarded)
                for handler in node.handlers:
                    walk(handler.body, guarded)
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    walk(case.body, guarded)
            # Imports are intentionally ignored: the source modules share
            # standard-library imports and the flattened file can keep them.
    walk(tree.body)
    return found


def _top_level_names(path: Path) -> dict[str, int]:
    """Compatibility helper used by older tests; includes conditional defs."""
    names: dict[str, int] = {}
    for name, line, _guarded in module_bindings(path.read_text(encoding="utf-8"), str(path)):
        names[name] = line
    return names


def find_duplicate_bindings(sources: list[tuple[str, str]]) -> list[str]:
    """Find cross-module names a flattened build would silently overwrite."""
    seen: dict[str, tuple[str, int]] = {}
    duplicates: list[str] = []
    for filename, source in sources:
        for name, line, guarded in module_bindings(source, filename):
            if name == "__all__" or guarded:
                # A later guarded definition runs only if the name is absent,
                # so it cannot silently shadow an earlier module's definition.
                continue
            if name in seen:
                previous_file, previous_line = seen[name]
                if previous_file != filename:
                    duplicates.append(
                        "%s: %s:%d and %s:%d" %
                        (name, previous_file, previous_line, filename, line))
            else:
                seen[name] = (filename, line)
    return duplicates


def check_duplicate_names(files: list[str] | None = None) -> None:
    """A flattened module shares one global namespace.

    Later top-level definitions silently replace earlier ones. That once made
    the core matcher's candidate normalizer disappear behind the entry's OFD
    lookup function, so every non-generic vendor fell back to Generic. Refuse
    to build if two source modules define the same top-level name, including
    annotated assignments and definitions inside conditional blocks.

    A definition guarded by ``if "name" not in globals():`` is safe when it
    comes later; it deliberately keeps the already-flattened definition. This
    is how the source module keeps its stand-alone fallback without breaking
    the single-file build. ``__all__`` is export metadata and is merged by the
    source module, so it is not treated as a runtime collision.
    """
    sources = []
    for filename in files or FILES:
        path = HERE / filename
        if not path.exists():
            raise SystemExit("missing source module: %s" % path)
        sources.append((filename, path.read_text(encoding="utf-8")))
    duplicates = find_duplicate_bindings(sources)
    if duplicates:
        raise SystemExit(
            "duplicate top-level names in the flattened build:\n  " +
            "\n  ".join(duplicates))


def _check_output(output: str) -> None:
    """Validate the generated file before it replaces the previous build."""
    try:
        ast.parse(output)
    except SyntaxError as error:
        raise SystemExit("flattened build is not valid Python: %s" % error) from error
    _, _, metadata = parse_metadata(output)
    versions = PLUGIN_VERSION_RE.findall(output)
    if len(versions) != 1:
        raise SystemExit(
            "flattened build must contain exactly one PLUGIN_VERSION assignment; "
            "found %d" % len(versions))
    if versions[0] != metadata["version"]:
        raise SystemExit(
            "version mismatch: header has %r but PLUGIN_VERSION is %r" %
            (metadata["version"], versions[0]))


def render() -> str:
    """Return the flattened single-file build without writing it."""
    check_duplicate_names()
    entry_path = HERE / "filament_match.py"
    if not entry_path.exists():
        raise SystemExit(f"missing {entry_path}")
    header, entry_body, _metadata = parse_metadata(
        entry_path.read_text(encoding="utf-8"))

    parts = [header]
    parts.append("# Generated by plugin/build_single_file.py — edit the source modules, not this file.\n")
    parts.append("\n# ---- filament_match_core.py ----\n")
    parts.append(strip_module_boundaries((HERE / "filament_match_core.py").read_text(encoding="utf-8")))
    parts.append("\n# ---- filament_match_sources.py ----\n")
    parts.append(strip_module_boundaries((HERE / "filament_match_sources.py").read_text(encoding="utf-8")))
    parts.append("\n# ---- filament_match.py ----\n")
    parts.append(strip_module_boundaries(entry_body))
    output = "\n".join(parts)
    _check_output(output)
    return output


def main() -> None:
    output = render()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(output, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
