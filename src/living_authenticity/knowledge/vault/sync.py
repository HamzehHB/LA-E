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
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from src.living_authenticity.database.lancedb_manager import (
    DatabaseOperationError,
    LanceDBManager,
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
SYNC_STATE_FRESH = "fresh"
SYNC_STATE_STALE = "stale"
SYNC_STATE_FAILED = "error"
SYNC_STATE_REVOKED = "revoked"


class VaultSyncError(Exception):
    """Raised when vault synchronization encounters a critical error."""


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
        """Explicitly authorize synchronization for one Vault.

        In accordance with SECURITY.md and README.md architectural contracts
        (operator action only — no watcher and no background reindex), auto_watch
        defaults to False so background synchronization is not active in the operational path.
        """
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

                # 6. Perform initial reconciliation
                sync_report = self.reconcile()

                if not sync_report.get("fresh", False):
                    self._authorized = False
                    self._status = SYNC_STATE_FAILED
                    self._error_message = sync_report.get("error") or "Initial reconciliation failed"
                    return sync_report

                # 7. Start continuous change detection monitor if requested
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

    def _is_file_eligible(self, file_path: Path) -> bool:
        """Verify strict source eligibility: must be authentic note in authorized Vault."""
        try:
            if not file_path.is_file() or file_path.is_symlink():
                return False
            if file_path.suffix.lower() != ".md":
                return False

            # Must stay strictly inside the authorized Vault
            PathBoundary(self._vault_root).validate(str(file_path))

            # Exclude staging, audit, vector DB, repository workspace
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

    def check_changes(self) -> dict:
        """Inspect current Vault files against recorded manifest without modifying the index.

        Returns diff summary (added, modified, removed, stale, unchanged) and updates
        session status to SYNC_STATE_STALE if changes are detected, while leaving the
        persistent vector database untouched.
        """
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
            current_snapshot = snapshot_vault(self._vault_root)
            prev_meta = self._store.get_metadata()
            prev_manifest = prev_meta.get("manifest", self._manifest or {})
            diff = diff_manifests(prev_manifest, current_snapshot)
            if diff.get("stale"):
                self._status = SYNC_STATE_STALE
            return {
                "authorized": True,
                "status": self._status,
                "stale": diff.get("stale", False),
                "added": diff.get("added", []),
                "modified": diff.get("modified", []),
                "removed": diff.get("removed", []),
                "unchanged": diff.get("unchanged", []),
            }

    def reconcile(self) -> dict:
        """Detect and incrementally synchronize Vault changes into the dedicated index."""
        with self._lock:
            if not self._authorized or not self._vault_root or self._store is None:
                return {
                    "authorized": False,
                    "status": SYNC_STATE_UNAUTHORIZED,
                    "fresh": False,
                    "message": "Synchronization not authorized",
                }

            self._status = SYNC_STATE_IN_PROGRESS
            try:
                # 1. Snapshot current vault
                current_snapshot = snapshot_vault(self._vault_root)
                prev_meta = self._store.get_metadata()
                prev_manifest = prev_meta.get("manifest", self._manifest or {})

                # 2. Diff against previous manifest
                diff = diff_manifests(prev_manifest, current_snapshot)

                added_paths = diff.get("added", [])
                modified_paths = diff.get("modified", [])
                removed_paths = diff.get("removed", [])

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

                # 3. Handle removed notes
                v_root_path = Path(self._vault_root)
                for rel_path in removed_paths:
                    full_source = str((v_root_path / rel_path).resolve())
                    self._store.delete_source(full_source)

                # 4. Handle modified notes: delete old vectors, then re-ingest
                for rel_path in modified_paths:
                    full_path = (v_root_path / rel_path).resolve()
                    if not self._is_file_eligible(full_path):
                        continue
                    full_source = str(full_path)
                    self._store.delete_source(full_source)
                    self._index_single_file(full_path)

                # 5. Handle added notes: delete any lingering vectors first to ensure idempotent retry, then ingest
                for rel_path in added_paths:
                    full_path = (v_root_path / rel_path).resolve()
                    if not self._is_file_eligible(full_path):
                        continue
                    full_source = str(full_path)
                    self._store.delete_source(full_source)
                    self._index_single_file(full_path)

                # 6. Update row count and metadata
                rows = self._store.show_all()
                self._indexed_vectors = len(rows) if isinstance(rows, list) else 0
                self._indexed_notes = len(current_snapshot.get("entries", {}))
                self._manifest = current_snapshot
                self._last_synced_at = datetime.now(timezone.utc).isoformat()
                self._status = SYNC_STATE_FRESH
                self._error_message = None

                new_meta = {
                    "vault_id": self._vault_id,
                    "last_synced_at": self._last_synced_at,
                    "indexed_notes": self._indexed_notes,
                    "indexed_vectors": self._indexed_vectors,
                    "embedding_dimension": getattr(self._embedder, "dimension", 1024),
                    "status": self._status,
                    "manifest": current_snapshot,
                }
                self._store.save_metadata(new_meta)

                self._emit(
                    kind="status",
                    stage="synchronization",
                    message=(
                        f"Vault index fresh for {self._vault_id} "
                        f"({self._indexed_notes} note(s), {self._indexed_vectors} vector(s))"
                    ),
                    metadata=new_meta,
                )

                return {
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

            except Exception as exc:
                logger.exception("Vault synchronization failed")
                self._status = SYNC_STATE_FAILED
                self._error_message = str(exc)
                self._emit(
                    kind="warning",
                    stage="synchronization",
                    message=f"Vault synchronization failed for {self._vault_id}: {exc}",
                    metadata={"vault_id": self._vault_id, "error": str(exc)},
                )
                return {
                    "authorized": self._authorized,
                    "vault_id": self._vault_id,
                    "status": self._status,
                    "fresh": False,
                    "error": str(exc),
                }

    def _index_single_file(self, file_path: Path) -> int:
        """Ingest and index a single eligible markdown file into the dedicated store."""
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

        if self._store is None:
            raise VaultSyncError("Store not initialized")

        count = 0
        for unit, vector in zip(units, vectors):
            try:
                self._store.store(
                    text=(unit.cleaned_text or unit.normalized_text or unit.meaning or unit.original_text or ""),
                    embedding=vector,
                    metadata={"source": unit.source, "position": unit.position},
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
            }


class VaultSyncMonitor(threading.Thread):
    """Background monitor thread strictly constrained to the authorized Vault."""

    def __init__(self, session: VaultSyncSession, interval_seconds: float = 1.5) -> None:
        super().__init__(name=f"VaultSyncMonitor-{session.vault_id}", daemon=True)
        self._session = session
        self._interval = interval_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Reconcile any changes detected in the authorized Vault
                if self._session.is_authorized:
                    self._session.reconcile()
            except Exception as exc:
                logger.debug(f"Monitor reconciliation cycle error: {exc}")
            # Wait for next check interval or stop signal
            self._stop_event.wait(self._interval)

    def stop(self) -> None:
        self._stop_event.set()
        # Non-blocking or short-timeout join to prevent UI freeze
        self.join(timeout=0.5)


__all__ = [
    "VaultSyncSession",
    "VaultSyncError",
    "VaultSyncMonitor",
    "BatchEmbeddingService",
    "SYNC_STATE_UNAUTHORIZED",
    "SYNC_STATE_AUTHORIZING",
    "SYNC_STATE_ACTIVE",
    "SYNC_STATE_CHANGE_DETECTED",
    "SYNC_STATE_IN_PROGRESS",
    "SYNC_STATE_FRESH",
    "SYNC_STATE_STALE",
    "SYNC_STATE_FAILED",
    "SYNC_STATE_REVOKED",
]
