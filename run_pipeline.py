"""Run the prototype orchestration pipeline."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import hashlib
import json
import logging
import time

from codex_dispatch import CodexClient
from codex_agent import CodexAgent
from orchestrator import Orchestrator
from util.git import get_git_short, is_dirty
from util.time import utc_now_iso, utc_timestamp
from util import paths
from util.io import atomic_write
from util.manifest import validate_manifest
import util.openai as openai

PROMPT_PREFIX = "Analyze file: "
VERSION = "0.1"


def write_run_json(run_path: Path, data: dict) -> None:
    atomic_write(run_path / "run.json", json.dumps(data, indent=2).encode())


def parse_args(argv: list[str] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="manifest.txt")
    ap.add_argument("--findings-dir", default="findings")
    ap.add_argument("--prompt-prefix", default=PROMPT_PREFIX)
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--repo-root", default=str(paths.REPO_ROOT))
    ap.add_argument("--model", default=openai.DEFAULT_MODEL)
    ap.add_argument("--reasoning-effort", default=openai.DEFAULT_REASONING_EFFORT)
    ap.add_argument("--service-tier", default=openai.DEFAULT_SERVICE_TIER)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args([] if argv is None else argv)

    repo_root = Path(args.repo_root).resolve()
    os.chdir(repo_root)
    paths.REPO_ROOT = repo_root
    if openai.openai_generate_response.__kwdefaults__ is not None:
        openai.openai_generate_response.__kwdefaults__.update(
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            service_tier=args.service_tier,
        )

    manifest_path = Path(args.manifest)
    try:
        manifest_files = validate_manifest(manifest_path)
    except Exception as exc:
        print(f"Manifest error: {exc}")
        raise SystemExit(1)

    git_short = get_git_short()
    run_ts = utc_timestamp()
    run_id = f"{run_ts}_{git_short}"
    findings_root = Path(args.findings_dir)
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
        "prompt_prefix": args.prompt_prefix,
        "started_at": utc_now_iso(),
        "finished_at": None,
        "counts": {"manifest_files": 0, "findings_written": 0, "errors": 0},
        "git": {"commit": git_short, "dirty": is_dirty()},
        "version": args.version,
        "manifest_sha1": hashlib.sha1(manifest_path.read_bytes()).hexdigest(),
        "llm": {
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "service_tier": args.service_tier,
        },
    }
    write_run_json(run_path, run_data)

    counts = run_data["counts"]
    counts["manifest_files"] = len(manifest_files)

    codex = CodexClient()
    codex_agent = CodexAgent(codex, workdir=str(repo_root))
    orch = Orchestrator(codex_agent.run)

    try:
        initial = orch.gather_initial_findings(manifest_files, args.prompt_prefix)
    except Exception as exc:  # pragma: no cover - unexpected
        logger.error("initial gathering failed: %s", exc)
        initial = []

    for f in initial:
        try:
            rel_path = Path(f["files"][0])
            abs_path = repo_root / rel_path
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

    try:
        orch.process_findings(run_path)
    except Exception as exc:
        logger.error("Aborting run due to LLM failure: %s", exc)
        counts["errors"] += 1
        run_data["finished_at"] = utc_now_iso()
        write_run_json(run_path, run_data)
        raise SystemExit(1)

    run_data["finished_at"] = utc_now_iso()
    write_run_json(run_path, run_data)


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
