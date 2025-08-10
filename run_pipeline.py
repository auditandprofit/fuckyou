"""Run the prototype orchestration pipeline."""
from pathlib import Path
import json

from agent import run_agent
from orchestrator import Orchestrator


PROMPT_PREFIX = "Analyze file: "


def main() -> None:
    manifest_path = Path("manifest.txt")
    findings_dir = Path("findings")
    findings_dir.mkdir(exist_ok=True)

    orch = Orchestrator(run_agent)
    findings = orch.gather_initial_findings(manifest_path, PROMPT_PREFIX)

    # Persist initial findings
    for idx, finding in enumerate(findings, 1):
        with open(findings_dir / f"finding_{idx}.json", "w") as fh:
            json.dump(finding, fh, indent=2)

    # Process each finding
    orch.process_findings(findings_dir)


if __name__ == "__main__":
    main()
