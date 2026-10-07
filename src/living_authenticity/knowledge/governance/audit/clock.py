"""Shared local-clock timestamp contract for audit and run reporting.

Timestamps come from the local system clock of the machine performing
the operation, rendered as timezone-aware ISO-8601 with the local UTC
offset (never a hardcoded timezone, never a fixed conversion).
Approval timestamps elsewhere use ``datetime.now(timezone.utc)`` — the
same instant, also timezone-aware — so all persisted timestamps remain
chronologically comparable.
"""
from datetime import datetime


def local_now_iso() -> str:
    """Return the current local system time as aware ISO-8601."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


__all__ = ("local_now_iso",)
