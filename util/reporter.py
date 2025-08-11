import json
import os
from typing import Any

class Reporter:
    """Lightweight event reporter for optional live mode."""

    def __init__(self, enabled: bool = False, fmt: str = "text") -> None:
        self.enabled = enabled
        self.fmt = fmt
        self._pretty = False
        self._fmt = None

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
        r = cls(enabled=enabled, fmt=fmt)
        if r.enabled and fmt == "text":
            pretty_env = os.getenv("ANCHOR_LIVE_PRETTY", "1")
            if pretty_env not in {"0", "false", "False"}:
                try:
                    from .live_text import LiveTextFormatter
                    import shutil
                    import sys

                    width = shutil.get_terminal_size((100, 20)).columns
                    r._pretty = True
                    r._fmt = LiveTextFormatter(
                        sys.stdout, getattr(sys.stdout, "isatty", lambda: False)(), width
                    )
                except Exception:
                    r._pretty = False
        return r

    def log(self, event: str, **data: Any) -> None:
        if not self.enabled:
            return
        if self.fmt == "json":
            payload = {"event": event, **data}
            print(json.dumps(payload), flush=True)
        else:
            if self._pretty and self._fmt is not None:
                self._fmt.handle(event, **data)
                return
            parts = [event]
            for k, v in data.items():
                if isinstance(v, (list, tuple)):
                    v = ",".join(str(x) for x in v)
                parts.append(f"{k}={v}")
            print(" ".join(parts), flush=True)
