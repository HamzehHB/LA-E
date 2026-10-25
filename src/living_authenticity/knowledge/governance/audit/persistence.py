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
import re
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from src.living_authenticity.security.path_boundary import PathBoundary

from .clock import local_now_iso
from .outcome import AuditRecord

AUDIT_LOG_FILENAME = "audit.jsonl"
EMERGENCY_LOG_FILENAME = "audit_emergency.jsonl"
PASSED_SUBDIR = "passed"
FAILED_SUBDIR = "failed"
# Operational (non-proposal) failures — infrastructure errors, aborted
# runs, and cancellation outcomes — are routed to their own partition so
# they are never mistaken for proposal pass/fail terminal outcomes.
ERRORS_SUBDIR = "errors"

_STORAGE_CATEGORIES = frozenset({"passed", "failed", "errors"})

# Terminal outcomes that classify as operational errors rather than
# proposal pass/fail records. The set is closed: anything not listed
# keeps the existing passed/failed contract.
_ERROR_TERMINAL_OUTCOMES = frozenset({
    "llm_safe_stop",
    "audit_persistence_failed",
    "cancelled",
    "cancel_requested",
    "interrupted",
    "infrastructure_error",
    "guard_error",
})


def _extract_terminal_outcome(record, payload: dict | None = None) -> str:
    """Extract terminal outcome safely from AuditRecord or dictionary."""
    if isinstance(record, dict):
        outcome = record.get("terminal_outcome") or record.get("outcome")
        if outcome:
            return str(outcome)
    else:
        outcome = getattr(record, "terminal_outcome", "") or getattr(record, "outcome", "")
        if outcome:
            return str(outcome)
    if payload is not None and isinstance(payload, dict):
        payload_term = payload.get("terminal_outcome") or payload.get("outcome")
        if payload_term:
            return str(payload_term)
    return ""


def _audit_storage_category(record) -> str:
    """Return passed/failed/errors for one audit record (see docstring contract).

    passed: ONLY executed-CREATE (execution_executed). errors: ONLY the
    closed operational-outcome set. failed: everything else, including
    pre-execution checkpoints, rejections, holds, NEEDS_REVIEW,
    DO_NOT_IMPORT, safe stops, guard/revalidation/execution failures.
    """
    if isinstance(record, dict):
        if record.get("execution_executed"):
            return "passed"
        outcome = _extract_terminal_outcome(record).strip()
        if outcome in _ERROR_TERMINAL_OUTCOMES:
            return "errors"
        return "failed"
    if getattr(record, "execution_executed", False):
        return "passed"
    outcome = _extract_terminal_outcome(record).strip()
    if outcome in _ERROR_TERMINAL_OUTCOMES:
        return "errors"
    return "failed"


def audit_storage_category(record) -> str:
    """Public entry point for the partition contract (passed/failed/errors)."""
    return _audit_storage_category(record)


_FORBIDDEN_RECORD_KEYS = frozenset({
    "credential", "credentials", "secret", "secrets", "token",
    "api_key", "apikey", "password", "credential_env_value",
})


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


def record_to_dict(record: object) -> dict[str, Any]:
    """Return a sanitized canonical mapping of one audit record."""
    if isinstance(record, dict):
        sanitized = _sanitize(record)
        if isinstance(sanitized, dict):
            return {str(k): v for k, v in sanitized.items()}
        return {}
    fields: dict[str, Any] = {}
    for name in getattr(record, "__dataclass_fields__", {}) or {}:
        try:
            fields[str(name)] = getattr(record, name)
        except Exception:
            continue
    sanitized = _sanitize(fields)
    if isinstance(sanitized, dict):
        return {str(k): v for k, v in sanitized.items()}
    return {}


# Fields present in persisted journal lines that were added after record_digest computation
POST_DIGEST_FIELDS = frozenset({"record_digest", "persistence_timestamp"})


def compute_record_digest(payload: dict) -> str:
    """Return the sha256 of the canonical JSON encoding of ``payload``.

    Explicitly excludes post-digest fields ('record_digest', 'persistence_timestamp')
    to ensure deterministic recomputation against stored records.
    """
    to_hash = {k: v for k, v in payload.items() if k not in POST_DIGEST_FIELDS}
    canonical = json.dumps(to_hash, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_digest(payload: dict) -> str:
    """Return the sha256 of the canonical JSON encoding of ``payload``."""
    return compute_record_digest(payload)


def verify_record_digest(stored_data: object) -> tuple[bool, str]:
    """Verify that stored_data contains a valid, matching record_digest.

    Returns (is_valid, failure_reason).
    """
    if not isinstance(stored_data, dict):
        return False, f"stored record is not a dictionary (got {type(stored_data).__name__})"
    digest = stored_data.get("record_digest")
    if not isinstance(digest, str) or not digest.strip():
        return False, "missing or empty 'record_digest'"
    digest = digest.strip()
    if len(digest) != 64 or not all(c in "0123456789abcdefABCDEF" for c in digest):
        return False, f"malformed 'record_digest' (not a 64-character hex string): {digest!r}"
    expected_digest = compute_record_digest(stored_data)
    if digest.lower() != expected_digest.lower():
        return False, f"record_digest mismatch: stored={digest!r} != recomputed={expected_digest!r}"
    return True, ""


@dataclasses.dataclass(frozen=True)
class EmergencyRecoveryResult:
    """Outcome of an emergency history recovery operation."""
    all_ok: bool
    total_required: int
    persisted_count: int
    receipts: list[dict]
    failed_record_ids: list[str]
    conflicts: list[dict]
    errors: dict[str, str]
    duplicates: list[dict] = dataclasses.field(default_factory=list)

    def __iter__(self):
        return iter(self.receipts)

    def __getitem__(self, item):
        return self.receipts[item]

    def __len__(self):
        return len(self.receipts)


class EmergencyJournalReadResult(tuple):
    """Result of reading errors/audit_emergency.jsonl.

    Enables both 2-tuple unpacking for backward-compatibility:
        records_by_id, malformed_lines = writer.read_emergency_records()
    and attribute access:
        result.records_by_id
        result.malformed_lines
        result.duplicates
        result.all_records_by_id
    """
    _records_by_id: dict[str, dict]
    _malformed_lines: list[dict]
    _duplicates: list[dict]
    _all_records_by_id: dict[str, list[dict]]

    def __new__(
        cls,
        records_by_id: dict[str, dict],
        malformed_lines: list[dict],
        duplicates: list[dict] | None = None,
        all_records_by_id: dict[str, list[dict]] | None = None,
    ):
        inst = super().__new__(cls, (records_by_id, malformed_lines))
        inst._records_by_id = records_by_id
        inst._malformed_lines = malformed_lines
        inst._duplicates = duplicates if duplicates is not None else []
        inst._all_records_by_id = all_records_by_id if all_records_by_id is not None else {}
        return inst

    @property
    def records_by_id(self) -> dict[str, dict]:
        return self._records_by_id

    @property
    def malformed_lines(self) -> list[dict]:
        return self._malformed_lines

    @property
    def duplicates(self) -> list[dict]:
        return self._duplicates

    @property
    def all_records_by_id(self) -> dict[str, list[dict]]:
        return self._all_records_by_id


STORAGE_GENERATED_METADATA_FIELDS = frozenset({
    "storage_category",
    "emergency",
    "emergency_reason",
    "emergency_timestamp",
    "persistence_timestamp",
    "record_digest",
})


def _extract_identifiable_record_id(raw_line: str) -> str:
    """Best-effort regex extraction of record_id from a raw, truncated, or malformed line."""
    m = re.search(r'"record_id"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw_line)
    if m:
        try:
            return json.loads(f'"{m.group(1)}"')
        except Exception:
            return m.group(1)
    return ""


def _audit_record_dataclass_defaults() -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for f in dataclasses.fields(AuditRecord):
        if f.default is not dataclasses.MISSING:
            val = f.default
        elif f.default_factory is not dataclasses.MISSING:
            val = f.default_factory()
        else:
            val = ""
        if isinstance(val, (list, tuple)):
            defaults[f.name] = list(val)
        else:
            defaults[f.name] = val
    return defaults


ALL_AUDIT_RECORD_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(AuditRecord))
AUDIT_BOOLEAN_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(AuditRecord) if f.type is bool)
AUDIT_RECORD_SCHEMA_DEFAULTS: dict[str, Any] = _audit_record_dataclass_defaults()


