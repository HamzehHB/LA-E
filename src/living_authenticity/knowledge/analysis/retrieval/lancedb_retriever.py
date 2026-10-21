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
    ) -> None:
        if not isinstance(embedding_service, VectorEmbeddingService):
            raise TypeError("embedding_service must expose embed(text)")
        if not isinstance(db_manager, LanceDBManager):
            raise TypeError("db_manager must be a LanceDBManager")
        if isinstance(max_candidates, bool) or not isinstance(
            max_candidates, int
        ):
            raise TypeError("max_candidates must be an int")
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        if cache is not None and not isinstance(cache, TemporaryEmbeddingCache):
            raise TypeError("cache must be a TemporaryEmbeddingCache or None")
        self._embedding_service = embedding_service
        self._db_manager = db_manager
        self._max_candidates = max_candidates
        self._cache = cache
        self._vault_id = str(vault_id or "")
        self._name = "lancedb_vector"

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
        self, query: KnowledgeUnit, corpus: Sequence[Any]
    ) -> RetrievalResult:
        if not isinstance(query, KnowledgeUnit):
            raise TypeError(
                "query must be a KnowledgeUnit, got " + type(query).__name__
            )
        text = _unit_text(query)
        if not text.strip():
            metadata = self._cache.to_audit_dict() if self._cache is not None else {}
            return RetrievalResult(
                query_unit_id=query.id,
                query_source=query.source,
                query_position=query.position,
                strategy=self._name,
                candidates=(),
                corpus_size=len(corpus),
                query_terms=(),
                note="Query has no analyzable content; no vector candidates retrieved.",
                metadata=metadata,
            )

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

        table = self._db_manager.get_table()
        search_vec = (
            query_vec.tolist()
            if hasattr(query_vec, "tolist")
            else list(query_vec)
        )
        rows = table.search(search_vec).limit(self._max_candidates).to_list()
        candidates = []
        for row in rows:
            row_id = row.get("id", "")
            if not isinstance(row_id, str) or not row_id:
                continue
            if row_id == query.id:
                continue
            candidates.append(
                RetrievedCandidate(
                    unit_id=row_id,
                    source=row.get("source", "")
                    if isinstance(row.get("source", ""), str)
                    else "",
                    position=_position_of(row),
                    text=row.get("text", "")
                    if isinstance(row.get("text", ""), str)
                    else "",
                    matched_terms=(),
                    overlap_score=0,
                    retriever=self._name,
                )
            )

        if self._cache is not None:
            self._cache.mark_retrieval_end(candidate_count=len(candidates))
            metadata = self._cache.to_audit_dict()
        else:
            metadata = {}

        if self._vault_id:
            metadata["vault_id"] = self._vault_id

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

        return RetrievalResult(
            query_unit_id=query.id,
            query_source=query.source,
            query_position=query.position,
            strategy=self._name,
            candidates=tuple(candidates),
            corpus_size=len(corpus),
            query_terms=(),
            note=note,
            metadata=metadata,
        )
