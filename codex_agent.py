from __future__ import annotations

import json
from pathlib import Path

from codex_dispatch import CodexClient, CodexError, CodexTimeout

BANNER = (
    "Deterministic security auditor. No network. No writes. JSON only. "
    "You are one stage in a fixed pipeline (discover→derive→plan→exec→judge→narrow). "
    "Do only this stage. Your JSON is consumed verbatim by the next stage."
)


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
                "Purpose:\n- Ground the claim in specific repo text.\n- Cite ≤3 concrete regions.\n- Formulate a concrete, falsifiable security bug claim.\n- Return 1–3 evidence.highlights (required).\n\n"
                "Claim requirements:\n- One sentence, falsifiable.\n- Include a brief attacker/trust-boundary clause (≤ 12 words).\n- No speculation.\n\n"
                "Output JSON:\n{\"schema_version\":1,\n \"stage\":\"discover\",\n \"claim\":\"<security bug claim>\",\n \"files\": [\"<repo-rel path>\", ...],\n \"evidence\":{\"highlights\": [\n    {\"path\":\"<repo-rel>\",\"region\":{\"start_line\":<int>,\"end_line\":<int>},\"why\":\"<security-relevant reason>\"}\n ]}}\n"
            )
        if kind == "exec":
            return (
                "SYSTEM:\n"
                f"{BANNER}\nSTAGE: exec\n\n"
                "USER:\n"
                f"Primary file: {path}\n"
                f"Goal: {payload}\n\n"
                "Policies:\n"
                "- No network. No file modifications. Read-only analysis only.\n"
                "- Do not spawn external processes.\n"
                "- You may read any file under the repository root and search across the tree.\n"
                "- If summary is not \"error:...\", include ≥1 entry in \"citations\" with exact \"path\", \"start_line\", and \"end_line\" that support your claim.\n"
                "- If an action would violate policy or cannot be performed, return \n"
                "  {\"schema_version\":1,\"stage\":\"exec\",\"summary\":\"error: <reason>\",\"citations\":[],\"notes\":\"\"}\n\n"
                "Output STRICT JSON:\n"
                "{\"schema_version\":1,\"stage\":\"exec\",\"summary\":\"<short or 'error: ...'>\"," 
                " \"citations\":[{\"path\":\"<repo-rel>\",\"start_line\":<int>,\"end_line\":<int>,\"sha1\":\"<hex, optional>\"}],"
                " \"notes\":\"<optional>\"}\n"
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
                and data.get("schema_version") == 1
                and data.get("stage") == "exec"
                and isinstance(data.get("summary"), str)
                and isinstance(data.get("citations"), list)
            ):
                raise ValueError("invalid exec observation")
            for c in data.get("citations", []):
                if not (
                    isinstance(c, dict)
                    and isinstance(c.get("path"), str)
                    and isinstance(c.get("start_line"), int)
                    and isinstance(c.get("end_line"), int)
                    and (c.get("sha1") is None or isinstance(c.get("sha1"), str))
                ):
                    raise ValueError("invalid citation object")
            if not data["summary"].startswith("error:") and not data["citations"]:
                data["summary"] = "error: missing-citation"
            return data
        if kind == "discover":
            if not (
                isinstance(data, dict)
                and data.get("schema_version") == 1
                and data.get("stage") == "discover"
            ):
                raise ValueError("invalid discover result")
            highlights = (((data or {}).get("evidence") or {}).get("highlights") or [])
            if len(highlights) == 0:
                raise ValueError("discover: missing evidence.highlights (require 1–3)")
            if len(highlights) > 3:
                data["evidence"]["highlights"] = highlights[:3]
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
                    "schema_version": 1,
                    "stage": "exec",
                    "summary": "error: timeout",
                    "citations": [],
                    "notes": "",
                }
            return {"error": "timeout", "goal": task}
        except CodexError as exc:
            if kind == "exec":
                return {
                    "schema_version": 1,
                    "stage": "exec",
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
                    "schema_version": 1,
                    "stage": "exec",
                    "summary": f"error: {exc}",
                    "citations": [],
                    "notes": "",
                }
            raise

