"""Isolated run-scoped temporary cache for current-input embeddings.

Separation of concerns:
1. Persistent vector index data: represents previously ingested knowledge corpus.
   The persistent index is derived data, never authoritative knowledge, and is
   updated only through explicit ingestion/index synchronization.
2. Temporary input embeddings: generated from the active input and its processed
   chunks for the active Retrieval operation. These embeddings are stored in this
   isolated, run-scoped temporary cache, used for candidate search, and cleaned
   up after processing finishes (success, failure, cancellation, safe-stop).

Temporary embeddings must NEVER:
* be inserted into the persistent knowledge vector index;
* become authoritative knowledge or vault notes;
* survive into unrelated runs or cross-pollinate across runs/users;
* be logged as raw vectors in audit receipts.

Provider Independence:
This module does not import or depend on BGE-M3, LanceDB, or any vendor SDK.
Embeddings are accepted as generic array/sequence objects.
"""
from dataclasses import dataclass
import datetime
import uuid
from typing import Any


class CacheCleanupError(RuntimeError):
    """Raised when temporary cache cleanup encounters an error."""


@dataclass(frozen=True)
class CacheReceipt:
    """Audit-safe summary receipt of a temporary embedding cache lifecycle.

    Contains identifiers, counts, timestamps, and cleanup outcomes.
    Never exposes raw vector arrays or private input text.
    """

    scope_id: str
    run_id: str
    input_ref: str
    provider_name: str
    model_name: str
    model_version: str
    dimension: int
    chunk_count: int
    embeddings_generated: int
    embeddings_failed: int
    init_time_iso: str
    cleanup_time_iso: str = ""
    cleaned_up: bool = False
    cleanup_error: str = ""
    embedding_start_time_iso: str = ""
    embedding_end_time_iso: str = ""
    retrieval_start_time_iso: str = ""
    retrieval_end_time_iso: str = ""
    candidate_count: int = 0


