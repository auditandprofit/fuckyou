from __future__ import annotations

import json
from pathlib import Path

from codex_dispatch import CodexClient, CodexError, CodexTimeout

MAX_BYTES = 100_000


class CodexAgent:
    def __init__(self, codex: CodexClient, *, workdir: str, default_flags: list[str] | None = None, timeout: float = 60):
        self.codex = codex
        self.workdir = workdir
        self.default_flags = default_flags or []
        self.timeout = timeout

    # ---------------- Parsing & Validation -----------------
    def _parse_task(self, task: str):
        if task.startswith("codex:discover:"):
            return ("discover", task.split("codex:discover:", 1)[1].strip())
        if task.startswith("codex:exec:"):
            rest = task.split("codex:exec:", 1)[1]
            if "::" not in rest:
                raise ValueError("unsupported task")
            path, payload = rest.split("::", 1)
            return ("exec", path.strip(), payload.strip())
        if ":" not in task:
            raise ValueError("unsupported task")
        verb, rest = task.split(":", 1)
        verb = verb.lower()
        if verb == "py":
            if ":" not in rest:
                raise ValueError("unsupported task")
            subverb, path = rest.split(":", 1)
            verb = f"py:{subverb.lower()}"
        else:
            path = rest
        verb = verb.strip()
        path = path.strip()
        if verb not in {"read", "stat", "py:functions", "py:classes"}:
            raise ValueError("unsupported task")
        path = self._repo_rel(path)
        return (verb, path)

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
                "SYSTEM:\nStrict JSON only.\n\nUSER:\n"
                f"Action: DISCOVER\nPath: {path}\n"
                'Return: {"type":"discover","claim":"...","files":["..."],"evidence":{...}}\n'
            )
        if kind == "exec":
            return (
                "SYSTEM:\nStrict JSON only.\n\nUSER:\n"
                f"Action: EXEC\nPath: {path}\nTask: {payload}\n"
                'Return: {"type":"exec","task":"...","result":{...}}\n'
            )
        action = {
            "read": "READ",
            "stat": "STAT",
            "py:functions": "PY_FUNCTIONS",
            "py:classes": "PY_CLASSES",
        }[kind]
        return (
            "SYSTEM:\n"
            "You are a deterministic repository tool. Only output valid JSON.\n"
            "No extra text. No markdown.\n\n"
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
    def _postprocess(self, kind: str, path: str, res) -> dict:
        try:
            data = json.loads(res.stdout)
        except Exception:
            return {"error": "invalid-json", "stdout_head": res.stdout[:512]}
        if data.get("type") == "read" and isinstance(data.get("bytes"), str):
            b = data["bytes"]
            if len(b) > MAX_BYTES:
                data["bytes"] = b[:MAX_BYTES]
        if data.get("type") == "exec":
            inner = data.get("result", {})
            if isinstance(inner, dict) and inner.get("type") == "read" and isinstance(inner.get("bytes"), str):
                b = inner["bytes"]
                if len(b) > MAX_BYTES:
                    inner["bytes"] = b[:MAX_BYTES]
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

