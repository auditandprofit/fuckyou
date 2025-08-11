from __future__ import annotations

import ast
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Set, Tuple

from util import paths

# Regex-based mapping of dependency names to risk lenses
DEP_LENS_MAP: dict[str, str] = {
    "requests": "ssrf",
    "httpx": "ssrf",
    "jinja2": "template",
    "PyJWT|jwt": "crypto",
    "cryptography": "crypto",
    "xml.*|defusedxml": "xxe",
    "lxml": "xxe",
    "paramiko": "ssh",
    "psycopg2|mysql|sqlite3": "sql",
    "boto3": "cloud-iam",
    "pickle": "deser",
    "yaml": "deser",
    "toml": "deser",
    "tarfile": "path",
    "zipfile": "path",
    "shutil": "path",
    "subprocess": "exec",
    "os": "exec",
    "shlex": "exec",
    "flask": "authz",
    "fastapi": "authz",
    "django": "authz",
}

_DEP_LENS_RE = [(re.compile(k, re.IGNORECASE), v) for k, v in DEP_LENS_MAP.items()]

# Risk ranking: lower index => higher priority
LENS_ORDER = [
    "ssrf",
    "template",
    "crypto",
    "xxe",
    "sql",
    "cloud-iam",
    "exec",
    "path",
    "deser",
    "authz",
    "ssh",
]


def _lens_for_module(name: str) -> str | None:
    for pat, lens in _DEP_LENS_RE:
        if pat.fullmatch(name):
            return lens
    return None


def _walk_imports(code: str) -> set[str]:
    modules: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return modules
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                modules.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                name = node.module.split(".")[0]
                modules.add(name)
    return modules


@lru_cache(None)
def _deps_from_requirements(root: Path) -> set[str]:
    deps: set[str] = set()
    for req in root.glob("requirements*.txt"):
        try:
            for line in req.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                name = re.split(r"[=<>\[]", line, 1)[0].strip()
                if name:
                    deps.add(name)
        except Exception:
            continue
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            try:
                import tomllib  # py3.11+
            except ModuleNotFoundError:
                import tomli as tomllib  # py3.10

            data = tomllib.loads(pyproject.read_text())
            for dep in data.get("project", {}).get("dependencies", []) or []:
                name = re.split(r"[=<>\[]", dep, 1)[0].strip()
                if name:
                    deps.add(name)
        except Exception:
            pass
    return deps


def scan_imports(path: Path) -> set[str]:
    try:
        code = path.read_text()
    except Exception:
        return set()
    return _walk_imports(code)


@lru_cache(None)
def dep_lenses(root: Path) -> set[str]:
    """Infer lenses from repo-wide imports and dependencies."""

    modules = set()
    modules |= _deps_from_requirements(root)
    for py in root.rglob("*.py"):
        try:
            modules |= _walk_imports(py.read_text())
        except Exception:
            continue
    lenses: set[str] = set()
    for m in modules:
        lens = _lens_for_module(m)
        if lens:
            lenses.add(lens)
    return lenses


def variants_for(path: Path) -> List[Tuple[str, str]]:
    """Return ``(lens, source)`` tuples for up to two lenses for ``path``."""

    root = paths.REPO_ROOT
    local_modules = scan_imports(path)
    local_lenses = {l for m in local_modules if (l := _lens_for_module(m))}
    lenses: List[Tuple[str, str]] = []

    if os.getenv("ANCHOR_DEP_RULES", "1") not in {"0", "false", "False"}:
        global_lenses = dep_lenses(root)
    else:
        global_lenses = set()

    # first take local lenses
    for lens in local_lenses:
        lenses.append((lens, "module"))

    # supplement with global lenses until we have 2
    for lens in LENS_ORDER:
        if len(lenses) >= 2:
            break
        if lens in global_lenses and lens not in {l for l, _ in lenses}:
            lenses.append((lens, "dep"))

    # order by LENS_ORDER
    ordered: List[Tuple[str, str]] = []
    for l in LENS_ORDER:
        for lens, src in lenses:
            if lens == l and (lens, src) not in ordered:
                ordered.append((lens, src))
                if len(ordered) == 2:
                    return ordered
    return ordered

