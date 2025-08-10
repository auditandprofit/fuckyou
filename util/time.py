from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format with second precision."""
    return datetime.utcnow().replace(microsecond=0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def utc_timestamp() -> str:
    """Return current UTC time formatted as YYYYMMDD_HHMMSS."""
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")