def normalize_boolean_field(val: object) -> tuple[bool | None, bool]:
    """Normalize an explicitly supported boolean representation.

    Accepts:
    - bool: True or False (strictly type(val) is bool)
    - str: "true" -> True, "false" -> False (case-insensitive stripped)

    Returns:
    (normalized_bool, is_valid)
    If is_valid is False, val is not an explicitly supported boolean representation.
    """
    if type(val) is bool:
        return val, True
    if isinstance(val, str):
        cleaned = val.strip().lower()
        if cleaned == "true":
            return True, True
        if cleaned == "false":
            return False, True
    return None, False


def canonical_domain_payload(rec_or_dict: object) -> dict[str, Any]:
    """Return a deterministic mapping of the canonical domain content of an audit record.

    Excludes storage-generated metadata fields (storage_category, emergency,
    emergency_reason, emergency_timestamp, persistence_timestamp, record_digest).
    Preserves all meaningful domain fields: terminal_outcome, execution outcome,
    artifact metadata, proposal metadata, conflict linkage, and timestamps.
    Normalizes:
    - terminal_outcome: extracted via _extract_terminal_outcome
    - outcome: removed to prevent duplicate mismatch with terminal_outcome
    - boolean fields: normalized via normalize_boolean_field (retains invalid values for reporting)
    - list/tuple fields: normalized to list
    - strings: stripped
    """
    raw: dict[str, Any] = dict(record_to_dict(rec_or_dict))

    # Strip storage-generated metadata fields
    for field in STORAGE_GENERATED_METADATA_FIELDS:
        raw.pop(field, None)

    # Effective terminal outcome
    effective_outcome = _extract_terminal_outcome(rec_or_dict, raw).strip()
    raw["terminal_outcome"] = effective_outcome
    raw.pop("outcome", None)

    # Preserve conflict fields if present on dictionary
    if isinstance(rec_or_dict, dict):
        for k in ("conflict_with_record_id", "conflict_type", "conflicting_fields",
                  "original_stored_digest", "conflicting_content_digest"):
            if k in rec_or_dict and k not in raw:
                raw[str(k)] = rec_or_dict[k]

    canonical: dict[str, Any] = {}
    for k, v in raw.items():
        k_str = str(k)
        if k_str in STORAGE_GENERATED_METADATA_FIELDS:
            continue
        if k_str in AUDIT_BOOLEAN_FIELDS:
            norm_b, is_valid = normalize_boolean_field(v)
            if is_valid:
                canonical[k_str] = norm_b
            else:
                canonical[k_str] = v
        elif isinstance(v, (list, tuple)):
            canonical[k_str] = list(v)
        elif isinstance(v, str):
            canonical[k_str] = v.strip()
        else:
            canonical[k_str] = v

    return canonical


def _check_emergency_record_match(rec, existing_data: dict) -> tuple[bool, list[str]]:
    """Compare an in-memory audit record with an existing emergency journal entry.

    Verifies the integrity of existing_data and checks canonical domain attributes in both directions.
    Returns (is_match, list_of_differing_fields).
    """
    mismatches: list[str] = []

    # 1. Verify integrity of the stored record on disk first
    is_valid_digest, digest_err = verify_record_digest(existing_data)
    if not is_valid_digest:
        mismatches.append(f"stored record integrity verification failed: {digest_err}")
        return False, mismatches

    in_domain = canonical_domain_payload(rec)
    st_domain = canonical_domain_payload(existing_data)

    all_keys = sorted(set(in_domain.keys()) | set(st_domain.keys()))

    for key in all_keys:
        in_present = key in in_domain
        st_present = key in st_domain
        in_val = in_domain.get(key)
        st_val = st_domain.get(key)

        if key == "timestamp":
            # Storage/append timestamp generated at write time; domain timestamps are execution_timestamp / artifact_created_at
            continue

        if key == "run_id":
            # If both are empty/unset or unbound
            if not in_val and not st_val:
                continue
            if not in_val and isinstance(st_val, str) and st_val.startswith("run-unbound-"):
                continue
            if not st_val and isinstance(in_val, str) and in_val.startswith("run-unbound-"):
                continue
            if in_val != st_val:
                mismatches.append(f"run_id: in-memory={in_val!r} != stored={st_val!r}")
            continue

        # Handle omitted dataclass default fields in dictionary representations
        if not in_present or not st_present:
            if key in ALL_AUDIT_RECORD_FIELDS:
                expected_default = AUDIT_RECORD_SCHEMA_DEFAULTS.get(key, "")
                present_val = in_val if in_present else st_val
                # Check if the present value matches the unconfigured schema default
                if type(present_val) is type(expected_default) and present_val == expected_default:
                    continue
                side_missing = "stored" if not st_present else "in-memory"
                mismatches.append(f"{key}: domain value {present_val!r} present but omitted from {side_missing} record")
                continue
            else:
                side_missing = "in-memory" if not in_present else "stored"
                present_val = st_val if not in_present else in_val
                mismatches.append(f"{key}: unexpected domain field {present_val!r} missing from {side_missing} record")
                continue

        # Both sides explicitly present: compare with strict type and value semantics
        # 1. Boolean domain fields
        if key in AUDIT_BOOLEAN_FIELDS:
            norm_in, in_ok = normalize_boolean_field(in_val)
            norm_st, st_ok = normalize_boolean_field(st_val)
            if not in_ok or not st_ok:
                mismatches.append(
                    f"{key}: invalid/unsupported boolean representation: in-memory={in_val!r}, stored={st_val!r}"
                )
            elif norm_in != norm_st:
                mismatches.append(f"{key}: in-memory={norm_in!r} != stored={norm_st!r}")
            continue

        # 2. Strict boolean vs non-boolean check across ANY field (prevents 0 == False, 1 == True)
        if (type(in_val) is bool) != (type(st_val) is bool):
            mismatches.append(
                f"{key}: type mismatch (boolean vs non-boolean): in-memory={in_val!r} ({type(in_val).__name__}) != stored={st_val!r} ({type(st_val).__name__})"
            )
            continue

        # 3. None vs non-None check (prevents None == "" or None == [])
        if (in_val is None) != (st_val is None):
            mismatches.append(f"{key}: in-memory={in_val!r} != stored={st_val!r}")
            continue

        # 4. Collections
        if isinstance(in_val, list) or isinstance(st_val, list):
            if not isinstance(in_val, list) or not isinstance(st_val, list):
                mismatches.append(f"{key}: collection type mismatch: in-memory={in_val!r} != stored={st_val!r}")
            elif in_val != st_val:
                mismatches.append(f"{key}: in-memory={in_val!r} != stored={st_val!r}")
            continue

        # 5. Strings
        if isinstance(in_val, str) and isinstance(st_val, str):
            if in_val.strip() != st_val.strip():
                mismatches.append(f"{key}: in-memory={in_val!r} != stored={st_val!r}")
            continue

        # 6. Numbers / generic equality
        if type(in_val) is not type(st_val) and not (isinstance(in_val, (int, float)) and isinstance(st_val, (int, float))):
            mismatches.append(
                f"{key}: type mismatch: in-memory={in_val!r} ({type(in_val).__name__}) != stored={st_val!r} ({type(st_val).__name__})"
            )
            continue

        if in_val != st_val:
            mismatches.append(f"{key}: in-memory={in_val!r} != stored={st_val!r}")

    return (len(mismatches) == 0, mismatches)



