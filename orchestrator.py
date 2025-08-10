"""Prototype orchestrator for processing findings.

This module coordinates between a manifest, an agent, and stored findings.
LLM calls are routed through :mod:`util.openai` so that judgment, condition
generation, and task generation can be powered by the OpenAI API when
available.  In environments without an API key the orchestrator gracefully
falls back to stub behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List
import json
import logging

from util.openai import (
    openai_generate_response,
    openai_parse_function_call,
)


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
        self.logger = logging.getLogger(__name__)

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
            claim = str(agent_response).strip() or f"Review {code_path}"
            finding = {
                "claim": claim,
                "files": [str(code_path)],
                "evidence": agent_response,
            }
            findings.append(finding)
        return findings

    # -- Orchestration per finding -------------------------------------------
    def derive_conditions(self, finding: Dict) -> List[Condition]:
        """Use the LLM to deterministically derive conditions.

        The model is expected to call the ``emit_conditions`` function with a
        JSON payload of ``{"conditions": ["..."]}``.  If the OpenAI client is
        unavailable or the call fails, an empty list is returned.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "Given a bug finding, extract minimal checkable conditions "
                    "and respond via function call."
                ),
            },
            {
                "role": "user",
                "content": f"Finding claim: {finding.get('claim', '')}",
            },
        ]
        functions = [
            {
                "name": "emit_conditions",
                "description": "Return a list of condition descriptions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conditions": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["conditions"],
                },
            }
        ]
        try:
            response = openai_generate_response(
                messages=messages,
                functions=functions,
                function_call={"name": "emit_conditions"},
            )
            _, data = openai_parse_function_call(response)
            conds = [Condition(description=d) for d in data.get("conditions", [])]
            return conds
        except Exception as exc:  # pragma: no cover - network/credential issues
            self.logger.warning("condition generation failed: %s", exc)
            return []

    def generate_tasks(self, condition: Condition) -> List[str]:
        """Generate tasks to gather evidence for ``condition`` using the LLM."""
        messages = [
            {"role": "system", "content": "You generate step-by-step tasks."},
            {
                "role": "user",
                "content": f"Condition: {condition.description}",
            },
        ]
        functions = [
            {
                "name": "emit_tasks",
                "description": "Return a list of tasks to gather evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["tasks"],
                },
            }
        ]
        try:
            response = openai_generate_response(
                messages=messages,
                functions=functions,
                function_call={"name": "emit_tasks"},
                temperature=0,
            )
            _, data = openai_parse_function_call(response)
            return data.get("tasks", [])
        except Exception as exc:  # pragma: no cover
            self.logger.warning("task generation failed: %s", exc)
            return []

    def judge_condition(self, condition: Condition) -> str:
        """Use the LLM to judge whether ``condition`` is satisfied."""
        if not condition.evidence:
            return "unknown"
        messages = [
            {
                "role": "system",
                "content": "Decide if a condition is satisfied based on evidence.",
            },
            {
                "role": "user",
                "content": (
                    f"Condition: {condition.description}\nEvidence: {condition.evidence}"
                ),
            },
        ]
        functions = [
            {
                "name": "judge_condition",
                "description": "Judge condition state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["satisfied", "failed", "unknown"],
                        }
                    },
                    "required": ["state"],
                },
            }
        ]
        try:
            response = openai_generate_response(
                messages=messages,
                functions=functions,
                function_call={"name": "judge_condition"},
                temperature=0,
            )
            _, data = openai_parse_function_call(response)
            return data.get("state", "unknown")
        except Exception as exc:  # pragma: no cover
            self.logger.warning("judgement failed: %s", exc)
            return "unknown"

    def _execute_tasks(self, finding_file: Path, condition: Condition, tasks: list[str]) -> list[dict]:
        """Execute tasks via agent and persist a task log blob."""
        task_results = []
        for t in tasks:
            try:
                out = self.agent(t)
                task_results.append({"task": t, "output": out})
                condition.evidence.append(str(out)[:10000])
            except Exception as exc:
                task_results.append({"task": t, "error": str(exc)})
        with open(finding_file) as fh:
            data = json.load(fh)
        data.setdefault("tasks_log", []).append({
            "condition": condition.description,
            "executed": task_results,
        })
        with open(finding_file, "w") as fh:
            json.dump(data, fh, indent=2)
        return task_results

    def resolve_condition(self, condition: Condition, finding_path: Path) -> None:
        """Resolve a condition by generating tasks and judging the result."""
        tasks = self.generate_tasks(condition)
        if not tasks:
            return
        self.logger.info("Tasks for condition '%s': %s", condition.description, tasks)
        self._execute_tasks(finding_path, condition, tasks)
        condition.state = self.judge_condition(condition)

    def process_findings(self, findings_dir: Path) -> None:
        for finding_file in findings_dir.glob("finding_*.json"):
            self.logger.info("Processing %s", finding_file.name)
            with open(finding_file) as fh:
                finding = json.load(fh)
            conditions = self.derive_conditions(finding)
            for condition in conditions:
                self.resolve_condition(condition, finding_file)
            # reload tasks_log in case tasks were executed
            with open(finding_file) as fh:
                updated = json.load(fh)
            finding["tasks_log"] = updated.get("tasks_log", [])
            finding["conditions"] = [c.to_dict() for c in conditions]
            with open(finding_file, "w") as fh:
                json.dump(finding, fh, indent=2)
