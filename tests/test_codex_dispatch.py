import os
import threading
import _thread
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