class AuditWriter:
    """Append one audit record per call into the configured audit root.

    The root and the optional ``passed_dir``/``failed_dir`` subroots are
    all validated once at construction with ``PathBoundary`` against the
    root, so later appends can only ever create/append
    ``<subroot>/audit.jsonl`` — no traversal, no symlink escape, no
    destination taken from record content.
    """

    def __init__(self, audit_root="", passed_dir="", failed_dir="",
                 errors_dir="") -> None:
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
        self._errors = _resolve_subdir(errors_dir, ERRORS_SUBDIR)
        resolved = {self._passed, self._failed, self._errors}
        if len(resolved) != 3:
            raise ValueError(
                "passed, failed, and errors audit subroots must resolve "
                "differently")

    @property
    def root(self) -> str:
        return str(self._root)

    @property
    def passed_dir(self) -> str:
        return str(self._passed)

    @property
    def failed_dir(self) -> str:
        return str(self._failed)

    @property
    def errors_dir(self) -> str:
        return str(self._errors)

    def ensure_directories(self) -> None:
        """Ensure passed, failed, and errors subdirectories exist."""
        for directory in (self._passed, self._failed, self._errors):
            try:
                os.makedirs(str(directory), exist_ok=True)
            except OSError as exc:
                raise OSError("audit root unavailable") from exc

    def _target(self, category: str) -> Path:
        if category == "passed":
            directory = self._passed
        elif category == "errors":
            directory = self._errors
        else:
            directory = self._failed
        target = directory / AUDIT_LOG_FILENAME
        # Re-validated on every append: the only writable path shape is
        # <validated subroot>/audit.jsonl.
        return self._boundary.validate(str(target))

    def append(self, record) -> dict:
        """Persist ``record`` into primary journal (passed/ or failed/).

        Durability Semantics:
        - Primary persistence performs standard buffered file writes (receipt reports durability="written").
        - Buffered writes confirm data was written to the operating system's file stream without error,
          but do not execute hardware-level disk synchronization (fsync).
        - For hardware-synchronized writes, see append_emergency().
        """
        payload = record_to_dict(record)
        if not payload.get("record_id"):
            payload["record_id"] = "rec-" + uuid.uuid4().hex[:12]
        if not payload.get("run_id"):
            payload["run_id"] = "run-unbound-" + uuid.uuid4().hex[:12]
        if not payload.get("timestamp"):
            payload["timestamp"] = local_now_iso()
        payload["terminal_outcome"] = _extract_terminal_outcome(record, payload)
        category = audit_storage_category(record)
        payload["storage_category"] = category
        digest = compute_record_digest(payload)
        payload["record_digest"] = digest
        payload["persistence_timestamp"] = local_now_iso()
        if category == "passed":
            directory = self._passed
        elif category == "errors":
            directory = self._errors
        else:
            directory = self._failed
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
                "emergency": False,
                "durability": "written",
                "persisted": True,
                "timestamp": payload["timestamp"]}

    def append_emergency(self, record, failure_reason: str = "") -> dict:
        """Persist record into emergency journal in the errors partition.

        Used when primary append fails. Does not silently swallow errors.
        Enforces flush and hardware-level synchronization via os.fsync(fileno).

        Durability and Recovery Semantics:
        - Flushes Python internal buffers and calls os.fsync(fileno) on the open file descriptor
          (receipt reports durability="confirmed").
        - Synchronizes file content and metadata to the storage controller.
        - Important limits on crash durability: File-level fsync does not provide an unconditional
          guarantee of physical-media persistence across sudden hardware loss or power failure.
          On POSIX systems, directory entry durability requires an additional fsync on the parent
          directory descriptor, which is not supported on Windows directory handles.
        - Recovery: Emergency records are written to errors/audit_emergency.jsonl within the configured
          audit root without traversing or creating external paths.
        """
        payload = record_to_dict(record)
        if not payload.get("record_id"):
            payload["record_id"] = "rec-" + uuid.uuid4().hex[:12]
        if not payload.get("run_id"):
            payload["run_id"] = "run-unbound-" + uuid.uuid4().hex[:12]
        if not payload.get("timestamp"):
            payload["timestamp"] = local_now_iso()
        payload["terminal_outcome"] = _extract_terminal_outcome(record, payload)
        payload["storage_category"] = "errors"
        payload["emergency"] = True
        payload["emergency_reason"] = str(failure_reason) or "primary audit append failed"
        payload["emergency_timestamp"] = local_now_iso()

        # If record is a dictionary representing a conflict or supplemental entry, preserve conflict fields
        if isinstance(record, dict):
            for k in ("conflict_with_record_id", "conflict_type", "conflicting_fields",
                      "original_stored_digest", "conflicting_content_digest"):
                if k in record and k not in payload:
                    payload[k] = record[k]

        digest = compute_record_digest(payload)
        payload["record_digest"] = digest
        payload["persistence_timestamp"] = local_now_iso()

        directory = self._errors
        try:
            os.makedirs(str(directory), exist_ok=True)
        except OSError as exc:
            raise OSError("audit root unavailable for emergency journal") from exc

        target = self._boundary.validate(str(directory / EMERGENCY_LOG_FILENAME))
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          default=str) + "\n"
        try:
            with target.open("a", encoding="utf-8") as handle:
                pos = handle.tell()
                try:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                except Exception:
                    try:
                        handle.seek(pos)
                        handle.truncate()
                    except Exception:
                        pass
                    raise
        except OSError as exc:
            raise OSError(f"emergency audit persistence failed: {exc}") from exc

        return {"audit_path": str(target), "record_digest": digest,
                "record_id": payload["record_id"],
                "run_id": payload["run_id"],
                "storage_category": "errors",
                "emergency": True,
                "durability": "confirmed",
                "persisted": True,
                "timestamp": payload["timestamp"]}

    def read_emergency_records(self) -> EmergencyJournalReadResult:
        """Read all emergency records from errors/audit_emergency.jsonl.

        Validates the integrity of every stored record by verifying that its
        stored record_digest matches the canonical hash of its stored contents.
        Unverifiable or corrupted records are segregated into malformed_lines
        and excluded from the valid records_by_id dictionary.

        Tracks all occurrences of every record_id. If duplicate occurrences are
        detected in the append-only journal:
        - Repeated IDs with different canonical domain content are reported as conflicts.
        - Repeated IDs with identical content are reported as duplicate entries.
        - Neither conflicting entry is treated as clean/authoritative in records_by_id.

        Returns:
            EmergencyJournalReadResult(records_by_id, malformed_lines, duplicates, all_records_by_id)
            where records_by_id maps record_id to the verified dictionary on disk,
            malformed_lines contains entries that failed JSON decoding or digest verification,
            duplicates contains all duplicate/conflicting occurrences detected,
            and all_records_by_id groups all valid occurrences by record_id.
        """
        directory = self._errors
        target = directory / EMERGENCY_LOG_FILENAME
        if not target.is_file():
            return EmergencyJournalReadResult({}, [], [], {})

        records_by_id: dict[str, dict] = {}
        all_records_by_id: dict[str, list[dict]] = {}
        malformed: list[dict] = []
        duplicates: list[dict] = []
        try:
            content = target.read_text(encoding="utf-8")
            for line_no, raw_line in enumerate(content.splitlines(), start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception as exc:
                    rec_id = _extract_identifiable_record_id(line)
                    malformed.append({
                        "line_no": line_no,
                        "record_id": rec_id,
                        "error": f"JSON decode error: {exc}",
                        "raw": line,
                        "duplicate": False,
                        "integrity_failure": True,
                        "kind": "malformed_json",
                    })
                    continue

                if not isinstance(data, dict):
                    rec_id = _extract_identifiable_record_id(line)
                    malformed.append({
                        "line_no": line_no,
                        "record_id": rec_id,
                        "error": "not a JSON object",
                        "raw": line,
                        "duplicate": False,
                        "integrity_failure": True,
                        "kind": "not_json_object",
                    })
                    continue

                rec_id = str(data.get("record_id", "") or "").strip()
                if not rec_id:
                    rec_id = _extract_identifiable_record_id(line)
                    malformed.append({
                        "line_no": line_no,
                        "record_id": rec_id,
                        "error": "missing record_id",
                        "raw": line,
                        "duplicate": False,
                        "integrity_failure": True,
                        "kind": "missing_record_id",
                    })
                    continue

                # Recompute and verify stored record digest against canonical content
                is_valid_digest, digest_err = verify_record_digest(data)
                if not is_valid_digest:
                    malformed.append({
                        "line_no": line_no,
                        "record_id": rec_id,
                        "error": f"integrity verification failed: {digest_err}",
                        "raw": line,
                        "data": data,
                        "duplicate": False,
                        "integrity_failure": True,
                        "kind": "digest_invalid",
                    })
                    continue

                entry_info = {"line_no": line_no, "record_id": rec_id, "data": data, "raw": line}
                if rec_id not in all_records_by_id:
                    all_records_by_id[rec_id] = [entry_info]
                    records_by_id[rec_id] = data
                else:
                    all_records_by_id[rec_id].append(entry_info)
                    first_entry = all_records_by_id[rec_id][0]
                    first_line_no = first_entry["line_no"]
                    first_data = first_entry["data"]

                    # Check if canonical domain content matches or conflicts
                    is_match, mismatches = _check_emergency_record_match(data, first_data)
                    is_conflict = not is_match

                    dup_info = {
                        "record_id": rec_id,
                        "first_line_no": first_line_no,
                        "line_no": line_no,
                        "conflict": is_conflict,
                        "differing_fields": list(mismatches) if is_conflict else [],
                        "first_data": first_data,
                        "data": data,
                        "raw": line,
                    }
                    duplicates.append(dup_info)

                    if is_conflict:
                        err_desc = (
                            f"duplicate-ID conflict in journal for record_id '{rec_id}': "
                            f"line {line_no} conflicts with line {first_line_no} ({'; '.join(mismatches)})"
                        )
                        # Remove from clean records_by_id because conflict exists on disk
                        records_by_id.pop(rec_id, None)
                    else:
                        err_desc = (
                            f"duplicate record_id '{rec_id}' on line {line_no} repeats line {first_line_no} with identical domain content"
                        )
                    malformed.append({
                        "line_no": line_no,
                        "record_id": rec_id,
                        "error": err_desc,
                        "duplicate": True,
                        "conflict": is_conflict,
                        "integrity_failure": False,
                        "kind": "duplicate_conflict" if is_conflict else "duplicate_identical",
                        "differing_fields": list(mismatches) if is_conflict else [],
                        "first_line_no": first_line_no,
                        "raw": line,
                        "data": data,
                    })
        except OSError as exc:
            malformed.append({
                "line_no": 0,
                "record_id": "",
                "error": f"read error: {exc}",
                "raw": "",
                "duplicate": False,
                "integrity_failure": True,
                "kind": "read_error",
            })

        return EmergencyJournalReadResult(records_by_id, malformed, duplicates, all_records_by_id)

    def get_emergency_record_ids(self) -> set[str]:
        """Return the set of valid record_ids already persisted in errors/audit_emergency.jsonl."""
        records_by_id, _ = self.read_emergency_records()
        return set(records_by_id.keys())

    def _preserve_supplemental_record(
        self,
        conflict_id: str,
        supplemental_rec: dict,
        conflict_reason: str,
        expected_conflict_with: str,
        expected_conflict_type: str,
        expected_content_digest: str,
        records_by_id: dict[str, dict],
        all_records_by_id: dict[str, list[dict]],
        duplicates: list[dict],
        malformed_lines: list[dict],
        target_path: Path,
    ) -> tuple[bool, dict | None, str]:
        """Preserve supplemental evidence for an identity conflict, duplicate conflict, or corrupt collision.

        Validates every existing occurrence of conflict_id in the journal:
        - Inspects every occurrence in all_records_by_id.
        - Verifies digest, linkage, and canonical domain content of every occurrence.
        - Checks whether conflict_id is associated with duplicate entries, malformed entries, or integrity failures.
        - Rejects idempotent success if duplicate entries, conflicts, or integrity failures exist.
        - Does not append duplicate rows or overwrite existing entries.
        """
        integrity_failures = [
            m for m in malformed_lines
            if m.get("record_id") == conflict_id
            and (m.get("integrity_failure") is True or not m.get("duplicate"))
            and m.get("kind") not in ("duplicate_identical", "duplicate_conflict")
        ]
        dup_matches = [d for d in duplicates if d.get("record_id") == conflict_id]
        valid_occurrences = list(all_records_by_id.get(conflict_id, []))
        in_clean_records = conflict_id in records_by_id

        has_existing_claims = (
            bool(integrity_failures)
            or bool(dup_matches)
            or bool(valid_occurrences)
            or in_clean_records
        )

        if not has_existing_claims:
            # Brand new supplemental record: append fresh emergency entry
            try:
                raw_supp = self.append_emergency(
                    supplemental_rec,
                    failure_reason=f"supplemental_conflict_evidence: {conflict_reason}",
                )
                is_supp_valid, supp_v_err = validate_persistence_receipt(
                    raw_supp, require_emergency=True, expected_record_id=conflict_id
                )
                if is_supp_valid:
                    supp_data = record_to_dict(supplemental_rec)
                    supp_data["record_digest"] = raw_supp.get("record_digest", "")
                    records_by_id[conflict_id] = supp_data
                    all_records_by_id[conflict_id] = [
                        {"line_no": -1, "record_id": conflict_id, "data": supp_data, "raw": ""}
                    ]
                    return True, raw_supp, ""
                else:
                    return False, None, f"supplemental write returned invalid receipt: {supp_v_err}"
            except Exception as supp_exc:
                return False, None, f"failed to persist supplemental conflict evidence: {supp_exc}"

        # Existing claims exist on disk: validate EVERY occurrence and check duplicate policy
        errors_collected: list[str] = []

        # 1. Associated with malformed or corrupt entries
        if integrity_failures:
            for m in integrity_failures:
                line_no = m.get("line_no", "?")
                err = m.get("error", "unknown corruption")
                errors_collected.append(
                    f"existing supplemental record '{conflict_id}' is corrupt on disk / associated with malformed or digest invalid entry on line {line_no}: {err}"
                )

        # 2. Duplicate entries claiming conflict_id
        if dup_matches or len(valid_occurrences) > 1:
            conflict_dups = [d for d in dup_matches if d.get("conflict")]
            if conflict_dups or (valid_occurrences and not in_clean_records):
                diff_details = []
                for d in conflict_dups:
                    diff_details.extend(d.get("differing_fields", []))
                diff_str = f" ({'; '.join(diff_details)})" if diff_details else ""
                errors_collected.append(
                    f"existing supplemental record '{conflict_id}' has conflicting duplicate entries in journal across lines {[e['line_no'] for e in valid_occurrences]}{diff_str}"
                )
            else:
                errors_collected.append(
                    f"existing supplemental record '{conflict_id}' has duplicate entries in journal across lines {[e['line_no'] for e in valid_occurrences]} violating duplicate policy (duplicate entries cannot be reported as clean, uniquely verified evidence)"
                )

        # 3. Present in all_records_by_id but excluded from clean records_by_id
        if valid_occurrences and not in_clean_records and not any("absent from clean records mapping" in e for e in errors_collected):
            errors_collected.append(
                f"existing supplemental record '{conflict_id}' is present in journal occurrences but absent from clean records mapping"
            )

        # 4. Inspect every occurrence in valid_occurrences
        if not valid_occurrences and in_clean_records:
            # Fallback for synthetic/in-memory records_by_id entry without all_records_by_id entry
            valid_occurrences = [{"line_no": -1, "record_id": conflict_id, "data": records_by_id[conflict_id], "raw": ""}]

        for occ in valid_occurrences:
            occ_data = occ.get("data", {})
            occ_line = occ.get("line_no", -1)
            line_label = f"line {occ_line}" if occ_line > 0 else "stored record"

            # 4a. Verify stored digest
            is_dig_ok, dig_err = verify_record_digest(occ_data)
            if not is_dig_ok:
                errors_collected.append(
                    f"existing supplemental record '{conflict_id}' ({line_label}) digest invalid: {dig_err}"
                )
                continue

            # 4b. Confirm linkage fields
            link_errs: list[str] = []
            if occ_data.get("conflict_with_record_id") != expected_conflict_with:
                link_errs.append(
                    f"conflict_with_record_id mismatch: expected '{expected_conflict_with}', got '{occ_data.get('conflict_with_record_id')}'"
                )
            if occ_data.get("conflict_type") != expected_conflict_type:
                link_errs.append(
                    f"conflict_type mismatch: expected '{expected_conflict_type}', got '{occ_data.get('conflict_type')}'"
                )
            if occ_data.get("conflicting_content_digest") != expected_content_digest:
                link_errs.append(
                    f"conflicting_content_digest mismatch: expected '{expected_content_digest}', got '{occ_data.get('conflicting_content_digest')}'"
                )
            if link_errs:
                errors_collected.append(
                    f"existing supplemental record '{conflict_id}' ({line_label}) linkage mismatch: {'; '.join(link_errs)}"
                )

            # 4c. Verify canonical domain content match against incoming supplemental evidence
            is_supp_match, supp_mismatches = _check_emergency_record_match(supplemental_rec, occ_data)
            if not is_supp_match:
                errors_collected.append(
                    f"existing supplemental record '{conflict_id}' ({line_label}) content inconsistent: {'; '.join(supp_mismatches)}"
                )

        if errors_collected:
            return False, None, "; ".join(errors_collected)

        # All checks passed: exactly 1 valid occurrence, 0 corrupt lines, 0 duplicates, valid digest, valid linkage, matching content
        receipt = {
            "audit_path": str(target_path),
            "record_id": conflict_id,
            "persisted": True,
            "durability": "confirmed",
            "emergency": True,
            "note": "already_persisted_conflict_evidence",
        }
        return True, receipt, ""

    def append_emergency_history(
        self,
        records: object,
        failure_reason: str = "",
        failing_record_id: str = "",
    ) -> EmergencyRecoveryResult:
        """Idempotently preserve the complete audit history of an affected run in errors/audit_emergency.jsonl.

        Validates the integrity of existing emergency records before treating them as duplicates.
        If an existing record has the same ID but different content, records an explicit identity/content conflict
        and preserves the conflicting evidence without overwriting or deleting historical records.
        Returns an EmergencyRecoveryResult describing the outcome for every required record.
        """
        read_res = self.read_emergency_records()
        records_by_id = dict(read_res.records_by_id)
        malformed_lines = list(read_res.malformed_lines)
        duplicates = list(read_res.duplicates)
        all_records_by_id = dict(read_res.all_records_by_id)
        receipts: list[dict] = []
        failed_ids: list[str] = []
        conflicts: list[dict] = []
        errors: dict[str, str] = {}

        target = self._boundary.validate(str(self._errors / EMERGENCY_LOG_FILENAME))
        if isinstance(records, (list, tuple)):
            records_list = list(records)
        elif isinstance(records, Iterable) and not isinstance(records, (str, bytes, dict)):
            records_list = list(records)
        else:
            records_list = [records]

        if malformed_lines:
            for mal in malformed_lines:
                if mal.get("duplicate") or mal.get("kind") in ("duplicate_identical", "duplicate_conflict"):
                    continue
                err_msg = f"malformed journal line {mal.get('line_no')}: {mal.get('error')}"
                errors[f"malformed_line_{mal.get('line_no')}"] = err_msg

        if duplicates:
            for dup in duplicates:
                d_id = dup["record_id"]
                d_line = dup["line_no"]
                d_first = dup["first_line_no"]
                if dup["conflict"]:
                    err_msg = (
                        f"duplicate-ID conflict in journal for record_id '{d_id}': "
                        f"line {d_line} conflicts with line {d_first} ({'; '.join(dup['differing_fields'])})"
                    )
                else:
                    err_msg = (
                        f"duplicate record_id '{d_id}' in journal: line {d_line} duplicates line {d_first} with identical domain content"
                    )
                errors[f"duplicate_record_id_{d_id}_line_{d_line}"] = err_msg

        for rec in records_list:
            rec_id = str((rec.get("record_id") if isinstance(rec, dict) else getattr(rec, "record_id", "")) or "").strip()
            if not rec_id:
                err = "missing record_id on record to recover"
                receipts.append({
                    "record_id": "",
                    "run_id": str((rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", "")) or ""),
                    "storage_category": "errors",
                    "emergency": True,
                    "durability": "failed",
                    "persisted": False,
                    "reason": err,
                })
                failed_ids.append("unknown_missing_id")
                errors["unknown_missing_id"] = err
                continue

            # Check if this record_id is involved in duplicate entries in the journal
            dup_matches = [d for d in duplicates if d.get("record_id") == rec_id]
            if dup_matches:
                has_internal_conflict = any(d.get("conflict") for d in dup_matches)
                first_stored = all_records_by_id[rec_id][0]["data"]
                if has_internal_conflict:
                    mismatches = []
                    for d in dup_matches:
                        if d.get("conflict"):
                            mismatches.extend(d.get("differing_fields", []))
                    if not mismatches:
                        mismatches.append(f"internal journal conflict across lines {[e['line_no'] for e in all_records_by_id[rec_id]]}")

                    conflict_reason = (
                        f"duplicate-ID conflict in existing journal for record_id '{rec_id}': "
                        f"{'; '.join(mismatches)}"
                    )
                    supplemental_rec = dict(record_to_dict(rec))
                    supplemental_rec["run_id"] = str((rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", "")) or first_stored.get("run_id", ""))
                    term = _extract_terminal_outcome(rec, supplemental_rec)
                    supplemental_rec["terminal_outcome"] = term
                    supplemental_rec["artifact_path"] = str((rec.get("artifact_path") if isinstance(rec, dict) else getattr(rec, "artifact_path", "")) or supplemental_rec.get("artifact_path", "") or "")
                    supplemental_rec["artifact_reference"] = str((rec.get("artifact_reference") if isinstance(rec, dict) else getattr(rec, "artifact_reference", "")) or supplemental_rec.get("artifact_reference", "") or "")
                    supplemental_rec["execution_executed"] = bool((rec.get("execution_executed") if isinstance(rec, dict) else getattr(rec, "execution_executed", False)) or supplemental_rec.get("execution_executed", False))
                    supplemental_rec["proposal_action"] = str((rec.get("proposal_action") if isinstance(rec, dict) else getattr(rec, "proposal_action", "")) or supplemental_rec.get("proposal_action", "") or "")
                    supplemental_rec["proposal_hash"] = str((rec.get("proposal_hash") if isinstance(rec, dict) else getattr(rec, "proposal_hash", "")) or supplemental_rec.get("proposal_hash", "") or "")

                    incoming_content_digest = compute_record_digest(supplemental_rec)
                    conflict_id = f"{rec_id}-conflict-{incoming_content_digest[:12]}"
                    supplemental_rec["record_id"] = conflict_id
                    supplemental_rec["conflict_with_record_id"] = rec_id
                    supplemental_rec["conflict_type"] = "identity_content_mismatch"
                    supplemental_rec["conflicting_fields"] = list(mismatches)
                    supplemental_rec["original_stored_digest"] = first_stored.get("record_digest", "")
                    supplemental_rec["conflicting_content_digest"] = incoming_content_digest

                    supplemental_written, supplemental_receipt, supplemental_error = self._preserve_supplemental_record(
                        conflict_id=conflict_id,
                        supplemental_rec=supplemental_rec,
                        conflict_reason=conflict_reason,
                        expected_conflict_with=rec_id,
                        expected_conflict_type="identity_content_mismatch",
                        expected_content_digest=incoming_content_digest,
                        records_by_id=records_by_id,
                        all_records_by_id=all_records_by_id,
                        duplicates=duplicates,
                        malformed_lines=malformed_lines,
                        target_path=target,
                    )

                    conflict_receipt = {
                        "audit_path": str(target),
                        "record_id": rec_id,
                        "run_id": str((rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", "")) or first_stored.get("run_id", "")),
                        "storage_category": "errors",
                        "emergency": True,
                        "durability": "failed",
                        "persisted": False,
                        "conflict": True,
                        "duplicate_conflict": True,
                        "conflict_fields": list(mismatches),
                        "stored_digest": first_stored.get("record_digest", ""),
                        "incoming_digest": incoming_content_digest,
                        "supplemental_record_id": conflict_id,
                        "supplemental_preserved": supplemental_written,
                        "supplemental_receipt": supplemental_receipt,
                        "supplemental_error": supplemental_error,
                        "reason": conflict_reason if not supplemental_error else f"{conflict_reason}; supplemental preservation error: {supplemental_error}",
                    }
                    receipts.append(conflict_receipt)
                    conflicts.append(conflict_receipt)
                    failed_ids.append(rec_id)
                    errors[rec_id] = conflict_receipt["reason"]
                    if supplemental_error:
                        errors[f"{rec_id}_supplemental_preservation"] = supplemental_error
                    continue
                else:
                    # Duplicate occurrences on disk share identical domain content
                    first_stored = all_records_by_id[rec_id][0]["data"]
                    is_match, mismatches = _check_emergency_record_match(rec, first_stored)
                    if is_match:
                        # Conservative recovery policy: do not write uncontrolled duplicate lines
                        stored_digest = first_stored.get("record_digest", "")
                        receipt = {
                            "audit_path": str(target),
                            "record_digest": stored_digest,
                            "record_id": rec_id,
                            "run_id": first_stored.get("run_id", str((rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", "")))),
                            "storage_category": "errors",
                            "emergency": True,
                            "durability": "confirmed",
                            "persisted": True,
                            "timestamp": first_stored.get("timestamp", str((rec.get("timestamp") if isinstance(rec, dict) else getattr(rec, "timestamp", "")))),
                            "duplicate_detected": True,
                            "duplicate_line_count": len(all_records_by_id[rec_id]),
                            "note": "already_persisted_with_duplicate_entries",
                        }
                        receipts.append(receipt)
                        continue

            existing_entry = records_by_id.get(rec_id)
            if existing_entry is not None:
                # Existing record found: validate content integrity & domain match
                is_match, mismatches = _check_emergency_record_match(rec, existing_entry)
                if is_match:
                    stored_digest = existing_entry.get("record_digest", "")
                    receipt = {
                        "audit_path": str(target),
                        "record_digest": stored_digest,
                        "record_id": rec_id,
                        "run_id": existing_entry.get("run_id", str((rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", "")))),
                        "storage_category": "errors",
                        "emergency": True,
                        "durability": "confirmed",
                        "persisted": True,
                        "timestamp": existing_entry.get("timestamp", str((rec.get("timestamp") if isinstance(rec, dict) else getattr(rec, "timestamp", "")))),
                        "note": "already_persisted_in_emergency_verified",
                    }
                    receipts.append(receipt)
                    continue
                else:
                    # Identity / content conflict
                    conflict_reason = (
                        f"identity/content conflict: record_id '{rec_id}' exists with different content: "
                        f"{'; '.join(mismatches)}"
                    )
                    supplemental_rec = dict(record_to_dict(rec))
                    supplemental_rec["run_id"] = str((rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", "")) or existing_entry.get("run_id", ""))
                    term = _extract_terminal_outcome(rec, supplemental_rec)
                    supplemental_rec["terminal_outcome"] = term
                    supplemental_rec["artifact_path"] = str((rec.get("artifact_path") if isinstance(rec, dict) else getattr(rec, "artifact_path", "")) or supplemental_rec.get("artifact_path", "") or "")
                    supplemental_rec["artifact_reference"] = str((rec.get("artifact_reference") if isinstance(rec, dict) else getattr(rec, "artifact_reference", "")) or supplemental_rec.get("artifact_reference", "") or "")
                    supplemental_rec["execution_executed"] = bool((rec.get("execution_executed") if isinstance(rec, dict) else getattr(rec, "execution_executed", False)) or supplemental_rec.get("execution_executed", False))
                    supplemental_rec["proposal_action"] = str((rec.get("proposal_action") if isinstance(rec, dict) else getattr(rec, "proposal_action", "")) or supplemental_rec.get("proposal_action", "") or "")
                    supplemental_rec["proposal_hash"] = str((rec.get("proposal_hash") if isinstance(rec, dict) else getattr(rec, "proposal_hash", "")) or supplemental_rec.get("proposal_hash", "") or "")

                    incoming_content_digest = compute_record_digest(supplemental_rec)
                    conflict_id = f"{rec_id}-conflict-{incoming_content_digest[:12]}"
                    supplemental_rec["record_id"] = conflict_id
                    supplemental_rec["conflict_with_record_id"] = rec_id
                    supplemental_rec["conflict_type"] = "identity_content_mismatch"
                    supplemental_rec["conflicting_fields"] = list(mismatches)
                    supplemental_rec["original_stored_digest"] = existing_entry.get("record_digest", "")
                    supplemental_rec["conflicting_content_digest"] = incoming_content_digest

                    supplemental_written, supplemental_receipt, supplemental_error = self._preserve_supplemental_record(
                        conflict_id=conflict_id,
                        supplemental_rec=supplemental_rec,
                        conflict_reason=conflict_reason,
                        expected_conflict_with=rec_id,
                        expected_conflict_type="identity_content_mismatch",
                        expected_content_digest=incoming_content_digest,
                        records_by_id=records_by_id,
                        all_records_by_id=all_records_by_id,
                        duplicates=duplicates,
                        malformed_lines=malformed_lines,
                        target_path=target,
                    )

                    conflict_receipt = {
                        "audit_path": str(target),
                        "record_id": rec_id,
                        "run_id": str((rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", "")) or existing_entry.get("run_id", "")),
                        "storage_category": "errors",
                        "emergency": True,
                        "durability": "failed",
                        "persisted": False,
                        "conflict": True,
                        "conflict_fields": list(mismatches),
                        "stored_digest": existing_entry.get("record_digest", ""),
                        "incoming_digest": incoming_content_digest,
                        "supplemental_record_id": conflict_id,
                        "supplemental_preserved": supplemental_written,
                        "supplemental_receipt": supplemental_receipt,
                        "supplemental_error": supplemental_error,
                        "reason": conflict_reason if not supplemental_error else f"{conflict_reason}; supplemental preservation error: {supplemental_error}",
                    }
                    receipts.append(conflict_receipt)
                    conflicts.append(conflict_receipt)
                    failed_ids.append(rec_id)
                    errors[rec_id] = conflict_receipt["reason"]
                    if supplemental_error:
                        errors[f"{rec_id}_supplemental_preservation"] = supplemental_error
                    continue

            # Record not in records_by_id: check for collision with malformed or unverifiable entry on disk
            corrupt_matches = [m for m in malformed_lines if m.get("record_id") == rec_id]
            if corrupt_matches:
                corrupt_entry = corrupt_matches[0]
                collision_reason = (
                    f"unresolved collision with unverifiable journal entry for record_id '{rec_id}': "
                    f"line {corrupt_entry.get('line_no')}: {corrupt_entry.get('error')}"
                )
                supplemental_rec = dict(record_to_dict(rec))
                supplemental_rec["run_id"] = str(
                    (rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", ""))
                    or corrupt_entry.get("data", {}).get("run_id", "")
                )
                term = _extract_terminal_outcome(rec, supplemental_rec)
                supplemental_rec["terminal_outcome"] = term
                supplemental_rec["artifact_path"] = str((rec.get("artifact_path") if isinstance(rec, dict) else getattr(rec, "artifact_path", "")) or supplemental_rec.get("artifact_path", "") or "")
                supplemental_rec["artifact_reference"] = str((rec.get("artifact_reference") if isinstance(rec, dict) else getattr(rec, "artifact_reference", "")) or supplemental_rec.get("artifact_reference", "") or "")
                supplemental_rec["execution_executed"] = bool((rec.get("execution_executed") if isinstance(rec, dict) else getattr(rec, "execution_executed", False)) or supplemental_rec.get("execution_executed", False))
                supplemental_rec["proposal_action"] = str((rec.get("proposal_action") if isinstance(rec, dict) else getattr(rec, "proposal_action", "")) or supplemental_rec.get("proposal_action", "") or "")
                supplemental_rec["proposal_hash"] = str((rec.get("proposal_hash") if isinstance(rec, dict) else getattr(rec, "proposal_hash", "")) or supplemental_rec.get("proposal_hash", "") or "")

                incoming_content_digest = compute_record_digest(supplemental_rec)
                conflict_id = f"{rec_id}-corrupt-collision-{incoming_content_digest[:12]}"
                supplemental_rec["record_id"] = conflict_id
                supplemental_rec["conflict_with_record_id"] = rec_id
                supplemental_rec["conflict_type"] = "unverifiable_record_collision"
                supplemental_rec["conflicting_fields"] = [f"corrupt journal line {corrupt_entry.get('line_no')}: {corrupt_entry.get('error')}"]
                supplemental_rec["original_stored_digest"] = ""
                supplemental_rec["conflicting_content_digest"] = incoming_content_digest

                supplemental_written, supplemental_receipt, supplemental_error = self._preserve_supplemental_record(
                    conflict_id=conflict_id,
                    supplemental_rec=supplemental_rec,
                    conflict_reason=collision_reason,
                    expected_conflict_with=rec_id,
                    expected_conflict_type="unverifiable_record_collision",
                    expected_content_digest=incoming_content_digest,
                    records_by_id=records_by_id,
                    all_records_by_id=all_records_by_id,
                    duplicates=duplicates,
                    malformed_lines=malformed_lines,
                    target_path=target,
                )

                collision_receipt = {
                    "audit_path": str(target),
                    "record_id": rec_id,
                    "run_id": str((rec.get("run_id") if isinstance(rec, dict) else getattr(rec, "run_id", "")) or corrupt_entry.get("data", {}).get("run_id", "")),
                    "storage_category": "errors",
                    "emergency": True,
                    "durability": "failed",
                    "persisted": False,
                    "conflict": True,
                    "corrupt_collision": True,
                    "corrupt_line_no": corrupt_entry.get("line_no"),
                    "corrupt_error": corrupt_entry.get("error"),
                    "supplemental_record_id": conflict_id,
                    "supplemental_preserved": supplemental_written,
                    "supplemental_receipt": supplemental_receipt,
                    "supplemental_error": supplemental_error,
                    "reason": collision_reason if not supplemental_error else f"{collision_reason}; supplemental: {supplemental_error}",
                }
                receipts.append(collision_receipt)
                conflicts.append(collision_receipt)
                failed_ids.append(rec_id)
                errors[rec_id] = collision_receipt["reason"]
                if supplemental_error:
                    errors[f"{rec_id}_supplemental_preservation"] = supplemental_error
                continue


            # Record not yet in emergency log: append fresh emergency entry
            if rec_id == failing_record_id:
                reason = failure_reason or "primary audit append failed"
            else:
                reason = f"run_history_preserved: primary persistence failure on {failing_record_id or 'run'}: {failure_reason}"

            try:
                receipt = self.append_emergency(rec, failure_reason=reason)
                is_valid, v_err = validate_persistence_receipt(
                    receipt, require_emergency=True, expected_record_id=rec_id
                )
                if is_valid:
                    records_by_id[rec_id] = record_to_dict(rec)
                    records_by_id[rec_id]["record_digest"] = receipt.get("record_digest", "")
                    receipts.append(receipt)
                else:
                    err = f"emergency append returned invalid receipt: {v_err}"
                    fail_rec = {
                        "record_id": rec_id,
                        "run_id": str(getattr(rec, "run_id", "") or ""),
                        "storage_category": "errors",
                        "emergency": True,
                        "durability": "failed",
                        "persisted": False,
                        "reason": err,
                        "raw_receipt": receipt,
                    }
                    receipts.append(fail_rec)
                    failed_ids.append(rec_id)
                    errors[rec_id] = err
            except Exception as exc:
                err = f"emergency append failed: {exc}"
                fail_rec = {
                    "record_id": rec_id,
                    "run_id": str(getattr(rec, "run_id", "") or ""),
                    "storage_category": "errors",
                    "emergency": True,
                    "durability": "failed",
                    "persisted": False,
                    "reason": err,
                }
                receipts.append(fail_rec)
                failed_ids.append(rec_id)
                errors[rec_id] = err

        persisted_count = sum(
            1 for r in receipts
            if r.get("persisted") is True and r.get("durability") == "confirmed"
        )
        all_ok = (
            (persisted_count == len(records_list))
            and (len(failed_ids) == 0)
            and (len(malformed_lines) == 0)
            and (len(duplicates) == 0)
            and (len(conflicts) == 0)
            and (len(errors) == 0)
        )

        return EmergencyRecoveryResult(
            all_ok=all_ok,
            total_required=len(records_list),
            persisted_count=persisted_count,
            receipts=receipts,
            failed_record_ids=failed_ids,
            conflicts=conflicts,
            errors=errors,
            duplicates=duplicates,
        )


def validate_persistence_receipt(
    receipt: object,
    require_emergency: bool = False,
    expected_record_id: str | None = None,
) -> tuple[bool, str]:
    """Validate persistence receipt structure and durability guarantees fail-closed.

    A receipt is valid if and only if:
    1. It is a non-empty dictionary.
    2. 'persisted' is an explicit boolean True (not missing, not truthy int/str, not None, not False).
    3. Required fields exist and are non-empty: 'audit_path', 'record_id'.
    4. Exact record identity: if expected_record_id is supplied, receipt['record_id']
       must strictly match expected_record_id.
    5. Durability is present, recognized, and verified:
       - Missing or None durability fails closed.
       - For emergency writes (require_emergency=True), durability must be 'confirmed', and emergency must be True.
       - For primary writes, durability must be 'written' or 'confirmed'.
       - 'failed', 'unconfigured', 'unconfirmed', or any unknown durability fails closed.
    6. No contradictory fields (e.g. persisted=True with durability='failed', or require_emergency=True with emergency=False).

    Returns (is_valid, failure_reason).
    """
    if not isinstance(receipt, dict):
        return False, f"receipt is not a dictionary (got {type(receipt).__name__})"
    if not receipt:
        return False, "receipt is an empty dictionary"

    if "persisted" not in receipt:
        return False, "missing 'persisted' in receipt"
    persisted = receipt["persisted"]
    if not isinstance(persisted, bool):
        return False, f"'persisted' must be a boolean (got {type(persisted).__name__})"
    if persisted is not True:
        reason = receipt.get("reason") or receipt.get("error") or "receipt reports persisted=False"
        return False, str(reason)

    if "durability" not in receipt:
        return False, "missing 'durability' in receipt"
    durability = receipt.get("durability")
    if not isinstance(durability, str) or not durability.strip():
        return False, "missing or empty 'durability' in receipt"

    if durability in ("failed", "unconfigured", "unconfirmed"):
        return False, f"unconfirmed or failed durability: {durability!r}"

    if require_emergency:
        if durability != "confirmed":
            return False, f"emergency durability must be 'confirmed', got {durability!r}"
        if receipt.get("emergency") is not True:
            return False, "emergency receipt missing emergency=True marker"
    else:
        if durability not in ("written", "confirmed"):
            return False, f"invalid or unconfirmed durability: {durability!r}"

    if receipt.get("mode") in ("failed", "unconfigured", "unconfirmed"):
        return False, f"contradictory mode in receipt: {receipt.get('mode')!r}"

    audit_path = receipt.get("audit_path")
    if not isinstance(audit_path, str) or not audit_path.strip():
        return False, "missing or empty 'audit_path' in receipt"

    record_id = receipt.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        return False, "missing or empty 'record_id' in receipt"

    if expected_record_id is not None and str(expected_record_id).strip():
        exp_id = str(expected_record_id).strip()
        if record_id != exp_id:
            return False, f"record_id mismatch: receipt has {record_id!r}, expected {exp_id!r}"

    run_id = receipt.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        return False, "invalid or empty 'run_id' in receipt"

    return True, ""


def persist_record_resilient(writer, record, receipts=None, history=None) -> dict:
    """Persist one record with primary append and emergency journal fallback.

    Durability and Receipt Semantics:
    - persisted=True: The audit record was successfully written to the log.
    - persisted=False: Persistence was not achieved (unconfigured writer, or
      both primary and emergency persistence failed). Caller must initiate
      quarantine for executed staging artifacts.
    - durability="written": Standard buffered file write confirming data was written
      to the OS file stream without error (primary append). Does not execute
      hardware-level disk synchronization (fsync).
    - durability="confirmed": Emergency append flushed and synchronized via os.fsync(fileno).
      Note on crash limits: Synchronizes file content and metadata to storage controller,
      but does not provide unconditional guarantees against physical power loss (as POSIX
      directory entry durability requires parent directory fsync, which is unsupported on Windows).
    - durability="unconfirmed": Emergency append reported success but did not certify
      fsync-level hardware confirmation.
    - durability="failed": Neither primary nor emergency persistence succeeded.
    - durability="unconfigured": No AuditWriter configured.

    Recovery Contract:
    - When primary persistence of `record` fails, all audit records in `history` collected
      for the affected run up to that point are preserved into `errors/audit_emergency.jsonl`
      while keeping earlier primary records in passed/ or failed/ intact.
    """
    rec_id = getattr(record, "record_id", "")
    if writer is None:
        entry = {
            "record_id": rec_id,
            "persisted": False,
            "emergency": False,
            "durability": "unconfigured",
            "mode": "unconfigured",
            "reason": "no audit writer configured",
        }
        if receipts is not None:
            receipts.append(entry)
        return entry

    primary_reason = ""
    raw_receipt = None
    try:
        raw_receipt = writer.append(record)
        is_valid, invalid_reason = validate_persistence_receipt(
            raw_receipt,
            require_emergency=False,
            expected_record_id=rec_id or None,
        )
        if is_valid:
            entry = dict(raw_receipt)
            entry["persisted"] = True
            entry["emergency"] = False
            entry["durability"] = entry.get("durability", "written")
            if receipts is not None:
                receipts.append(entry)
            return entry
        else:
            primary_reason = f"invalid or unconfirmed primary receipt: {invalid_reason}"
    except Exception as primary_exc:
        primary_reason = str(primary_exc) or "primary audit persistence failed"

    # Primary persistence failed: preserve complete run history up to failure in errors partition
    records_to_recover = list(history) if history is not None else [record]
    if not any(getattr(r, "record_id", None) == rec_id for r in records_to_recover):
        records_to_recover.append(record)

    emergency_reason = ""
    raw_emerg = None
    recovery_all_ok = False
    failed_record_ids: list[str] = []
    history_receipts: list[dict] = []
    conflicts: list[dict] = []

    if hasattr(writer, "append_emergency_history"):
        try:
            recovery_result = writer.append_emergency_history(
                records_to_recover,
                failure_reason=primary_reason,
                failing_record_id=rec_id,
            )
            history_receipts = list(recovery_result)
            raw_emerg = next((r for r in history_receipts if r.get("record_id") == rec_id), None)
            if raw_emerg is None and history_receipts:
                raw_emerg = history_receipts[-1]

            conflicts = list(getattr(recovery_result, "conflicts", []))

            # 1. Inspect overall recovery status
            if hasattr(recovery_result, "all_ok"):
                recovery_all_ok = bool(recovery_result.all_ok)
                failed_record_ids = list(getattr(recovery_result, "failed_record_ids", []))
            else:
                failed_record_ids = [
                    r.get("record_id", "unknown") for r in history_receipts
                    if not (isinstance(r, dict) and r.get("persisted") is True and r.get("durability") == "confirmed")
                ]
                recovery_all_ok = len(failed_record_ids) == 0 and len(history_receipts) >= len(records_to_recover)

            # 2. Strict independent verification of EVERY receipt in history_receipts
            for required_rec in records_to_recover:
                req_id = str((required_rec.get("record_id") if isinstance(required_rec, dict) else getattr(required_rec, "record_id", "")) or "").strip()
                matching_receipt = next((r for r in history_receipts if r.get("record_id") == req_id), None)
                if matching_receipt is None:
                    recovery_all_ok = False
                    if req_id not in failed_record_ids:
                        failed_record_ids.append(req_id)
                else:
                    is_valid, v_err = validate_persistence_receipt(
                        matching_receipt,
                        require_emergency=True,
                        expected_record_id=req_id or None,
                    )
                    if not is_valid:
                        recovery_all_ok = False
                        if req_id not in failed_record_ids:
                            failed_record_ids.append(req_id)

            if not recovery_all_ok:
                err_details = ""
                if hasattr(recovery_result, "errors") and recovery_result.errors:
                    err_details = "; ".join(f"{k}: {v}" for k, v in recovery_result.errors.items())
                elif history_receipts:
                    reasons = [r.get("reason", "") for r in history_receipts if not r.get("persisted") and r.get("reason")]
                    if reasons:
                        err_details = "; ".join(reasons)
                if err_details:
                    emergency_reason = f"incomplete history recovery (failed records {failed_record_ids}): {err_details}"
                else:
                    emergency_reason = f"incomplete history recovery: failed to recover required records {failed_record_ids}"
        except Exception as exc:
            recovery_all_ok = False
            emergency_reason = f"emergency history recovery exception: {exc}"
    elif hasattr(writer, "append_emergency") or hasattr(type(writer), "append_emergency"):
        try:
            for h_rec in records_to_recover:
                h_id = str((h_rec.get("record_id") if isinstance(h_rec, dict) else getattr(h_rec, "record_id", "")) or "").strip()
                h_receipt = None
                if h_id == rec_id:
                    try:
                        h_receipt = writer.append_emergency(record, failure_reason=primary_reason)
                    except Exception as h_exc:
                        h_receipt = {
                            "record_id": h_id,
                            "persisted": False,
                            "durability": "failed",
                            "reason": str(h_exc),
                        }
                    raw_emerg = h_receipt
                else:
                    try:
                        h_receipt = writer.append_emergency(
                            h_rec,
                            failure_reason=f"run_history_preserved: primary persistence failure on {rec_id}: {primary_reason}",
                        )
                    except Exception as h_exc:
                        h_receipt = {
                            "record_id": h_id,
                            "persisted": False,
                            "durability": "failed",
                            "reason": str(h_exc),
                        }

                is_valid_rec, rec_err = validate_persistence_receipt(
                    h_receipt,
                    require_emergency=True,
                    expected_record_id=h_id or None,
                )
                if not is_valid_rec:
                    if h_id not in failed_record_ids:
                        failed_record_ids.append(h_id or "unknown_record")
                    if isinstance(h_receipt, dict):
                        h_receipt["persisted"] = False
                        if h_receipt.get("durability") != "unconfirmed":
                            h_receipt["durability"] = "failed"
                        h_receipt["validation_error"] = rec_err
                    else:
                        h_receipt = {
                            "record_id": h_id,
                            "persisted": False,
                            "durability": "failed",
                            "reason": rec_err,
                        }
                history_receipts.append(h_receipt)

            recovery_all_ok = len(failed_record_ids) == 0 and len(history_receipts) == len(records_to_recover)
            if not recovery_all_ok:
                reasons = [
                    r.get("validation_error") or r.get("reason", "")
                    for r in history_receipts
                    if isinstance(r, dict) and not r.get("persisted") and (r.get("validation_error") or r.get("reason"))
                ]
                err_details = "; ".join(reasons) if reasons else ""
                if err_details:
                    emergency_reason = f"incomplete history recovery (failed records {failed_record_ids}): {err_details}"
                else:
                    emergency_reason = f"incomplete history recovery: failed to recover required records {failed_record_ids}"
        except Exception as emergency_exc:
            recovery_all_ok = False
            emergency_reason = str(emergency_exc) or "emergency audit persistence failed"

    else:
        recovery_all_ok = False
        emergency_reason = "emergency journal not supported by writer"

    emerg_durability = raw_emerg.get("durability") if isinstance(raw_emerg, dict) else None

    # Only report overall success if the full recovery succeeded AND raw_emerg is confirmed valid
    if raw_emerg is not None and recovery_all_ok:
        is_emerg_valid, emerg_invalid = validate_persistence_receipt(
            raw_emerg,
            require_emergency=True,
            expected_record_id=rec_id or None,
        )
        if is_emerg_valid:
            entry = dict(raw_emerg)
            entry["persisted"] = True
            entry["emergency"] = True
            entry["durability"] = "confirmed"
            entry["primary_error"] = primary_reason
            entry["recovery_all_ok"] = True
            entry["history_receipts"] = history_receipts
            if receipts is not None:
                receipts.append(entry)
            return entry
        else:
            emergency_reason = f"invalid or unconfirmed emergency receipt for record {rec_id}: {emerg_invalid}"

    if emerg_durability == "unconfirmed":
        entry = {
            "record_id": rec_id,
            "persisted": False,
            "emergency": True,
            "mode": "unconfirmed",
            "durability": "unconfirmed",
            "primary_error": primary_reason,
            "emergency_error": "emergency append unconfirmed",
            "reason": f"primary failed: {primary_reason}; emergency unconfirmed",
            "raw_receipt": raw_receipt,
            "raw_emergency_receipt": raw_emerg,
            "recovery_all_ok": False,
            "history_receipts": history_receipts,
            "failed_record_ids": failed_record_ids,
        }
        if receipts is not None:
            receipts.append(entry)
        return entry

    entry = {
        "record_id": rec_id,
        "persisted": False,
        "emergency": True,
        "mode": "failed",
        "durability": "failed",
        "primary_error": primary_reason,
        "emergency_error": emergency_reason,
        "reason": f"primary failed: {primary_reason}; recovery incomplete: {emergency_reason}",
        "raw_receipt": raw_receipt,
        "raw_emergency_receipt": (
            raw_emerg.get("raw_receipt")
            if isinstance(raw_emerg, dict) and "raw_receipt" in raw_emerg
            else raw_emerg
        ),
        "recovery_all_ok": False,
        "history_receipts": history_receipts,
        "failed_record_ids": failed_record_ids,
        "conflicts": conflicts,
    }
    if receipts is not None:
        receipts.append(entry)
    return entry


def persist_record(writer, record, receipts=None) -> bool:
    """Persist one record through ``writer``; append a receipt if asked.

    Returns ``True`` when persistence succeeded. If no writer is configured,
    or if writer reports failure/failed durability, returns ``False`` (fails closed).
    """
    rec_id = getattr(record, "record_id", "")
    if writer is None:
        if receipts is not None:
            receipts.append({
                "record_id": rec_id,
                "persisted": False,
                "emergency": False,
                "durability": "unconfigured",
                "mode": "unconfigured",
                "reason": "no audit writer configured",
            })
        return False
    try:
        raw_receipt = writer.append(record)
    except Exception as exc:
        if receipts is not None:
            receipts.append({
                "record_id": rec_id,
                "persisted": False,
                "durability": "failed",
                "reason": str(exc) or "audit persistence failed",
            })
        return False

    is_valid, reason = validate_persistence_receipt(
        raw_receipt,
        require_emergency=False,
        expected_record_id=rec_id or None,
    )
    if not is_valid:
        entry = dict(raw_receipt) if isinstance(raw_receipt, dict) else {}
        entry["expected_record_id"] = rec_id
        entry["persisted"] = False
        entry["durability"] = "failed"
        entry["reason"] = f"invalid receipt: {reason}"
        if receipts is not None:
            receipts.append(entry)
        return False

    entry = dict(raw_receipt)
    entry["persisted"] = True
    entry.setdefault("emergency", False)
    entry.setdefault("durability", "written")
    if receipts is not None:
        receipts.append(entry)
    return True


__all__ = ("AUDIT_LOG_FILENAME", "EMERGENCY_LOG_FILENAME", "PASSED_SUBDIR",
           "FAILED_SUBDIR", "ERRORS_SUBDIR", "AuditWriter", "AuditRecord",
           "audit_storage_category", "persist_record", "persist_record_resilient",
           "validate_persistence_receipt", "record_digest", "record_to_dict")
