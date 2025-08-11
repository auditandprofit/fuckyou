import ast
import re
from functools import lru_cache
from pathlib import Path
from util import paths

# Mapping of module names to risk lenses
MODULE_LENS_MAP = {
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
    "jinja2": "authz",
}

# Risk ranking: lower index => higher priority
LENS_ORDER = ["exec", "path", "deser", "authz"]


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
            import tomllib

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


def variants_for(path: Path) -> list[str]:
    """Return up to two risk lenses for ``path`` based on imports/deps."""
    root = paths.REPO_ROOT
    modules = scan_imports(path)
    modules |= _deps_from_requirements(root)
    lenses = []
    for m in modules:
        lens = MODULE_LENS_MAP.get(m)
        if lens:
            lenses.append(lens)
    seen = set()
    ordered = []
    for lens in LENS_ORDER:
        if lens in lenses and lens not in seen:
            ordered.append(lens)
            seen.add(lens)
        if len(ordered) == 2:
            break
    return ordered
