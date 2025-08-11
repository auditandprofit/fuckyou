from __future__ import annotations

import re
from pathlib import Path
from typing import List

SINK_PATTERNS = [
    r"\beval\(",
    r"\bexec\(",
    r"subprocess\.",
    r"os\.system\(",
    r"pickle\.load[s]?\(",
    r"yaml\.load\(",
    r"tarfile\.open\(",
    r"requests\([^)]*verify=False",
]

ENTRY_PATTERNS = [
    r"@app\.route",
    r"FastAPI\(",
    r"argparse\.ArgumentParser",
    r"click\.command",
]

PATTERNS = [re.compile(p) for p in SINK_PATTERNS + ENTRY_PATTERNS]


def find(root: Path) -> List[Path]:
    """Return repository-relative paths with hotspot indicators."""
    results: set[Path] = set()
    for path in root.rglob("*.py"):
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        if any(p.search(text) for p in PATTERNS):
            results.add(path)
    return sorted(results)
