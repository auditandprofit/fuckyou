"""Run the prototype orchestration pipeline."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import logging
import time

from agent import run_agent
from orchestrator import Orchestrator
from util.git import get_git_short, is_dirty
from util.time import utc_now_iso, utc_timestamp
from util.paths import REPO_ROOT
from util.io import atomic_write
from util.manifest import validate_manifest
from util.openai import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SERVICE_TIER,
)

PROMPT_PREFIX = "Analyze file: "
VERSION = "0.1"


def write_run_json(run_path: Path, data: dict) -> None:
    atomic_write(run_path / "run.json", json.dumps(data, indent=2).encode())


def main() -> None:
    manifest_path = Path("manifest.txt")
    try:
        manifest_files = validate_manifest(manifest_path)
    except Exception as exc:
        print(f"Manifest error: {exc}")
        raise SystemExit(1)

    git_short = get_git_short()
    run_ts = utc_timestamp()
    run_id = f"{run_ts}_{git_short}"
    findings_root = Path("findings")
    run_path = findings_root / f"run_{run_id}"
    while run_path.exists():
        time.sleep(1)
        run_ts = utc_timestamp()
        run_id = f"{run_ts}_{git_short}"
        run_path = findings_root / f"run_{run_id}"
    run_path.mkdir(parents=True, exist_ok=False)

    logger = logging.getLogger("orchestrator")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(run_path / "orchestrator.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    logger.info("Run %s commit %s", run_id, git_short)

    run_data = {
        "run_id": run_id,
        "manifest_path": str(manifest_path),
        "prompt_prefix": PROMPT_PREFIX,
        "started_at": utc_now_iso(),
        "finished_at": None,
        "counts": {"manifest_files": 0, "findings_written": 0, "errors": 0},
        "git": {"commit": git_short, "dirty": is_dirty()},
        "version": VERSION,
        "manifest_sha1": hashlib.sha1(manifest_path.read_bytes()).hexdigest(),
        "llm": {
            "model": DEFAULT_MODEL,
            "reasoning_effort": DEFAULT_REASONING_EFFORT,
            "service_tier": DEFAULT_SERVICE_TIER,
        },
    }
    write_run_json(run_path, run_data)

    counts = run_data["counts"]
    counts["manifest_files"] = len(manifest_files)

    orch = Orchestrator(run_agent)

    try:
        initial = orch.gather_initial_findings(manifest_files, PROMPT_PREFIX)
    except Exception as exc:  # pragma: no cover - unexpected
        logger.error("initial gathering failed: %s", exc)
        initial = []

    for f in initial:
        try:
            rel_path = Path(f["files"][0])
            abs_path = REPO_ROOT / rel_path
            file_bytes = abs_path.read_bytes()
            finding_id = hashlib.sha1(rel_path.as_posix().encode()).hexdigest()[:12]
            finding = {
                "finding_id": finding_id,
                "schema_version": 1,
                "orchestrator_version": Orchestrator.VERSION,
                "claim": f["claim"],
                "files": f["files"],
                "evidence": {"seed": f["evidence"]},
                "provenance": {
                    "run_id": run_id,
                    "created_at": utc_now_iso(),
                    "input_hash": hashlib.sha1(file_bytes).hexdigest(),
                    "file_size": len(file_bytes),
                    "path": rel_path.as_posix(),
                },
                "status": "seeded",
                "conditions": [],
                "tasks_log": [],
            }
            atomic_write(
                run_path / f"finding_{finding_id}.json",
                json.dumps(finding, indent=2).encode(),
            )
            counts["findings_written"] += 1
            logger.info("Seeded finding %s", finding_id)
        except Exception as exc:  # per-file errors
            logger.error("Error processing %s: %s", f, exc)
            counts["errors"] += 1

    orch.process_findings(run_path)

    run_data["finished_at"] = utc_now_iso()
    write_run_json(run_path, run_data)


if __name__ == "__main__":
    main()
