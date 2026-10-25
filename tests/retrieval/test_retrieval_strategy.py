"""Tests for retrieval strategy selection, validation, and execution rules.

Verifies:
* strategy registry validation: lexical, semantic, hybrid (future-ready; unsupported in 0.1), unknown;
* elimination of silent fallbacks;
* lexical retrieval performs zero embedding calls and creates no embedding cache;
* semantic retrieval uses configured embedding abstraction and temporary cache;
* dimensionality mismatch stops safely with diagnostic error;
* malformed embedding output stops safely.
"""
import numpy as np
import pytest

from src.living_authenticity.database.lancedb_manager import LanceDBManager
from src.living_authenticity.database.schema import VECTOR_DIMENSION
from src.living_authenticity.knowledge.analysis.retrieval.cache import (
    TemporaryEmbeddingCache,
)
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
    LanceDBVectorRetriever,
)
from src.living_authenticity.knowledge.analysis.retrieval.strategy import (
    RetrievalStrategy,
    RetrievalStrategyRegistry,
    UnsupportedStrategyError,
)
from src.living_authenticity.knowledge.analysis.retrieval.token_overlap import (
    TokenOverlapRetriever,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)


class CountingStubEmbedder:
    """Stub embedder counting calls and supporting custom dimension."""

    def __init__(self, dimension=VECTOR_DIMENSION):
        self.dimension = dimension
        self.call_count = 0

    def embed(self, text):
        self.call_count += 1
        return np.ones(self.dimension, dtype=np.float32)


class MalformedEmbedder:
    """Embedder producing malformed outputs for safety tests."""

    def __init__(self, mode="none"):
        self.mode = mode

    def embed(self, text):
        if self.mode == "none":
            return None
        if self.mode == "empty":
            return np.array([], dtype=np.float32)
        if self.mode == "scalar":
            return 42.0
        return None


def test_strategy_registry_lexical_and_semantic():
    registry = RetrievalStrategyRegistry()
    lex = registry.validate("lexical")
    assert lex.name == "lexical"
    assert lex.is_supported is True
    assert lex.requires_embedding is False

    sem = registry.validate("semantic")
    assert sem.name == "semantic"
    assert sem.is_supported is True
    assert sem.requires_embedding is True


def test_strategy_registry_hybrid_future_ready_fails_closed():
    """Hybrid is registered as a planned strategy but MUST fail closed in 0.1."""
    registry = RetrievalStrategyRegistry()
    defn = registry.get("hybrid")
    assert defn is not None
    assert defn.name == "hybrid"
    assert defn.is_supported is False
    assert defn.requires_embedding is True

    with pytest.raises(UnsupportedStrategyError, match="Hybrid retrieval is a planned future strategy"):
        registry.validate("hybrid")


def test_strategy_registry_unknown_fails_closed():
    registry = RetrievalStrategyRegistry()
    with pytest.raises(UnsupportedStrategyError, match="unsupported retrieval strategy 'quantum'"):
        registry.validate("quantum")

    with pytest.raises(UnsupportedStrategyError, match="non-empty string"):
        registry.validate("")


def test_lexical_retrieval_requires_zero_embeddings():
    """Lexical retrieval must never invoke an embedding service or allocate an embedding cache."""
    retriever = TokenOverlapRetriever()
    assert retriever.name == "token_overlap"

    query = KnowledgeUnit(
        id="q-lex",
        source="query.md",
        original_text="observation of self presence and awareness",
        meaning="observation of self presence and awareness",
        cleaned_text="observation of self presence and awareness",
        normalized_text="observation of self presence and awareness",
        position=0,
    )
    corpus = [
        KnowledgeUnit(
            id="c-1",
            source="corpus.md",
            original_text="presence and quiet awareness in life",
            meaning="presence and quiet awareness in life",
            cleaned_text="presence and quiet awareness in life",
            normalized_text="presence and quiet awareness in life",
            position=0,
        )
    ]

    res = retriever.retrieve(query, corpus)
    assert res.has_candidates
    assert len(res.candidates) == 1
    assert res.candidates[0].overlap_score >= 1
    # No embedding metadata exists on lexical result, but standard evidence metadata is recorded
    assert "embeddings_generated" not in res.metadata
    assert "capabilities" in res.metadata
    assert res.metadata.get("corpus_size") == 1


