"""Thread-safe in-memory run-event log (observational only).

Events are append-only, monotonically sequenced, and replayable for
reconnection (``since(last_seq)``). Nothing here executes, authorizes,
or persists outside process memory.
"""
import threading


class RunEventLog:
    """Append-only event log with condition-based blocking reads."""

    def __init__(self, max_events: int = 2000) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._events: list = []
        self._next_seq = 1
        self._max_events = max_events
        self._closed = False

    def append(self, payload: dict) -> int:
        """Append one event; returns its sequence number."""
        if not isinstance(payload, dict):
            return 0
        with self._cond:
            entry = dict(payload)
            entry["seq"] = self._next_seq
            if not entry.get("timestamp"):
                from datetime import datetime, timezone

                entry["timestamp"] = datetime.now(timezone.utc).isoformat()
            self._next_seq += 1
            self._events.append(entry)
            if len(self._events) > self._max_events:
                del self._events[: len(self._events) - self._max_events]
            self._cond.notify_all()
            return entry["seq"]

    def since(self, seq: int) -> list:
        """Return all events with ``seq`` greater than the given number."""
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            seq = 0
        with self._lock:
            return [dict(e) for e in self._events if e["seq"] > seq]

    def latest_seq(self) -> int:
        with self._lock:
            return self._next_seq - 1

    def wait_since(self, seq: int, timeout: float) -> list:
        """Block until new events exist after ``seq`` or timeout elapses."""
        deadline = threading.TIMEOUT_MAX if timeout is None else timeout
        with self._cond:
            if not self._events or self._events[-1]["seq"] <= seq:
                self._cond.wait(timeout=deadline)
            if self._closed:
                return []
            return [dict(e) for e in self._events if e["seq"] > seq]

    def close(self) -> None:
        """Wake and release all waiting readers (shutdown path)."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()


__all__ = ["RunEventLog"]
