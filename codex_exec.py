import json
import subprocess
from pathlib import Path

def invoke_codex(*, codex_bin: str, prompt: str, work_dir: str, output_path: str, timeout: float):
    proc = subprocess.run(
        [codex_bin, "--output-last-message", output_path],
        input=prompt.encode(),
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )
    text = Path(output_path).read_text()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)
