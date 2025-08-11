import json
import os
from typing import Any

class Reporter:
    """Lightweight event reporter for optional live mode."""

    def __init__(self, enabled: bool = False, fmt: str = "text") -> None:
        self.enabled = enabled
        self.fmt = fmt

    @classmethod
    def from_env(
        cls, *, enabled: bool = False, fmt: str = "text"
    ) -> "Reporter":
        env_enabled = os.getenv("ANCHOR_LIVE")
        if env_enabled and env_enabled not in {"0", "false", "False"}:
            enabled = True
        env_fmt = os.getenv("ANCHOR_LIVE_FORMAT")
        if env_fmt:
            fmt = env_fmt
        return cls(enabled=enabled, fmt=fmt)

    def log(self, event: str, **data: Any) -> None:
        if not self.enabled:
            return
        if self.fmt == "json":
            payload = {"event": event, **data}
            print(json.dumps(payload), flush=True)
        else:
            parts = [event]
            for k, v in data.items():
                if isinstance(v, (list, tuple)):
                    v = ",".join(str(x) for x in v)
                parts.append(f"{k}={v}")
            print(" ".join(parts), flush=True)
