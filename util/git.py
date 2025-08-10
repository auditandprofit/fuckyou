from __future__ import annotations

import subprocess


def get_git_short() -> str:
    """Return the short hash of HEAD or 'no_git' if unavailable."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out
    except Exception:
        return "no_git"


def is_dirty() -> bool:
    """Return True if the git working tree has uncommitted changes."""
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return bool(out.strip())
    except Exception:
        return False
