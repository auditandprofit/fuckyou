"""Deterministic agent for safe task execution."""
from __future__ import annotations

from pathlib import Path
import ast
import hashlib
from typing import Any

from util.paths import REPO_ROOT, repo_rel

MAX_BYTES = 100_000


def _safe_path(p: str) -> Path:
    rel = repo_rel(Path(p))
    return REPO_ROOT / rel


def _sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def run_agent(goal: str) -> Any:
    try:
        if goal.startswith("read:"):
            p = _safe_path(goal.split(":", 1)[1])
            b = p.read_bytes()[:MAX_BYTES]
            return {
                "type": "read",
                "path": str(p),
                "bytes": b.decode("utf-8", "replace"),
                "sha1": _sha1_bytes(b),
            }
        if goal.startswith("stat:"):
            p = _safe_path(goal.split(":", 1)[1])
            b = p.read_bytes()
            return {
                "type": "stat",
                "path": str(p),
                "size": len(b),
                "sha1": _sha1_bytes(b),
            }
        if goal.startswith("py:functions:"):
            p = _safe_path(goal.split(":", 2)[2])
            tree = ast.parse(p.read_text())
            defs = [
                {"name": n.name, "args": len(n.args.args)}
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
            ]
            return {"type": "py:functions", "path": str(p), "functions": defs}
        if goal.startswith("py:classes:"):
            p = _safe_path(goal.split(":", 2)[2])
            tree = ast.parse(p.read_text())
            classes = []
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef):
                    methods = [m.name for m in n.body if isinstance(m, ast.FunctionDef)]
                    classes.append({"name": n.name, "methods": methods})
            return {"type": "py:classes", "path": str(p), "classes": classes}
        return {"type": "noop", "goal": goal}
    except Exception as exc:  # pragma: no cover - best effort
        return {"error": str(exc), "goal": goal}
