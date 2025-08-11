"""Prototype orchestrator for processing findings.

This module coordinates between a manifest, an agent, and stored findings.
LLM calls are routed through :mod:`util.openai` so that judgment, condition
generation, and task generation are powered by the OpenAI API.  All such calls
must succeed; if the API is unavailable the run aborts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Set
from collections import defaultdict
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from util.io import atomic_write
from util.openai import (
    openai_generate_response,
    openai_parse_function_call,
)
from util.time import utc_now_iso
from util.imports import variants_for

BANNER = (
    "Deterministic security auditor. No network. No writes. JSON only. "
    "You are one stage in a fixed pipeline (discover→derive→plan→exec→judge→narrow). "
    "Do only this stage. Your JSON is consumed verbatim by the next stage."
)


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
    last_verb: str = ""
    used_verbs: Set[str] = field(default_factory=set, repr=False)

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


# Helper ---------------------------------------------------------------------

def _latest_success(ev_list):
    for raw in reversed(ev_list or []):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        s = d.get("summary", "")
        if isinstance(s, str) and not s.startswith("error:"):
            return d
    return None


def _verb(s: str) -> str:
    return (s.split(None, 1)[0] or "").lower()


SINK_KEYWORDS = ["subprocess", "tarfile", "yaml.load"]


def _score_condition(condition: Condition) -> int:
    """Heuristic score for whether to deepen a condition."""
    score = 0
    obs = _latest_success(condition.evidence)
    if not obs:
        return score
    summary = obs.get("summary", "")
    if not summary.startswith("error:") and obs.get("citations"):
        score += 2
        for c in obs.get("citations", []):
            try:
                fp = Path(c.get("path", ""))
                lines = fp.read_text().splitlines()
                snippet = "\n".join(
                    lines[c.get("start_line", 1) - 1 : c.get("end_line", 1)]
                )
                if any(kw in snippet for kw in SINK_KEYWORDS):
                    score += 2
                    break
            except Exception:
                continue
    text = (summary + " " + obs.get("notes", "")).lower()
    if any(w in text for w in ["user-controlled", "taint", "entrypoint"]):
        score += 1
    return score


# ----- Orchestrator ----------------------------------------------------------

class Orchestrator:
    """Coordinate finding generation and condition evaluation."""

    VERSION = "0.2"

    def __init__(self, agent: Callable[[str], str], *, reporter=None):
        self.agent = agent
        self.logger = logging.getLogger(__name__)
        self.max_retries = int(os.getenv("ANCHOR_OPENAI_RETRIES", "3"))
        self.reporter = reporter
        self.auto_lens = os.getenv("ANCHOR_AUTO_LENS", "1") not in {"0", "false", "False"}
        self.plan_diversity = os.getenv("ANCHOR_PLAN_DIVERSITY", "1") not in {"0", "false", "False"}
        self.bfs_budget = int(os.getenv("ANCHOR_BFS_BUDGET", "10"))
        self.auto_lensed_files: Set[str] = set()
        self.discover_runs_by_lens = defaultdict(int)
        self.unique_claims_per_lens = defaultdict(int)
        self.breadth_examined = 0
        self.depth_escalated = 0
        self.escalation_hits = 0
        self._verb_counts: List[int] = []

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
        self, manifest_files: List[Path]
    ) -> List[Dict]:
        findings: List[Dict] = []
        seen: set[tuple[str, str]] = set()
        for code_path in manifest_files:
            variants = [""]
            if self.auto_lens:
                extra = variants_for(code_path)
                if extra:
                    self.auto_lensed_files.add(code_path.as_posix())
                variants.extend(extra)
            else:
                variants.extend(["deser", "authz", "path", "exec"])
            for v in variants[:3]:
                lens = v or "default"
                self.logger.info(
                    "Discovering %s::%s", code_path.as_posix(), lens
                )
                self.discover_runs_by_lens[lens] += 1
                data = self.agent(
                    f"codex:discover:{code_path.as_posix()}::{v}"
                )
                self.logger.info(
                    "Discovered %s::%s", code_path.as_posix(), lens
                )
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
                key = (
                    (claim or "").strip().lower(),
                    (files or [code_path.as_posix()])[0],
                )
                if key in seen:
                    continue
                seen.add(key)
                self.unique_claims_per_lens[lens] += 1
                findings.append({"claim": claim, "files": files, "evidence": evidence})
        return findings

    # -- Orchestration per finding -------------------------------------------
    def derive_conditions(self, finding: Dict) -> List[Condition]:
        """Use the LLM to deterministically derive conditions."""
        claim = finding.get("claim", "")
        related_files = finding.get("files", [])
        seed = (finding.get("evidence", {}) or {}).get("seed", {})
        highlights = (seed.get("highlights") or [])[:3]
        messages = [
            {
                "role": "system",
                "content": (
                    f"{BANNER}\nSTAGE: derive\n\n"
                    "You are a security-auditing assistant. Your sole objective is to "
                    "ADJUDICATE a specific bug claim as TRUE POSITIVE or FALSE POSITIVE "
                    "with defensible, testable evidence. Maintain determinism "
                    "(temperature=0). Use only repository-local information and permitted executions.\n\nDefinitions:\n- TRUE POSITIVE: Evidence demonstrates the claim holds under realistic "
                    "conditions within the codebase.\n- FALSE POSITIVE: Evidence demonstrates the claim does not hold, is unreachable, "
                    "or is otherwise invalid.\n- UNKNOWN: Evidence gathered so far is insufficient; propose targeted sub-checks.\n\n"
                    "Evidence must be concrete, minimally sufficient, and reproducible."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Goal: Break down the bug claim into minimal, testable security CONDITIONS that, if individually decided, collectively allow a final verdict (TRUE POSITIVE / FALSE POSITIVE).\n\n"
                    f"Inputs:\n- claim: \"{claim}\"\n- related_files: {json.dumps(related_files)}\n- seed_evidence_highlights: {json.dumps(highlights)}\n\n"
                    "Mindset:\n- Act as a FALSE-POSITIVE filter: add decisive checks that would invalidate the claim (e.g., input not user-controlled; guard exists on all paths; sanitization before sink).\n- Prefer objective, repo-local observations.\n\n"
                    "Constraints:\n- 1–5 conditions.\n- Each condition must be objectively checkable via codex-executable tasks.\n- Each condition must state an acceptance criterion tied to the final verdict.\n"
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
                        "schema_version": {"type": "integer"},
                        "stage": {"type": "string"},
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
                    "required": ["schema_version", "stage", "conditions"],
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

    def generate_tasks(self, condition: Condition, code_path: Path) -> List[dict]:
        """Generate tasks to gather evidence for ``condition``."""
        tasks: List[dict] = []
        last_sum = ""
        if condition.evidence:
            try:
                last_sum = json.loads(condition.evidence[-1]).get("summary", "")
            except Exception:
                pass
        messages = [
            {
                "role": "system",
                "content": (
                    f"{BANNER}\nSTAGE: plan\n\n"
                    "You are a security-auditing assistant. Your sole objective is to "
                    "ADJUDICATE a specific bug claim as TRUE POSITIVE or FALSE POSITIVE "
                    "with defensible, testable evidence. Maintain determinism (temperature=0). "
                    "Use only repository-local information and permitted executions."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Goal: Produce an ordered, minimal plan of NATURAL-LANGUAGE tasks that will decide this condition.\n\n"
                    f"Inputs:\n- condition: {{\"desc\":\"{condition.description}\",\"accept\":\"{condition.accept}\",\"reject\":\"{condition.reject}\"}}\n- suggested_tasks: {json.dumps(condition.suggested_tasks)}\n- last_observation_summary: {json.dumps(last_sum)}\n\n"
                    "Codex can: read files, grep/search, parse basic AST, trace call sites/paths, do simple static dataflow, summarize.\n"
                    "Operation classes = {read-file, search, ast-parse, callgraph, dataflow}.\n\n"
                    "Constraints:\n"
                    "- Emit 1–3 tasks, each is *exec*.\n"
                    "- If the last observation summary starts with 'error:', switch to a different operation class.\n"
                    "- Final task must directly test the condition's ACCEPT vs REJECT.\n"
                    "- Each task is a single clear action to perform in the repository (no pseudo-DSL).\n"
                    "- Begin each task with a verb from {search, read-file, ast-parse, callgraph, dataflow} (hint only; executor accepts natural language).\n"
                    "- The final task MUST state the exact evidence to return (e.g., function name + line range proving ACCEPT or REJECT).\n"
                    "- Use plain English; do not mention internal mode names.\n"
                    "- You may traverse the entire repo; include explicit paths when known.\n"
                    "- No external network/tools.\n"
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
                        "schema_version": {"type": "integer"},
                        "stage": {"type": "string"},
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "task": {"type": "string"},
                                    "why": {"type": "string"},
                                    "mode": {
                                        "type": "string",
                                        "enum": ["exec"],
                                    },
                                },
                                "required": ["task", "why", "mode"],
                            },
                        }
                    },
                    "required": ["schema_version", "stage", "tasks"],
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
        seen = set()
        for t in data.get("tasks", []) or []:
            mode = t.get("mode")
            task_text = t.get("task", "")
            if mode != "exec" or task_text is None:
                continue
            key = (mode, task_text)
            if key in seen:
                continue
            seen.add(key)
            path = code_path.as_posix()
            goal = f"codex:exec:{path}::{task_text}"
            tasks.append(
                {
                    "task": goal,
                    "why": t.get("why", ""),
                    "mode": mode,
                    "original": task_text,
                }
            )

        prev_verb = condition.last_verb
        if self.plan_diversity and prev_verb:
            alt = [t for t in tasks if _verb(t.get("original", "")) != prev_verb]
            if alt:
                tasks = alt

        if self.plan_diversity and len(condition.used_verbs) < 3:
            tasks = [
                t
                for t in tasks
                if _verb(t.get("original", "")) not in condition.used_verbs
            ] or tasks

        filtered: List[dict] = []
        verbs = set()
        for t in tasks:
            v = _verb(t.get("original", ""))
            if v in verbs:
                continue
            verbs.add(v)
            filtered.append(t)
        tasks = filtered[:3]

        if not any(_verb(t.get("original", "")) in {"callgraph", "dataflow"} for t in tasks):
            path = code_path.as_posix()
            tasks.append(
                {
                    "task": "codex:exec:{}::callgraph from any discovered sink symbol to any public entrypoint; return shortest path with file:line ranges.".format(
                        path
                    ),
                    "why": "force cross-file reachability",
                    "mode": "exec",
                    "original": "callgraph shortest-path",
                }
            )
        if self.reporter:
            self.reporter.log(
                "tasks:plan",
                tasks=[t.get("task", "") for t in tasks],
                verbs=[_verb(t.get("original", "")) for t in tasks],
            )
        return tasks

    def judge_condition(self, condition: Condition) -> str:
        """Deterministically judge ``condition`` based on available evidence."""
        if not condition.evidence:
            return "unknown"
        latest_ok = _latest_success(condition.evidence)
        latest_idx = None
        if latest_ok is not None:
            for i, raw in reversed(list(enumerate(condition.evidence))):
                try:
                    if json.loads(raw) == latest_ok:
                        latest_idx = i
                        break
                except Exception:
                    continue
        try:
            obs = latest_ok or json.loads(condition.evidence[-1])
            idx = latest_idx if latest_ok is not None and latest_idx is not None else len(condition.evidence) - 1
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
        prev_summaries = []
        for raw in condition.evidence[max(0, idx - 2): idx]:
            try:
                s = json.loads(raw).get("summary")
                if isinstance(s, str):
                    prev_summaries.append(s)
            except Exception:
                continue
        messages = [
            {
                "role": "system",
                "content": (
                    f"{BANNER}\nSTAGE: judge\n\nPrefer the latest successful observation; if it conflicts with any earlier success, return failed and explain. If unknown, state the single decisive evidence needed. evidence_refs index the provided citations (0-based)."
                    "\n- If code claims lack usable citations, return \"unknown\" and specify the single missing citation (path + line range) needed."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Condition: \"{condition.description}\"\n"
                    f"ACCEPT IF: {condition.accept}\n"
                    f"REJECT IF: {condition.reject}\n"
                    f"Summary: {summary}\n"
                    f"Citations: {json.dumps(citations)}\n"
                    f"PrevSummaries: {json.dumps(prev_summaries)}\n"
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
                        "schema_version": {"type": "integer"},
                        "stage": {"type": "string"},
                        "state": {"type": "string", "enum": ["satisfied", "failed", "unknown"]},
                        "rationale": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["schema_version", "stage", "state", "rationale", "evidence_refs"],
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
                    "Use only repository-local information and permitted executions."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Goal: Replace uncertainty with decisive subconditions that will lead to a verdict.\n\n"
                    f"Inputs:\n- parent_condition: \"{condition.description}\"\n"
                    f"- parent_accept: \"{condition.accept}\"\n"
                    f"- parent_reject: \"{condition.reject}\"\n"
                    f"- blocking_uncertainty: \"{blocking}\"\n"
                    f"- last_evidence: {last_ev}\n\n"
                    "Constraints:\n- 1–3 subconditions, each mapping to at least one executable task.\n"
                    "- Each subcondition must target an unmet part of the ACCEPT/REJECT criteria.\n"
                    "- Make them mutually informative (no overlap)."
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
                        "schema_version": {"type": "integer"},
                        "stage": {"type": "string"},
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
                    "required": ["schema_version", "stage", "conditions"],
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
        task_results: List[dict] = []

        def _run(t: dict):
            goal = t["task"]
            mode = t.get("mode", "exec")
            original = t.get("original", goal)
            stamp = utc_now_iso()
            goal_hash = hashlib.sha1(goal.encode()).hexdigest()
            try:
                out = self.agent(goal)
                return (t, out, stamp, goal_hash, None)
            except Exception as exc:
                return (t, None, stamp, goal_hash, str(exc))

        with ThreadPoolExecutor(
            max_workers=int(os.getenv("ANCHOR_WORKERS", "4"))
        ) as ex:
            results = [ex.submit(_run, t) for t in tasks]
            pairs = [f.result() for f in results]

        for t, out, stamp, goal_hash, err in sorted(
            pairs, key=lambda x: x[3]
        ):
            if err is None:
                condition.evidence.append(json.dumps(out))
                task_results.append(
                    {
                        "task": t["task"],
                        "mode": t.get("mode", "exec"),
                        "original": t.get("original", t["task"]),
                        "output": out,
                        "timestamp": stamp,
                        "input_sha1": goal_hash,
                    }
                )
            else:
                task_results.append(
                    {
                        "task": t["task"],
                        "mode": t.get("mode", "exec"),
                        "original": t.get("original", t["task"]),
                        "error": err,
                        "timestamp": stamp,
                        "input_sha1": goal_hash,
                    }
                )

        if pairs:
            verbs = [_verb(p[0].get("original", "")) for p in pairs]
            condition.used_verbs.update(verbs)
            condition.last_verb = verbs[-1]

        finding.setdefault("tasks_log", []).append(
            {"condition": condition.description, "executed": task_results}
        )
        atomic_write(finding_file, json.dumps(finding, indent=2).encode())
        if self.reporter:
            types = [("error" if "error" in r else "ok") for r in task_results]
            self.reporter.log("tasks:result", types=types)
        return task_results

    def resolve_condition(
        self,
        condition: Condition,
        finding_path: Path,
        *,
        max_steps: int = 3,
        start_step: int = 0,
    ) -> None:
        """Resolve a condition by iterating generate→execute→judge cycles."""
        with open(finding_path) as fh:
            finding = json.load(fh)
        code_path = Path(finding.get("provenance", {}).get("path", ""))
        for step in range(start_step, max_steps):
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
        all_data = []
        scored: List[tuple[int, Condition, Path, Dict]] = []
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
                self.logger.info(
                    "breadth_pass=1 for condition '%s'", condition.description
                )
                self.resolve_condition(condition, finding_file, max_steps=1)
                scored.append((
                    _score_condition(condition),
                    condition,
                    finding_file,
                    finding,
                ))
            all_data.append((finding_file, finding, conditions))

        self.breadth_examined = len(scored)
        for score, condition, finding_file, finding in sorted(
            scored, key=lambda x: x[0], reverse=True
        )[: self.bfs_budget]:
            if condition.state != "unknown":
                continue
            self.logger.info(
                "depth_pass=2 for condition '%s'", condition.description
            )
            self.resolve_condition(condition, finding_file, max_steps=max_steps, start_step=1)
            self.depth_escalated += 1
            if condition.state in {"satisfied", "failed"}:
                self.escalation_hits += 1

        for finding_file, finding, conditions in all_data:
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
            for c in conditions:
                if c.used_verbs:
                    self._verb_counts.append(len(c.used_verbs))
