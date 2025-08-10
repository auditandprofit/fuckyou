from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def repo_rel(path: Path) -> Path:
    """Return a repository-relative, normalized path.

    Raises ValueError if the path is outside the repository.
    """
    path = Path(path)
    abs_path = (REPO_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        rel = abs_path.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ValueError(f"{path} is outside the repository") from exc
    return rel
