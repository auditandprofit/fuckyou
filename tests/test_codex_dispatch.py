import os
import threading
import _thread
import os
import shutil
import subprocess
import pytest
from codex_dispatch import CodexClient


def test_exec_handles_keyboard_interrupt(tmp_path):
    codex = tmp_path / "codex"
    codex.write_text(
        """#!/usr/bin/env python3
import os, signal, sys, time
out_path = sys.argv[sys.argv.index('--output-last-message') + 1]
work = sys.argv[sys.argv.index('-C') + 1]
os.chdir(work)
open(out_path, 'w').write('')
open('pid', 'w').write(str(os.getpid()))
signal.signal(signal.SIGINT, lambda s, f: sys.exit(130))
time.sleep(60)
"""
    )
    codex.chmod(0o755)

    client = CodexClient(bin_path=str(codex))

    timer = threading.Timer(0.5, _thread.interrupt_main)
    timer.start()
    with pytest.raises(KeyboardInterrupt):
        client.exec(prompt="", workdir=str(tmp_path))
    timer.cancel()

    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_exec_uses_sandbox(tmp_path):
    codex = tmp_path / "codex"
    codex.write_text(
        """#!/usr/bin/env python3
import sys, json
out_path = sys.argv[sys.argv.index('--output-last-message') + 1]
open(out_path, 'w').write(json.dumps({}))
"""
    )
    codex.chmod(0o755)
    client = CodexClient(bin_path=str(codex))
    result = client.exec(prompt="", workdir=str(tmp_path))
    assert all("--dangerously-bypass-approvals-and-sandbox" not in c for c in result.cmd)


def test_wrap_no_network(monkeypatch, tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then exit 0; fi\n")
    codex.chmod(0o755)

    monkeypatch.setattr(shutil, "which", lambda n: "/bin/firejail" if n == "firejail" else None)
    client = CodexClient(bin_path=str(codex))
    assert client._wrap_no_network(["cmd"])[:3] == ["/bin/firejail", "--quiet", "--net=none"]

    monkeypatch.setattr(shutil, "which", lambda n: "/bin/unshare" if n == "unshare" else None)
    monkeypatch.setattr(subprocess, "check_call", lambda *a, **k: 0)
    client = CodexClient(bin_path=str(codex))
    assert client._wrap_no_network(["cmd"])[:2] == ["/bin/unshare", "-n"]

    monkeypatch.setattr(shutil, "which", lambda n: None)
    client = CodexClient(bin_path=str(codex))
    assert client._wrap_no_network(["cmd"]) == ["cmd"]
