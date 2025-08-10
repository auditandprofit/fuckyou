"""Prototype orchestrator for processing findings.

This module coordinates between a manifest, an agent, and stored findings.
The agent and LLM completion are represented by stubs to keep the prototype
self-contained.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List
import json


# ----- Stubs -----------------------------------------------------------------

def orchestrator_completion(prompt: str) -> str:
    """Stub for LLM completion used by the orchestrator."""
    return ""


# ----- Data structures -------------------------------------------------------

@dataclass
class Condition:
    """A checkable assertion about a finding."""

    description: str
    state: str = "unknown"  # can be "satisfied" or "failed" in the future
    evidence: List[str] = field(default_factory=list)
    subconditions: List["Condition"] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "description": self.description,
            "state": self.state,
            "evidence": self.evidence,
            "subconditions": [c.to_dict() for c in self.subconditions],
        }


# ----- Orchestrator ----------------------------------------------------------

class Orchestrator:
    """Coordinate finding generation and condition evaluation."""

    def __init__(self, agent: Callable[[str], str]):
        self.agent = agent

    # -- Seed input -----------------------------------------------------------
    def load_manifest(self, manifest_path: Path) -> List[Path]:
        with open(manifest_path) as fh:
            return [Path(line.strip()) for line in fh if line.strip()]

    def gather_initial_findings(
        self, manifest_path: Path, prompt_prefix: str
    ) -> List[Dict]:
        findings: List[Dict] = []
        for code_path in self.load_manifest(manifest_path):
            prompt = f"{prompt_prefix}{code_path}"
            agent_response = self.agent(prompt)
            finding = {
                "claim": agent_response,
                "files": [str(code_path)],
                "evidence": agent_response,
            }
            findings.append(finding)
        return findings

    # -- Orchestration per finding -------------------------------------------
    def derive_conditions(self, finding: Dict) -> List[Condition]:
        # Deterministically derive a minimal set of conditions.
        # Stub implementation returns no conditions.
        orchestrator_completion(finding.get("claim", ""))
        return []

    def resolve_condition(self, condition: Condition) -> None:
        # Stub evaluation loop. Real implementation would create tasks and
        # interact with agents to gather evidence.
        return None

    def process_findings(self, findings_dir: Path) -> None:
        for finding_file in findings_dir.glob("*.json"):
            with open(finding_file) as fh:
                finding = json.load(fh)
            conditions = self.derive_conditions(finding)
            for condition in conditions:
                self.resolve_condition(condition)
            finding["conditions"] = [c.to_dict() for c in conditions]
            with open(finding_file, "w") as fh:
                json.dump(finding, fh, indent=2)
