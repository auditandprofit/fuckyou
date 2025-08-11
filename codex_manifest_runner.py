from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable


def _iter_manifest_files(manifest_path: Path) -> Iterable[Path]:
    """Yield file paths listed in ``manifest_path``.

    The manifest may be a JSON file of the form ``{"files": [...]}`` or a
    simple newline-delimited text file.  Paths are yielded exactly as they
    appear in the manifest (no normalization is performed).
    """

    if manifest_path.suffix == ".json":
        data = json.loads(manifest_path.read_text())
        for entry in data.get("files", []):
            yield Path(entry)
    else:
        for line in manifest_path.read_text().splitlines():
            entry = line.strip()
            if entry:
                yield Path(entry)


def _invoke_codex(codex_bin: str, file_path: Path, output_dir: Path) -> str:
    """Invoke codex for ``file_path`` and return the last message."""

    work_dir = file_path.parent
    digest = hashlib.sha256(str(file_path).encode()).hexdigest()[:8]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{digest}.txt"

    cmd = [
        codex_bin,
        "exec",
        "--output-last-message",
        str(output_path),
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C",
        str(work_dir),
    ]

    proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
    proc.wait()
    return output_path.read_text().strip()


def run_manifest(
    codex_bin: str, manifest_path: Path, output_dir: Path = Path("codex_outputs")
) -> Dict[Path, str]:
    """Run codex for each file in ``manifest_path``.

    Returns a mapping from each manifest file path to the final assistant
    message written by codex.
    """

    results: Dict[Path, str] = {}
    for file_path in _iter_manifest_files(manifest_path):
        results[file_path] = _invoke_codex(codex_bin, file_path, output_dir)
    return results


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("codex_bin", help="Path to the codex executable")
    ap.add_argument("manifest", help="Path to manifest.json or manifest.txt")
    ap.add_argument(
        "--output-dir",
        default="codex_outputs",
        help="Directory where codex last messages will be written",
    )
    args = ap.parse_args(argv)

    run_manifest(args.codex_bin, Path(args.manifest), Path(args.output_dir))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
