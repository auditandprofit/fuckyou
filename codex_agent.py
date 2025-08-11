from __future__ import annotations

import json
from pathlib import Path

from codex_dispatch import CodexClient, CodexError, CodexTimeout

MAX_BYTES = 100_000
BANNER = "Deterministic security auditor. No network. No writes. JSON only."


class CodexAgent:
    def __init__(self, codex: CodexClient, *, workdir: str, default_flags: list[str] | None = None, timeout: float = 60):
        self.codex = codex
        self.workdir = workdir
        self.default_flags = default_flags or []
        self.timeout = timeout

    # ---------------- Parsing & Validation -----------------
    def _parse_task(self, task: str):
        if task.startswith("codex:discover:"):
            kind = "discover"
            path = task.split("codex:discover:", 1)[1].strip()
            payload = None
        elif task.startswith("codex:exec:"):
            rest = task.split("codex:exec:", 1)[1]
            if "::" not in rest:
                raise ValueError("unsupported task")
            path, payload = rest.split("::", 1)
            kind = "exec"
            path = path.strip()
            payload = payload.strip()
        else:
            if ":" not in task:
                raise ValueError("unsupported task")
            verb, rest = task.split(":", 1)
            verb = verb.lower()
            if verb == "py":
                if ":" not in rest:
                    raise ValueError("unsupported task")
                subverb, path = rest.split(":", 1)
                kind = f"py:{subverb.lower()}"
            else:
                path = rest
                kind = verb
            kind = kind.strip()
            path = path.strip()
            if kind not in {"read", "stat", "py:functions", "py:classes"}:
                raise ValueError("unsupported task")
            payload = None

        if kind in {"discover", "exec", "read", "stat", "py:functions", "py:classes"}:
            path = self._repo_rel(path)

        if kind == "exec":
            return (kind, path, payload)
        return (kind, path)

    def _repo_rel(self, p: str) -> str:
        root = Path(self.workdir).resolve()
        abspath = (root / p).resolve()
        if root not in abspath.parents and abspath != root:
            raise ValueError("path outside repo")
        return str(abspath.relative_to(root))

    # ---------------- Prompt -----------------
    def _build_prompt(self, kind: str, path: str, payload: str | None = None) -> str:
        if kind == "discover":
            return (
                "SYSTEM:\n"
                f"{BANNER}\nSTAGE: discover\n\n"
                "USER:\n"
                f"Action: DISCOVER\nPath: {path}\n\n"
                "Purpose:\n- Formulate a concrete security bug claim (plausible vulnerability) grounded in this file and directly related code (imports/callers/siblings).\n- Return minimal related files + seed evidence.\n\n"
                "Output JSON:\n{\"type\":\"discover\",\n \"claim\":\"<security bug claim>\",\n \"files\": [\"<repo-rel path>\", ...],\n \"evidence\":{\"highlights\": [\n    {\"path\":\"<repo-rel>\",\"region\":{\"start_line\":<int>,\"end_line\":<int>},\"why\":\"<security-relevant reason>\"}\n ]}}\n"
            )
        if kind == "exec":
            return (
                "SYSTEM:\n"
                f"{BANNER}\nSTAGE: exec\n\n"
                "USER:\n"
                f"Repository path: {path}\nGoal: {payload}\n\n"
                "Output STRICT JSON: {\"type\":\"exec_observation\",\"summary\":\"<short>\",\"citations\":[],\"notes\":\"<optional>\"}\n"
            )
        action = {
            "read": "READ",
            "stat": "STAT",
            "py:functions": "PY_FUNCTIONS",
            "py:classes": "PY_CLASSES",
        }[kind]
        return (
            "SYSTEM:\n"
            f"{BANNER}\nSTAGE: {kind}\n\n"
            "USER:\n"
            f"Action: {action}\n"
            f"Path: {path}\n"
            "Constraints:\n"
            "- Do not access network.\n"
            "- Do not modify files.\n"
            "- If anything fails, return {\"error\": \"...\"} with a concise reason.\n\n"
            "Output JSON schema (strict):\n"
            "For READ:\n"
            "  {\"type\":\"read\",\"path\":\"<abs or repo-rel>\",\"bytes\":\"<utf-8, may contain replacements>\",\"sha1\":\"<hex>\"}\n\n"
            "For STAT:\n"
            "  {\"type\":\"stat\",\"path\":\"<...>\",\"size\":<int>,\"sha1\":\"<hex>\"}\n\n"
            "For PY_FUNCTIONS:\n"
            "  {\"type\":\"py:functions\",\"path\":\"<...>\",\"functions\":[{\"name\":\"<str>\",\"args\":<int>},...]}\n\n"
            "For PY_CLASSES:\n"
            "  {\"type\":\"py:classes\",\"path\":\"<...>\",\"classes\":[{\"name\":\"<str>\",\"methods\":[\"<str>\", ...]},...]}\n\n"
            "Task:\n"
            f"{kind}:{path}\n"
        )

    # ---------------- Post-processing -----------------
    def _postprocess(self, kind: str, path: str, res):
        try:
            data = json.loads(res.stdout)
        except Exception as exc:
            raise ValueError("non-json output") from exc
        if kind == "exec":
            if not (
                isinstance(data, dict)
                and data.get("type") == "exec_observation"
                and isinstance(data.get("summary"), str)
                and isinstance(data.get("citations"), list)
            ):
                raise ValueError("invalid exec observation")
            return data
        if not (isinstance(data, dict) and data.get("type")):
            raise ValueError("invalid response")
        return data

    # ---------------- Public API -----------------
    def run(self, task: str) -> dict:
        parsed = self._parse_task(task)
        kind = parsed[0]
        if kind == "discover":
            path = parsed[1]
            prompt = self._build_prompt("discover", path)
        elif kind == "exec":
            _, path, payload = parsed
            prompt = self._build_prompt("exec", path, payload)
        else:
            path = parsed[1]
            prompt = self._build_prompt(kind, path)
        try:
            res = self.codex.exec(
                prompt=prompt,
                workdir=self.workdir,
                extra_flags=self.default_flags,
                timeout=self.timeout,
            )
        except CodexTimeout:
            return {"error": "timeout", "goal": task}
        except CodexError as exc:
            return {
                "error": "codex-exit",
                "goal": task,
                "code": exc.result.returncode,
                "stderr_head": exc.result.stderr[:512],
            }
        return self._postprocess(kind, path, res)

