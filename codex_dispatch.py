from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence
import threading
import tempfile


class CodexNotFound(FileNotFoundError):
    """Raised when the codex binary cannot be located."""


class CodexTimeout(TimeoutError):
    """Raised when the codex process exceeds the provided timeout."""


@dataclass
class CodexExecResult:
    stdout: str
    stderr: str
    returncode: int
    duration_sec: float
    cmd: list[str]


class CodexError(RuntimeError):
    """Raised when codex exits with a non-zero status."""

    def __init__(self, result: CodexExecResult):
        super().__init__(result.stderr)
        self.result = result


class CodexClient:
    """Thin wrapper around the codex CLI for deterministic execution."""

    def __init__(
        self,
        *,
        bin_path: Optional[str] = None,
        retries: int = 0,
        backoff_base: float = 2.0,
        semaphore: Optional[threading.Semaphore] = None,
        default_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.bin_path = bin_path or self._find_codex_bin()
        self.retries = retries
        self.backoff_base = backoff_base
        self.semaphore = semaphore
        self.default_env = dict(default_env or {})

    def _find_codex_bin(self) -> str:
        path = shutil.which("codex")
        if not path:
            raise CodexNotFound("codex binary not found")
        return path

    def exec(
        self,
        *,
        prompt: str,
        workdir: str,
        extra_flags: Optional[Sequence[str]] = None,
        timeout: float = 60.0,
    ) -> CodexExecResult:
        """Execute codex with the given prompt and return the result."""

        out_file = Path(tempfile.mkstemp(prefix="codex_last_")[1])
        cmd = [
            self.bin_path,
            "exec",
            "--output-last-message",
            str(out_file),
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C",
            workdir,
            *(extra_flags or []),
        ]
        env = os.environ.copy()
        env.update(self.default_env)

        attempt = 0
        while True:
            attempt += 1
            if self.semaphore is None:
                ctx = _NullCtx()
            else:
                ctx = self.semaphore
            with ctx:
                start = time.time()
                stdout_buf: list[str] = []
                stderr_buf: list[str] = []
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        text=True,
                    )
                    assert proc.stdin is not None
                    proc.stdin.write(prompt)
                    proc.stdin.close()

                    def _forward(src, dst, buf):
                        for line in src:
                            buf.append(line)
                            dst.write(line)
                            dst.flush()

                    assert proc.stdout is not None
                    assert proc.stderr is not None
                    th_out = threading.Thread(
                        target=_forward, args=(proc.stdout, sys.stdout, stdout_buf)
                    )
                    th_err = threading.Thread(
                        target=_forward, args=(proc.stderr, sys.stderr, stderr_buf)
                    )
                    th_out.start()
                    th_err.start()
                    try:
                        proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired as exc:
                        proc.kill()
                        proc.wait()
                        th_out.join()
                        th_err.join()
                        if attempt > self.retries:
                            raise CodexTimeout(str(exc)) from exc
                        time.sleep(self.backoff_base ** attempt)
                        continue
                    except KeyboardInterrupt:
                        proc.send_signal(signal.SIGINT)
                        proc.wait()
                        th_out.join()
                        th_err.join()
                        raise
                    th_out.join()
                    th_err.join()
                except subprocess.TimeoutExpired as exc:
                    # Safety net if Popen itself times out (rare)
                    proc.kill()
                    proc.wait()
                    th_out.join()
                    th_err.join()
                    if attempt > self.retries:
                        raise CodexTimeout(str(exc)) from exc
                    time.sleep(self.backoff_base ** attempt)
                    continue
            duration = time.time() - start
            try:
                last_msg = out_file.read_text()
            except Exception:
                last_msg = ""
            finally:
                try:
                    out_file.unlink()
                except OSError:
                    pass
            result = CodexExecResult(
                stdout=last_msg,
                stderr="".join(stderr_buf),
                returncode=proc.returncode,
                duration_sec=duration,
                cmd=cmd,
            )
            if proc.returncode != 0:
                if attempt > self.retries:
                    raise CodexError(result)
                time.sleep(self.backoff_base ** attempt)
                continue
            return result


class _NullCtx:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False
