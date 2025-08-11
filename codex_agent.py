from __future__ import annotations

import json
from pathlib import Path

from codex_dispatch import CodexClient, CodexError, CodexTimeout

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
            path = self._repo_rel(path)
            return (kind, path)
        if task.startswith("codex:exec:"):
            rest = task.split("codex:exec:", 1)[1]
            if "::" not in rest:
                raise ValueError("unsupported task")
            path, payload = rest.split("::", 1)
            path = self._repo_rel(path.strip())
            return ("exec", path, payload.strip())
        raise ValueError("unsupported task")

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
                "Output STRICT JSON:\n"
                "{\"type\":\"exec_observation\",\"summary\":\"<short or 'error: ...'>\",\"citations\":[{\"path\":\"<repo-rel>\",\"start\":<int>,\"end\":<int>,\"sha1\":\"<hex>\"}],\"notes\":\"<optional>\"}\n"
                "If execution fails, still follow the schema with summary starting 'error:' and empty citations.\n"
            )
        raise ValueError("unsupported kind")

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
            for c in data.get("citations", []):
                if not (
                    isinstance(c, dict)
                    and isinstance(c.get("path"), str)
                    and isinstance(c.get("start"), int)
                    and isinstance(c.get("end"), int)
                    and isinstance(c.get("sha1"), str)
                ):
                    raise ValueError("invalid citation object")
            return data
        if kind == "discover":
            return data
        raise ValueError("unsupported kind")

    # ---------------- Public API -----------------
    def run(self, task: str) -> dict:
        parsed = self._parse_task(task)
        kind = parsed[0]
        if kind == "discover":
            path = parsed[1]
            prompt = self._build_prompt("discover", path)
        else:  # exec
            _, path, payload = parsed
            prompt = self._build_prompt("exec", path, payload)
        try:
            res = self.codex.exec(
                prompt=prompt,
                workdir=self.workdir,
                extra_flags=self.default_flags,
                timeout=self.timeout,
            )
            return self._postprocess(kind, path, res)
        except CodexTimeout:
            if kind == "exec":
                return {
                    "type": "exec_observation",
                    "summary": "error: timeout",
                    "citations": [],
                    "notes": "",
                }
            return {"error": "timeout", "goal": task}
        except CodexError as exc:
            if kind == "exec":
                return {
                    "type": "exec_observation",
                    "summary": f"error: codex-exit {exc.result.returncode}",
                    "citations": [],
                    "notes": exc.result.stderr[:512],
                }
            return {
                "error": "codex-exit",
                "goal": task,
                "code": exc.result.returncode,
                "stderr_head": exc.result.stderr[:512],
            }
        except Exception as exc:
            if kind == "exec":
                return {
                    "type": "exec_observation",
                    "summary": f"error: {exc}",
                    "citations": [],
                    "notes": "",
                }
            raise

