"""Vector-similarity retrieval over an isolated LanceDB vector store.

The retriever embeds the query with a locally configured embedding
service and searches an explicitly supplied, isolated LanceDB table.
It implements the existing ``KnowledgeRetriever`` contract: candidates
are inert contextual evidence only.

Semantic boundaries preserved by construction:

* Retrieval is not decision-making; candidates decide nothing.
* Similarity is not identity, duplication, or authorization.
* The vector store is a retrieval index, never an authoritative
  knowledge store; positions survive only when the table schema
  records them (older tables simply lack the field).
* Persistent vector index data is never mutated by temporary input
  embedding or candidate search.
* Current-input embeddings are stored in a run-scoped temporary cache
  and released after retrieval processing.
"""
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from src.living_authenticity.database.lancedb_manager import (
    DatabaseOperationError,
    LanceDBManager,
    VaultMetadataError,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)

from .base_retriever import KnowledgeRetriever
from .cache import TemporaryEmbeddingCache
from .candidate import RetrievedCandidate
from .pipeline import RelevanceAssessment
from .result import RetrievalResult


@runtime_checkable
class VectorEmbeddingService(Protocol):
    """Anything able to embed one text for vector search."""

    def embed(self, text: str) -> Any:
        """Return the embedding vector for ``text``."""


def _validate_vector_output(vec: Any, expected_dim: int = 0) -> None:
    """Validate that vector output is well-formed, non-empty, and has expected dimension."""
    if vec is None:
        raise ValueError("embedding service returned None instead of a vector")
    if hasattr(vec, "__len__"):
        length = len(vec)
    elif hasattr(vec, "shape") and len(vec.shape) > 0:
        length = int(vec.shape[0])
    else:
        raise ValueError("embedding service returned an unshaped or non-vector object")
    if length <= 0:
        raise ValueError("embedding service returned an empty vector")
    if expected_dim > 0 and length != expected_dim:
        raise ValueError(
            f"embedding dimensionality mismatch: expected {expected_dim}, got {length}"
        )


def _unit_text(unit: KnowledgeUnit) -> str:
    """Best available text of a unit for embedding."""
    return (
        unit.cleaned_text
        or unit.normalized_text
        or unit.meaning
        or unit.original_text
        or ""
    )


def _position_of(row: dict) -> int:
    """Return the stored intra-file position, defaulting to 0."""
    value = row.get("position")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