class TemporaryEmbeddingCache:
    """Isolated in-memory cache for temporary current-input embeddings.

    Bounded to one active run/retrieval operation. Discards all temporary
    vectors upon cleanup.
    """

    def __init__(
        self,
        run_id: str,
        input_ref: str = "",
        provider_name: str = "",
        model_name: str = "",
        model_version: str = "",
        dimension: int = 0,
        chunk_count: int = 0,
        scope_id: str | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        self._run_id = run_id.strip()
        self._scope_id = scope_id or f"cache-{self._run_id}-{uuid.uuid4().hex[:8]}"
        self._input_ref = str(input_ref)
        self._provider_name = str(provider_name)
        self._model_name = str(model_name)
        self._model_version = str(model_version)
        self._dimension = int(dimension)
        self._chunk_count = int(chunk_count)

        self._init_time_iso = (
            datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        self._cleanup_time_iso = ""
        self._cleaned_up = False
        self._cleanup_error = ""

        self._embedding_start_time_iso = ""
        self._embedding_end_time_iso = ""
        self._retrieval_start_time_iso = ""
        self._retrieval_end_time_iso = ""
        self._candidate_count = 0

        self._entries: dict[str, Any] = {}
        self._failed_entries: dict[str, str] = {}
        self._generated_count = 0
        self._fail_cleanup_for_testing = False

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def input_ref(self) -> str:
        return self._input_ref

    @property
    def cleaned_up(self) -> bool:
        return self._cleaned_up

    @property
    def cleanup_error(self) -> str:
        return self._cleanup_error

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    @chunk_count.setter
    def chunk_count(self, value: int) -> None:
        self._chunk_count = int(value)

    @property
    def generated_count(self) -> int:
        return self._generated_count

    @property
    def failed_count(self) -> int:
        return len(self._failed_entries)

    def put(self, key: str, vector: Any) -> None:
        """Store one temporary embedding vector for unit/query ``key``."""
        if self._cleaned_up:
            raise RuntimeError(
                f"cannot store embedding in cleaned-up cache (scope={self._scope_id})"
            )
        if not isinstance(key, str) or not key.strip():
            raise ValueError("embedding key must be a non-empty string")
        if vector is None:
            raise ValueError("embedding vector cannot be None")
        self._entries[key] = vector
        self._generated_count += 1

    def get(self, key: str) -> Any:
        """Retrieve stored temporary vector for ``key``."""
        if self._cleaned_up:
            raise RuntimeError(
                f"cannot access cleaned-up cache (scope={self._scope_id})"
            )
        return self._entries.get(key)

    def has(self, key: str) -> bool:
        """Return True if ``key`` is currently stored in this temporary cache."""
        if self._cleaned_up:
            return False
        return key in self._entries

    def record_embedding_failure(self, key: str, error: str) -> None:
        """Record diagnostic details of a failed embedding operation."""
        self._failed_entries[key] = str(error)

    def mark_embedding_start(self) -> None:
        if not self._embedding_start_time_iso:
            self._embedding_start_time_iso = (
                datetime.datetime.now(datetime.timezone.utc).isoformat()
            )

    def mark_embedding_end(self) -> None:
        self._embedding_end_time_iso = (
            datetime.datetime.now(datetime.timezone.utc).isoformat()
        )

    def mark_retrieval_start(self) -> None:
        if not self._retrieval_start_time_iso:
            self._retrieval_start_time_iso = (
                datetime.datetime.now(datetime.timezone.utc).isoformat()
            )

    def mark_retrieval_end(self, candidate_count: int = 0) -> None:
        self._retrieval_end_time_iso = (
            datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        self._candidate_count = candidate_count

    def cleanup(self) -> bool:
        """Release temporary embeddings and mark the cache cleaned up.

        Returns True on success; returns False if cleanup encounters an error.
        Guarantees that stored vectors in _entries are cleared even if an
        error occurs or is simulated.
        """
        if self._cleaned_up:
            return True
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._cleanup_time_iso = now
        try:
            if self._fail_cleanup_for_testing:
                raise CacheCleanupError("simulated cache release failure")
            self._entries.clear()
            self._cleaned_up = True
            self._cleanup_error = ""
            return True
        except Exception as exc:
            self._entries.clear()
            self._cleaned_up = False
            self._cleanup_error = str(exc)
            return False

    def receipt(self) -> CacheReceipt:
        """Return an audit-safe receipt representing the lifecycle and metrics."""
        return CacheReceipt(
            scope_id=self._scope_id,
            run_id=self._run_id,
            input_ref=self._input_ref,
            provider_name=self._provider_name,
            model_name=self._model_name,
            model_version=self._model_version,
            dimension=self._dimension,
            chunk_count=self._chunk_count,
            embeddings_generated=self._generated_count,
            embeddings_failed=len(self._failed_entries),
            init_time_iso=self._init_time_iso,
            cleanup_time_iso=self._cleanup_time_iso,
            cleaned_up=self._cleaned_up,
            cleanup_error=self._cleanup_error,
            embedding_start_time_iso=self._embedding_start_time_iso,
            embedding_end_time_iso=self._embedding_end_time_iso,
            retrieval_start_time_iso=self._retrieval_start_time_iso,
            retrieval_end_time_iso=self._retrieval_end_time_iso,
            candidate_count=self._candidate_count,
        )

    def to_audit_dict(self) -> dict[str, Any]:
        """Convert receipt to dictionary for audit records."""
        rec = self.receipt()
        return {
            "scope_id": rec.scope_id,
            "run_id": rec.run_id,
            "input_ref": rec.input_ref,
            "provider_name": rec.provider_name,
            "model_name": rec.model_name,
            "model_version": rec.model_version,
            "dimension": rec.dimension,
            "chunk_count": rec.chunk_count,
            "embeddings_generated": rec.embeddings_generated,
            "embeddings_failed": rec.embeddings_failed,
            "init_time": rec.init_time_iso,
            "cleanup_time": rec.cleanup_time_iso,
            "cleaned_up": rec.cleaned_up,
            "cleanup_error": rec.cleanup_error,
            "embedding_start_time": rec.embedding_start_time_iso,
            "embedding_end_time": rec.embedding_end_time_iso,
            "retrieval_start_time": rec.retrieval_start_time_iso,
            "retrieval_end_time": rec.retrieval_end_time_iso,
            "candidate_count": rec.candidate_count,
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False

    def __repr__(self) -> str:
        return (
            f"<TemporaryEmbeddingCache scope_id='{self._scope_id}' "
            f"run_id='{self._run_id}' entries={len(self._entries)} "
            f"cleaned_up={self._cleaned_up}>"
        )
