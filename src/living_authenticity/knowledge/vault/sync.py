"""Authorized Vault-to-index synchronization engine (Process A).

Maintains a dedicated persistent vector index inside the configured database-storage
area for an explicitly authorized Vault.

Key architectural boundaries:
- Synchronization starts ONLY after explicit user authorization.
- One authorized Vault -> its own persistent vector database/index -> synchronization.
- Different Vaults never share or contaminate persistent index tables or directories.
- The persistent index is derived data; the authoritative Vault remains the source of truth.
- The Vault is strictly read-only: synchronization NEVER writes to or mutates the Vault.
- Staging, Audit, and Vector DB directories are strictly excluded from corpus ingestion.
- Continuous change detection is strictly constrained to the authorized Vault root.
"""
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from src.living_authenticity.database.lancedb_manager import (
    DatabaseLockError,
    DatabaseOperationError,
    LanceDBManager,
    VaultMetadataCorruptionError,
    VaultMetadataError,
    VaultMetadataMissingError,
)
from src.living_authenticity.knowledge.vault.context import (
    diff_manifests,
    snapshot_vault,
)
from src.living_authenticity.knowledge.vault.identity import (
    compute_vault_id,
    resolve_vault_index_dir,
)
from src.living_authenticity.security.path_boundary import PathBoundary

logger = logging.getLogger(__name__)

# Canonical synchronization lifecycle states
SYNC_STATE_UNAUTHORIZED = "unauthorized"
SYNC_STATE_AUTHORIZING = "authorizing"
SYNC_STATE_ACTIVE = "sync_active"
SYNC_STATE_CHANGE_DETECTED = "change_detected"
SYNC_STATE_IN_PROGRESS = "sync_in_progress"
SYNC_STATE_PENDING_APPROVAL = "pending_approval"
SYNC_STATE_APPROVED = "approved"
SYNC_STATE_REJECTED = "rejected"
SYNC_STATE_FRESH = "fresh"
SYNC_STATE_STALE = "stale"
SYNC_STATE_STALE_APPROVAL = "stale_approval"
SYNC_STATE_FAILED = "error"
SYNC_STATE_REVOKED = "revoked"


class VaultSyncError(Exception):
    """Raised when vault synchronization encounters a critical error."""


class VaultSyncSchemaResolutionError(VaultSyncError):
    """Raised when canonical Core schema resolution fails during vault synchronization."""


class VaultSyncUnauthorizedError(VaultSyncError):
    """Raised when reconciliation is attempted without explicit human approval."""


class VaultSyncConcurrencyError(VaultSyncError):
    """Raised when database lock contention or concurrent synchronization conflicts occur (retained for backward compatibility)."""


class VaultSyncSnapshotDriftError(VaultSyncError):
    """Raised when vault files mutate while reconciliation is reading or processing them."""


class VaultSyncStaleApprovalError(VaultSyncError):
    """Raised when an approval ticket is stale, expired, or references an altered snapshot."""


