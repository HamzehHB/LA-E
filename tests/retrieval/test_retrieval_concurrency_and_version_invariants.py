"""Targeted Regression Suite: Retrieval Version Integrity & Invariants.

Verifies table version integrity and retrieval error-handling invariants for LanceDBVectorRetriever:
- Test 1: Telemetry and fail-closed behavior upon embedding failure.
- Test 2: Exception safety and fail-closed behavior upon storage search failure.
- Test 3: Table version verification & fail-closed detection of out-of-band store mutation.
- Test 4: Core provenance consistency and metadata fidelity during retrieval.
- Test 5: Initial vector table version missing or empty detection.
- Test 6: Refreshed table handle re-open failure handling.
- Test 7: Refreshed table empty version detection.
"""

from pathlib import Path
import time
import lancedb
import pytest

from src.living_authenticity.database.lancedb_manager import (
    DatabaseOperationError,
    LanceDBManager,
)
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
    LanceDBVectorRetriever,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.vault.core_validator import (
    compute_core_record_id,
)
from tests._test_core_helpers import (
    build_test_sync_authorization,
    build_test_validated_core,
)


class HookedRetrieverTestEmbedder:
    """Deterministic embedding service with optional hooks for testing error paths."""
    dimension = 1024

    def __init__(self, on_embed_callback=None):
        self.on_embed_callback = on_embed_callback

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self.on_embed_callback is not None:
            self.on_embed_callback()
        return [[0.01 * (i + 1)] * self.dimension for i in range(len(texts))]

    def embed_query(self, text: str) -> list[float]:
        if self.on_embed_callback is not None:
            self.on_embed_callback()
        return [0.01] * self.dimension

    def embed(self, text: str) -> list[float]:
        return self.embed_query(text)


def _setup_fresh_store(tmp_path: Path, name: str = "test_store") -> tuple[LanceDBManager, HookedRetrieverTestEmbedder]:
    db_dir = tmp_path / name
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = HookedRetrieverTestEmbedder()

    doc1 = "Authentic observation of phenomena and structured inquiry."
    doc2 = "Formal verification of retrieval properties in indexed stores."
    store.store(doc1, embedder.embed(doc1), {"source": "notes/obs.md", "position": 0})
    store.store(doc2, embedder.embed(doc2), {"source": "notes/verif.md", "position": 1})
    store.save_metadata({"status": "fresh", "indexed_vectors": 2})
    return store, embedder


def _create_query(query_id: str = "q-test") -> KnowledgeUnit:
    return KnowledgeUnit(
        id=query_id,
        source="query.md",
        original_text="Observation of formal retrieval properties.",
        meaning="Observation of formal retrieval properties.",
        cleaned_text="Observation of formal retrieval properties.",
        normalized_text="observation of formal retrieval properties.",
        position=0,
    )


# ==============================================================================
# Test 1 & 2: Exception Safety and Telemetry
# ==============================================================================

def test_retrieval_telemetry_on_embed_failure(tmp_path: Path):
    """When embedding fails, retriever must emit failed event and fail closed cleanly."""
    store, _ = _setup_fresh_store(tmp_path, "db_test_e1")

    def failing_embed():
        raise RuntimeError("Embedding pipeline transient outage")

    failing_embedder = HookedRetrieverTestEmbedder(on_embed_callback=failing_embed)
    retriever = LanceDBVectorRetriever(
        embedding_service=failing_embedder,
        db_manager=store,
        max_candidates=5,
    )

    query = _create_query("q-test-e1")
    events = []
    with pytest.raises(RuntimeError, match="Embedding pipeline transient outage"):
        retriever.retrieve(query, corpus=[], observer=lambda e: events.append(e))

    # Verify candidate_retrieval failed was emitted
    fail_events = [e for e in events if e.get("phase") == "failed" and e.get("operation") == "candidate_retrieval"]
    assert len(fail_events) == 1
    assert "Embedding pipeline transient outage" in fail_events[0]["error"]


def test_retrieval_exception_safety_on_search_failure(tmp_path: Path, monkeypatch):
    """When table search fails, retriever must fail closed cleanly."""
    store, embedder = _setup_fresh_store(tmp_path, "db_test_e2")

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        max_candidates=5,
    )

    orig_get_table = store.get_table
    class BrokenSearchTable:
        def __init__(self, inner):
            self._inner = inner
            self.version = getattr(inner, "version", 1)

        def count_rows(self):
            return 2

        def search(self, *args, **kwargs):
            raise RuntimeError("Underlying storage I/O error during search")

    monkeypatch.setattr(store, "get_table", lambda: BrokenSearchTable(orig_get_table()))

    query = _create_query("q-test-e2")
    with pytest.raises(DatabaseOperationError, match="Underlying storage I/O error during search"):
        retriever.retrieve(query, corpus=[])


# ==============================================================================
# Test 3: Table Version Reliability & Out-of-Band Mutation Detection
# ==============================================================================

def test_store_version_check_detects_mutation_and_fails_closed(tmp_path: Path):
    """If table version increments during retrieval, retrieval must fail closed with DatabaseOperationError."""
    store, embedder = _setup_fresh_store(tmp_path, "db_test_f")

    class MutatingEmbedder:
        dimension = 1024

        def embed(self, text):
            # External out-of-band write while retrieval query is underway
            db_ext = lancedb.connect(store.db_path)
            tbl_ext = db_ext.open_table("knowledge_vectors")
            tbl_ext.add([{
                "id": "ext_out_of_band_row",
                "embedding": [0.02] * 1024,
                "text": "out of band mutation",
                "source": "ext.md",
                "position": 99,
                "is_core": False,
                "core_hash": "",
            }])
            return [0.01] * 1024

    retriever = LanceDBVectorRetriever(
        embedding_service=MutatingEmbedder(),
        db_manager=store,
        max_candidates=5,
    )

    query = _create_query("q-test-f")
    with pytest.raises(DatabaseOperationError, match="Store mutated during retrieval: fail closed"):
        retriever.retrieve(query, corpus=[])


