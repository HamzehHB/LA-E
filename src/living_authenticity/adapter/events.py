"""Thread-safe in-memory run-event log (observational only).

Events are append-only, monotonically sequenced, and replayable for
reconnection (``since(last_seq)`` or ``snapshot_replay()``). Nothing here
executes, authorizes, or persists outside process memory.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("RunEventLog")

# Complete, authoritative catalog of all 18 run-scoped event kinds.
RUN_SCOPED_EVENT_KINDS = frozenset({
    "run_started",
    "run_completed",
    "run_rejected",
    "stage_started",
    "stage_completed",
    "status",
    "warning",
    "error",
    "stopped",
    "human_review_required",
    "approval_decided",
    "consent_required",
    "revalidation_result",
    "execution_result",
    "completed",
    "sync_approval_required",
    "audit_persisted",
    "audit_failed",
})


class TransportEvictionError(Exception):
    """Raised when an event replay cursor is older than the retained buffer."""

    def __init__(self, cursor: int, min_seq: int, total_evicted: int) -> None:
        super().__init__(
            f"Cursor {cursor} has been evicted from transport buffer "
            f"(min_seq={min_seq}, total_evicted={total_evicted})"
        )
        self.cursor = cursor
        self.min_seq = min_seq
        self.total_evicted = total_evicted


@dataclass(frozen=True)
class ReplaySnapshot:
    min_seq: int
    max_seq: int
    total_evicted: int
    is_evicted: bool
    events: List[Dict[str, Any]]
    hand_off_cursor: int


class RunEventLog:
    """Append-only event log with dual-sequence numbering, run sealing,
    and condition-based blocking reads.
    """

    def __init__(self, max_events: int = 2000) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._events: list = []
        self._next_seq = 1
        self._max_events = max_events
        self._closed = False

        # Phase 4 Increment 1 dual-sequence and sealing state
        self._run_counters: dict[str, int] = {}
        self._sealed_runs: set[str] = set()
        self._post_seal_rejections: deque = deque(maxlen=100)
        self._total_post_seal_rejections = 0
        self._total_evicted = 0
        self._min_seq = 1

    def append(self, payload: dict) -> int:
        """Append one event; returns its global sequence number, or 0 if rejected."""
        if not isinstance(payload, dict):
            return 0

        raw_kind = payload.get("kind")
        kind = str(raw_kind).strip() if raw_kind is not None else ""
        is_run_scoped = kind in RUN_SCOPED_EVENT_KINDS

        raw_run_id = payload.get("run_id")
        has_run_id_key = "run_id" in payload and raw_run_id is not None
        clean_run_id: Optional[str] = (
            raw_run_id.strip() if isinstance(raw_run_id, str) and raw_run_id.strip() else None
        )
        is_valid_run_id = clean_run_id is not None

        now_iso = datetime.now(timezone.utc).isoformat()
        rejection_warning: Optional[tuple] = None

        with self._cond:
            if is_run_scoped:
                # Scenarios 1, 2, 3, 7
                if clean_run_id is None:
                    # Scenario 2 (missing) or Scenario 3 (malformed)
                    reason = "malformed_run_id" if (has_run_id_key and raw_run_id != "") else "missing_run_id"
                    rejection_warning = (now_iso, "", kind, str(payload.get("stage", "")), reason)
                elif clean_run_id in self._sealed_runs:
                    # Scenario 7: Sealed run
                    self._total_post_seal_rejections += 1
                    rec = {
                        "timestamp": now_iso,
                        "run_id": clean_run_id,
                        "kind": kind,
                        "stage": str(payload.get("stage", "")),
                        "reason": "run_sealed",
                    }
                    self._post_seal_rejections.append(rec)
                    rejection_warning = (
                        rec["timestamp"],
                        rec["run_id"],
                        rec["kind"],
                        rec["stage"],
                        rec["reason"],
                    )
                else:
                    # Scenario 1: Valid run-scoped event
                    entry = dict(payload)
                    entry["run_id"] = clean_run_id
                    entry["seq"] = self._next_seq
                    self._next_seq += 1

                    current_run_seq = self._run_counters.get(clean_run_id, 0) + 1
                    self._run_counters[clean_run_id] = current_run_seq
                    entry["run_seq"] = current_run_seq

                    if not entry.get("timestamp"):
                        entry["timestamp"] = now_iso

                    self._events.append(entry)
                    if len(self._events) > self._max_events:
                        evicted_count = len(self._events) - self._max_events
                        del self._events[:evicted_count]
                        self._total_evicted += evicted_count
                        self._min_seq = self._events[0]["seq"] if self._events else self._next_seq

                    self._cond.notify_all()
                    return entry["seq"]
            else:
                # Scenarios 4, 5, 6: Global event
                entry = dict(payload)
                entry["seq"] = self._next_seq
                self._next_seq += 1
                entry["run_seq"] = None

                if clean_run_id is not None:
                    # Scenario 5: Valid contextual run_id preserved
                    entry["run_id"] = clean_run_id
                elif has_run_id_key:
                    # Scenario 6: Malformed run_id key present, sanitized to None
                    entry["run_id"] = None
                # Scenario 4: No run_id key present, remains absent or None

                if not entry.get("timestamp"):
                    entry["timestamp"] = now_iso

                self._events.append(entry)
                if len(self._events) > self._max_events:
                    evicted_count = len(self._events) - self._max_events
                    del self._events[:evicted_count]
                    self._total_evicted += evicted_count
                    self._min_seq = self._events[0]["seq"] if self._events else self._next_seq

                self._cond.notify_all()
                return entry["seq"]

        # Outside lock: log warning if rejected
        if rejection_warning is not None:
            logger.warning(
                "Rejected event: timestamp=%s run_id=%s kind=%s stage=%s reason=%s",
                rejection_warning[0],
                rejection_warning[1],
                rejection_warning[2],
                rejection_warning[3],
                rejection_warning[4],
            )
            return 0

        return 0

    def seal_run(self, run_id: str) -> None:
        """Permanently seal a run. Subsequent appends for this run will be rejected."""
        if not isinstance(run_id, str) or not run_id.strip():
            return
        with self._cond:
            self._sealed_runs.add(run_id.strip())

    def is_sealed(self, run_id: str) -> bool:
        """Check whether a run has been sealed."""
        if not isinstance(run_id, str) or not run_id.strip():
            return False
        with self._cond:
            return run_id.strip() in self._sealed_runs

    def snapshot_replay(self, after: int = 0, run_id: Optional[str] = None) -> ReplaySnapshot:
        """Capture connection bounds and initial event slice in a single lock acquisition."""
        try:
            after = int(after)
        except (TypeError, ValueError):
            after = 0

        clean_run_id = run_id.strip() if isinstance(run_id, str) and run_id.strip() else None

        with self._cond:
            min_seq = self._min_seq
            max_seq = self._next_seq - 1
            total_evicted = self._total_evicted

            is_evicted = bool(total_evicted > 0 and after < min_seq - 1)
            if is_evicted:
                return ReplaySnapshot(
                    min_seq=min_seq,
                    max_seq=max_seq,
                    total_evicted=total_evicted,
                    is_evicted=True,
                    events=[],
                    hand_off_cursor=after,
                )

            events = [
                dict(e) for e in self._events
                if e["seq"] > after and (clean_run_id is None or e.get("run_id") == clean_run_id)
            ]

            return ReplaySnapshot(
                min_seq=min_seq,
                max_seq=max_seq,
                total_evicted=total_evicted,
                is_evicted=False,
                events=events,
                hand_off_cursor=max_seq,
            )

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

    def wait_since(self, seq: int, timeout: float, run_id: Optional[str] = None) -> list:
        """Block until new events exist after ``seq`` or timeout elapses."""
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            seq = 0

        clean_run_id = run_id.strip() if isinstance(run_id, str) and run_id.strip() else None
        deadline = threading.TIMEOUT_MAX if timeout is None else timeout

        with self._cond:
            if self._total_evicted > 0 and seq < self._min_seq - 1:
                raise TransportEvictionError(cursor=seq, min_seq=self._min_seq, total_evicted=self._total_evicted)

            def _matching_events():
                return [
                    dict(e) for e in self._events
                    if e["seq"] > seq and (clean_run_id is None or e.get("run_id") == clean_run_id)
                ]

            matching = _matching_events()
            if not matching:
                self._cond.wait(timeout=deadline)

            if self._closed:
                return []

            if self._total_evicted > 0 and seq < self._min_seq - 1:
                raise TransportEvictionError(cursor=seq, min_seq=self._min_seq, total_evicted=self._total_evicted)

            return _matching_events()

    def bounds(self) -> dict:
        """Return current buffer bounds and eviction count under lock."""
        with self._lock:
            return {
                "min_seq": self._min_seq,
                "max_seq": self._next_seq - 1,
                "total_evicted": self._total_evicted,
            }

    def post_seal_diagnostics(self) -> dict:
        """Return internal diagnostic summary of post-seal rejections."""
        with self._cond:
            return {
                "total_rejections": self._total_post_seal_rejections,
                "recent_rejections": list(self._post_seal_rejections),
            }

    def close(self) -> None:
        """Wake and release all waiting readers (shutdown path)."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()


__all__ = [
    "RunEventLog",
    "RUN_SCOPED_EVENT_KINDS",
    "TransportEvictionError",
    "ReplaySnapshot",
]
