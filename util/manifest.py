from __future__ import annotations

from pathlib import Path
from typing import List

from util.paths import repo_rel, REPO_ROOT


def validate_manifest(manifest_path: Path) -> List[Path]:
    """Validate manifest file paths and return normalized repo-relative Paths."""
    paths: List[Path] = []
    seen: set[str] = set()
    with open(manifest_path) as fh:
        for line in fh:
            entry = line.strip()
            if not entry:
                continue
            rel = repo_rel(Path(entry))
            abs_path = REPO_ROOT / rel
            if not abs_path.exists():
                raise FileNotFoundError(f"Missing manifest file: {entry}")
            rel_str = rel.as_posix()
            if rel_str in seen:
                raise ValueError(f"Duplicate path in manifest: {entry}")
            seen.add(rel_str)
            paths.append(rel)
    return sorted(paths, key=lambda p: p.as_posix())
