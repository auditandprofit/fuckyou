from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Tuple

# Lightweight regex patterns for various risk categories
_CATEGORY_PATTERNS: dict[str, Iterable[str]] = {
    "network": [r"requests", r"httpx\.", r"urllib", r"urlopen"],
    "filesystem": [r"\bopen\(", r"os\.chmod", r"os\.chown", r"tempfile"],
    "template": [r"jinja2", r"render_", r"Template"],
    "crypto": [r"jwt", r"fernet", r"hmac", r"os\.urandom"],
    "config": [r"os\.environ", r"dotenv", r"boto3", r"AWS_[A-Z_]*"],
    "server": [r"uvicorn", r"gunicorn", r"click\.command", r"typer"],
    "serialization": [r"json\.load", r"yaml\.load", r"toml\.load", r"xml", r"defusedxml"],
    "archive": [r"tarfile", r"zipfile"],
    "subprocess": [r"subprocess", r"os\.system"],
}

# Simple category weights; higher weight => higher priority
_CATEGORY_WEIGHTS = {
    "network": 4,
    "filesystem": 3,
    "template": 3,
    "crypto": 3,
    "config": 2,
    "server": 2,
    "serialization": 2,
    "archive": 1,
    "subprocess": 1,
}

_COMPILED = {
    cat: [re.compile(p) for p in pats] for cat, pats in _CATEGORY_PATTERNS.items()
}


def find(root: Path, *, categories: set[str] | None = None) -> List[Tuple[Path, str, int]]:
    """Return ``(path, category, score)`` for files matching hotspot patterns."""

    results: list[Tuple[Path, str, int]] = []
    for path in root.rglob("*.py"):
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        for cat, patterns in _COMPILED.items():
            if categories and cat not in categories:
                continue
            matches = sum(1 for p in patterns if p.search(text))
            if matches:
                score = _CATEGORY_WEIGHTS.get(cat, 1) + matches
                results.append((path, cat, score))
                break
    return results

