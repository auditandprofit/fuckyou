"""Prototype orchestrator for processing findings.

This module coordinates between a manifest, an agent, and stored findings.
LLM calls are routed through :mod:`util.openai` so that judgment, condition
generation, and task generation are powered by the OpenAI API.  All such calls
must succeed; if the API is unavailable the run aborts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List
import hashlib
import json
import logging
import os
import re
import time

from util.io import atomic_write
from util.openai import (
    openai_generate_response,
    openai_parse_function_call,
)
from util.time import utc_now_iso

BANNER = "Deterministic security auditor. No network. No writes. JSON only."


# ----- Data structures -------------------------------------------------------

@dataclass
class Condition:
    """A checkable assertion about a finding."""

    description: str
    why: str = ""
    accept: str = ""
    reject: str = ""
    suggested_tasks: List[str] = field(default_factory=list)
    state: str = "unknown"  # can be "satisfied" or "failed" in the future
    rationale: str = ""
    evidence_refs: List[int] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    subconditions: List["Condition"] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "description": self.description,
            "why": self.why,
            "accept": self.accept,
            "reject": self.reject,
            "suggested_tasks": self.suggested_tasks,
            "state": self.state,
            "rationale": self.rationale,
            "evidence_refs": self.evidence_refs,
            "evidence": self.evidence,
            "subconditions": [c.to_dict() for c in self.subconditions],
        }


# ----- Orchestrator ----------------------------------------------------------

class Orchestrator:
    """Coordinate finding generation and condition evaluation."""

    VERSION = "0.2"

    def __init__(self, agent: Callable[[str], str], *, reporter=None):
        self.agent = agent
        self.logger = logging.getLogger(__name__)
        self.max_retries = int(os.getenv("ANCHOR_OPENAI_RETRIES", "3"))
        self.reporter = reporter

    def _with_retries(self, func: Callable, *args, **kwargs):
        last_exc = None
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - network issues
                last_exc = exc
                self.logger.warning(
                    "OpenAI call failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (2**attempt))
        raise last_exc

    # -- Seed input -----------------------------------------------------------
    def gather_initial_findings(
        self, manifest_files: List[Path], _prompt_prefix: str
    ) -> List[Dict]:
        findings: List[Dict] = []
        for code_path in manifest_files:
            self.logger.info("Discovering %s", code_path.as_posix())
            data = self.agent(f"codex:discover:{code_path.as_posix()}")
            self.logger.info("Discovered %s", code_path.as_posix())
            if isinstance(data, str):
                claim, files, evidence = data.strip(), [code_path.as_posix()], {}
            elif isinstance(data, dict):
                claim = data.get("claim") or f"Review {code_path.as_posix()}"
                files = data.get("files") or [code_path.as_posix()]
                evidence = data.get("evidence", {})
            else:
                claim, files, evidence = (
                    f"Review {code_path.as_posix()}",
                    [code_path.as_posix()],
                    {},
                )
            findings.append({"claim": claim, "files": files, "evidence": evidence})
        return findings

    # -- Orchestration per finding -------------------------------------------
    def derive_conditions(self, finding: Dict) -> List[Condition]:
        """Use the LLM to deterministically derive conditions."""
        claim = finding.get("claim", "")
        related_files = finding.get("files", [])
        messages = [
            {
                "role": "system",
                "content": (
                    f"{BANNER}\nSTAGE: derive\n\n"
                    "You are a security-auditing assistant. Your sole objective is to "
                    "ADJUDICATE a specific bug claim as TRUE POSITIVE or FALSE POSITIVE "
                    "with defensible, testable evidence. Maintain determinism "
                    "(temperature=0). Output STRICT JSON only—no markdown/prose. Use only "
                    "repository-local information and permitted executions. No network. "
                    "No file writes.\n\nDefinitions:\n- TRUE POSITIVE: Evidence demonstrates the claim holds under realistic "
                    "conditions within the codebase.\n- FALSE POSITIVE: Evidence demonstrates the claim does not hold, is unreachable, "
                    "or is otherwise invalid.\n- UNKNOWN: Evidence gathered so far is insufficient; propose targeted sub-checks.\n\n"
                    "Evidence must be concrete, minimally sufficient, and reproducible."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Goal: Break down the bug claim into minimal, testable security CONDITIONS that, if individually decided, collectively allow a final verdict (TRUE POSITIVE / FALSE POSITIVE).\n\n"
                    f"Inputs:\n- claim: \"{claim}\"\n- related_files: {json.dumps(related_files)}\n\n"
                    "Mindset:\n- Act as a FALSE-POSITIVE filter: add decisive checks that would invalidate the claim (e.g., input not user-controlled; guard exists on all paths; sanitization before sink).\n- Prefer objective, repo-local observations.\n\n"
                    "Constraints:\n- 1–5 conditions.\n- Each condition must be objectively checkable via codex-executable tasks.\n- Each condition must state an acceptance criterion tied to the final verdict.\n\n"
                    "Output JSON:\n{\"conditions\":[\n  {\n    \\\"desc\\\":\\\"<short, testable statement>\\\",\\n    \\\"why\\\":\\\"<why this condition matters to verifying/refuting the claim>\\\",\\n    \\\"accept\\\":\\\"<what observation(s) would satisfy>\\\",\\n    \\\"reject\\\":\\\"<what observation(s) would fail>\\\",\\n    \\\"suggested_tasks\\\":[\\\"<exec-able task string>\\\", \\\"...\\\"]\\n  }\n]}\n"
                ),
            },
        ]
        functions = [
            {
                "name": "emit_conditions",
                "description": "Return condition objects.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conditions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "desc": {"type": "string"},
                                    "why": {"type": "string"},
                                    "accept": {"type": "string"},
                                    "reject": {"type": "string"},
                                    "suggested_tasks": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": [
                                    "desc",
                                    "why",
                                    "accept",
                                    "reject",
                                    "suggested_tasks",
                                ],
                            },
                        }
                    },
                    "required": ["conditions"],
                },
            }
        ]
        self.logger.info("LLM derive_conditions for claim: %s", claim)
        if self.reporter:
            self.reporter.log("condition:request", claim=claim)
        response = self._with_retries(
            openai_generate_response,
            messages=messages,
            functions=functions,
            function_call={"name": "emit_conditions"},
            temperature=0,
        )
        _, data = openai_parse_function_call(response)
        conds = []
        for d in data.get("conditions", []) or []:
            conds.append(
                Condition(
                    description=d.get("desc", ""),
                    why=d.get("why", ""),
                    accept=d.get("accept", ""),
                    reject=d.get("reject", ""),
                    suggested_tasks=d.get("suggested_tasks", []),
                )
            )
        if self.reporter:
            self.reporter.log(
                "condition:derived",
                count=len(conds),
                conditions=[c.description for c in conds],
            )
        return conds

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
        tasks: List[dict] = []
        for t in condition.suggested_tasks:
            norm = self._normalize_task(t, code_path)
            tasks.append({"task": norm if norm else t, "original": t})
        messages = [
            {
                "role": "system",
                "content": (
                    f"{BANNER}\nSTAGE: plan\n\n"
                    "You are a security-auditing assistant. Your sole objective is to "
                    "ADJUDICATE a specific bug claim as TRUE POSITIVE or FALSE POSITIVE "
                    "with defensible, testable evidence. Maintain determinism (temperature=0). "
                    "Output STRICT JSON only—no markdown/prose. Use only repository-local "
                    "information and permitted executions. No network. No file writes."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Goal: Produce an ordered, minimal plan of NATURAL-LANGUAGE tasks that will decide this condition.\n\n"
                    f"Inputs:\n- condition: {{\"desc\":\"{condition.description}\",\"accept\":\"{condition.accept}\",\"reject\":\"{condition.reject}\"}}\n- suggested_tasks: {json.dumps(condition.suggested_tasks)}\n\n"
                    "Constraints:\n"
                    "- 1–4 tasks, strictly necessary and sufficient.\n"
                    "- Each task is a single clear action to perform in the repo (no pseudo-DSL).\n"
                    "- Examples: \"Trace control flow backward from <location> to confirm a permission check exists\", \"List call sites of <fn> and inspect argument validation\", \"Search for uses of <symbol> that bypass <guard>\".\n"
                    "- No external network/tools.\n\n"
                    "Output JSON:\n"
                    "{\"tasks\":[{\"task\":\"<natural language goal>\",\"why\":\"<what this will prove or rule out>\"}]}"
                ),
            },
        ]
        functions = [
            {
                "name": "emit_tasks",
                "description": "Return task objects.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "task": {"type": "string"},
                                    "why": {"type": "string"}
                                },
                                "required": ["task", "why"],
                            },
                        }
                    },
                    "required": ["tasks"],
                },
            }
        ]
        self.logger.info(
            "LLM generate_tasks for condition: %s", condition.description
        )
        if self.reporter:
            self.reporter.log("tasks:request", condition=condition.description)
        response = self._with_retries(
            openai_generate_response,
            messages=messages,
            functions=functions,
            function_call={"name": "emit_tasks"},
            temperature=0,
        )
        _, data = openai_parse_function_call(response)
        for t in data.get("tasks", []) or []:
            norm = self._normalize_task(t.get("task", ""), code_path)
            tasks.append({
                "task": norm if norm else t.get("task", ""),
                "why": t.get("why", ""),
                "original": t.get("task", ""),
            })
        if self.reporter:
            self.reporter.log(
                "tasks:plan", tasks=[t.get("task", "") for t in tasks]
            )
        return tasks

    def judge_condition(self, condition: Condition) -> str:
        """Deterministically judge ``condition`` based on available evidence."""
        if not condition.evidence:
            return "unknown"
        latest_raw = condition.evidence[-1]
        try:
            obs = json.loads(latest_raw)
        except Exception:
            condition.rationale = "latest observation not valid JSON"
            return "unknown"
        summary = obs.get("summary")
        citations = obs.get("citations")
        missing = []
        if summary is None:
            missing.append("summary")
        if citations is None:
            missing.append("citations")
        if missing:
            condition.rationale = f"missing {' & '.join(missing)}"
            return "unknown"
        messages = [
            {
                "role": "system",
                "content": (
                    f"{BANNER}\nSTAGE: judge\n\nDecide condition state from the latest observation only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Condition: \"{condition.description}\"\n"
                    f"Summary: {summary}\n"
                    f"Citations: {json.dumps(citations)}\n\n"
                    "Output JSON: {\"state\":\"satisfied|failed|unknown\",\"rationale\":\"<short>\",\"evidence_refs\":[-1]}"
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
                        "state": {"type": "string", "enum": ["satisfied", "failed", "unknown"]},
                        "rationale": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["state", "rationale", "evidence_refs"],
                },
            }
        ]
        self.logger.info(
            "LLM judge_condition for condition: %s", condition.description
        )
        response = self._with_retries(
            openai_generate_response,
            messages=messages,
            functions=functions,
            function_call={"name": "judge_condition"},
            temperature=0,
        )
        _, data = openai_parse_function_call(response)
        condition.rationale = data.get("rationale", "")
        condition.evidence_refs = data.get("evidence_refs", [])
        state = data.get("state", "unknown")
        if self.reporter:
            self.reporter.log("judge", state=state, rationale=condition.rationale)
        return state
    # -- Sub-condition narrowing -------------------------------------------
    def _narrow_subconditions(self, condition: Condition) -> List[Condition]:
        """Deterministically derive sub-conditions for an uncertain condition."""
        last_ev = condition.evidence[-1] if condition.evidence else ""
        blocking = condition.rationale or "condition unresolved"
        messages = [
            {
                "role": "system",
                "content": (
                    f"{BANNER}\nSTAGE: narrow\n\n"
                    "You are a security-auditing assistant. Your sole objective is to "
                    "ADJUDICATE a specific bug claim as TRUE POSITIVE or FALSE POSITIVE "
                    "with defensible, testable evidence. Maintain determinism (temperature=0). "
                    "Output STRICT JSON only—no markdown/prose. Use only repository-local "
                    "information and permitted executions. No network. No file writes."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Goal: Replace uncertainty with decisive subconditions that will lead to a verdict.\n\n"
                    f"Inputs:\n- parent_condition: \"{condition.description}\"\n- blocking_uncertainty: \"{blocking}\"\n- last_evidence: {last_ev}\n\n"
                    "Constraints:\n- 1–3 subconditions, each mapping to at least one executable task.\n- Make them mutually informative (no overlap).\n\nOutput JSON:\n{\"conditions\":[{\n  \"desc\":\"<testable discriminator>\",\n  \"why\":\"<how it resolves the uncertainty>\",\n  \"accept\":\"<...>\",\n  \"reject\":\"<...>\",\n  \"suggested_tasks\":[\"<exec-able task>\", \"...\"]\n}]}"
                ),
            },
        ]
        functions = [
            {
                "name": "emit_conditions",
                "description": "Return subcondition objects.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conditions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "desc": {"type": "string"},
                                    "why": {"type": "string"},
                                    "accept": {"type": "string"},
                                    "reject": {"type": "string"},
                                    "suggested_tasks": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": [
                                    "desc",
                                    "why",
                                    "accept",
                                    "reject",
                                    "suggested_tasks",
                                ],
                            },
                        }
                    },
                    "required": ["conditions"],
                },
            }
        ]
        self.logger.info(
            "LLM narrow_subconditions for condition: %s", condition.description
        )
        if self.reporter:
            self.reporter.log("subconditions:request", condition=condition.description)
        response = self._with_retries(
            openai_generate_response,
            messages=messages,
            functions=functions,
            function_call={"name": "emit_conditions"},
            temperature=0,
        )
        _, data = openai_parse_function_call(response)
        subs: List[Condition] = []
        for d in data.get("conditions", []) or []:
            subs.append(
                Condition(
                    description=d.get("desc", ""),
                    why=d.get("why", ""),
                    accept=d.get("accept", ""),
                    reject=d.get("reject", ""),
                    suggested_tasks=d.get("suggested_tasks", []),
                )
            )
        condition.subconditions.extend(subs)
        if self.reporter:
            self.reporter.log(
                "subconditions:derived",
                count=len(subs),
                conditions=[c.description for c in subs],
            )
        return subs

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
                if goal.startswith(("read:", "stat:", "py:functions:", "py:classes:")):
                    out = self.agent(goal)
                else:
                    out = self.agent(f"codex:exec:{code_path}::{goal}")
                    condition.evidence.append(json.dumps(out)[:10000])
                task_results.append(
                    {
                        "task": goal,
                        "original": original,
                        "output": out,
                        "timestamp": stamp,
                        "input_sha1": goal_hash,
                    }
                )
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
        if self.reporter:
            types = [("error" if "error" in r else "ok") for r in task_results]
            self.reporter.log("tasks:result", types=types)
        return task_results

    def resolve_condition(
        self, condition: Condition, finding_path: Path, *, max_steps: int = 3
    ) -> None:
        """Resolve a condition by iterating generate→execute→judge cycles."""
        with open(finding_path) as fh:
            finding = json.load(fh)
        code_path = Path(finding.get("provenance", {}).get("path", ""))
        for step in range(max_steps):
            self.logger.info(
                "Resolve step %d for condition '%s'", step + 1, condition.description
            )
            if self.reporter:
                self.reporter.log("resolve:step", n=step + 1)
            tasks = self.generate_tasks(condition, code_path)
            self.logger.info(
                "Generated %d tasks for '%s'", len(tasks), condition.description
            )
            if not tasks:
                break
            self._execute_tasks(finding_path, condition, tasks)
            state = self.judge_condition(condition)
            condition.state = state
            self.logger.info(
                "Condition '%s' state after step %d: %s",
                condition.description,
                step + 1,
                state,
            )
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
            claim = finding.get("claim", "")
            path = finding.get("provenance", {}).get("path", "")
            if self.reporter:
                self.reporter.log("finding:open", claim=claim, path=path)
            self.logger.info("Deriving conditions for %s", finding_file.name)
            conditions = self.derive_conditions(finding)
            self.logger.info(
                "Derived %d conditions for %s", len(conditions), finding_file.name
            )
            for condition in conditions:
                self.resolve_condition(condition, finding_file, max_steps=max_steps)
            # reload tasks_log in case tasks were executed
            with open(finding_file) as fh:
                updated = json.load(fh)
            finding["tasks_log"] = updated.get("tasks_log", [])
            finding["conditions"] = [c.to_dict() for c in conditions]
            states = {c.state for c in conditions}
            if states and states == {"satisfied"}:
                finding["verdict"] = {
                    "state": "TRUE_POSITIVE",
                    "reason": "all conditions satisfied",
                }
            elif states and "satisfied" not in states and "failed" in states:
                finding["verdict"] = {
                    "state": "FALSE_POSITIVE",
                    "reason": "at least one condition failed",
                }
            else:
                finding["verdict"] = {
                    "state": "UNKNOWN",
                    "reason": "conditions unresolved",
                }
            atomic_write(finding_file, json.dumps(finding, indent=2).encode())
            if self.reporter:
                self.reporter.log("finding:complete")
