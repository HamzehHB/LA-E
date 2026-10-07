"""Append-only persistent audit writer (traceability only).

``AuditWriter`` persists :class:`AuditRecord` objects as canonical JSON
lines under the single configured ``audit.root``, partitioned into the
user-configurable ``passed`` and ``failed`` subroots (defaults:
``<audit.root>/passed`` and ``<audit.root>/failed``). It is the only
component besides ``ControlledExecutor`` allowed to touch the
filesystem, and the roots must stay disjoint:

* ``ControlledExecutor`` -> staging root (staging artifacts only);
* ``AuditWriter`` -> audit root/subroots (audit records only,
  append-only).

Authoritative storage contract: ``passed`` holds ONLY the
executed-CREATE terminal record (approved CREATE that actually created
its staging artifact); ``failed`` holds every other terminal record,
including approved + revalidated pre-execution checkpoints (a safety
prerequisite persisted BEFORE execution, never proof of a passed
terminal outcome), rejections, holds, safe stops, and failures. A
rejected or held proposal stored under ``failed`` is an intentional
safety outcome, not necessarily a system error.

The writer creates its configured root/subroots when missing, never
overwrites/deletes/renames existing records, never writes outside the
audit boundary (subroots are ``PathBoundary``-validated against the
root, so traversal such as ``passed/../../x`` and symlink escapes are
rejected), and never records credentials: the sanitizer removes
credential-like keys recursively through dicts, lists, tuples, and
nested dataclass structures before anything is written.
"""
import dataclasses
import hashlib
import json
import os
import uuid
from pathlib import Path

from src.living_authenticity.security.path_boundary import PathBoundary

from .clock import local_now_iso

AUDIT_LOG_FILENAME = "audit.jsonl"
PASSED_SUBDIR = "passed"
FAILED_SUBDIR = "failed"

_STORAGE_CATEGORIES = frozenset({"passed", "failed"})

_FORBIDDEN_RECORD_KEYS = frozenset({
    "credential", "credentials", "secret", "secrets", "token",
    "api_key", "apikey", "password", "credential_env_value",
})


def audit_storage_category(record) -> str:
    """Return ``"passed"`` or ``"failed"`` for one audit record.

    Authoritative contract: ``passed`` holds ONLY the executed-CREATE
    terminal record — an approved CREATE that actually succeeded in
    creating its staging artifact (``execution_executed``). Everything
    else — including approved + revalidated pre-execution checkpoints
    (a safety prerequisite, not proof of a passed terminal outcome),
    rejections, holds, NEEDS_REVIEW, DO_NOT_IMPORT, safe stops,
    guard/revalidation/execution failures — is a ``failed``-category
    terminal record.
    """
    if getattr(record, "execution_executed", False):
        return "passed"
    return "failed"


