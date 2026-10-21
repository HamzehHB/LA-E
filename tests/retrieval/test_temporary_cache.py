"""Tests for the isolated run-scoped temporary embedding cache.

Verifies:
* run-scoped isolation (no cross-run leakage or shared state);
* lifecycle and guaranteed cleanup via context manager and explicit cleanup;
* clearing of vectors on cleanup and refusal of access after release;
* observable cleanup failure handling without false success claims;
* audit receipt privacy (zero raw vectors, zero private text);
* persistent vector store immutability (temporary embeddings never mutate the index).
"""
import numpy as np
import pytest

from src.living_authenticity.database.lancedb_manager import LanceDBManager
from src.living_authenticity.database.schema import VECTOR_DIMENSION
from src.living_authenticity.knowledge.analysis.retrieval.cache import (
    CacheCleanupError,
    CacheReceipt,
    TemporaryEmbeddingCache,
)
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
    LanceDBVectorRetriever,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)


class DeterministicStubEmbedder:
    """Deterministic stub embedder without external model dependencies."""

    def __init__(self, dimension=VECTOR_DIMENSION):
        self.dimension = dimension
        self.calls = 0

    def embed_batch(self, texts):
        vectors = []
        for text in texts:
            self.calls += 1
            seed = abs(hash(text)) % 97 + 1
            vec = np.array(
                [
                    float((seed * (i + 1)) % 13) / 13.0
                    for i in range(self.dimension)
                ],
                dtype=np.float32,
            )
            norm = float(np.linalg.norm(vec))
            vectors.append(vec / norm if norm > 0 else vec)
        return vectors

    def embed(self, text):
        return self.embed_batch([text])[0]


def test_cache_initialization_and_properties():
    cache = TemporaryEmbeddingCache(
        run_id="run-101",
        input_ref="chat_input:inp-101",
        provider_name="test_provider",
        model_name="test_model",
        model_version="1.2.3",
        dimension=1024,
        chunk_count=3,
    )
    assert cache.run_id == "run-101"
    assert cache.input_ref == "chat_input:inp-101"
    assert cache.dimension == 1024
    assert cache.chunk_count == 3
    assert cache.scope_id.startswith("cache-run-101-")
    assert not cache.cleaned_up
    assert cache.cleanup_error == ""
    assert cache.generated_count == 0
    assert cache.failed_count == 0


def test_cache_storage_retrieval_and_lifecycle():
    cache = TemporaryEmbeddingCache(run_id="run-102", dimension=1024)
    dummy_vec = np.ones(1024, dtype=np.float32)

    assert not cache.has("ku-1")
    cache.put("ku-1", dummy_vec)
    assert cache.has("ku-1")
    assert np.array_equal(cache.get("ku-1"), dummy_vec)
    assert cache.generated_count == 1

    # Cleanup releases entries
    assert cache.cleanup() is True
    assert cache.cleaned_up is True
    assert cache.cleanup_error == ""
    assert not cache.has("ku-1")

    # Access after cleanup is rejected safely
    with pytest.raises(RuntimeError, match="cleaned-up"):
        cache.put("ku-2", dummy_vec)

    with pytest.raises(RuntimeError, match="cleaned-up"):
        cache.get("ku-1")


def test_cache_isolation_between_runs():
    cache_a = TemporaryEmbeddingCache(run_id="run-A", dimension=1024)
    cache_b = TemporaryEmbeddingCache(run_id="run-B", dimension=1024)

    assert cache_a.scope_id != cache_b.scope_id

    vec_a = np.ones(1024, dtype=np.float32) * 1.0
    vec_b = np.ones(1024, dtype=np.float32) * 2.0

    cache_a.put("unit-common", vec_a)
    cache_b.put("unit-common", vec_b)

    assert cache_a.get("unit-common")[0] == 1.0
    assert cache_b.get("unit-common")[0] == 2.0

    # Cleaning up cache_a leaves cache_b intact
    cache_a.cleanup()
    assert cache_a.cleaned_up is True
    assert cache_b.cleaned_up is False
    assert cache_b.has("unit-common")
    assert cache_b.get("unit-common")[0] == 2.0

    cache_b.cleanup()
    assert cache_b.cleaned_up is True