class VaultSyncRollbackError(VaultSyncError):
    """Raised when initial synchronization fails and subsequent store cleanup/rollback also fails."""

    def __init__(
        self,
        message: str,
        original_error: BaseException | None = None,
        cleanup_errors: list[str] | None = None,
        store_state: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.original_error = original_error
        self.cleanup_errors = list(cleanup_errors or [])
        self.store_state = dict(store_state or {})


def compute_snapshot_fingerprint(snapshot: dict) -> str:
    """Return a deterministic SHA256 fingerprint for a vault snapshot."""
    entries = snapshot.get("entries", {}) if isinstance(snapshot, dict) else {}
    canonical = [
        {
            "path": k.replace("\\", "/"),
            "sha256": entries[k].get("sha256", ""),
            "size": entries[k].get("size", 0),
        }
        for k in sorted(entries.keys())
    ]
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_diff_fingerprint(diff: dict) -> str:
    """Return a deterministic SHA256 fingerprint for a manifest diff."""
    canonical = {
        "added": sorted([p.replace("\\", "/") for p in diff.get("added", [])]),
        "modified": sorted([p.replace("\\", "/") for p in diff.get("modified", [])]),
        "removed": sorted([p.replace("\\", "/") for p in diff.get("removed", [])]),
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()





# Private capability sentinels and trust verifiers imported from authoritative core_validator
from src.living_authenticity.knowledge.vault.core_validator import (
    _SESSION_PROOF,
    _TICKET_PROOF,
    is_trusted_approval_ticket,
    is_trusted_vault_sync_session,
)


class VaultSyncApprovalTicket:
    """One-time approval ticket bound to snapshot and Core fingerprint required for persistent reconciliation."""

    def __init__(
        self,
        ticket_id: str,
        vault_id: str,
        vault_root: str,
        run_id: str,
        snapshot_fingerprint: str,
        core_hash: str,
        diff_fingerprint: str,
        expires_at: float,
        session_id: str = "",
        approval_id: str = "",
        operation_type: str = "incremental_sync",
        database_baseline_version: Any = "",
        decision: str = "approve",
    ) -> None:
        self.ticket_id = ticket_id
        self.vault_id = vault_id
        self.vault_root = vault_root
        self.run_id = run_id
        self.snapshot_fingerprint = snapshot_fingerprint
        self.core_hash = core_hash
        self.diff_fingerprint = diff_fingerprint
        self.created_at = time.time()
        self.expires_at = expires_at
        self.session_id = session_id
        self.approval_id = approval_id
        self.operation_type = operation_type
        self.database_baseline_version = database_baseline_version
        self.decision = decision
        self._ticket_proof: Any = None
        self._authorization_issued = False
        self._issuance_in_progress = False
        self.used = False
        self._consumed = False
        self._lock = threading.RLock()

    @classmethod
    def _issue_approved_ticket(
        cls,
        *,
        ticket_id: str,
        vault_id: str,
        vault_root: str,
        run_id: str,
        snapshot_fingerprint: str,
        core_hash: str,
        diff_fingerprint: str,
        expires_at: float,
        session_id: str = "",
        approval_id: str = "",
        operation_type: str = "incremental_sync",
        database_baseline_version: Any = "",
        session_proof: Any = None,
        issuance_nonce: Any = None,
        session: Any = None,
    ) -> "VaultSyncApprovalTicket":
        """Private factory used strictly by VaultSyncSession to issue authentic approval tickets."""
        if session_proof is not _SESSION_PROOF:
            raise ValueError("Cannot issue approved ticket without authentic session proof")
        if session is None:
            raise ValueError("Cannot issue approved ticket without authentic session")
        s_lock = getattr(session, "_lock", None)
        lock_ctx = s_lock if s_lock is not None else threading.Lock()
        with lock_ctx:
            active_nonce = getattr(session, "_current_issuance_nonce", None)
            if active_nonce is not issuance_nonce or issuance_nonce is None:
                raise ValueError("Cannot issue approved ticket outside active resolve_approval execution")
            # Atomically consume the issuance nonce so it can never be used again for another ticket
            session._current_issuance_nonce = None
        ticket = cls(
            ticket_id=ticket_id,
            vault_id=vault_id,
            vault_root=vault_root,
            run_id=run_id,
            snapshot_fingerprint=snapshot_fingerprint,
            core_hash=core_hash,
            diff_fingerprint=diff_fingerprint,
            expires_at=expires_at,
            session_id=session_id,
            approval_id=approval_id,
            operation_type=operation_type,
            database_baseline_version=database_baseline_version,
            decision="approve",
        )
        ticket._ticket_proof = _TICKET_PROOF
        return ticket

    def mark_used(self) -> bool:
        with self._lock:
            if self.used:
                return False
            self.used = True
            return True

    def mark_consumed(self) -> bool:
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True

    @property
    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def is_valid(self, now: float | None = None) -> bool:
        with self._lock:
            t = now if now is not None else time.time()
            return (not self.used) and (not self._consumed) and (t < self.expires_at)





class _PendingSyncApprovalRecord:
    """Internal pending approval state for an uncommitted vault change set."""

    def __init__(
        self,
        approval_id: str,
        vault_id: str,
        vault_root: str,
        run_id: str,
        diff: dict,
        snapshot_fingerprint: str,
        core_hash: str,
        diff_fingerprint: str,
        manifest: dict,
        expires_at: float,
        session_id: str = "",
        operation_type: str = "incremental_sync",
    ) -> None:
        self.approval_id = approval_id
        self.vault_id = vault_id
        self.vault_root = vault_root
        self.run_id = run_id
        self.diff = diff
        self.snapshot_fingerprint = snapshot_fingerprint
        self.core_hash = core_hash
        self.diff_fingerprint = diff_fingerprint
        self.manifest = manifest
        self.created_at = time.time()
        self.expires_at = expires_at
        self.session_id = session_id
        self.operation_type = operation_type
        self.state = SYNC_STATE_PENDING_APPROVAL
        self._lock = threading.Lock()
        self._event = threading.Event()

    def is_expired(self, now: float | None = None) -> bool:
        t = now if now is not None else time.time()
        return t >= self.expires_at


@runtime_checkable
class BatchEmbeddingService(Protocol):
    """Protocol defining the minimal batch embedding capability required for synchronization."""

    def embed_batch(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        ...


class VaultSyncSession:
    """Manages the authorized synchronization lifecycle for one Vault."""

    def __init__(self, stage_observer: Callable[[dict], None] | None = None) -> None:
        self._lock = threading.RLock()
        self._stage_observer = stage_observer
        self._authorized = False
        self._vault_root: str = ""
        self._vector_db_root: str = ""
        self._staging_root: str = ""
        self._audit_root: str = ""
        self._vault_id: str = ""
        self._index_dir: Path | None = None
        self._status: str = SYNC_STATE_UNAUTHORIZED
        self._last_synced_at: str = ""
        self._error_message: str | None = None
        self._indexed_notes: int = 0
        self._indexed_vectors: int = 0
        self._manifest: dict = {}
        self._store: LanceDBManager | None = None
        self._embedder: Any = None
        self._pipeline: Any = None
        self._monitor: "VaultSyncMonitor | None" = None
        self._active_ticket: VaultSyncApprovalTicket | None = None
        self._pending_approval: _PendingSyncApprovalRecord | None = None
        self._initial_sync_active: bool = False
        self._session_proof: object = _SESSION_PROOF
        self._session_id: str = str(uuid.uuid4())
        self._initial_sync_privilege: Any = None
        self._current_reconciliation_ticket: VaultSyncApprovalTicket | None = None
        self._initial_sync_authorization_issued: bool = False
        self._initial_issuance_in_progress: bool = False
        self._current_issuance_nonce: Any = None
        self._issued_tickets: set[int] = set()
        self._approved_tickets_by_id: dict[str, Any] = {}
        self._approval_history: dict[str, dict[str, Any]] = {}

    def is_ticket_issued_by_session(self, ticket: Any) -> bool:
        """Verify whether an approval ticket was legitimately issued by this session instance."""
        if ticket is None:
            return False
        t_id = getattr(ticket, "ticket_id", None)
        if not t_id or not isinstance(t_id, str):
            return False
        with self._lock:
            if id(ticket) not in self._issued_tickets:
                return False
            registered = self._approved_tickets_by_id.get(t_id)
            if registered is not ticket:
                return False
            appr_id = getattr(ticket, "approval_id", None)
            if not appr_id or appr_id not in self._approval_history:
                return False
            rec = self._approval_history[appr_id]
            if rec.get("ticket_id") != t_id or rec.get("decision") != "approve":
                return False
            return True

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    @property
    def is_authorized(self) -> bool:
        with self._lock:
            return self._authorized

    @property
    def vault_id(self) -> str:
        with self._lock:
            return self._vault_id

    @property
    def vault_root(self) -> str:
        with self._lock:
            return self._vault_root

    @property
    def index_dir(self) -> Path | None:
        with self._lock:
            return self._index_dir

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def is_fresh(self) -> bool:
        with self._lock:
            return self._authorized and self._status == SYNC_STATE_FRESH

    def _emit(self, kind: str, stage: str, message: str, metadata: dict | None = None) -> None:
        if self._stage_observer is None:
            return
        payload: dict[str, Any] = {
            "kind": kind,
            "stage": stage,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            payload["metadata"] = metadata
        try:
            self._stage_observer(payload)
        except Exception:
            pass

    def _is_genuine_first_init(self) -> bool:
        """Verify whether this Vault is genuinely being initialized for the first time."""
        if self._store is None:
            return True
        state_info = self._store.inspect_store_state()
        state = state_info.get("state")
        if state == "error":
            raise VaultSyncError(
                f"Existing vector store is in error or unreadable state: {state_info.get('error', 'unknown error')}. "
                "Initial sync privilege denied. Explicit recovery or store reset required."
            )
        if state == "genuine_first_init":
            return True
        if state in ("missing_metadata", "corrupted_metadata", "uninitialized_empty", "inconsistent_initialized"):
            raise VaultSyncError(
                f"Existing vector store has invalid synchronization metadata state '{state}'. "
                "Initial sync privilege denied. Explicit recovery or store reset required."
            )
        return False

    def _rollback_initial_sync(self) -> tuple[bool, list[str], dict]:
        """Attempt to drop/clear created tables after initial sync failure and verify genuinely empty state.

        Returns (verified_clean, cleanup_errors, store_state).
        """
        cleanup_errors: list[str] = []
        if self._store is None:
            return True, [], {}

        # 1. Clean up index_metadata table
        try:
            table_names = self._store._table_names()
        except Exception as exc:
            cleanup_errors.append(f"Failed inspecting store tables: {exc}")
            table_names = set()

        if "index_metadata" in table_names:
            try:
                self._store.drop_table("index_metadata")
            except Exception as drop_exc:
                cleanup_errors.append(f"Primary drop of index_metadata failed: {drop_exc}")
                # Fallback: delete rows
                try:
                    tbl = self._store.db.open_table("index_metadata")
                    tbl.delete("id IS NOT NULL")
                except Exception as del_exc:
                    cleanup_errors.append(f"Fallback deletion of index_metadata rows failed: {del_exc}")

        # 1b. Clean up store lifecycle marker table if present
        if "_store_lifecycle" in table_names:
            try:
                self._store.drop_table("_store_lifecycle")
            except Exception as drop_exc:
                cleanup_errors.append(f"Primary drop of _store_lifecycle failed: {drop_exc}")

        # 2. Clean up knowledge_vectors table
        if "knowledge_vectors" in table_names:
            try:
                self._store.drop_table("knowledge_vectors")
            except Exception as drop_exc:
                cleanup_errors.append(f"Primary drop of knowledge_vectors failed: {drop_exc}")
                # Fallback: delete rows
                try:
                    self._store.delete_all()
                except Exception as del_exc:
                    cleanup_errors.append(f"Fallback deletion of knowledge_vectors rows failed: {del_exc}")

        # 3. Post-cleanup verification of genuinely empty store
        try:
            state_info = self._store.inspect_store_state()
            state = state_info.get("state")
            vector_count = int(state_info.get("vector_count", 0))
            has_meta = bool(state_info.get("has_metadata_table", False))
            verified_clean = bool(
                state == "genuine_first_init"
                and vector_count == 0
                and not has_meta
                and not cleanup_errors
            )
        except Exception as insp_exc:
            cleanup_errors.append(f"Post-cleanup verification failed: {insp_exc}")
            state_info = {"state": "error", "error": str(insp_exc)}
            verified_clean = False

        return verified_clean, cleanup_errors, state_info

    def authorize(
        self,
        vault_root: str,
        vector_db_root: str,
        embedder: BatchEmbeddingService | Any = None,
        model_path: str | None = None,
        pipeline: Any = None,
        staging_root: str = "",
        audit_root: str = "",
        auto_watch: bool = False,
    ) -> dict:
        """Explicitly authorize synchronization for one Vault."""
        with self._lock:
            # 1. Path and boundary checks
            if not isinstance(vault_root, str) or not vault_root.strip():
                raise VaultSyncError("vault_root must be a non-empty string")
            v_path = Path(vault_root).resolve()
            if not v_path.is_dir() or v_path.is_symlink():
                raise VaultSyncError("vault_root must be an existing non-symlink directory")

            if not isinstance(vector_db_root, str) or not vector_db_root.strip():
                raise VaultSyncError("vector_db_root must be a non-empty string")
            vdb_path = Path(vector_db_root).resolve()

            # Ensure Vault and Vector DB are disjoint
            if PathBoundary(str(v_path)).is_allowed(str(vdb_path)) or PathBoundary(str(vdb_path)).is_allowed(str(v_path)):
                raise VaultSyncError("vector_db_root and vault_root must be mutually disjoint")

            # 2. Stop any existing monitor before switching or re-authorizing
            if self._monitor is not None:
                self._monitor.stop()
                self._monitor = None

            # 3. Compute distinct Vault identity and isolated persistent directory
            self._vault_root = str(v_path)
            self._vector_db_root = str(vdb_path)
            self._staging_root = staging_root
            self._audit_root = audit_root
            self._vault_id = compute_vault_id(self._vault_root)
            self._index_dir = resolve_vault_index_dir(self._vector_db_root, self._vault_root)

            try:
                # 4. Initialize dedicated persistent store
                self._store = LanceDBManager(db_path=str(self._index_dir))
                # Inspect store state prior to creating tables to accurately distinguish genuine_first_init
                is_first_init = self._is_genuine_first_init()
                self._store.create_knowledge_vector_table()

                # 5. Initialize embedder and pipeline
                if embedder is not None and hasattr(embedder, "embed_batch"):
                    self._embedder = embedder
                elif model_path:
                    from src.living_authenticity.embedding.service import EmbeddingService
                    self._embedder = EmbeddingService(model_path)
                else:
                    raise VaultSyncError("An embedder or valid model_path is required for vector synchronization")

                if pipeline is not None:
                    self._pipeline = pipeline
                else:
                    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
                    self._pipeline = EvidenceFirstPipeline()

                self._authorized = True
                self._status = SYNC_STATE_AUTHORIZING
                self._error_message = None

                self._emit(
                    kind="status",
                    stage="synchronization",
                    message=f"Vault synchronization authorized for {self._vault_id}",
                    metadata={"vault_id": self._vault_id, "status": self._status},
                )

                # 6. Genuine first init vs existing store handling
                was_empty_before_init = bool(is_first_init and self._store.inspect_store_state().get("vector_count", 0) == 0)
                if is_first_init:
                    self._initial_sync_privilege = self._session_proof if was_empty_before_init else None
                    self._initial_sync_active = True
                    self._initial_sync_authorization_issued = False
                    try:
                        sync_report = self.reconcile()
                    except Exception as exc:
                        if self._store is not None and was_empty_before_init:
                            verified_clean, cleanup_errors, state_info = self._rollback_initial_sync()
                            if not verified_clean:
                                self._status = SYNC_STATE_FAILED
                                self._authorized = False
                                err_msg = (
                                    f"Initial synchronization failed ({exc}), and rollback cleanup could not verify "
                                    f"empty store state: {'; '.join(cleanup_errors) or 'store remains in dirty/uncertain state'}"
                                )
                                self._error_message = err_msg
                                self._emit(
                                    kind="error",
                                    stage="synchronization",
                                    message=err_msg,
                                    metadata={
                                        "vault_id": self._vault_id,
                                        "original_error": str(exc),
                                        "cleanup_errors": cleanup_errors,
                                        "store_state": state_info,
                                        "rollback_verified": False,
                                        "recovery_state": "uncertain_store_state",
                                    },
                                )
                                raise VaultSyncRollbackError(
                                    err_msg,
                                    original_error=exc,
                                    cleanup_errors=cleanup_errors,
                                    store_state=state_info,
                                ) from exc
                            else:
                                self._status = SYNC_STATE_FAILED
                                self._authorized = False
                                self._error_message = str(exc)
                                self._emit(
                                    kind="warning",
                                    stage="synchronization",
                                    message=f"Initial synchronization failed ({exc}); store rollback verified clean.",
                                    metadata={
                                        "vault_id": self._vault_id,
                                        "original_error": str(exc),
                                        "rollback_verified": True,
                                    },
                                )
                        raise
                    finally:
                        self._initial_sync_active = False
                        self._initial_sync_privilege = None
                        self._initial_sync_authorization_issued = False

                    if not sync_report.get("fresh", False):
                        if self._store is not None and was_empty_before_init:
                            verified_clean, cleanup_errors, state_info = self._rollback_initial_sync()
                            if not verified_clean:
                                self._authorized = False
                                self._status = SYNC_STATE_FAILED
                                err_msg = (
                                    f"Initial reconciliation failed ({sync_report.get('error')}), and rollback cleanup "
                                    f"could not verify empty store state: {'; '.join(cleanup_errors) or 'store remains in dirty/uncertain state'}"
                                )
                                self._error_message = err_msg
                                sync_report["rollback"] = "failed"
                                sync_report["rollback_verified"] = False
                                sync_report["cleanup_errors"] = cleanup_errors
                                sync_report["store_state"] = state_info
                                sync_report["recovery_state"] = "uncertain_store_state"
                                self._emit(
                                    kind="error",
                                    stage="synchronization",
                                    message=err_msg,
                                    metadata={
                                        "vault_id": self._vault_id,
                                        "error": sync_report.get("error"),
                                        "cleanup_errors": cleanup_errors,
                                        "store_state": state_info,
                                        "rollback_verified": False,
                                        "recovery_state": "uncertain_store_state",
                                    },
                                )
                                return sync_report
                            else:
                                sync_report["rollback"] = "verified_clean"
                                sync_report["rollback_verified"] = True
                        self._authorized = False
                        self._status = SYNC_STATE_FAILED
                        self._error_message = sync_report.get("error") or "Initial reconciliation failed"
                        return sync_report
                else:
                    # Existing store with valid baseline metadata: inspect state without automatic reconciliation
                    changes = self.check_changes()
                    if changes.get("stale"):
                        self._status = SYNC_STATE_STALE
                    else:
                        self._status = SYNC_STATE_FRESH
                    sync_report = {
                        "authorized": True,
                        "vault_id": self._vault_id,
                        "status": self._status,
                        "fresh": self.is_fresh,
                        "indexed_notes": self._indexed_notes,
                        "indexed_vectors": self._indexed_vectors,
                        "last_synced_at": self._last_synced_at,
                    }

                # 7. Start continuous change detection monitor if requested (strictly observation-only)
                if auto_watch and self._status in (SYNC_STATE_FRESH, SYNC_STATE_ACTIVE):
                    self._monitor = VaultSyncMonitor(self)
                    self._monitor.start()

                return sync_report
            except Exception:
                self._authorized = False
                self._status = SYNC_STATE_FAILED
                raise

    def revoke(self) -> dict:
        """Revoke synchronization authorization; cleanly stops all synchronization."""
        with self._lock:
            if self._monitor is not None:
                self._monitor.stop()
                self._monitor = None

            self._authorized = False
            self._status = SYNC_STATE_REVOKED
            old_vault_id = self._vault_id
            self._active_ticket = None
            self._pending_approval = None

            self._emit(
                kind="status",
                stage="synchronization",
                message=f"Vault synchronization revoked for {old_vault_id}",
                metadata={"vault_id": old_vault_id, "status": self._status},
            )

            return {
                "authorized": False,
                "vault_id": old_vault_id,
                "status": self._status,
                "message": "Authorization revoked cleanly",
            }

    def close(self) -> None:
        """Shutdown helper for application termination."""
        with self._lock:
            if self._monitor is not None:
                self._monitor.stop()
                self._monitor = None
            self._active_ticket = None
            self._pending_approval = None

    def _is_file_eligible(self, file_path: Path) -> bool:
        """Verify strict source eligibility: must be authentic note in authorized Vault."""
        try:
            if not file_path.is_file() or file_path.is_symlink():
                return False
            if file_path.suffix.lower() != ".md":
                return False

            PathBoundary(self._vault_root).validate(str(file_path))

            from Config.settings import ROOT as REPOSITORY_ROOT
            if PathBoundary(str(REPOSITORY_ROOT)).is_allowed(str(file_path)):
                return False
            if self._staging_root and PathBoundary(self._staging_root).is_allowed(str(file_path)):
                return False
            if self._audit_root and PathBoundary(self._audit_root).is_allowed(str(file_path)):
                return False
            if self._vector_db_root and PathBoundary(self._vector_db_root).is_allowed(str(file_path)):
                return False

            return True
        except Exception:
            return False

    def _check_index_baseline_consistency(self, meta: dict | None, validated_core: Any = None) -> tuple[bool, str]:
        """Verify whether the vector store index baseline is complete and consistent with metadata."""
        if not meta or not isinstance(meta, dict):
            return False, "synchronization metadata is missing or invalid"

        status = str(meta.get("status", ""))
        if status != SYNC_STATE_FRESH:
            return False, f"synchronization metadata status is '{status or 'unrecorded'}', not '{SYNC_STATE_FRESH}'"

        manifest = meta.get("manifest")
        if not isinstance(manifest, dict) or "entries" not in manifest or not isinstance(manifest.get("entries"), dict):
            return False, "manifest is missing or malformed in synchronization metadata"

        if self._store is None:
            return False, "store not initialized"

        state_info = self._store.inspect_store_state()
        if state_info.get("state") != "valid":
            return False, f"vector store state is '{state_info.get('state')}'"

        try:
            rows = self._store.show_all()
        except Exception as exc:
            return False, f"failed inspecting vector rows: {exc}"

        if not isinstance(rows, list):
            return False, "vector store returned non-list rows"

        from src.living_authenticity.knowledge.vault.core_validator import (
            is_core_relative_path,
            resolve_schema_core_folder,
            CoreResolutionError,
        )
        try:
            CORE_FOLDER = resolve_schema_core_folder()
        except Exception as exc:
            return False, f"failed resolving schema Core folder: {exc}"

        def _is_core_path(rel_p: str) -> bool:
            try:
                return is_core_relative_path(rel_p)
            except Exception:
                norm = rel_p.replace("\\", "/").lstrip("/")
                return norm == CORE_FOLDER or norm.startswith(f"{CORE_FOLDER}/")

        def _norm_path(p: str) -> str:
            return str(Path(p).resolve()).replace("\\", "/").lower()

        v_root = Path(self._vault_root).resolve() if self._vault_root else None
        if not v_root or not v_root.is_dir():
            return False, "vault_root is invalid or not a directory"

        val_core_norm = ""
        if validated_core is not None and getattr(validated_core, "path", None):
            try:
                val_core_norm = _norm_path(str(validated_core.path))
            except Exception as exc:
                return False, f"failed resolving validated_core path: {exc}"

        # 1. Row integrity & anti-impersonation isolation
        ordinary_rows: list[dict] = []
        core_rows: list[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                return False, "vector store contains non-dict row"
            rid = str(r.get("id", ""))
            r_is_core = bool(r.get("is_core", False))
            r_src = str(r.get("source", ""))
            r_norm_src = _norm_path(r_src) if r_src else ""

            is_core_src = bool(
                _is_core_path(r_src)
                or (val_core_norm and r_norm_src == val_core_norm)
            )

            if r_is_core:
                if not is_core_src:
                    return False, f"detected ordinary vector record '{rid}' impersonating Core (is_core=True for non-Core source '{r_src}')"
                if not rid.startswith("core_vec_"):
                    return False, f"detected Core vector record '{rid}' lacking deterministic 'core_vec_' prefix"
                core_rows.append(r)
            else:
                if is_core_src:
                    return False, f"detected Core vector record '{rid}' misclassified as ordinary (is_core=False)"
                if rid.startswith("core_vec_"):
                    return False, f"detected ordinary vector record '{rid}' with Core record ID prefix"
                ordinary_rows.append(r)

        # 2. Group ordinary rows by normalized source
        rows_by_source: dict[str, list[dict]] = {}
        for r in ordinary_rows:
            s_norm = _norm_path(r.get("source", ""))
            rows_by_source.setdefault(s_norm, []).append(r)

        # 3. Verify each ordinary entry in manifest at Chunk level
        non_core_entries = [p for p in manifest["entries"] if not _is_core_path(p)]
        expected_sources: set[str] = set()
        total_expected_ordinary_chunks = 0

        import hashlib

        for rel_p in non_core_entries:
            note_p = (v_root / rel_p).resolve()
            if not note_p.is_file():
                return False, f"ordinary note '{rel_p}' in manifest does not exist on disk"

            try:
                content_bytes = note_p.read_bytes()
            except (OSError, PermissionError) as exc:
                return False, f"failed reading ordinary note '{rel_p}' due to filesystem error: {exc}"
            except Exception as exc:
                return False, f"failed reading ordinary note '{rel_p}': {exc}"

            cur_sha = hashlib.sha256(content_bytes).hexdigest()
            exp_sha = manifest["entries"][rel_p].get("sha256")
            if cur_sha != exp_sha:
                return False, f"ordinary note '{rel_p}' content modified (sha256 mismatch: expected {exp_sha}, got {cur_sha})"

            try:
                raw_text = content_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                return False, f"failed decoding ordinary note '{rel_p}' as utf-8: {exc}"

            if not raw_text.strip():
                expected_units = []
            else:
                try:
                    ingestion = self._pipeline.ingestion.ingest(str(note_p))
                    expected_units = [
                        u for u in ingestion.knowledge_units
                        if (u.cleaned_text or u.normalized_text or u.meaning or u.original_text or "").strip()
                    ]
                except Exception as exc:
                    return False, f"failed extracting knowledge units from ordinary note '{rel_p}': {exc}"

            norm_src = _norm_path(str(note_p))
            expected_sources.add(norm_src)
            total_expected_ordinary_chunks += len(expected_units)
            found_rows = rows_by_source.get(norm_src, [])

            # Check A: Exact Chunk count for this note
            if len(found_rows) != len(expected_units):
                diff_desc = f"missing {len(expected_units) - len(found_rows)} record(s)" if len(found_rows) < len(expected_units) else f"unexpected {len(found_rows) - len(expected_units)} record(s)"
                return False, f"ordinary note '{rel_p}' has {len(expected_units)} expected chunk record(s), but found {len(found_rows)} in index ({diff_desc})"

            # Check B: Position completeness
            expected_positions = {u.position for u in expected_units}
            found_positions = {r.get("position") for r in found_rows if r.get("position") is not None}
            if not expected_positions.issubset(found_positions):
                missing_pos = expected_positions - found_positions
                return False, f"ordinary note '{rel_p}' is missing chunk position(s): {sorted(missing_pos)}"

            # Check C: Deterministic Record IDs, completeness, and duplicates
            found_ids = [str(r.get("id")) for r in found_rows if r.get("id")]
            if len(found_ids) != len(set(found_ids)):
                return False, f"ordinary note '{rel_p}' contains duplicate vector record IDs in index"

            expected_ids = {u.id for u in expected_units if getattr(u, "id", None)}
            if expected_ids:
                missing_ids = expected_ids - set(found_ids)
                if missing_ids:
                    return False, f"ordinary note '{rel_p}' is missing expected vector record ID(s): {sorted(missing_ids)}"
                unexpected_ids = set(found_ids) - expected_ids
                if unexpected_ids:
                    return False, f"ordinary note '{rel_p}' has unexpected vector record ID(s): {sorted(unexpected_ids)}"

            # Check D: Source attribution
            for r in found_rows:
                if _norm_path(r.get("source", "")) != norm_src:
                    return False, f"vector record '{r.get('id')}' has misattributed source: expected '{note_p}', got '{r.get('source')}'"

        # 4. Unexpected ordinary records check
        unexpected_sources = set(rows_by_source.keys()) - expected_sources
        if unexpected_sources:
            first_unexp = next(iter(unexpected_sources))
            return False, f"vector store contains unexpected vector record(s) for unmanifested source '{first_unexp}'"

        if len(ordinary_rows) != total_expected_ordinary_chunks:
            return False, f"total ordinary vector records in index ({len(ordinary_rows)}) does not match expected ({total_expected_ordinary_chunks})"

        exp_vectors = meta.get("indexed_vectors")
        if isinstance(exp_vectors, int) and exp_vectors > 0:
            exp_core_count = len(getattr(validated_core, "units", ())) if validated_core is not None else 0
            exp_non_core = max(0, exp_vectors - exp_core_count)
            if len(ordinary_rows) != exp_non_core:
                return False, f"expected exactly {exp_non_core} ordinary vector records, but found {len(ordinary_rows)}"

        return True, ""

    def _validate_sync_candidate_consistency(
        self,
        candidate_meta: dict,
        commit_snapshot: dict,
        commit_core: Any = None,
    ) -> tuple[bool, str]:
        """Verify whether the vector store live rows match candidate metadata before saving Fresh."""
        if not candidate_meta or not isinstance(candidate_meta, dict):
            return False, "Candidate metadata is missing or invalid"

        status = str(candidate_meta.get("status", ""))
        if status != SYNC_STATE_FRESH:
            return False, f"Candidate status is '{status}', expected '{SYNC_STATE_FRESH}'"

        if self._store is None:
            return False, "Store not initialized"

        try:
            rows = self._store.show_all()
        except Exception as exc:
            return False, f"Failed reading vector rows from store: {exc}"

        if not isinstance(rows, list):
            return False, "Store show_all did not return a list"

        exp_count = candidate_meta.get("indexed_vectors", 0)
        if len(rows) != exp_count:
            return False, f"Vector row count ({len(rows)}) does not match candidate indexed_vectors ({exp_count})"

        from src.living_authenticity.knowledge.vault.core_validator import (
            is_core_relative_path,
            resolve_schema_core_folder,
            CoreResolutionError,
        )
        try:
            CORE_FOLDER = resolve_schema_core_folder()
        except Exception as exc:
            return False, f"failed resolving schema Core folder: {exc}"

        val_core_norm = ""
        val_core_hash = ""
        if commit_core is not None and getattr(commit_core, "path", None):
            try:
                val_core_norm = str(Path(commit_core.path).resolve()).replace("\\", "/").lower()
                val_core_hash = getattr(commit_core, "sha256", "")
            except Exception as exc:
                return False, f"failed resolving commit_core path: {exc}"

        for r in rows:
            if not isinstance(r, dict):
                return False, "Vector store contains non-dict row"
            r_id = str(r.get("id", ""))
            r_is_core = bool(r.get("is_core", False))
            r_src = str(r.get("source", ""))
            r_norm_src = str(Path(r_src).resolve()).replace("\\", "/").lower() if r_src else ""
            try:
                is_core_path = (
                    is_core_relative_path(r_src)
                    or (val_core_norm and r_norm_src == val_core_norm)
                )
            except Exception:
                norm = r_src.replace("\\", "/").lstrip("/")
                is_core_path = (norm == CORE_FOLDER or norm.startswith(f"{CORE_FOLDER}/")) or (val_core_norm and r_norm_src == val_core_norm)
            if r_is_core:
                if not is_core_path:
                    return False, f"Ordinary record '{r_id}' impersonating Core (is_core=True for non-core source '{r_src}')"
                if not r_id.startswith("core_vec_"):
                    return False, f"Core record '{r_id}' missing required 'core_vec_' prefix"
                if val_core_hash and r.get("core_hash") != val_core_hash:
                    return False, f"Core record '{r_id}' has mismatching core_hash"
            else:
                if r_id.startswith("core_vec_"):
                    return False, f"Non-core record '{r_id}' carrying 'core_vec_' prefix"

        return True, ""

    def check_changes(self, snapshot: dict | None = None) -> dict:
        """Inspect current Vault files against recorded manifest without modifying the index."""
        with self._lock:
            if not self._authorized or not self._vault_root or self._store is None:
                return {
                    "authorized": False,
                    "status": self._status,
                    "stale": False,
                    "added": [],
                    "modified": [],
                    "removed": [],
                    "unchanged": [],
                }
            current_snapshot = snapshot if snapshot is not None else snapshot_vault(self._vault_root)
            prev_meta = self._store.get_metadata()
            prev_manifest = prev_meta.get("manifest", self._manifest or {})
            diff = diff_manifests(prev_manifest, current_snapshot)

            from src.living_authenticity.knowledge.vault.core_validator import (
                resolve_schema_core_folder,
                validate_and_load_core,
                CoreResolutionError,
                is_core_relative_path,
                VAULT_CORE_FOLDER,
            )

            try:
                CORE_FOLDER = resolve_schema_core_folder()
                schema_core_error = None
            except CoreResolutionError as exc:
                CORE_FOLDER = ""
                schema_core_error = exc

            def _is_core(rel_p: str) -> bool:
                if not rel_p:
                    return False
                if not CORE_FOLDER:
                    # Explicit deliberate fail-safe classification mechanism when schema resolution has already failed:
                    # ensure paths under canonical '01 Core' never enter ordinary changes.
                    norm = rel_p.replace("\\", "/").lstrip("/")
                    return norm == VAULT_CORE_FOLDER or norm.startswith(f"{VAULT_CORE_FOLDER}/")

                try:
                    return is_core_relative_path(rel_p)
                except CoreResolutionError as exc:
                    logger.error(f"Core path classification failed for '{rel_p}': {exc.safe_diagnostic}")
                    raise VaultSyncSchemaResolutionError(
                        f"Core path classification failed for '{rel_p}': {exc.safe_diagnostic}"
                    ) from exc
                except Exception as exc:
                    logger.error(f"Unexpected error classifying path '{rel_p}': {exc}")
                    raise VaultSyncError(f"Failed classifying path '{rel_p}': {exc}") from exc

            added_all = diff.get("added", [])
            modified_all = diff.get("modified", [])
            removed_all = diff.get("removed", [])

            core_added = [p for p in added_all if _is_core(p)]
            core_modified = [p for p in modified_all if _is_core(p)]
            core_removed = [p for p in removed_all if _is_core(p)]
            core_changed = bool(core_added or core_modified or core_removed)

            ordinary_added = [p for p in added_all if not _is_core(p)]
            ordinary_modified = [p for p in modified_all if not _is_core(p)]
            ordinary_removed = [p for p in removed_all if not _is_core(p)]

            stale_core_vectors = False
            cur_core_hash = ""
            core_valid = True
            core_error = None
            core_error_category = None
            val_core = None

            core_folder_present = bool(CORE_FOLDER and (Path(self._vault_root) / CORE_FOLDER).exists())
            prev_core_hash = str(prev_meta.get("core_hash", ""))
            prev_has_core = bool(prev_core_hash or prev_meta.get("core_source_path"))
            should_validate_core = core_folder_present or prev_has_core or core_changed or (schema_core_error is not None)

            if should_validate_core:
                if schema_core_error is not None:
                    core_valid = False
                    core_error = schema_core_error.safe_diagnostic
                    core_error_category = schema_core_error.category.value
                    stale_core_vectors = True
                else:
                    try:
                        val_core = validate_and_load_core("personal_vault", vault_root=self._vault_root, pipeline=self._pipeline)
                        cur_core_hash = val_core.sha256
                        core_valid = True
                    except CoreResolutionError as exc:
                        core_valid = False
                        core_error = exc.safe_diagnostic
                        core_error_category = exc.category.value
                        stale_core_vectors = True
                    except Exception as exc:
                        from src.living_authenticity.security.diagnostic import sanitize_diagnostic_text
                        core_valid = False
                        core_error = sanitize_diagnostic_text(str(exc))
                        core_error_category = "core_validation_error"
                        stale_core_vectors = True

            missing_core_ids: list[str] = []
            if core_valid and cur_core_hash:
                if prev_core_hash and cur_core_hash != prev_core_hash:
                    stale_core_vectors = True
                elif core_changed:
                    stale_core_vectors = True
                elif not prev_core_hash:
                    stale_core_vectors = True
                elif val_core is not None:
                    from src.living_authenticity.knowledge.vault.core_validator import (
                        compute_core_record_id,
                    )
                    expected_ids = {
                        compute_core_record_id(u, val_core)
                        for u in getattr(val_core, 'units', ())
                    }
                    if expected_ids and self._store is not None:
                        try:
                            existing_ids = self._store.get_existing_core_record_ids(str(val_core.path.resolve()))
                            missing = expected_ids - existing_ids
                            if missing:
                                stale_core_vectors = True
                                missing_core_ids = sorted(missing)
                        except Exception as exc:
                            from src.living_authenticity.security.diagnostic import sanitize_diagnostic_text
                            core_valid = False
                            core_error = sanitize_diagnostic_text(f"Core vector inspection failed: {exc}")
                            core_error_category = "core_vector_inspection_error"
                            stale_core_vectors = True

            prev_status = str(prev_meta.get("status", "")) if prev_meta else ""
            has_valid_baseline = bool(prev_meta and prev_status == SYNC_STATE_FRESH)
            is_baseline_consistent, baseline_inc_reason = self._check_index_baseline_consistency(prev_meta, val_core)

            filesystem_stale = bool(diff.get("stale", False) or not has_valid_baseline or not is_baseline_consistent)
            is_stale = bool(filesystem_stale or stale_core_vectors or not core_valid)
            if is_stale:
                self._status = SYNC_STATE_STALE

            case_a_eligible = bool(
                has_valid_baseline
                and not diff.get("stale", False)
                and is_baseline_consistent
                and core_valid
                and cur_core_hash
                and prev_core_hash
                and cur_core_hash == prev_core_hash
                and not core_changed
                and missing_core_ids
            )

            return {
                "authorized": True,
                "status": self._status,
                "stale": is_stale,
                "filesystem_stale": filesystem_stale,
                "core_valid": core_valid,
                "core_error": core_error,
                "core_error_category": core_error_category,
                "core_changed": core_changed,
                "stale_core_vectors": stale_core_vectors,
                "missing_core_ids": missing_core_ids,
                "case_a_eligible": case_a_eligible,
                "core_changes": {
                    "added": core_added,
                    "modified": core_modified,
                    "removed": core_removed,
                },
                "ordinary_changes": {
                    "added": ordinary_added,
                    "modified": ordinary_modified,
                    "removed": ordinary_removed,
                },
                "added": added_all,
                "modified": modified_all,
                "removed": removed_all,
                "unchanged": diff.get("unchanged", []),
                "current_core_hash": cur_core_hash,
                "previous_core_hash": prev_core_hash,
                "snapshot_fingerprint": compute_snapshot_fingerprint(current_snapshot),
                "diff_fingerprint": compute_diff_fingerprint(diff),
            }

    def request_approval(self, run_id: str, ttl_seconds: float = 300.0) -> dict:
        """Create a pending synchronization approval request bound to current changes."""
        with self._lock:
            if not self._authorized or not self._vault_root or self._store is None:
                raise VaultSyncError("Synchronization not authorized")

            current_snapshot = snapshot_vault(self._vault_root)
            changes = self.check_changes(snapshot=current_snapshot)
            snap_fp = compute_snapshot_fingerprint(current_snapshot)
            diff_fp = compute_diff_fingerprint(changes)
            core_hash = changes.get("current_core_hash", "")
            now = time.time()
            expires_at = now + ttl_seconds
            approval_id = str(uuid.uuid4())

            state_info = self._store.inspect_store_state()
            store_state = state_info.get("state")
            prev_meta_check = self._store.get_metadata()
            is_unverified = (
                store_state in ("unverified", "missing_metadata", "corrupted_metadata", "interrupted_sync", "inconsistent_baseline", "inconsistent_initialized")
                or prev_meta_check.get("status") == "unverified"
                or prev_meta_check.get("unverified") is True
            )
            required_op = "clean_rebuild" if is_unverified else "incremental_sync"

            record = _PendingSyncApprovalRecord(
                approval_id=approval_id,
                vault_id=self._vault_id,
                vault_root=self._vault_root,
                run_id=run_id,
                diff=changes,
                snapshot_fingerprint=snap_fp,
                core_hash=core_hash,
                diff_fingerprint=diff_fp,
                manifest=current_snapshot,
                expires_at=expires_at,
                session_id=str(getattr(self, "_session_id", "")),
                operation_type=required_op,
            )
            self._pending_approval = record
            self._active_ticket = None
            self._status = SYNC_STATE_PENDING_APPROVAL

            self._emit(
                kind="sync_approval_required",
                stage="synchronization",
                message=f"Vault synchronization approval required for {self._vault_id}",
                metadata={
                    "approval_id": approval_id,
                    "vault_id": self._vault_id,
                    "run_id": run_id,
                    "diff": changes,
                    "expires_at": expires_at,
                },
            )

            return {
                "approval_id": approval_id,
                "vault_id": self._vault_id,
                "run_id": run_id,
                "diff": changes,
                "snapshot_fingerprint": snap_fp,
                "core_hash": core_hash,
                "expires_at": expires_at,
            }

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        run_id: str = "",
        vault_id: str = "",
    ) -> dict:
        """Process explicit operator decision and issue a single-use ticket upon approval."""
        with self._lock:
            pending = self._pending_approval
            if pending is None or pending.approval_id != approval_id:
                raise VaultSyncUnauthorizedError(f"No pending vault sync approval found for id '{approval_id}'")
            if run_id and pending.run_id and pending.run_id != run_id:
                raise VaultSyncUnauthorizedError(f"run_id mismatch: expected '{pending.run_id}', got '{run_id}'")
            if vault_id and pending.vault_id and pending.vault_id != vault_id:
                raise VaultSyncUnauthorizedError(f"vault_id mismatch: expected '{pending.vault_id}', got '{vault_id}'")
            if pending.state != SYNC_STATE_PENDING_APPROVAL:
                raise VaultSyncUnauthorizedError(f"Pending approval '{approval_id}' is already {pending.state}")
            if pending.is_expired():
                pending.state = SYNC_STATE_STALE_APPROVAL
                self._pending_approval = None
                raise VaultSyncStaleApprovalError(f"Pending approval '{approval_id}' has expired")

            if decision == "reject":
                pending.state = SYNC_STATE_REJECTED
                self._status = SYNC_STATE_STALE
                self._pending_approval = None
                self._active_ticket = None
                self._emit(
                    kind="approval_decided",
                    stage="synchronization",
                    message="Vault synchronization rejected by human operator",
                    metadata={"approval_id": approval_id, "decision": "reject"},
                )
                return {
                    "status": "ok",
                    "decision": "reject",
                    "approval_id": approval_id,
                    "vault_id": self._vault_id,
                }

            if decision == "approve":
                if not self._authorized or self._store is None:
                    raise VaultSyncUnauthorizedError(
                        "Cannot approve vault sync: session is not authorized or vector store is uninitialized"
                    )
                table_version = ""
                try:
                    tables = self._store._table_names()
                except Exception as exc:
                    raise VaultSyncError(
                        f"Failed inspecting vector database tables for approval ticket: {exc}"
                    ) from exc
                if "knowledge_vectors" in tables:
                    try:
                        tbl = self._store.get_table()
                        ver = getattr(tbl, "version", None)
                    except Exception as exc:
                        raise VaultSyncError(
                            f"Failed inspecting vector database table version for approval ticket: {exc}"
                        ) from exc
                    if ver is None or not str(ver).strip():
                        raise VaultSyncError(
                            "Vector table 'knowledge_vectors' exists but its version is missing, empty, or unreadable"
                        )
                    table_version = str(ver).strip()
                elif pending.operation_type != "clean_rebuild":
                    state_info = self._store.inspect_store_state()
                    st = state_info.get("state")
                    if st not in ("genuine_first_init", "uninitialized_empty") or "index_metadata" in tables:
                        raise VaultSyncError(
                            f"Vector table 'knowledge_vectors' is absent but store state is '{st}', "
                            "not genuine first initialization. A clean_rebuild approval is required."
                        )
                pending.state = SYNC_STATE_APPROVED
                ticket_id = str(uuid.uuid4())
                issuance_nonce = object()
                self._current_issuance_nonce = issuance_nonce
                try:
                    ticket = VaultSyncApprovalTicket._issue_approved_ticket(
                        ticket_id=ticket_id,
                        vault_id=pending.vault_id,
                        vault_root=pending.vault_root,
                        run_id=pending.run_id,
                        snapshot_fingerprint=pending.snapshot_fingerprint,
                        core_hash=pending.core_hash,
                        diff_fingerprint=pending.diff_fingerprint,
                        expires_at=pending.expires_at,
                        session_id=pending.session_id,
                        approval_id=pending.approval_id,
                        operation_type=pending.operation_type,
                        database_baseline_version=table_version,
                        session_proof=self._session_proof,
                        issuance_nonce=issuance_nonce,
                        session=self,
                    )
                finally:
                    self._current_issuance_nonce = None
                self._approved_tickets_by_id[ticket_id] = ticket
                self._approval_history[approval_id] = {
                    "ticket_id": ticket_id,
                    "decision": "approve",
                    "approved_at": time.time(),
                    "core_hash": pending.core_hash,
                    "vault_id": pending.vault_id,
                }
                self._issued_tickets.add(id(ticket))
                self._active_ticket = ticket
                self._pending_approval = None
                self._status = SYNC_STATE_APPROVED
                self._emit(
                    kind="approval_decided",
                    stage="synchronization",
                    message="Vault synchronization approved by human operator",
                    metadata={
                        "approval_id": approval_id,
                        "decision": "approve",
                        "ticket_id": ticket_id,
                        "expires_at": ticket.expires_at,
                    },
                )
                return {
                    "status": "ok",
                    "decision": "approve",
                    "approval_id": approval_id,
                    "vault_id": self._vault_id,
                    "ticket_id": ticket_id,
                    "expires_at": ticket.expires_at,
                }

            raise VaultSyncUnauthorizedError(f"Invalid decision '{decision}'; must be 'approve' or 'reject'")

    def _recover_missing_core_records(self, validated_core, auth_ticket: Any = None) -> int:
        """Case A non-destructive recovery: insert only missing deterministic Core records.

        Guarantees:
        - Only missing deterministic Core records are inserted.
        - Existing valid records are preserved without deletion or replacement.
        - delete_source() is NEVER called.
        - No authoritative Vault files are modified.
        - The accepted baseline manifest is NOT altered or replaced.
        - Operation is idempotent.
        """
        if validated_core is None or self._store is None:
            return 0
        from src.living_authenticity.knowledge.vault.core_validator import (
            compute_core_record_id,
        )
        units = list(getattr(validated_core, "units", ()))
        if not units:
            return 0

        expected_ids = {
            compute_core_record_id(u, validated_core)
            for u in units
        }
        existing_ids = self._store.get_existing_core_record_ids(str(validated_core.path.resolve()))
        missing_ids = expected_ids - existing_ids
        if not missing_ids:
            return 0

        missing_units = [
            u for u in units
            if compute_core_record_id(u, validated_core) in missing_ids
        ]
        if not missing_units:
            return 0

        texts = [
            (u.cleaned_text or u.normalized_text or u.meaning or u.original_text or "")
            for u in missing_units
        ]
        vectors = self._embedder.embed_batch(texts)
        if vectors is None or len(vectors) != len(missing_units):
            raise VaultSyncError(
                f"Embedding service returned invalid vector batch for Core recovery: "
                f"expected {len(missing_units)}, got {len(vectors) if vectors is not None else 0}"
            )
        exp_dim = getattr(self._embedder, "dimension", 0)
        for v in vectors:
            if v is None:
                raise VaultSyncError("Embedding batch for Core recovery contains None vector")
            v_len = len(v) if hasattr(v, "__len__") else (int(v.shape[0]) if hasattr(v, "shape") else 0)
            if v_len <= 0 or (exp_dim > 0 and v_len != exp_dim):
                raise VaultSyncError(f"Embedding vector dimensionality mismatch during Core recovery: expected {exp_dim}, got {v_len}")

        inserted = 0
        for unit, vector in zip(missing_units, vectors):
            rec_id = compute_core_record_id(unit, validated_core)
            metadata = {
                "source": unit.source,
                "position": unit.position,
                "is_core": True,
                "core_hash": validated_core.sha256,
                "record_id": rec_id,
            }
            self._store.store(
                text=(unit.cleaned_text or unit.normalized_text or unit.meaning or unit.original_text or ""),
                embedding=vector,
                metadata=metadata,
                record_id=rec_id,
                validated_core=validated_core,
            )
            inserted += 1

        # Post-write independent verification of Core completeness
        post_existing_ids = self._store.get_existing_core_record_ids(str(validated_core.path.resolve()))
        if not expected_ids.issubset(post_existing_ids):
            still_missing = expected_ids - post_existing_ids
            self._status = SYNC_STATE_STALE
            raise VaultSyncError(
                f"Case A recovery post-write verification failed: {len(still_missing)} Core record(s) still missing: {sorted(still_missing)}"
            )

        rows = self._store.show_all()
        self._indexed_vectors = len(rows) if isinstance(rows, list) else 0
        self._error_message = None

        self._emit(
            kind="status",
            stage="synchronization",
            message=f"Case A recovery: restored {inserted} missing Core vector record(s) non-destructively",
            metadata={"recovered_vectors": inserted, "total_vectors": self._indexed_vectors},
        )
        return inserted

    def _resolve_reconcile_authorization(
        self,
        ticket: VaultSyncApprovalTicket | None,
        prev_meta: dict,
        current_snap_fp: str,
        validated_core: Any,
        active_core_path: str,
        active_core_hash: str,
        prev_core_hash: str,
        has_disk_changes: bool,
        core_hash_changed: bool,
        diff: dict | None = None,
    ) -> tuple[VaultSyncApprovalTicket | None, dict | None]:
        """Validate authorization gate for reconciliation, returning (effective_ticket, early_result)."""
        eff_ticket = ticket if ticket is not None else self._active_ticket

        if self._initial_sync_active:
            # Genuine First Initialization privilege
            return (None, None)

        if eff_ticket is None:
            prev_status = str(prev_meta.get("status", "")) if prev_meta else ""
            has_valid_baseline = bool(prev_meta and prev_status == SYNC_STATE_FRESH)
            is_baseline_consistent, baseline_inc_reason = self._check_index_baseline_consistency(prev_meta, validated_core)

            # Check if any missing Core vector records exist in the store
            if validated_core is not None:
                from src.living_authenticity.knowledge.vault.core_validator import (
                    compute_core_record_id,
                )
                expected_ids = {
                    compute_core_record_id(u, validated_core)
                    for u in getattr(validated_core, "units", ())
                }
                existing_ids = self._store.get_existing_core_record_ids(active_core_path) if self._store is not None else set()
                if expected_ids and not expected_ids.issubset(existing_ids):
                    self._status = SYNC_STATE_STALE
                    raise VaultSyncUnauthorizedError(
                        "Missing Core vector records detected in store; explicit operator approval ticket is required to reconcile or recover missing Core records."
                    )

            if not has_disk_changes and not core_hash_changed and self.is_fresh and has_valid_baseline and is_baseline_consistent:
                return (
                    None,
                    {
                        "authorized": True,
                        "vault_id": self._vault_id,
                        "status": self._status,
                        "fresh": True,
                        "added": 0,
                        "modified": 0,
                        "removed": 0,
                        "indexed_notes": self._indexed_notes,
                        "indexed_vectors": self._indexed_vectors,
                        "last_synced_at": self._last_synced_at,
                    },
                )
            # Unauthorized modification, interrupted sync, or index inconsistency: reject
            self._status = SYNC_STATE_STALE
            raise VaultSyncUnauthorizedError(
                f"Vault changes, interrupted synchronization, or index inconsistency detected "
                f"({baseline_inc_reason or 'operator approval required'}); explicit operator approval ticket is required to reconcile."
            )

        # Ticket provided: verify ticket invariants
        validated_ticket = eff_ticket
        if not isinstance(validated_ticket, VaultSyncApprovalTicket):
            raise VaultSyncUnauthorizedError("Invalid ticket object supplied")
        if validated_ticket.used or not validated_ticket.is_valid():
            raise VaultSyncStaleApprovalError("Ticket is already used or has expired")
        if validated_ticket is not self._active_ticket:
            raise VaultSyncUnauthorizedError("Ticket object does not match active approved ticket")
        if validated_ticket.vault_id != self._vault_id or validated_ticket.vault_root != self._vault_root:
            raise VaultSyncUnauthorizedError("Ticket vault identity mismatch")
        if validated_ticket.snapshot_fingerprint != current_snap_fp:
            raise VaultSyncStaleApprovalError(
                "Vault snapshot mutated since approval was granted; ticket invalidated."
            )
        if validated_ticket.core_hash != active_core_hash:
            raise VaultSyncStaleApprovalError(
                "Authoritative Core mutated since approval was granted; ticket invalidated."
            )
        if getattr(validated_ticket, "diff_fingerprint", None) and diff is not None:
            current_diff_fp = compute_diff_fingerprint(diff)
            if validated_ticket.diff_fingerprint != current_diff_fp:
                raise VaultSyncStaleApprovalError(
                    "Vault change set mutated since approval was granted (diff fingerprint mismatch); ticket invalidated."
                )

        # Verify ticket operation type matches store requirement
        store_state_info = self._store.inspect_store_state() if self._store is not None else {}
        store_state = store_state_info.get("state")
        is_unverified = (
            store_state in ("unverified", "missing_metadata", "corrupted_metadata", "interrupted_sync", "inconsistent_baseline")
            or prev_meta.get("status") == "unverified"
            or prev_meta.get("unverified") is True
        )
        ticket_op = getattr(validated_ticket, "operation_type", "incremental_sync")
        if is_unverified and ticket_op != "clean_rebuild":
            raise VaultSyncUnauthorizedError(
                "Store is unverified or modified outside authorized sync; an incremental_sync ticket cannot authorize reconciliation. An explicit clean_rebuild approval ticket is required."
            )

        if (
            ticket_op != "clean_rebuild"
            and self._store is not None
        ):
            try:
                tables = self._store._table_names()
            except Exception as exc:
                raise VaultSyncError(
                    f"Failed inspecting vector database tables for reconciliation authorization: {exc}"
                ) from exc

            baseline_ver = getattr(validated_ticket, "database_baseline_version", None)
            baseline_ver_str = str(baseline_ver).strip() if baseline_ver is not None else ""

            if "knowledge_vectors" in tables:
                if not baseline_ver_str:
                    raise VaultSyncUnauthorizedError(
                        "Approval ticket lacks required database baseline version for existing vector store"
                    )
                try:
                    live_tbl = self._store.get_table()
                    live_ver = getattr(live_tbl, "version", None)
                except Exception as exc:
                    raise VaultSyncError(
                        f"Failed inspecting live vector database table version for reconciliation: {exc}"
                    ) from exc
                if live_ver is None or not str(live_ver).strip():
                    raise VaultSyncError(
                        "Live vector table 'knowledge_vectors' version is missing, empty, or unreadable"
                    )
                if str(live_ver).strip() != baseline_ver_str:
                    raise VaultSyncStaleApprovalError(
                        f"Vector database mutated out-of-band since approval ticket was granted "
                        f"(baseline version '{baseline_ver_str}' != current '{str(live_ver).strip()}'); "
                        f"ticket invalidated."
                    )
            else:
                if baseline_ver_str:
                    raise VaultSyncStaleApprovalError(
                        f"Vector database table 'knowledge_vectors' existed at approval "
                        f"(baseline version '{baseline_ver_str}') but has disappeared before reconciliation; "
                        f"ticket invalidated."
                    )
                is_genuine_first_init = (
                    store_state in ("genuine_first_init", "uninitialized_empty")
                    and not prev_meta
                    and "index_metadata" not in tables
                )
                if not is_genuine_first_init:
                    raise VaultSyncUnauthorizedError(
                        "Vector database table 'knowledge_vectors' is absent without verified proof of "
                        "genuine first initialization; incremental ticket cannot authorize reconciliation."
                    )

        # Atomically consume ticket
        validated_ticket.mark_used()
        self._active_ticket = None
        return (validated_ticket, None)

    def _execute_reconcile_mutations(
        self,
        ticket: VaultSyncApprovalTicket | None,
        current_snapshot: dict,
        added_paths: list[str],
        modified_paths: list[str],
        removed_paths: list[str],
        validated_core: Any,
        v_root_path: Path,
    ) -> tuple[dict[str, str], int]:
        """Apply vector deletions and ingestions according to operation type (clean rebuild vs incremental)."""
        processed_hashes: dict[str, str] = {}
        case_a_recovered: int = 0
        if self._store is None:
            return processed_hashes, case_a_recovered

        is_clean_rebuild = bool(ticket is not None and getattr(ticket, "operation_type", "") == "clean_rebuild")
        if is_clean_rebuild:
            table_names = self._store._table_names() if hasattr(self._store, "_table_names") else set()
            if "knowledge_vectors" in table_names:
                self._store.delete_all()
            elif hasattr(self._store, "create_knowledge_vector_table"):
                self._store.create_knowledge_vector_table()
            for rel_path, entry_meta in current_snapshot.get("entries", {}).items():
                full_path = (v_root_path / rel_path).resolve()
                if not self._is_file_eligible(full_path):
                    continue
                full_source = str(full_path)
                expected_file_sha = entry_meta.get("sha256")
                file_bytes = full_path.read_bytes()
                actual_sha = hashlib.sha256(file_bytes).hexdigest()
                if expected_file_sha and actual_sha != expected_file_sha:
                    raise VaultSyncSnapshotDriftError(
                        f"File '{rel_path}' changed on disk during reconciliation (TOCTOU violation)"
                    )
                processed_hashes[rel_path] = actual_sha
                self._index_single_file(full_path, validated_core=validated_core)
            if validated_core is not None and self._is_file_eligible(validated_core.path):
                self._recover_missing_core_records(validated_core, auth_ticket=ticket)
        else:
            # 3. Handle removed notes
            for rel_path in removed_paths:
                full_source = str((v_root_path / rel_path).resolve())
                self._store.delete_source(full_source)

            # 4. Handle modified notes: delete old vectors, verify content, re-ingest
            for rel_path in modified_paths:
                full_path = (v_root_path / rel_path).resolve()
                if not self._is_file_eligible(full_path):
                    continue
                full_source = str(full_path)
                expected_file_sha = current_snapshot.get("entries", {}).get(rel_path, {}).get("sha256")
                file_bytes = full_path.read_bytes()
                actual_sha = hashlib.sha256(file_bytes).hexdigest()
                if expected_file_sha and actual_sha != expected_file_sha:
                    raise VaultSyncSnapshotDriftError(
                        f"File '{rel_path}' changed on disk during reconciliation (TOCTOU violation)"
                    )
                processed_hashes[rel_path] = actual_sha

                self._store.delete_source(full_source)
                self._index_single_file(full_path, validated_core=validated_core)

            # 5. Handle added notes: delete lingering vectors for idempotence, verify content, ingest
            for rel_path in added_paths:
                full_path = (v_root_path / rel_path).resolve()
                if not self._is_file_eligible(full_path):
                    continue
                full_source = str(full_path)
                expected_file_sha = current_snapshot.get("entries", {}).get(rel_path, {}).get("sha256")
                file_bytes = full_path.read_bytes()
                actual_sha = hashlib.sha256(file_bytes).hexdigest()
                if expected_file_sha and actual_sha != expected_file_sha:
                    raise VaultSyncSnapshotDriftError(
                        f"File '{rel_path}' changed on disk during reconciliation (TOCTOU violation)"
                    )
                processed_hashes[rel_path] = actual_sha

                self._store.delete_source(full_source)
                self._index_single_file(full_path, validated_core=validated_core)

            # 5b. Recover missing Core vectors non-destructively if needed
            case_a_recovered = 0
            if validated_core is not None and self._is_file_eligible(validated_core.path):
                case_a_recovered = self._recover_missing_core_records(validated_core, auth_ticket=ticket)

        return processed_hashes, case_a_recovered

    def _verify_pre_commit_and_revalidate_core(
        self,
        ticket: VaultSyncApprovalTicket | None,
        processed_hashes: dict[str, str],
        should_validate_core: bool,
    ) -> tuple[dict | None, dict, Any, str, str]:
        """Perform pre-commit snapshot comparison, TOCTOU checks, and Core revalidation."""
        commit_snapshot = snapshot_vault(self._vault_root)
        commit_snap_fp = compute_snapshot_fingerprint(commit_snapshot)

        # Verify snapshot fingerprint has not drifted
        if ticket is not None and commit_snap_fp != ticket.snapshot_fingerprint:
            self._status = SYNC_STATE_STALE
            self._emit(
                kind="warning",
                stage="synchronization",
                message="Vault modified during reconciliation; manifest commit aborted",
                metadata={"vault_id": self._vault_id},
            )
            return (
                {
                    "authorized": True,
                    "vault_id": self._vault_id,
                    "status": self._status,
                    "fresh": False,
                    "stale": True,
                    "error": "filesystem_modified_during_reconciliation",
                },
                commit_snapshot,
                None,
                "",
                "",
            )

        # Verify processed file contents have not changed back or drifted
        for rel_p, orig_sha in processed_hashes.items():
            current_entry_sha = commit_snapshot.get("entries", {}).get(rel_p, {}).get("sha256")
            if current_entry_sha != orig_sha:
                self._status = SYNC_STATE_STALE
                return (
                    {
                        "authorized": True,
                        "vault_id": self._vault_id,
                        "status": self._status,
                        "fresh": False,
                        "stale": True,
                        "error": f"file_{rel_p}_modified_during_processing",
                    },
                    commit_snapshot,
                    None,
                    "",
                    "",
                )

        # Revalidate Core immediately before committing
        commit_core = None
        active_core_hash = ""
        active_core_path = ""
        if should_validate_core:
            from src.living_authenticity.knowledge.vault.core_validator import validate_and_load_core
            commit_core = validate_and_load_core(
                "personal_vault",
                vault_root=self._vault_root,
                pipeline=self._pipeline,
            )
            if ticket is not None and commit_core.sha256 != ticket.core_hash:
                self._status = SYNC_STATE_STALE
                return (
                    {
                        "authorized": True,
                        "vault_id": self._vault_id,
                        "status": self._status,
                        "fresh": False,
                        "stale": True,
                        "error": "authoritative_core_modified_during_reconciliation",
                    },
                    commit_snapshot,
                    None,
                    "",
                    "",
                )
            active_core_hash = commit_core.sha256
            active_core_path = str(commit_core.path.resolve())

        return (None, commit_snapshot, commit_core, active_core_hash, active_core_path)

    def reconcile(self, ticket: VaultSyncApprovalTicket | None = None) -> dict:
        """Detect and incrementally synchronize Vault changes into the dedicated index.

        Strictly enforces the human approval gate:
        - Controlled Initial Sync privilege allowed only during genuine first initialization.
        - Strict Case A recovery allowed without new approval only if disk changes == 0 and Core unchanged.
        - All filesystem and Core changes (Case B) require an explicit, unexpired, unused ticket.
        """
        with self._lock:
            if not self._authorized or not self._vault_root or self._store is None:
                return {
                    "authorized": False,
                    "status": SYNC_STATE_UNAUTHORIZED,
                    "fresh": False,
                    "message": "Synchronization not authorized",
                }

            if self._index_dir is None:
                if self._store is not None and getattr(self._store, "db_path", None):
                    self._index_dir = Path(self._store.db_path)
                else:
                    raise VaultSyncError("index_dir not initialized")

            try:
                # 1. Snapshot current vault
                current_snapshot = snapshot_vault(self._vault_root)
                current_snap_fp = compute_snapshot_fingerprint(current_snapshot)
                prev_meta = self._store.get_metadata()
                prev_manifest = prev_meta.get("manifest", self._manifest or {})
                diff = diff_manifests(prev_manifest, current_snapshot)

                added_paths = diff.get("added", [])
                modified_paths = diff.get("modified", [])
                removed_paths = diff.get("removed", [])
                has_disk_changes = bool(added_paths or modified_paths or removed_paths)

                # Revalidate authoritative Core
                from src.living_authenticity.knowledge.vault.core_validator import (
                    resolve_schema_core_folder,
                    is_core_relative_path,
                    CoreResolutionError,
                )
                try:
                    schema_core_folder = resolve_schema_core_folder()
                    core_folder_present = (Path(self._vault_root) / schema_core_folder).exists()
                    has_core_changes = any(
                        is_core_relative_path(p)
                        for p in added_paths + modified_paths + removed_paths
                    )
                except CoreResolutionError as exc:
                    logger.error(f"Core schema resolution failed during reconciliation: {exc.safe_diagnostic}")
                    raise VaultSyncSchemaResolutionError(
                        f"Core schema resolution failed during reconciliation: {exc.safe_diagnostic}"
                    ) from exc
                except Exception as exc:
                    logger.error(f"Unexpected error determining Core folder status during reconciliation: {exc}")
                    raise VaultSyncError(
                        f"Failed determining Core status during reconciliation: {exc}"
                    ) from exc

                prev_has_core = bool(prev_meta.get("core_hash") or prev_meta.get("core_source_path"))
                should_validate_core = core_folder_present or prev_has_core or has_core_changes

                validated_core = None
                active_core_path = ""
                active_core_hash = ""
                if should_validate_core:
                    from src.living_authenticity.knowledge.vault.core_validator import validate_and_load_core
                    validated_core = validate_and_load_core(
                        "personal_vault",
                        vault_root=self._vault_root,
                        pipeline=self._pipeline,
                    )
                    active_core_path = str(validated_core.path.resolve())
                    active_core_hash = validated_core.sha256

                prev_core_hash = str(prev_meta.get("core_hash", ""))
                core_hash_changed = bool(active_core_hash and prev_core_hash and active_core_hash != prev_core_hash)

                # 2. Authorization check
                auth_ticket, early_result = self._resolve_reconcile_authorization(
                    ticket=ticket,
                    prev_meta=prev_meta,
                    current_snap_fp=current_snap_fp,
                    validated_core=validated_core,
                    active_core_path=active_core_path,
                    active_core_hash=active_core_hash,
                    prev_core_hash=prev_core_hash,
                    has_disk_changes=has_disk_changes,
                    core_hash_changed=core_hash_changed,
                    diff=diff,
                )
                if early_result is not None:
                    return early_result

                self._status = SYNC_STATE_IN_PROGRESS
                self._current_reconciliation_ticket = auth_ticket
                # Persist in-progress status in store before mutating tables
                in_progress_meta = dict(prev_meta) if prev_meta else {}
                in_progress_meta["status"] = SYNC_STATE_IN_PROGRESS
                in_progress_meta["vault_id"] = self._vault_id
                if auth_ticket is not None:
                    in_progress_meta["sync_in_progress_ticket_id"] = auth_ticket.ticket_id
                self._store.save_metadata(in_progress_meta)

                v_root_path = Path(self._vault_root)

                # Clean old Core path if renamed
                old_core_path = str(prev_meta.get("core_source_path", ""))
                if old_core_path and old_core_path != active_core_path:
                    self._store.delete_source(old_core_path)

                if diff.get("stale"):
                    self._emit(
                        kind="status",
                        stage="synchronization",
                        message=(
                            f"Vault change detected: {len(added_paths)} added, "
                            f"{len(modified_paths)} modified, {len(removed_paths)} removed"
                        ),
                        metadata={
                            "vault_id": self._vault_id,
                            "added": len(added_paths),
                            "modified": len(modified_paths),
                            "removed": len(removed_paths),
                        },
                    )

                # 3-5. Apply mutations
                processed_hashes, case_a_recovered = self._execute_reconcile_mutations(
                    ticket=auth_ticket,
                    current_snapshot=current_snapshot,
                    added_paths=added_paths,
                    modified_paths=modified_paths,
                    removed_paths=removed_paths,
                    validated_core=validated_core,
                    v_root_path=v_root_path,
                )

                # 6. Pre-Commit Verification (Closure of TOCTOU and ABA Races)
                drift_err, commit_snapshot, commit_core, active_core_hash, active_core_path = (
                    self._verify_pre_commit_and_revalidate_core(
                        ticket=auth_ticket,
                        processed_hashes=processed_hashes,
                        should_validate_core=should_validate_core,
                    )
                )
                if drift_err is not None:
                    return drift_err

                # 7. Commit metadata atomically
                rows = self._store.show_all()
                self._indexed_vectors = len(rows) if isinstance(rows, list) else 0
                self._indexed_notes = len(commit_snapshot.get("entries", {}))
                self._manifest = commit_snapshot
                self._last_synced_at = datetime.now(timezone.utc).isoformat()
                self._error_message = None

                tbl = self._store.get_table()
                committed_table_version = str(getattr(tbl, "version", "")).strip()

                from src.living_authenticity.knowledge.vault.core_validator import compute_core_vector_digest
                core_rows = [r for r in rows if isinstance(r, dict) and bool(r.get("is_core", False))]
                core_digest = compute_core_vector_digest(core_rows) if core_rows else ""
                core_record_ids = sorted(str(r.get("id", "")) for r in core_rows)

                new_meta = {
                    "vault_id": self._vault_id,
                    "last_synced_at": self._last_synced_at,
                    "indexed_notes": self._indexed_notes,
                    "indexed_vectors": self._indexed_vectors,
                    "embedding_dimension": getattr(self._embedder, "dimension", 1024),
                    "status": SYNC_STATE_FRESH,
                    "manifest": commit_snapshot,
                    "core_hash": active_core_hash,
                    "core_source_path": active_core_path,
                    "core_vector_digest": core_digest,
                    "core_vector_count": len(core_rows),
                    "core_record_ids": core_record_ids,
                    "vector_table_version": committed_table_version,
                }
                is_cand_valid, cand_reason = self._validate_sync_candidate_consistency(
                    candidate_meta=new_meta,
                    commit_snapshot=commit_snapshot,
                    commit_core=commit_core,
                )
                if not is_cand_valid:
                    raise VaultSyncError(
                        f"Pre-commit consistency validation failed: {cand_reason}. Refusing to certify store as Fresh."
                    )
                if commit_core is not None:
                    from src.living_authenticity.knowledge.vault.core_validator import (
                        create_sync_authorization,
                    )
                    sync_auth = create_sync_authorization(
                        session=self,
                        validated_core=commit_core,
                        ticket=auth_ticket,
                    )
                    self._store.save_metadata(
                        new_meta,
                        validated_core=commit_core,
                        sync_auth=sync_auth,
                        expected_session_id=self.session_id,
                        expected_ticket_id=getattr(auth_ticket, "ticket_id", None),
                        expected_vault_id=self.vault_id,
                    )
                else:
                    self._store.save_metadata(new_meta)

                if auth_ticket is not None:
                    auth_ticket.mark_consumed()
                self._current_reconciliation_ticket = None
                self._status = SYNC_STATE_FRESH

                self._emit(
                    kind="status",
                    stage="synchronization",
                    message=(
                        f"Vault index fresh for {self._vault_id} "
                        f"({self._indexed_notes} note(s), {self._indexed_vectors} vector(s))"
                    ),
                    metadata=new_meta,
                )

                result_payload = {
                    "authorized": True,
                    "vault_id": self._vault_id,
                    "status": self._status,
                    "fresh": True,
                    "added": len(added_paths),
                    "modified": len(modified_paths),
                    "removed": len(removed_paths),
                    "indexed_notes": self._indexed_notes,
                    "indexed_vectors": self._indexed_vectors,
                    "last_synced_at": self._last_synced_at,
                }
                if case_a_recovered > 0:
                    result_payload["case_a_recovery"] = True
                    result_payload["recovered_vectors"] = case_a_recovered
                return result_payload

            except VaultSyncSchemaResolutionError:
                self._status = SYNC_STATE_FAILED
                raise
            except DatabaseLockError as exc:
                self._status = SYNC_STATE_STALE
                raise VaultSyncConcurrencyError(
                    f"Database lock contention encountered for vault '{self._vault_id}': {exc}"
                ) from exc
            except (VaultSyncUnauthorizedError, VaultSyncStaleApprovalError, VaultSyncSnapshotDriftError):
                self._status = SYNC_STATE_STALE
                raise
            except VaultSyncConcurrencyError:
                raise
            except Exception as exc:
                from src.living_authenticity.security.diagnostic import sanitize_diagnostic_text
                safe_err = sanitize_diagnostic_text(str(exc))
                logger.exception("Vault synchronization failed")
                self._status = SYNC_STATE_FAILED
                self._error_message = safe_err
                self._emit(
                    kind="warning",
                    stage="synchronization",
                    message=f"Vault synchronization failed for {self._vault_id}: {safe_err}",
                    metadata={"vault_id": self._vault_id, "error": safe_err},
                )
                return {
                    "authorized": self._authorized,
                    "vault_id": self._vault_id,
                    "status": self._status,
                    "fresh": False,
                    "error": safe_err,
                }
            finally:
                self._current_reconciliation_ticket = None

    def _index_single_file(self, file_path: Path, validated_core=None) -> int:
        """Ingest and index a single eligible markdown file into the dedicated store."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            resolve_schema_core_folder,
            is_core_relative_path,
            CoreResolutionError,
        )

        try:
            resolved_file = file_path.resolve()
        except Exception as exc:
            raise VaultSyncError(f"Failed resolving note path '{file_path}': {exc}") from exc

        # 1. Authoritative Core path classification (fail-closed)
        try:
            if self._vault_root:
                v_root = Path(self._vault_root).resolve()
                try:
                    rel = resolved_file.relative_to(v_root)
                    rel_str = str(rel)
                except ValueError:
                    # Note outside vault root; classify via authoritative schema Core folder
                    schema_folder = resolve_schema_core_folder()
                    p_str = str(resolved_file).replace("\\", "/")
                    is_core_path = f"/{schema_folder}/" in p_str or p_str.startswith(f"{schema_folder}/")
                    rel_str = ""
                else:
                    is_core_path = is_core_relative_path(rel_str)
            else:
                schema_folder = resolve_schema_core_folder()
                p_str = str(resolved_file).replace("\\", "/")
                is_core_path = f"/{schema_folder}/" in p_str or p_str.startswith(f"{schema_folder}/")
        except CoreResolutionError as exc:
            logger.error(f"Core schema resolution failed while classifying note {file_path}: {exc.safe_diagnostic}")
            raise VaultSyncSchemaResolutionError(
                f"Core schema resolution failed while classifying note '{file_path}': {exc.safe_diagnostic}"
            ) from exc
        except Exception as exc:
            logger.error(f"Unexpected error classifying note {file_path}: {exc}")
            raise VaultSyncError(f"Failed classifying note '{file_path}': {exc}") from exc

        # 2. Strict Core governance boundary
        is_core_note = False
        core_hash_val = ""
        if is_core_path:
            if validated_core is None:
                raise VaultSyncError(
                    f"Cannot index Core note '{file_path}' without validated_core context"
                )
            try:
                val_path = str(validated_core.path.resolve())
            except Exception as exc:
                raise VaultSyncError(
                    f"Failed resolving active validated Core path for '{file_path}': {exc}"
                ) from exc

            if str(resolved_file) != val_path:
                raise VaultSyncError(
                    f"Cannot index extraneous or mismatched Core note '{file_path}' "
                    f"(active validated Core note is '{validated_core.path}')"
                )
            is_core_note = True
            core_hash_val = validated_core.sha256
            units = list(validated_core.units)
        else:
            try:
                ingestion = self._pipeline.ingestion.ingest(str(file_path))
            except Exception as exc:
                logger.error(f"Failed ingesting note {file_path}: {exc}")
                raise VaultSyncError(f"Failed ingesting note '{file_path}': {exc}") from exc

            units = [
                u for u in ingestion.knowledge_units
                if (u.cleaned_text or u.normalized_text or u.meaning or u.original_text or "").strip()
            ]

        if not units:
            return 0

        texts = [
            (u.cleaned_text or u.normalized_text or u.meaning or u.original_text or "")
            for u in units
        ]
        try:
            vectors = self._embedder.embed_batch(texts)
        except Exception as exc:
            logger.error(f"Failed generating embeddings for note {file_path}: {exc}")
            raise VaultSyncError(f"Failed generating embeddings for note '{file_path}': {exc}") from exc

        if vectors is None or len(vectors) != len(units):
            raise VaultSyncError(
                f"Embedding service returned invalid vector batch for note '{file_path}': "
                f"expected {len(units)}, got {len(vectors) if vectors is not None else 0}"
            )
        exp_dim = getattr(self._embedder, "dimension", 0)
        for v in vectors:
            if v is None:
                raise VaultSyncError(f"Embedding batch for note '{file_path}' contains None vector")
            v_len = len(v) if hasattr(v, "__len__") else (int(v.shape[0]) if hasattr(v, "shape") else 0)
            if v_len <= 0 or (exp_dim > 0 and v_len != exp_dim):
                raise VaultSyncError(f"Embedding vector dimensionality mismatch for note '{file_path}': expected {exp_dim}, got {v_len}")

        if self._store is None:
            raise VaultSyncError("Store not initialized")

        count = 0
        for unit, vector in zip(units, vectors):
            record_id = None
            if is_core_note and validated_core is not None:
                from src.living_authenticity.knowledge.vault.core_validator import (
                    is_validated_core_unit,
                    compute_core_record_id,
                )
                if is_validated_core_unit(unit, context=validated_core):
                    record_id = compute_core_record_id(unit, validated_core)
            else:
                record_id = getattr(unit, "id", None)

            metadata = {
                "source": unit.source,
                "position": unit.position,
                "is_core": is_core_note,
                "core_hash": core_hash_val,
            }
            if record_id:
                metadata["record_id"] = record_id
                metadata["id"] = record_id

            if is_core_note:
                if validated_core is None or not record_id:
                    raise VaultSyncError(f"Cannot store Core vector for '{file_path}' without validated_core context and deterministic record_id")
                try:
                    self._store.store(
                        text=(unit.cleaned_text or unit.normalized_text or unit.meaning or unit.original_text or ""),
                        embedding=vector,
                        metadata=metadata,
                        record_id=record_id,
                        validated_core=validated_core,
                    )
                except Exception as exc:
                    logger.error(f"Failed storing Core vector for note {file_path}: {exc}")
                    raise VaultSyncError(f"Failed storing Core vector for note '{file_path}': {exc}") from exc
            else:
                try:
                    self._store.store(
                        text=(unit.cleaned_text or unit.normalized_text or unit.meaning or unit.original_text or ""),
                        embedding=vector,
                        metadata=metadata,
                        record_id=record_id,
                        validated_core=None,
                    )
                except Exception as exc:
                    logger.error(f"Failed storing vector for note {file_path}: {exc}")
                    raise VaultSyncError(f"Failed storing vector for note '{file_path}': {exc}") from exc
            count += 1
        return count

    def get_status(self) -> dict:
        """Return the current synchronization and freshness status."""
        with self._lock:
            return {
                "authorized": self._authorized,
                "vault_id": self._vault_id,
                "sync_active": self._monitor is not None and self._monitor.is_alive(),
                "status": self._status,
                "fresh": self.is_fresh,
                "last_synced_at": self._last_synced_at,
                "indexed_notes": self._indexed_notes,
                "indexed_vectors": self._indexed_vectors,
                "error": self._error_message,
                "pending_approval": self._pending_approval.approval_id if self._pending_approval else None,
                "has_active_ticket": bool(self._active_ticket and self._active_ticket.is_valid()),
            }


class VaultSyncMonitor(threading.Thread):
    """Background monitor thread strictly constrained to observation-only change detection.

    NEVER calls reconcile(), writes to LanceDB, or updates index manifests.
    """

    def __init__(self, session: VaultSyncSession, interval_seconds: float = 1.5) -> None:
        super().__init__(name=f"VaultSyncMonitor-{session.vault_id}", daemon=True)
        self._session = session
        self._interval = interval_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Observe drift only — NEVER call reconcile()
                if self._session.is_authorized:
                    changes = self._session.check_changes()
                    if changes.get("stale"):
                        self._session._status = SYNC_STATE_STALE
                        self._session._emit(
                            kind="drift_detected",
                            stage="monitor",
                            message=f"Filesystem drift detected in vault {self._session.vault_id}",
                            metadata={"changes": changes},
                        )
            except Exception as exc:
                logger.debug(f"Monitor observation cycle error: {exc}")
            self._stop_event.wait(self._interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=0.5)


__all__ = [
    "VaultSyncSession",
    "VaultSyncError",
    "VaultSyncSchemaResolutionError",
    "VaultSyncUnauthorizedError",
    "VaultSyncRollbackError",
    "VaultSyncConcurrencyError",
    "VaultSyncSnapshotDriftError",
    "VaultSyncStaleApprovalError",
    "VaultSyncApprovalTicket",
    "VaultSyncMonitor",
    "BatchEmbeddingService",
    "compute_snapshot_fingerprint",
    "compute_diff_fingerprint",
    "SYNC_STATE_UNAUTHORIZED",
    "SYNC_STATE_AUTHORIZING",
    "SYNC_STATE_ACTIVE",
    "SYNC_STATE_CHANGE_DETECTED",
    "SYNC_STATE_IN_PROGRESS",
    "SYNC_STATE_PENDING_APPROVAL",
    "SYNC_STATE_APPROVED",
    "SYNC_STATE_REJECTED",
    "SYNC_STATE_FRESH",
    "SYNC_STATE_STALE",
    "SYNC_STATE_STALE_APPROVAL",
    "SYNC_STATE_FAILED",
    "SYNC_STATE_REVOKED",
    "_SESSION_PROOF",
    "_TICKET_PROOF",
    "is_trusted_vault_sync_session",
    "is_trusted_approval_ticket",
]