def _sanitize(value: object) -> object:
    """Drop credential-like keys recursively; return plain data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            value = dataclasses.asdict(value)
        except Exception:
            value = str(value)
    if isinstance(value, dict):
        cleaned: dict = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_RECORD_KEYS:
                continue
            cleaned[key] = _sanitize(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Unknown object: serialize to a plain string rather than persisting
    # an opaque structure that might carry sensitive content.
    return str(value)


def record_to_dict(record) -> dict:
    """Return a sanitized canonical mapping of one audit record."""
    fields: dict = {}
    for name in getattr(record, "__dataclass_fields__", {}) or {}:
        try:
            fields[name] = getattr(record, name)
        except Exception:
            continue
    sanitized = _sanitize(fields)
    return sanitized if isinstance(sanitized, dict) else {}


def record_digest(payload: dict) -> str:
    """Return the sha256 of the canonical JSON encoding of ``payload``."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditWriter:
    """Append one audit record per call into the configured audit root.

    The root and the optional ``passed_dir``/``failed_dir`` subroots are
    all validated once at construction with ``PathBoundary`` against the
    root, so later appends can only ever create/append
    ``<subroot>/audit.jsonl`` — no traversal, no symlink escape, no
    destination taken from record content.
    """

    def __init__(self, audit_root="", passed_dir="", failed_dir="") -> None:
        if not isinstance(audit_root, str) or not audit_root.strip():
            raise ValueError("audit_root must be a non-empty string")
        candidate = Path(audit_root)
        if candidate.is_symlink():
            raise ValueError("audit root is a symlink")
        try:
            boundary = PathBoundary(audit_root)
        except Exception as exc:
            raise ValueError("audit root invalid") from exc
        self._boundary = boundary
        self._root = Path(audit_root).expanduser().resolve()

        def _resolve_subdir(value, default_name) -> Path:
            text = value.strip() if isinstance(value, str) else ""
            path = Path(text) if text else (self._root / default_name)
            if path.is_symlink():
                raise ValueError("audit subdir is a symlink: " + default_name)
            try:
                boundary.validate(str(path))
            except Exception as exc:
                raise ValueError(
                    "audit subdir outside audit root: " + default_name
                ) from exc
            return path.expanduser().resolve()

        self._passed = _resolve_subdir(passed_dir, PASSED_SUBDIR)
        self._failed = _resolve_subdir(failed_dir, FAILED_SUBDIR)
        if self._passed == self._failed:
            raise ValueError(
                "passed and failed audit subroots must resolve differently")

    @property
    def root(self) -> str:
        return str(self._root)

    @property
    def passed_dir(self) -> str:
        return str(self._passed)

    @property
    def failed_dir(self) -> str:
        return str(self._failed)

    def _target(self, category: str) -> Path:
        directory = self._passed if category == "passed" else self._failed
        target = directory / AUDIT_LOG_FILENAME
        # Re-validated on every append: the only writable path shape is
        # <validated subroot>/audit.jsonl.
        return self._boundary.validate(str(target))

    def append(self, record) -> dict:
        """Persist ``record``; return a receipt dict (no secrets)."""
        payload = record_to_dict(record)
        if not payload.get("record_id"):
            payload["record_id"] = "rec-" + uuid.uuid4().hex[:12]
        if not payload.get("run_id"):
            payload["run_id"] = "run-unbound-" + uuid.uuid4().hex[:12]
        if not payload.get("timestamp"):
            payload["timestamp"] = local_now_iso()
        payload["terminal_outcome"] = str(
            getattr(record, "outcome", "") or "")
        category = audit_storage_category(record)
        payload["storage_category"] = category
        digest = record_digest(payload)
        payload["record_digest"] = digest
        payload["persistence_timestamp"] = local_now_iso()
        directory = self._passed if category == "passed" else self._failed
        try:
            os.makedirs(str(directory), exist_ok=True)
        except OSError as exc:
            raise OSError("audit root unavailable") from exc
        target = self._target(category)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          default=str) + "\n"
        try:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            raise OSError("audit persistence failed") from exc
        return {"audit_path": str(target), "record_digest": digest,
                "record_id": payload["record_id"],
                "run_id": payload["run_id"],
                "storage_category": category,
                "timestamp": payload["timestamp"]}


def persist_record(writer, record, receipts=None) -> bool:
    """Persist one record through ``writer``; append a receipt if asked.

    Returns ``True`` when persistence succeeded **or** when no writer is
    configured (no persistence requirement exists in that case), so the
    execution gate treats "audit persistence must succeed before
    execution" as satisfied only when there is nothing to persist or the
    append genuinely landed.
    """
    if writer is None:
        return True
    try:
        receipt = writer.append(record)
    except Exception as exc:
        if receipts is not None:
            receipts.append({"record_id": getattr(record, "record_id", ""),
                             "persisted": False,
                             "reason": str(exc) or "audit persistence failed"})
        return False
    if receipts is not None:
        entry = dict(receipt)
        entry["persisted"] = True
        receipts.append(entry)
    return True


__all__ = ("AUDIT_LOG_FILENAME", "PASSED_SUBDIR", "FAILED_SUBDIR",
           "AuditWriter", "audit_storage_category", "persist_record",
           "record_digest", "record_to_dict")