def test_cache_context_manager_guaranteed_cleanup():
    cache_instance = None
    with TemporaryEmbeddingCache(run_id="run-ctx") as cache:
        cache_instance = cache
        cache.put("ku-ctx", np.zeros(128))
        assert not cache.cleaned_up

    assert cache_instance.cleaned_up is True
    assert cache_instance.receipt().cleanup_time_iso != ""


def test_cache_cleanup_on_exception():
    cache_instance = None
    try:
        with TemporaryEmbeddingCache(run_id="run-exc") as cache:
            cache_instance = cache
            cache.put("ku-exc", np.zeros(128))
            raise ValueError("boom during retrieval processing")
    except ValueError:
        pass

    assert cache_instance is not None
    assert cache_instance.cleaned_up is True


def test_cache_observable_cleanup_failure():
    cache = TemporaryEmbeddingCache(run_id="run-fail")
    cache.put("ku-f", np.zeros(64))
    cache._fail_cleanup_for_testing = True

    # Cleanup reports failure honestly
    ok = cache.cleanup()
    assert ok is False
    assert cache.cleaned_up is False
    assert "simulated cache release failure" in cache.cleanup_error

    # Receipt records failed cleanup status truthfully
    receipt = cache.receipt()
    assert receipt.cleaned_up is False
    assert "simulated cache release failure" in receipt.cleanup_error


def test_cache_privacy_guarantee():
    cache = TemporaryEmbeddingCache(
        run_id="run-priv",
        input_ref="vault:private_user_secret.md",
        provider_name="bge_provider",
        dimension=1024,
    )
    secret_vector = np.array([0.1337] * 1024, dtype=np.float32)
    cache.put("chunk-1", secret_vector)

    # String representation must not expose raw vector or private contents
    rep = repr(cache)
    assert "0.1337" not in rep
    assert "entries=1" in rep

    # Audit dict / receipt must contain only metadata, counts, and hashes
    receipt_dict = cache.to_audit_dict()
    assert "scope_id" in receipt_dict
    assert "embeddings_generated" in receipt_dict
    assert receipt_dict["embeddings_generated"] == 1
    assert "entries" not in receipt_dict
    assert "0.1337" not in str(receipt_dict)


def test_persistent_vector_store_immutability(tmp_path):
    """Temporary input embeddings and retrieval must NEVER mutate the persistent vector store."""
    isolated_db = tmp_path / "persistent_vector_store"
    manager = LanceDBManager(db_path=str(isolated_db))
    manager.create_knowledge_vector_table()

    embedder = DeterministicStubEmbedder(dimension=VECTOR_DIMENSION)

    # Seed the persistent index with 2 previously ingested knowledge notes
    doc1 = "Existing foundational note on authentic awareness."
    doc2 = "Second existing note on analytical evidence."
    manager.store(doc1, embedder.embed(doc1), {"source": "note1.md"})
    manager.store(doc2, embedder.embed(doc2), {"source": "note2.md"})

    records_before = manager.show_all()
    assert len(records_before) == 2

    # Perform retrieval using an isolated temporary cache
    cache = TemporaryEmbeddingCache(
        run_id="run-retrieval-query",
        input_ref="text:query_01",
        dimension=VECTOR_DIMENSION,
    )
    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=manager,
        max_candidates=2,
        cache=cache,
    )

    query_unit = KnowledgeUnit(
        id="new-input-unit-999",
        source="active_input.txt",
        original_text="A new unindexed query exploring awareness.",
        meaning="A new unindexed query exploring awareness.",
        cleaned_text="A new unindexed query exploring awareness.",
        normalized_text="a new unindexed query exploring awareness.",
        position=0,
    )

    result = retriever.retrieve(query_unit, corpus=[])
    assert result.has_candidates
    assert cache.has("new-input-unit-999")
    assert cache.generated_count == 1

    # Cleanup temporary cache
    cache.cleanup()
    assert cache.cleaned_up

    # Verify persistent store: zero rows added, zero changes to persistent corpus
    records_after = manager.show_all()
    assert len(records_after) == 2
    assert records_after == records_before
    # Confirm the new input unit was never stored in the persistent vector table
    stored_texts = [r["text"] for r in records_after]
    assert "A new unindexed query exploring awareness." not in stored_texts
