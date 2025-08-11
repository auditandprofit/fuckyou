import json
import hashlib
import subprocess
from pathlib import Path

def run_manifest(codex_bin: str, manifest_path: Path, out_dir: Path) -> None:
    data = json.loads(Path(manifest_path).read_text())
    out_dir.mkdir(parents=True, exist_ok=True)
    for file_path in data.get("files", []):
        digest = hashlib.sha256(file_path.encode()).hexdigest()[:8]
        out_file = out_dir / f"{digest}.txt"
        work_dir = Path(file_path).parent
        subprocess.run(
            [codex_bin, "exec", "--output-last-message", str(out_file), "-C", str(work_dir)],
            input=b"",
            cwd=Path("."),
            check=True,
        )
