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

from src.living_authenticity.database.lancedb_manager import LanceDBManager
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

    def set_observer(self, observer: Any) -> None:
        if observer is not None and not callable(observer):
            raise TypeError("observer must be callable or None")
        self._observer = observer

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
        payload = {
            "kind": "status",
            "stage": "retrieval",
            "operation": operation,
            "phase": phase,
            "message": message,
        }
        if run_id:
            payload["run_id"] = run_id
        if extra:
            payload.update(extra)
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

    def set_cache(self, cache: TemporaryEmbeddingCache | None) -> None:
        if cache is not None and not isinstance(cache, TemporaryEmbeddingCache):
            raise TypeError("cache must be a TemporaryEmbeddingCache or None")
        self._cache = cache

    def set_vault_id(self, vault_id: str) -> None:
        self._vault_id = str(vault_id or "")

    def retrieve(
        self,
        query: KnowledgeUnit,
        corpus: Sequence[Any] = (),
        observer: Any = None,
        run_id: str = "",
    ) -> RetrievalResult:
        if not isinstance(query, KnowledgeUnit):
            raise TypeError(
                "query must be a KnowledgeUnit, got " + type(query).__name__
            )
        self._telemetry_failures = []
        eff_obs = observer if observer is not None else self._observer

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

        # Capability 2: Candidate Retrieval
        # Emitted BEFORE acquiring the search table, inspecting collection, or generating embeddings
        self._emit(
            eff_obs,
            "candidate_retrieval",
            "Acquiring search table, inspecting searchable collection, and generating embeddings",
            phase="started",
            run_id=run_id,
        )
        try:
            table = self._db_manager.get_table()
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
            rows = table.search(search_vec).limit(self._max_candidates).to_list()
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
                assessments.append(
                    RelevanceAssessment(
                        candidate_unit_id=row_id,
                        candidate_source=source_val,
                        candidate_position=pos_val,
                        candidate_text=text_val,
                        overlap_score=0,
                        matched_terms=(),
                        similarity_score=similarity_val,
                        evidence_basis=evidence,
                        strategy=self._name,
                        vector_distance=dist_val,
                    )
                )
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

        # Capability 4: Candidate Identification
        self._emit(
            eff_obs,
            "candidate_identification",
            "Establishing canonical candidate identities and ordering",
            phase="started",
            run_id=run_id,
        )
        try:
            sorted_assessments = sorted(
                assessments,
                key=lambda a: (
                    -a.similarity_score,
                    a.candidate_source,
                    a.candidate_position,
                    a.candidate_unit_id,
                ),
            )
            seen_keys: set[str] = set()
            candidates: list[RetrievedCandidate] = []
            for a in sorted_assessments:
                canon_id = f"{a.candidate_source}:{a.candidate_position}:{a.candidate_unit_id}"
                if canon_id in seen_keys:
                    continue
                seen_keys.add(canon_id)
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