class LanceDBVectorRetriever(KnowledgeRetriever):
    """Retrieve candidate knowledge units via vector similarity.

    ``overlap_score`` stays 0 and ``matched_terms`` stays empty: token
    evidence is the comparison stage's job, not this stage's. The vector
    store only selects which existing items are worth comparing.
    """

    def __init__(
        self,
        embedding_service: VectorEmbeddingService,
        db_manager: LanceDBManager,
        max_candidates: int = 5,
        cache: TemporaryEmbeddingCache | None = None,
        vault_id: str = "",
        observer: Any = None,
        validated_core_hash: str = "",
        core_boost_factor: float = 0.30,
        validated_core_source_path: str = "",
        validated_core: Any = None,
        raise_on_integrity_error: bool = False,
    ) -> None:
        if not isinstance(embedding_service, VectorEmbeddingService):
            raise TypeError("embedding_service must expose embed(text)")
        if not isinstance(db_manager, LanceDBManager):
            raise TypeError("db_manager must be a LanceDBManager")
        if isinstance(max_candidates, bool) or not isinstance(
            max_candidates, int
        ):
            raise TypeError("max_candidates must be an int")
        if max_candidates < 0:
            raise ValueError("max_candidates must be non-negative")
        if cache is not None and not isinstance(cache, TemporaryEmbeddingCache):
            raise TypeError("cache must be a TemporaryEmbeddingCache or None")
        if observer is not None and not callable(observer):
            raise TypeError("observer must be callable or None")
        self._embedding_service = embedding_service
        self._db_manager = db_manager
        self._max_candidates = max_candidates
        self._cache = cache
        self._vault_id = str(vault_id or "")
        self._observer = observer
        self._name = "lancedb_vector"
        self._core_boost_factor = float(core_boost_factor) if core_boost_factor is not None else 0.30
        self._raise_on_integrity_error = bool(raise_on_integrity_error)
        self._core_baseline_verified = True
        self._core_baseline_error = ""
        self.set_validated_core(validated_core)

    def _clear_validated_core(self) -> None:
        self.validated_core = None
        self._validated_core_hash = ""
        self._validated_core_source_path = ""
        self._core_record_manifest = {}

    def set_validated_core(self, validated_core: Any) -> None:
        from src.living_authenticity.knowledge.vault.core_validator import (
            is_trusted_validation_context,
            verify_core_units_provenance,
            compute_core_record_id,
            normalize_source_identity,
        )
        val_ctx = getattr(validated_core, "validation_context", None) if validated_core is not None else None
        if val_ctx is None or not is_trusted_validation_context(val_ctx):
            self._clear_validated_core()
            return

        units = getattr(validated_core, "units", None)
        if not units:
            self._clear_validated_core()
            return

        try:
            verify_core_units_provenance(units, context=validated_core, expected_core_hash=val_ctx.core_hash)
        except Exception:
            self._clear_validated_core()
            return

        record_manifest: dict[str, tuple[int, frozenset[str], str, str]] = {}
        for u in units:
            rec_id = compute_core_record_id(u, validated_core)
            valid_texts = {
                t.strip() for t in (
                    getattr(u, "cleaned_text", None),
                    getattr(u, "original_text", None),
                    getattr(u, "normalized_text", None),
                    getattr(u, "meaning", None),
                )
                if t and isinstance(t, str) and t.strip()
            }
            norm_source = normalize_source_identity(str(getattr(u, "source", "")))
            u_id = str(getattr(u, "id", ""))
            record_manifest[rec_id] = (
                int(getattr(u, "position", 0)),
                frozenset(valid_texts),
                norm_source,
                u_id,
            )

        self.validated_core = validated_core
        self._validated_core_hash = val_ctx.core_hash
        self._validated_core_source_path = val_ctx.source_path
        self._core_record_manifest = record_manifest

    def set_observer(self, observer: Any) -> None:
        if observer is not None and not callable(observer):
            raise TypeError("observer must be callable or None")
        self._observer = observer

    def set_validated_core_hash(self, core_hash: str) -> None:
        # Neutralized: raw hash alone cannot establish or modify validated Core authority
        pass

    def set_validated_core_source_path(self, path: str) -> None:
        # Neutralized: raw path alone cannot establish or modify validated Core authority
        pass

    def _emit(
        self,
        observer: Any,
        operation: str,
        message: str,
        phase: str = "completed",
        extra: dict | None = None,
        run_id: str = "",
    ) -> None:
        if observer is None or not callable(observer):
            return
        from src.living_authenticity.security.diagnostic import sanitize_diagnostic_text
        payload = {
            "kind": "status",
            "stage": "retrieval",
            "operation": operation,
            "phase": phase,
            "message": sanitize_diagnostic_text(message),
        }
        if run_id:
            payload["run_id"] = run_id
        if extra:
            sanitized_extra = {
                k: sanitize_diagnostic_text(v) if isinstance(v, str) else v
                for k, v in extra.items()
            }
            payload.update(sanitized_extra)
        try:
            observer(payload)
        except Exception as obs_exc:
            if not hasattr(self, "_telemetry_failures"):
                self._telemetry_failures = []
            self._telemetry_failures.append({
                "operation": operation,
                "phase": phase,
                "error": str(obs_exc) or type(obs_exc).__name__,
            })

    @property
    def name(self) -> str:
        return self._name

    @property
    def max_candidates(self) -> int:
        return self._max_candidates

    @property
    def cache(self) -> TemporaryEmbeddingCache | None:
        return self._cache

    @property
    def vault_id(self) -> str:
        return self._vault_id

    @property
    def core_boost_factor(self) -> float:
        return self._core_boost_factor

    def set_cache(self, cache: TemporaryEmbeddingCache | None) -> None:
        if cache is not None and not isinstance(cache, TemporaryEmbeddingCache):
            raise TypeError("cache must be a TemporaryEmbeddingCache or None")
        self._cache = cache

    def set_vault_id(self, vault_id: str) -> None:
        self._vault_id = str(vault_id or "")

    def _verify_core_database_baseline_integrity(
        self, table: Any, meta: dict, raise_on_error: bool = False
    ) -> tuple[bool, str]:
        """Verify the cryptographic and structural integrity of Core vector records before retrieval.

        Fail-closed invariant:
        If Core records exist in the database or if validated_core is configured:
        1. index_metadata MUST contain the approved baseline integrity fields:
           - core_vector_digest (64-char hex SHA-256)
           - core_vector_count (non-negative int)
           - core_record_ids (list of strings)
        2. The live database Core records MUST match:
           - len(live_core_rows) == core_vector_count
           - sorted(live_core_ids) == sorted(core_record_ids)
           - compute_core_vector_digest(live_core_rows) == core_vector_digest
        3. No ordinary row in the live database may carry a 'core_vec_' ID prefix.
        4. If validated_core is configured on the retriever:
           - meta['core_hash'] MUST match validated_core.sha256 exactly.

        Any violation raises a typed DatabaseOperationError or VaultMetadataCorruptionError/VaultMetadataMissingError,
        or marks the Core baseline as unverified/untrusted, ensuring that corrupted or out-of-band modified Core
        embeddings and records receive zero Core ranking elevation before candidate selection and relevance assessment.
        """
        from src.living_authenticity.database.lancedb_manager import (
            DatabaseOperationError,
            VaultMetadataMissingError,
            VaultMetadataCorruptionError,
        )
        from src.living_authenticity.knowledge.vault.core_validator import compute_core_vector_digest

        has_core_in_meta = bool(meta.get("core_hash") or meta.get("core_source_path") or "core_vector_digest" in meta)
        has_validated_core = self.validated_core is not None

        try:
            live_rows = table.to_arrow().to_pylist()
        except Exception as exc:
            err = f"Failed inspecting live vector database records: {exc}"
            if raise_on_error:
                raise DatabaseOperationError(err) from exc
            return False, err

        live_core_rows = [r for r in live_rows if isinstance(r, dict) and bool(r.get("is_core", False))]
        has_core_in_db = bool(live_core_rows)

        if not has_core_in_meta and not has_validated_core and not has_core_in_db:
            return True, ""

        # 1. Baseline Metadata Presence & Structure Checks
        if "core_vector_digest" not in meta:
            err = "Vector store index metadata is missing required Core baseline integrity digest ('core_vector_digest')"
            if raise_on_error:
                raise VaultMetadataMissingError(err)
            return False, err

        core_digest = meta.get("core_vector_digest")
        if not isinstance(core_digest, str):
            err = f"Vector store index metadata contains invalid 'core_vector_digest' type: {type(core_digest).__name__}"
            if raise_on_error:
                raise VaultMetadataCorruptionError(err)
            return False, err

        if "core_vector_count" not in meta:
            err = "Vector store index metadata is missing required Core record count ('core_vector_count')"
            if raise_on_error:
                raise VaultMetadataMissingError(err)
            return False, err
        core_count = meta.get("core_vector_count")
        if not isinstance(core_count, int) or isinstance(core_count, bool) or core_count < 0:
            err = f"Vector store index metadata contains invalid 'core_vector_count': {core_count}"
            if raise_on_error:
                raise VaultMetadataCorruptionError(err)
            return False, err

        if "core_record_ids" not in meta:
            err = "Vector store index metadata is missing required Core record IDs ('core_record_ids')"
            if raise_on_error:
                raise VaultMetadataMissingError(err)
            return False, err
        core_ids = meta.get("core_record_ids")
        if not isinstance(core_ids, list):
            err = f"Vector store index metadata contains invalid 'core_record_ids' type: {type(core_ids).__name__}"
            if raise_on_error:
                raise VaultMetadataCorruptionError(err)
            return False, err

        if core_count > 0:
            if len(core_digest) != 64 or not all(c in "0123456789abcdefABCDEF" for c in core_digest):
                err = f"Vector store index metadata contains malformed 'core_vector_digest' (expected 64-char hex SHA-256): '{core_digest}'"
                if raise_on_error:
                    raise VaultMetadataCorruptionError(err)
                return False, err
            if len(core_ids) != core_count:
                err = f"Vector store index metadata baseline inconsistency: core_record_ids length ({len(core_ids)}) != core_vector_count ({core_count})"
                if raise_on_error:
                    raise VaultMetadataCorruptionError(err)
                return False, err

        # 2. Live Database Core Record Verification
        # Check for rogue ordinary records carrying Core ID prefix
        rogue_core_ids = [
            str(r.get("id", "")) for r in live_rows
            if isinstance(r, dict) and not bool(r.get("is_core", False)) and str(r.get("id", "")).startswith("core_vec_")
        ]
        if rogue_core_ids:
            err = f"Detected ordinary vector record carrying Core record ID prefix 'core_vec_': {rogue_core_ids}"
            if raise_on_error:
                raise DatabaseOperationError(err)
            return False, err

        # Check Core record count
        if len(live_core_rows) != core_count:
            err = f"Vector store Core record count mismatch: metadata baseline expected {core_count}, but live database contains {len(live_core_rows)}"
            if raise_on_error:
                raise DatabaseOperationError(err)
            return False, err

        # Check Core record IDs
        live_core_ids = sorted(str(r.get("id", "")) for r in live_core_rows)
        expected_core_ids = sorted(str(i) for i in core_ids)
        if live_core_ids != expected_core_ids:
            err = "Vector store Core record IDs mismatch between metadata baseline and live database"
            if raise_on_error:
                raise DatabaseOperationError(err)
            return False, err

        # Check Core Vector Digest (detects out-of-band embedding or content tampering)
        if core_count > 0:
            live_digest = compute_core_vector_digest(live_core_rows)
            if live_digest != core_digest:
                err = "Vector store Core baseline integrity digest mismatch: live Core vectors or embeddings have been mutated out-of-band"
                if raise_on_error:
                    raise DatabaseOperationError(err)
                return False, err

        # 3. Validated Core Hash Binding Check
        if has_validated_core and self.validated_core is not None:
            val_hash = getattr(self.validated_core, "sha256", "")
            if not isinstance(val_hash, str) or not val_hash.strip():
                err = f"Retriever validated Core has malformed or missing hash: '{val_hash}'"
                if raise_on_error:
                    raise DatabaseOperationError(err)
                return False, err

            if "core_hash" not in meta:
                err = "Vector store index metadata is missing required 'core_hash' for validated Core"
                if raise_on_error:
                    raise VaultMetadataMissingError(err)
                return False, err

            raw_meta_hash = meta.get("core_hash")
            if not isinstance(raw_meta_hash, str) or not raw_meta_hash.strip():
                err = f"Vector store index metadata contains malformed or invalid 'core_hash': '{raw_meta_hash}'"
                if raise_on_error:
                    raise VaultMetadataCorruptionError(err)
                return False, err

            meta_hash = raw_meta_hash.strip()
            if meta_hash != val_hash.strip():
                err = f"Vector store Core hash '{meta_hash}' does not match active validated Core '{val_hash.strip()}'"
                if raise_on_error:
                    raise DatabaseOperationError(err)
                return False, err

        return True, ""

    def retrieve(
        self,
        query: KnowledgeUnit,
        corpus: Sequence[Any] = (),
        observer: Any = None,
        run_id: str = "",
        should_cancel: Any = None,
        raise_on_integrity_error: bool | None = None,
    ) -> RetrievalResult:
        if not isinstance(query, KnowledgeUnit):
            raise TypeError(
                "query must be a KnowledgeUnit, got " + type(query).__name__
            )
        self._telemetry_failures = []
        eff_obs = observer if observer is not None else self._observer

        def _is_canc() -> bool:
            if should_cancel is None:
                return False
            if callable(should_cancel):
                return bool(should_cancel())
            return bool(should_cancel)

        if _is_canc():
            self._emit(
                eff_obs,
                "query_preparation",
                f"Vector retrieval cancelled before query preparation for unit '{query.id}'",
                phase="cancelled",
                run_id=run_id,
            )
            return RetrievalResult(
                query_unit_id=query.id,
                query_source=query.source,
                query_position=query.position,
                strategy=self._name,
                candidates=(),
                corpus_size=None,
                query_terms=(),
                note="Retrieval cancelled by user request",
                metadata={"cancelled": True, "cancelled_at_substep": "query_preparation"},
            )

        # Capability 1: Query Preparation
        self._emit(
            eff_obs,
            "query_preparation",
            f"Preparing query for vector search from unit '{query.id}'",
            phase="started",
            extra={"query_unit_id": query.id, "strategy": self._name},
            run_id=run_id,
        )
        try:
            text = _unit_text(query)
        except Exception as exc:
            self._emit(
                eff_obs,
                "query_preparation",
                f"Query preparation failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise
        self._emit(
            eff_obs,
            "query_preparation",
            f"Query prepared for vector search from unit '{query.id}'",
            phase="completed",
            extra={"query_unit_id": query.id, "strategy": self._name},
            run_id=run_id,
        )

        if not text.strip() or self._max_candidates == 0:
            metadata = self._cache.to_audit_dict() if self._cache is not None else {}
            metadata.setdefault("capabilities", (
                "query_preparation",
                "candidate_retrieval",
                "relevance_assessment",
                "candidate_identification",
                "retrieval_evidence_recording",
            ))
            metadata.setdefault("corpus_size_reporting", "unexamined")
            metadata.setdefault("corpus_size_unknown", True)
            # Truthfully complete remaining stages with 0 items without accessing database
            reason_skip = "query text is empty" if not text.strip() else "max_candidates is 0"
            for op, msg in (
                ("candidate_retrieval", f"Vector search skipped: {reason_skip}"),
                ("relevance_assessment", "Relevance assessment skipped: no candidates"),
                ("candidate_identification", "Candidate identification skipped: no candidates"),
            ):
                self._emit(eff_obs, op, msg, phase="started", run_id=run_id)
                self._emit(eff_obs, op, msg, phase="completed", extra={"candidate_count": 0}, run_id=run_id)

            if getattr(self, "_telemetry_failures", None):
                metadata["telemetry_delivery_failures"] = list(self._telemetry_failures)

            note_text = (
                "Query has no analyzable content; no vector candidates retrieved."
                if not text.strip()
                else "max_candidates=0 configured; returning empty candidates."
            )

            # Capability 5: Retrieval Evidence Recording
            self._emit(
                eff_obs,
                "retrieval_evidence_recording",
                "Recording empty query vector evidence into RetrievalResult",
                phase="started",
                run_id=run_id,
            )
            try:
                result = RetrievalResult(
                    query_unit_id=query.id,
                    query_source=query.source,
                    query_position=query.position,
                    strategy=self._name,
                    candidates=(),
                    corpus_size=None,
                    query_terms=(),
                    note=note_text,
                    metadata=metadata,
                )
            except Exception as exc:
                self._emit(
                    eff_obs,
                    "retrieval_evidence_recording",
                    f"Retrieval evidence recording failed: {exc}",
                    phase="failed",
                    extra={"error": str(exc)},
                    run_id=run_id,
                )
                raise

            self._emit(
                eff_obs,
                "retrieval_evidence_recording",
                "Evidence recorded: empty query result",
                phase="completed",
                extra={"candidate_count": 0, "has_candidates": False},
                run_id=run_id,
            )
            if getattr(self, "_telemetry_failures", None):
                result.metadata["telemetry_delivery_failures"] = list(self._telemetry_failures)

            return result

        if _is_canc():
            self._emit(
                eff_obs,
                "candidate_retrieval",
                f"Vector retrieval cancelled before candidate retrieval for unit '{query.id}'",
                phase="cancelled",
                run_id=run_id,
            )
            return RetrievalResult(
                query_unit_id=query.id,
                query_source=query.source,
                query_position=query.position,
                strategy=self._name,
                candidates=(),
                corpus_size=None,
                query_terms=(),
                note="Retrieval cancelled by user request",
                metadata={"cancelled": True, "cancelled_at_substep": "candidate_retrieval"},
            )

        # Capability 2: Candidate Retrieval
        # Emitted BEFORE acquiring the search table, inspecting collection, or generating embeddings
        self._emit(
            eff_obs,
            "candidate_retrieval",
            "Acquiring search table, inspecting searchable collection, and generating embeddings",
            phase="started",
            run_id=run_id,
        )
        def _check_store_version(initial_ver: str) -> None:
            try:
                if hasattr(self._db_manager, "get_table") and callable(self._db_manager.get_table):
                    live_tbl = self._db_manager.get_table()
                elif hasattr(self._db_manager, "db") and self._db_manager.db is not None:
                    live_tbl = self._db_manager.db.open_table("knowledge_vectors")
                else:
                    raise DatabaseOperationError("Vector database handle unavailable to verify table version")
            except Exception as exc:
                raise DatabaseOperationError(f"Failed opening refreshed vector table: {exc}") from exc

            live_ver = getattr(live_tbl, "version", None)
            if live_ver is None or not str(live_ver).strip():
                raise DatabaseOperationError("Refreshed table version missing or invalid")

            if str(live_ver).strip() != str(initial_ver).strip():
                raise DatabaseOperationError("Store mutated during retrieval: fail closed")

        try:
            from src.living_authenticity.database.lancedb_manager import (
                DatabaseOperationError,
                VaultMetadataMissingError,
                VaultMetadataCorruptionError,
            )
            state_info = self._db_manager.inspect_store_state()
            state = state_info.get("state")
            if state == "missing_metadata":
                raise VaultMetadataMissingError(
                    f"Cannot retrieve from vector store with missing metadata (state: '{state}')"
                )
            if state == "corrupted_metadata":
                raise VaultMetadataCorruptionError(
                    f"Cannot retrieve from a corrupted vector store (state: '{state}')"
                )
            if state == "error":
                raise DatabaseOperationError(
                    f"Cannot retrieve from a failed vector store (state: '{state}', error: '{state_info.get('error')}')"
                )
            if state == "interrupted_sync":
                raise DatabaseOperationError(
                    "Cannot retrieve from an unverified, incomplete or uncommitted vector store (interrupted synchronization)"
                )
            if state == "inconsistent_baseline":
                raise DatabaseOperationError(
                    "Cannot retrieve from a vector store with inconsistent vector and metadata baseline"
                )

            v_count = state_info.get("vector_count", 0)
            meta: dict[str, Any] = state_info.get("metadata") or {}
            if v_count == 0:
                if state not in ("genuine_first_init", "uninitialized_empty", "valid"):
                    raise DatabaseOperationError(
                        f"Cannot retrieve from an unverified or invalid vector store (state: '{state}')"
                    )
            else:
                if state != "valid":
                    raise DatabaseOperationError(
                        f"Cannot retrieve from an unverified or invalid vector store (state: '{state}')"
                    )
                meta_status = str(meta.get("status", ""))
                if meta_status != "fresh" or meta.get("unverified") is True:
                    raise DatabaseOperationError(
                        f"Cannot retrieve from an unverified, incomplete or uncommitted vector store (status: '{meta_status or 'unverified'}')"
                    )
                if "manifest" in meta and not isinstance(meta.get("manifest"), dict):
                    raise VaultMetadataCorruptionError(
                        "Cannot retrieve from vector store: manifest is malformed in metadata"
                    )

            table = self._db_manager.get_table()
            raw_ver_pre = getattr(table, "version", None)
            if raw_ver_pre is None or not str(raw_ver_pre).strip():
                raise DatabaseOperationError("Vector table version is missing or invalid before baseline verification")
            v_pre = str(raw_ver_pre).strip()

            eff_raise = raise_on_integrity_error if raise_on_integrity_error is not None else self._raise_on_integrity_error
            if v_count > 0:
                is_base_valid, base_err = self._verify_core_database_baseline_integrity(
                    table=table, meta=meta, raise_on_error=eff_raise
                )
                self._core_baseline_verified = is_base_valid
                self._core_baseline_error = base_err
            else:
                self._core_baseline_verified = True
                self._core_baseline_error = ""

            # Post-verification table version snapshot to close TOCTOU window
            try:
                live_tbl_post = self._db_manager.get_table()
            except Exception as exc:
                raise DatabaseOperationError(
                    f"Failed reacquiring vector table for post-verification snapshot: {exc}"
                ) from exc

            if live_tbl_post is None:
                raise DatabaseOperationError(
                    "Reacquired vector table is None after baseline verification"
                )

            raw_ver_post = getattr(live_tbl_post, "version", None)
            if raw_ver_post is None or not str(raw_ver_post).strip():
                raise DatabaseOperationError(
                    "Refreshed table version missing or invalid after baseline verification"
                )
            v_post = str(raw_ver_post).strip()

            if v_pre != v_post:
                raise DatabaseOperationError("Store mutated during baseline verification: fail closed")

            initial_version = v_post
            table = live_tbl_post
            corpus_size: int | None = None
            corpus_mode = "unknown"
            try:
                if hasattr(table, "count_rows") and callable(table.count_rows):
                    corpus_size = int(table.count_rows())
                    corpus_mode = "vector_index_rows"
                elif hasattr(table, "__len__"):
                    corpus_size = int(len(table))
                    corpus_mode = "vector_index_rows"
            except Exception:
                corpus_size = None
                corpus_mode = "unknown"

            if self._cache is not None:
                self._cache.mark_embedding_start()
                if self._cache.has(query.id):
                    query_vec = self._cache.get(query.id)
                else:
                    try:
                        query_vec = self._embedding_service.embed(text)
                        _validate_vector_output(
                            query_vec, expected_dim=self._cache.dimension
                        )
                        self._cache.put(query.id, query_vec)
                    except Exception as exc:
                        self._cache.record_embedding_failure(query.id, str(exc))
                        raise
                self._cache.mark_embedding_end()
                self._cache.mark_retrieval_start()
            else:
                query_vec = self._embedding_service.embed(text)
                _validate_vector_output(query_vec, expected_dim=0)

            search_vec = (
                query_vec.tolist()
                if hasattr(query_vec, "tolist")
                else list(query_vec)
            )
            try:
                if not hasattr(table, "search") or not callable(getattr(table, "search", None)):
                    raise DatabaseOperationError("Reacquired vector table lacks callable search method")
                rows = table.search(search_vec).limit(self._max_candidates).to_list()
            except (DatabaseOperationError, VaultMetadataError):
                raise
            except Exception as search_exc:
                raise DatabaseOperationError(f"Vector search failed: {search_exc}") from search_exc

            _check_store_version(initial_version)
        except Exception as exc:
            self._emit(
                eff_obs,
                "candidate_retrieval",
                f"Candidate retrieval failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise

        self._emit(
            eff_obs,
            "candidate_retrieval",
            f"Vector candidate retrieval: {len(rows)} matching items returned from index",
            phase="completed",
            extra={
                "neighbor_count": len(rows),
                "corpus_size": corpus_size if corpus_size is not None else "unknown",
            },
            run_id=run_id,
        )

        if _is_canc():
            self._emit(
                eff_obs,
                "relevance_assessment",
                f"Vector retrieval cancelled before relevance assessment for unit '{query.id}'",
                phase="cancelled",
                run_id=run_id,
            )
            return RetrievalResult(
                query_unit_id=query.id,
                query_source=query.source,
                query_position=query.position,
                strategy=self._name,
                candidates=(),
                corpus_size=corpus_size,
                query_terms=(),
                note="Retrieval cancelled by user request",
                metadata={"cancelled": True, "cancelled_at_substep": "relevance_assessment"},
            )

        # Capability 3: Relevance Assessment
        self._emit(
            eff_obs,
            "relevance_assessment",
            f"Evaluating vector distance and relevance for {len(rows)} candidates",
            phase="started",
            run_id=run_id,
        )
        try:
            assessments: list[RelevanceAssessment] = []
            for row in rows:
                row_id = row.get("id", "")
                if not isinstance(row_id, str) or not row_id:
                    continue
                if row_id == query.id:
                    # Invariant: Never assess query unit as its own candidate
                    continue
                source_val = (
                    row.get("source", "")
                    if isinstance(row.get("source", ""), str)
                    else ""
                )
                pos_val = _position_of(row)
                text_val = (
                    row.get("text", "")
                    if isinstance(row.get("text", ""), str)
                    else ""
                )
                raw_dist = row.get("_distance")
                dist_val: float | None = None
                if isinstance(raw_dist, (int, float)) and not isinstance(raw_dist, bool):
                    dist_val = float(raw_dist)

                similarity_val = (
                    float(1.0 / (1.0 + dist_val))
                    if dist_val is not None and dist_val >= 0
                    else 0.0
                )
                evidence = (
                    f"vector_distance={dist_val:.4f}"
                    if dist_val is not None
                    else "vector_similarity"
                )
                raw_is_core = bool(row.get("is_core", False))
                row_core_hash = str(row.get("core_hash", "") or "")
                row_source = str(row.get("source", "") or "")
                row_id = str(row.get("id", "") or "")

                from src.living_authenticity.knowledge.vault.core_validator import normalize_source_identity
                is_verified_core = False
                verified_unit_id = ""
                record_manifest = getattr(self, "_core_record_manifest", {})
                if (
                    getattr(self, "_core_baseline_verified", True)
                    and self._validated_core_hash
                    and self._validated_core_source_path
                    and raw_is_core
                    and row_core_hash
                    and row_core_hash == self._validated_core_hash
                    and self.validated_core is not None
                    and record_manifest
                ):
                    if normalize_source_identity(row_source) == normalize_source_identity(self._validated_core_source_path):
                        if row_id in record_manifest:
                            pos_exp, texts_exp, norm_src_exp, u_id_exp = record_manifest[row_id]
                            clean_text = text_val.strip()
                            if pos_val == pos_exp and clean_text in texts_exp and normalize_source_identity(row_source) == norm_src_exp:
                                is_verified_core = True
                                verified_unit_id = u_id_exp

                assessments.append(
                    RelevanceAssessment(
                        candidate_unit_id=verified_unit_id if is_verified_core else row_id,
                        candidate_source=source_val,
                        candidate_position=pos_val,
                        candidate_text=text_val,
                        overlap_score=0,
                        matched_terms=(),
                        similarity_score=similarity_val,
                        evidence_basis=evidence,
                        strategy=self._name,
                        vector_distance=dist_val,
                        is_core=is_verified_core,
                        core_hash=row_core_hash if is_verified_core else "",
                    )
                )
                _check_store_version(initial_version)
        except Exception as exc:
            self._emit(
                eff_obs,
                "relevance_assessment",
                f"Relevance assessment failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise

        self._emit(
            eff_obs,
            "relevance_assessment",
            f"Vector relevance assessment: evaluated {len(assessments)} candidates via vector similarity",
            phase="completed",
            extra={"assessed_count": len(assessments)},
            run_id=run_id,
        )

        if _is_canc():
            self._emit(
                eff_obs,
                "candidate_identification",
                f"Vector retrieval cancelled before candidate identification for unit '{query.id}'",
                phase="cancelled",
                run_id=run_id,
            )
            return RetrievalResult(
                query_unit_id=query.id,
                query_source=query.source,
                query_position=query.position,
                strategy=self._name,
                candidates=(),
                corpus_size=corpus_size,
                query_terms=(),
                note="Retrieval cancelled by user request",
                metadata={"cancelled": True, "cancelled_at_substep": "candidate_identification"},
            )

        # Capability 4: Candidate Identification
        self._emit(
            eff_obs,
            "candidate_identification",
            "Establishing canonical candidate identities and ordering",
            phase="started",
            run_id=run_id,
        )
        try:
            from src.living_authenticity.knowledge.vault.core_validator import normalize_source_identity

            def _ranking_key(a: RelevanceAssessment):
                is_verified = bool(
                    getattr(self, "_core_baseline_verified", True)
                    and self._validated_core_hash
                    and self._validated_core_source_path
                    and a.is_core
                    and a.core_hash
                    and a.core_hash == self._validated_core_hash
                    and normalize_source_identity(a.candidate_source) == normalize_source_identity(self._validated_core_source_path)
                )
                boost = (1.0 + self._core_boost_factor) if (is_verified and self._core_boost_factor > 0) else 1.0
                effective_score = round(a.similarity_score * boost, 6)
                return (
                    -effective_score,
                    -round(a.similarity_score, 6),
                    a.candidate_source,
                    a.candidate_position,
                    a.candidate_unit_id,
                )

            sorted_assessments = sorted(assessments, key=_ranking_key)
            seen_keys: set[str] = set()
            candidates: list[RetrievedCandidate] = []
            for a in sorted_assessments:
                canon_id = f"{a.candidate_source}:{a.candidate_position}:{a.candidate_unit_id}"
                if canon_id in seen_keys:
                    continue
                seen_keys.add(canon_id)
                is_cand_core = bool(
                    getattr(self, "_core_baseline_verified", True)
                    and self._validated_core_hash
                    and self._validated_core_source_path
                    and a.is_core
                    and a.core_hash
                    and a.core_hash == self._validated_core_hash
                    and normalize_source_identity(a.candidate_source) == normalize_source_identity(self._validated_core_source_path)
                )
                candidates.append(
                    RetrievedCandidate(
                        unit_id=a.candidate_unit_id,
                        source=a.candidate_source,
                        position=a.candidate_position,
                        text=a.candidate_text,
                        matched_terms=a.matched_terms,
                        overlap_score=a.overlap_score,
                        retriever=self._name,
                        evidence_basis=a.evidence_basis,
                        candidate_identity_key=canon_id,
                        vector_distance=a.vector_distance,
                        is_core=is_cand_core,
                        core_hash=a.core_hash if is_cand_core else "",
                    )
                )
                if len(candidates) >= self._max_candidates:
                    break
        except Exception as exc:
            self._emit(
                eff_obs,
                "candidate_identification",
                f"Candidate identification failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise

        self._emit(
            eff_obs,
            "candidate_identification",
            f"Candidate identification: established {len(candidates)} stable vector candidate identities",
            phase="completed",
            extra={"candidate_count": len(candidates)},
            run_id=run_id,
        )

        if self._cache is not None:
            self._cache.mark_retrieval_end(candidate_count=len(candidates))
            metadata = self._cache.to_audit_dict()
        else:
            metadata = {}

        if self._vault_id:
            metadata["vault_id"] = self._vault_id

        metadata.setdefault(
            "capabilities",
            (
                "query_preparation",
                "candidate_retrieval",
                "relevance_assessment",
                "candidate_identification",
                "retrieval_evidence_recording",
            ),
        )
        metadata.setdefault("corpus_size_reporting", corpus_mode)
        if corpus_size is None:
            metadata.setdefault("corpus_size_unknown", True)

        if candidates:
            note = (
                "Vector similarity selected "
                + str(len(candidates))
                + " stored item(s) as contextual evidence only; "
                "similarity is not identity, confidence, or authorization."
            )
        else:
            note = (
                "The isolated vector store returned no stored item for "
                "the query."
            )

        if getattr(self, "_telemetry_failures", None):
            metadata["telemetry_delivery_failures"] = list(self._telemetry_failures)

        if not getattr(self, "_core_baseline_verified", True):
            metadata["core_baseline_verified"] = False
            metadata["core_trusted"] = False
            metadata["core_baseline_error"] = getattr(self, "_core_baseline_error", "")
            note = f"Vector store Core baseline integrity unverified: {getattr(self, '_core_baseline_error', '')}"
        else:
            metadata["core_baseline_verified"] = True
            metadata["core_trusted"] = True

        if _is_canc():
            self._emit(
                eff_obs,
                "retrieval_evidence_recording",
                f"Vector retrieval cancelled before evidence recording for unit '{query.id}'",
                phase="cancelled",
                run_id=run_id,
            )
            return RetrievalResult(
                query_unit_id=query.id,
                query_source=query.source,
                query_position=query.position,
                strategy=self._name,
                candidates=(),
                corpus_size=corpus_size,
                query_terms=(),
                note="Retrieval cancelled by user request",
                metadata={"cancelled": True, "cancelled_at_substep": "retrieval_evidence_recording"},
            )

        # Capability 5: Retrieval Evidence Recording
        self._emit(
            eff_obs,
            "retrieval_evidence_recording",
            "Recording vector evidence and metadata into RetrievalResult",
            phase="started",
            run_id=run_id,
        )
        try:
            result = RetrievalResult(
                query_unit_id=query.id,
                query_source=query.source,
                query_position=query.position,
                strategy=self._name,
                candidates=tuple(candidates),
                corpus_size=corpus_size,
                query_terms=(),
                note=note,
                metadata=metadata,
            )
        except Exception as exc:
            self._emit(
                eff_obs,
                "retrieval_evidence_recording",
                f"Retrieval evidence recording failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise
        self._emit(
            eff_obs,
            "retrieval_evidence_recording",
            f"Retrieval evidence recorded: {len(candidates)} vector candidates bound into RetrievalResult",
            phase="completed",
            extra={"has_candidates": bool(candidates)},
            run_id=run_id,
        )
        if getattr(self, "_telemetry_failures", None):
            result.metadata["telemetry_delivery_failures"] = list(self._telemetry_failures)

        return result