# ==============================================================================
# Test 4: Core Provenance Consistency
# ==============================================================================

def test_core_provenance_consistency_during_retrieval(tmp_path: Path):
    """Core provenance verification must preserve Core candidate identity and metadata without leakage."""
    store = LanceDBManager(str(tmp_path / "db_test_g"))
    store.create_knowledge_vector_table()
    embedder = HookedRetrieverTestEmbedder()

    core_unit = KnowledgeUnit(
        id="core_unit_1",
        source="01 Core/core_foundational.md",
        original_text="Foundational authenticity postulate.",
        meaning="Foundational authenticity postulate.",
        position=0,
    )
    validated_core = build_test_validated_core(
        units=[core_unit],
        source_path="01 Core/core_foundational.md",
        core_hash="sha256_mock_core_12345",
    )
    rec_id = compute_core_record_id(core_unit, validated_core)

    store.store(
        text="Foundational authenticity postulate.",
        embedding=embedder.embed("Foundational authenticity postulate."),
        metadata={
            "source": "01 Core/core_foundational.md",
            "position": 0,
            "is_core": True,
            "core_hash": "sha256_mock_core_12345",
        },
        record_id=rec_id,
        validated_core=validated_core,
    )
    val_auth = build_test_sync_authorization(validated_core)
    store.save_metadata(
        {"status": "fresh", "indexed_vectors": 1},
        validated_core=validated_core,
        sync_auth=val_auth,
    )

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        max_candidates=5,
    )
    retriever.set_validated_core(validated_core)

    query = KnowledgeUnit(
        id="q-core-query",
        source="query.md",
        original_text="Foundational authenticity postulate inquiry.",
        meaning="Foundational authenticity postulate inquiry.",
        position=0,
    )

    res = retriever.retrieve(query, corpus=[])
    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.is_core is True
    assert cand.core_hash == "sha256_mock_core_12345"
    assert cand.unit_id == "core_unit_1"


# ==============================================================================
# Tests 5, 6, 7: Retrieval Live Table Version Verification & Fail-Closed Robustness
# ==============================================================================

def test_retrieval_initial_table_version_missing_or_empty_fails_closed(tmp_path: Path):
    """If the initial vector table version is None or empty, retrieval fails closed immediately."""
    store, embedder = _setup_fresh_store(tmp_path, "db_finding6_init_ver")

    real_get_table = store.get_table

    class FakeTableWithoutVersion:
        def __getattr__(self, name):
            if name == "version":
                return None
            return getattr(real_get_table(), name)

    store.get_table = lambda: FakeTableWithoutVersion()  # type: ignore

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        max_candidates=5,
    )
    query = _create_query("q-finding6-init-none")

    with pytest.raises(DatabaseOperationError, match="Vector table version is missing or invalid"):
        retriever.retrieve(query, corpus=[])

    store.get_table = real_get_table


def test_retrieval_refreshed_table_open_failure_fails_closed(tmp_path: Path):
    """If re-opening the vector table handle fails during verification, retrieval fails closed."""
    store, base_embedder = _setup_fresh_store(tmp_path, "db_finding6_refresh_err")
    real_get_table = store.get_table

    refresh_active = [False]

    class HookedEmbedder(HookedRetrieverTestEmbedder):
        def embed(self, text):
            refresh_active[0] = True
            return super().embed(text)

    def fail_on_refresh():
        if refresh_active[0]:
            raise RuntimeError("Underlying storage unavailable during refresh")
        return real_get_table()

    store.get_table = fail_on_refresh  # type: ignore

    retriever = LanceDBVectorRetriever(
        embedding_service=HookedEmbedder(),
        db_manager=store,
        max_candidates=5,
    )
    query = _create_query("q-finding6-refresh-fail")

    with pytest.raises(DatabaseOperationError, match="Failed opening refreshed vector table"):
        retriever.retrieve(query, corpus=[])

    store.get_table = real_get_table


def test_retrieval_refreshed_table_version_empty_fails_closed(tmp_path: Path):
    """If refreshed table version is None or empty string, retrieval fails closed."""
    store, base_embedder = _setup_fresh_store(tmp_path, "db_finding6_refresh_ver_none")
    real_get_table = store.get_table

    refresh_active = [False]

    class FakeTableWithNoneVersion:
        def __getattr__(self, name):
            if name == "version":
                return "   "
            return getattr(real_get_table(), name)

    class HookedEmbedder(HookedRetrieverTestEmbedder):
        def embed(self, text):
            refresh_active[0] = True
            return super().embed(text)

    def none_version_on_refresh():
        if refresh_active[0]:
            return FakeTableWithNoneVersion()
        return real_get_table()

    store.get_table = none_version_on_refresh  # type: ignore

    retriever = LanceDBVectorRetriever(
        embedding_service=HookedEmbedder(),
        db_manager=store,
        max_candidates=5,
    )
    query = _create_query("q-finding6-refresh-empty")

    with pytest.raises(DatabaseOperationError, match="Refreshed table version missing or invalid"):
        retriever.retrieve(query, corpus=[])

    store.get_table = real_get_table
