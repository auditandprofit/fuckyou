from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import List


def git_changed_files(since: str | None = None, window_days: int | None = None) -> List[Path]:
    """Return repo-relative Paths changed since ``since`` or within ``window_days``."""

    try:
        if window_days is not None:
            since_date = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")
            out = subprocess.check_output(
                ["git", "log", "--since", since_date, "--name-only", "--pretty=format:"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        else:
            args = ["git", "diff", "--name-only"]
            if since:
                args.append(f"{since}..HEAD")
            out = subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return []
    files = [f.strip() for f in out.splitlines() if f.strip()]
    return [Path(f) for f in files if Path(f).exists() and f.endswith(".py")]

