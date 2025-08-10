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
import hashlib
import json
import logging
import re

from util.io import atomic_write
from util.openai import (
    openai_generate_response,
    openai_parse_function_call,
)
from util.time import utc_now_iso


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

    VERSION = "0.1"

    def __init__(self, agent: Callable[[str], str]):
        self.agent = agent
        self.logger = logging.getLogger(__name__)

    # -- Seed input -----------------------------------------------------------
    def gather_initial_findings(
        self, manifest_files: List[Path], _prompt_prefix: str
    ) -> List[Dict]:
        findings: List[Dict] = []
        for code_path in manifest_files:
            data = self.agent(f"codex:discover:{code_path.as_posix()}")
            findings.append(
                {
                    "claim": data.get("claim", f"Review {code_path.as_posix()}"),
                    "files": data.get("files", [code_path.as_posix()]),
                    "evidence": data.get("evidence", {}),
                }
            )
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
            code_path = Path(finding["files"][0]) if finding.get("files") else Path("?")
            conds = [Condition(description="Target file exists")]
            if code_path.suffix == ".py":
                conds.append(Condition(description="Target file parses as Python"))
            return conds

    def _plan_tasks(self, condition: Condition, code_path: Path) -> List[str]:
        desc = condition.description.lower()
        tasks: List[str] = []
        path = code_path.as_posix()
        if "exists" in desc:
            tasks.append(f"stat:{path}")
        if "parses" in desc and code_path.suffix == ".py":
            tasks.append(f"py:functions:{path}")
        return tasks

    def _normalize_task(self, text: str, code_path: Path) -> str | None:
        t = text.lower()
        path = code_path.as_posix()
        if re.search(r"stat|exists", t):
            return f"stat:{path}"
        if code_path.suffix == ".py":
            if re.search(r"functions|parse|syntax", t):
                return f"py:functions:{path}"
            if "class" in t:
                return f"py:classes:{path}"
        if "read" in t:
            return f"read:{path}"
        return None

    def generate_tasks(self, condition: Condition, code_path: Path) -> List[dict]:
        """Generate tasks to gather evidence for ``condition``."""
        messages = [
            {"role": "system", "content": "You generate step-by-step tasks."},
            {"role": "user", "content": f"Condition: {condition.description}"},
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
        tasks: List[dict] = []
        try:
            response = openai_generate_response(
                messages=messages,
                functions=functions,
                function_call={"name": "emit_tasks"},
                temperature=0,
            )
            _, data = openai_parse_function_call(response)
            tasks = [{"task": t, "original": t} for t in data.get("tasks", []) or []]
        except Exception as exc:  # pragma: no cover
            self.logger.warning("task generation failed: %s", exc)
        if not tasks:
            for t in self._plan_tasks(condition, code_path):
                tasks.append({"task": t, "original": t})
        return tasks

    def judge_condition(self, condition: Condition) -> str:
        """Deterministically judge ``condition`` based on available evidence."""
        if not condition.evidence:
            return "unknown"
        latest = condition.evidence[-1]
        try:
            data = json.loads(latest)
        except Exception:
            data = {}
        if data.get("error"):
            if "exists" in condition.description.lower() or "parses" in condition.description.lower():
                return "failed"
        typ = data.get("type")
        if typ == "exec":
            typ = data.get("result", {}).get("type")
        if typ == "stat":
            return "satisfied"
        if typ == "py:functions":
            return "satisfied"
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

    # -- Sub-condition narrowing -------------------------------------------
    def _narrow_subconditions(self, condition: Condition) -> List[Condition]:
        """Deterministically derive sub-conditions for an uncertain condition."""
        messages = [
            {
                "role": "system",
                "content": (
                    "Propose minimal, checkable sub-conditions that would "
                    "resolve uncertainty."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Parent condition: {condition.description}\n"
                    f"Current evidence: {condition.evidence[-1:]}"
                ),
            },
        ]
        functions = [
            {
                "name": "emit_conditions",
                "description": "Return a list of subcondition descriptions.",
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
                temperature=0,
            )
            _, data = openai_parse_function_call(response)
            subs = [Condition(description=d) for d in data.get("conditions", [])]
            condition.subconditions.extend(subs)
            return subs
        except Exception:  # pragma: no cover - openai failure
            return []

    def _execute_tasks(self, finding_file: Path, condition: Condition, tasks: List[dict]) -> List[dict]:
        """Execute tasks via agent and persist a task log blob."""
        with open(finding_file) as fh:
            finding = json.load(fh)
        code_path = finding.get("provenance", {}).get("path", "")
        task_results: List[dict] = []
        for t in tasks:
            goal = t["task"]
            original = t.get("original", goal)
            stamp = utc_now_iso()
            goal_hash = hashlib.sha1(goal.encode()).hexdigest()
            try:
                out = self.agent(f"codex:exec:{code_path}::{goal}")
                task_results.append(
                    {
                        "task": goal,
                        "original": original,
                        "output": out,
                        "timestamp": stamp,
                        "input_sha1": goal_hash,
                    }
                )
                condition.evidence.append(json.dumps(out)[:10000])
            except Exception as exc:
                task_results.append(
                    {
                        "task": goal,
                        "original": original,
                        "error": str(exc),
                        "timestamp": stamp,
                        "input_sha1": goal_hash,
                    }
                )
        finding.setdefault("tasks_log", []).append(
            {"condition": condition.description, "executed": task_results}
        )
        atomic_write(finding_file, json.dumps(finding, indent=2).encode())
        return task_results

    def resolve_condition(
        self, condition: Condition, finding_path: Path, *, max_steps: int = 3
    ) -> None:
        """Resolve a condition by iterating generate→execute→judge cycles."""
        with open(finding_path) as fh:
            finding = json.load(fh)
        code_path = Path(finding.get("provenance", {}).get("path", ""))
        for step in range(max_steps):
            tasks = self.generate_tasks(condition, code_path)
            if not tasks:
                break
            self.logger.info(
                "Tasks for condition '%s' [step %d]: %s",
                condition.description,
                step + 1,
                tasks,
            )
            self._execute_tasks(finding_path, condition, tasks)
            state = self.judge_condition(condition)
            condition.state = state
            if state != "unknown":
                return
            subs = self._narrow_subconditions(condition)
            if subs:
                for sub in subs:
                    self.resolve_condition(sub, finding_path, max_steps=max_steps)
                states = {c.state for c in subs}
                if states == {"satisfied"}:
                    condition.state = "satisfied"
                    return
                if "failed" in states and "satisfied" not in states:
                    condition.state = "failed"
                    return

    def process_findings(self, findings_dir: Path, *, max_steps: int = 3) -> None:
        for finding_file in findings_dir.glob("finding_*.json"):
            self.logger.info("Processing %s", finding_file.name)
            with open(finding_file) as fh:
                finding = json.load(fh)
            conditions = self.derive_conditions(finding)
            for condition in conditions:
                self.resolve_condition(condition, finding_file, max_steps=max_steps)
            # reload tasks_log in case tasks were executed
            with open(finding_file) as fh:
                updated = json.load(fh)
            finding["tasks_log"] = updated.get("tasks_log", [])
            finding["conditions"] = [c.to_dict() for c in conditions]
            atomic_write(finding_file, json.dumps(finding, indent=2).encode())