def test_semantic_retrieval_caches_and_validates_dimensions(tmp_path):
    isolated_db = tmp_path / "vec_dim_test"
    manager = LanceDBManager(db_path=str(isolated_db))
    manager.create_knowledge_vector_table()

    embedder = CountingStubEmbedder(dimension=VECTOR_DIMENSION)
    cache = TemporaryEmbeddingCache(run_id="run-dim", dimension=VECTOR_DIMENSION)

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=manager,
        max_candidates=2,
        cache=cache,
    )

    query = KnowledgeUnit(
        id="q-sem-1",
        source="query.md",
        original_text="Exploring genuine presence and self-reflection.",
        meaning="Exploring genuine presence and self-reflection.",
        cleaned_text="Exploring genuine presence and self-reflection.",
        normalized_text="exploring genuine presence and self-reflection.",
        position=0,
    )

    # First retrieval computes embedding and stores in temporary cache
    res1 = retriever.retrieve(query, corpus=[])
    assert embedder.call_count == 1
    assert cache.has("q-sem-1")
    assert res1.metadata.get("embeddings_generated") == 1

    # Second retrieval of same unit reuses temporary cache without calling embedder
    res2 = retriever.retrieve(query, corpus=[])
    assert embedder.call_count == 1  # No extra call
    assert res2.metadata.get("embeddings_generated") == 1


def test_semantic_retrieval_dimension_mismatch_fails_safely(tmp_path):
    isolated_db = tmp_path / "vec_mismatch_test"
    manager = LanceDBManager(db_path=str(isolated_db))
    manager.create_knowledge_vector_table()

    # Wrong dimension: 384 instead of schema expected 1024
    embedder = CountingStubEmbedder(dimension=384)
    cache = TemporaryEmbeddingCache(run_id="run-dim-bad", dimension=VECTOR_DIMENSION)

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=manager,
        max_candidates=2,
        cache=cache,
    )

    query = KnowledgeUnit(
        id="q-mismatch",
        source="query.md",
        original_text="Presence test text",
        meaning="Presence test text",
        cleaned_text="Presence test text",
        normalized_text="presence test text",
        position=0,
    )

    with pytest.raises(ValueError, match="dimensionality mismatch"):
        retriever.retrieve(query, corpus=[])

    assert cache.failed_count == 1


def test_semantic_retrieval_malformed_vector_fails_safely(tmp_path):
    isolated_db = tmp_path / "vec_malformed_test"
    manager = LanceDBManager(db_path=str(isolated_db))
    manager.create_knowledge_vector_table()

    cache = TemporaryEmbeddingCache(run_id="run-malformed", dimension=VECTOR_DIMENSION)
    retriever_none = LanceDBVectorRetriever(
        embedding_service=MalformedEmbedder(mode="none"),
        db_manager=manager,
        cache=cache,
    )

    query = KnowledgeUnit(
        id="q-none",
        source="query.md",
        original_text="Presence test",
        meaning="Presence test",
        cleaned_text="Presence test",
        normalized_text="presence test",
        position=0,
    )

    with pytest.raises(ValueError, match="None instead of a vector"):
        retriever_none.retrieve(query, corpus=[])

    retriever_empty = LanceDBVectorRetriever(
        embedding_service=MalformedEmbedder(mode="empty"),
        db_manager=manager,
        cache=cache,
    )
    with pytest.raises(ValueError, match="empty vector"):
        retriever_empty.retrieve(query, corpus=[])
