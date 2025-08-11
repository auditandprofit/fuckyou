from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Sequence, Tuple


def _invoke_codex(cmd: Sequence[str], prompt: str, timeout: float) -> Tuple[str, str]:
    """Execute ``codex`` with ``prompt`` and return its stdout/stderr.

    The command should include ``--output-last-message <path>`` so that the
    final assistant message is persisted to disk by ``codex`` itself.  This
    helper only handles process invocation and timeout/return-code management;
    callers are expected to read ``output-last-message`` afterwards.
    """

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.stdin = None

        out, err = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=out, stderr=err)
    except subprocess.TimeoutExpired as te:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGINT)
        except Exception:
            pass
        proc.kill()
        raise
    return out, err


def invoke_codex(
    *,
    codex_bin: str,
    prompt: str,
    work_dir: str,
    output_path: str,
    timeout: float = 60.0,
    extra_flags: Sequence[str] | None = None,
):
    """Run codex in exec mode and return the parsed last assistant message.

    ``codex`` writes its final assistant message to ``output_path``.  The
    message is read, stripped of optional Markdown fences, and returned as a
    parsed JSON object.  If the content cannot be parsed as JSON, a
    ``json.JSONDecodeError`` is raised to allow callers to fail closed on
    formatting errors.
    """

    cmd = [
        codex_bin,
        "exec",
        "--output-last-message",
        output_path,
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C",
        work_dir,
        *(extra_flags or []),
    ]

    _invoke_codex(cmd, prompt, timeout)

    last = Path(output_path).read_text(encoding="utf-8").strip()
    if last.startswith("```"):
        lines = last.splitlines()[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        last = "\n".join(lines)
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        raise
