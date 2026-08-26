"""Structural symbol index — what a file DEFINES, not what text it contains.

verify_claims answers "does this string exist anywhere in the repo?" Answers make a
stronger claim than that: symbol S is a field of class C, defined in file F, at line L.
Grep verifies the weakest term and throws the other three away, which is how a battery
run had a fabricated field certified:

    claim   `reg_id` is a field of PartyRegistration at models.py:264-290
    grep    FOUND  reg_id  <- krakend/krakend.json:2909
                              {"@comment":"=== Inventory: party locations ==="}

`reg_id` occurs zero times in models.py. In a repo this size almost any plausible
identifier appears somewhere, so repo-wide existence is close to worthless as evidence
for a scoped claim -- and certifying a fabrication is worse than missing one, because it
converts a guess into evidence.

This answers containment instead: does class C actually declare field A? Deterministic,
no model involved, same AST walk tools/context.py already uses for skeletons.

Deliberately covers only what the answers actually claim, measured across four batteries:
Python classes and their assigned fields, Python functions, TypeScript exports (the RTK
hooks), and route decorators (the endpoints). Anything outside that returns "unknown"
and the caller falls back to the existing grep -- an index that cannot see a language
must not report its symbols as absent.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

PROJECT_ROOT = Path(config.PROJECT_ROOT)

# (mtime, index) per file, so repeated claims about one file parse it once.
_CACHE: dict[str, tuple[float, dict]] = {}

_TS_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:const|function|class|interface|type|enum)\s+(\w+)"
    r"|^\s*(use[A-Z]\w*)\s*,?\s*$"      # a hook listed in an export block
    r"|^\s*export\s*\{([^}]*)\}",
    re.MULTILINE,
)
_ROUTE_RE = re.compile(r"@\w+\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']",
                       re.IGNORECASE)


def _py_index(src: str) -> dict:
    """Classes -> their declared fields, plus module-level functions, with line numbers."""
    try:
        tree = ast.parse(src.lstrip("﻿"))
    except (SyntaxError, ValueError):
        return {}
    classes: dict[str, dict] = {}
    functions: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            fields: dict[str, int] = {}
            for stmt in node.body:
                # `name = Column(...)` and `name: T = ...` are both field declarations.
                targets = []
                if isinstance(stmt, ast.Assign):
                    targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    targets = [stmt.target]
                for t in targets:
                    fields[t.id] = stmt.lineno
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fields[stmt.name] = stmt.lineno
            classes[node.name] = {"line": node.lineno, "fields": fields,
                                  "bases": [b.id for b in node.bases
                                            if isinstance(b, ast.Name)]}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node.lineno)
    return {"classes": classes, "functions": functions, "lang": "py"}


def _ts_index(src: str) -> dict:
    """Exported names. Covers `export const useX`, `export { a, b }`, and the bare
    listing style RTK slices use, where hooks appear one per line in an export block."""
    exports: dict[str, int] = {}
    for i, line in enumerate(src.splitlines(), start=1):
        m = re.match(r"\s*export\s+(?:const|function|class|interface|type|enum)\s+(\w+)", line)
        if m:
            exports.setdefault(m.group(1), i)
            continue
        m = re.match(r"\s*(use[A-Z]\w*)\s*,\s*$", line)
        if m:
            exports.setdefault(m.group(1), i)
    for m in re.finditer(r"export\s*\{([^}]*)\}", src, re.DOTALL):
        line = src[:m.start()].count("\n") + 1
        for name in re.split(r"[,\s]+", m.group(1)):
            name = name.strip()
            if re.fullmatch(r"\w+", name or ""):
                exports.setdefault(name, line)
    return {"exports": exports, "lang": "ts"}


def _routes(src: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in _ROUTE_RE.finditer(src):
        line = src[:m.start()].count("\n") + 1
        out.setdefault(f"{m.group(1).upper()} {m.group(2)}", line)
    return out


def index_file(rel_path: str) -> dict | None:
    """Structural index of one file, or None when it cannot be indexed.

    None means "this checker cannot see this file" and must never be read as "the
    symbol is absent" -- the caller falls back to grep.
    """
    path = PROJECT_ROOT / rel_path
    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _CACHE.get(rel_path)
    if cached and cached[0] == stat.st_mtime:
        return cached[1]
    if stat.st_size > 4_000_000:
        return None
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    suffix = path.suffix.lower()
    if suffix == ".py":
        idx = _py_index(src)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        idx = _ts_index(src)
    else:
        return None
    if not idx:
        return None
    idx["routes"] = _routes(src)
    _CACHE[rel_path] = (stat.st_mtime, idx)
    return idx


def declares(rel_path: str, symbol: str) -> tuple[bool | None, int | None, str]:
    """Does `rel_path` DEFINE `symbol`?

    Returns (verdict, line, where):
      (True,  line, "class Foo" | "function" | "export" | "route")
      (False, None, "")   -- indexed, and the symbol is not defined here
      (None,  None, "")   -- not indexable; caller must fall back to grep
    """
    idx = index_file(rel_path)
    if idx is None:
        return None, None, ""
    if idx.get("lang") == "py":
        classes = idx.get("classes", {})
        if symbol in classes:
            return True, classes[symbol]["line"], "class"
        for cname, meta in classes.items():
            if symbol in meta["fields"]:
                return True, meta["fields"][symbol], f"field of {cname}"
        if symbol in idx.get("functions", {}):
            return True, idx["functions"][symbol], "function"
    else:
        exports = idx.get("exports", {})
        if symbol in exports:
            return True, exports[symbol], "export"
    for route, line in idx.get("routes", {}).items():
        if symbol in route:
            return True, line, "route"
    return False, None, ""


def field_of(rel_path: str, class_name: str, field: str) -> tuple[bool | None, str]:
    """Is `field` declared on `class_name` in `rel_path`?

    Returns (verdict, detail). detail names the class that DOES declare it when the
    answer put it on the wrong one -- the `effective_from` case, which is real in
    models.py but belongs to HSNCatalogue, not PartyRegistration.
    """
    idx = index_file(rel_path)
    if idx is None or idx.get("lang") != "py":
        return None, ""
    classes = idx.get("classes", {})
    if class_name not in classes:
        return None, ""
    if field in classes[class_name]["fields"]:
        return True, ""
    owners = [c for c, meta in classes.items() if field in meta["fields"]]
    if owners:
        return False, f"declared on {', '.join(sorted(owners)[:3])} instead"
    return False, ""
