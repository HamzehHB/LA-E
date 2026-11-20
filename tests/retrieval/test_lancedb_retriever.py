"""Tests for embedding service and LanceDB vector retriever integration."""
import numpy as np
import pytest

from src.living_authenticity.database.lancedb_manager import LanceDBManager
from src.living_authenticity.database.schema import VECTOR_DIMENSION
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
    LanceDBVectorRetriever,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)


class DeterministicEmbedder:
    """Tiny deterministic embedder; no model files required."""

    def __init__(self, dimension=1024):
        self.dimension = dimension

    def embed_batch(self, texts):
        vectors = []
        for text in texts:
            seed = abs(hash(text)) % 97 + 1
            vec = np.array(
                [float((seed * (i + 1)) % 13) / 13.0
                 for i in range(self.dimension)],
                dtype=np.float32,
            )
            norm = float(np.linalg.norm(vec))
            vectors.append(vec / norm if norm > 0 else vec)
        return vectors

    def embed(self, text):
        return self.embed_batch([text])[0]


def test_lancedb_manager_isolated_db(tmp_path):
    isolated_db = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(isolated_db))
    table = manager.create_knowledge_vector_table()
    assert table is not None
    assert manager.show_all() == []

    # Store a dummy vector
    service = DeterministicEmbedder(dimension=VECTOR_DIMENSION)
    vec = service.embed("Test knowledge text")
    manager.store("Test knowledge text", vec, {"source": "test_note.md"})

    records = manager.show_all()
    assert len(records) == 1
    assert records[0]["text"] == "Test knowledge text"
    assert records[0]["source"] == "test_note.md"
    assert len(records[0]["embedding"]) == VECTOR_DIMENSION


def test_lancedb_vector_retriever(tmp_path):
    isolated_db = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(isolated_db))
    manager.create_knowledge_vector_table()
    service = DeterministicEmbedder(dimension=VECTOR_DIMENSION)

    # Store candidate notes
    doc1 = "The quiet morning facilitates deep reflection and research."
    doc2 = "Mathematical logic and formal verification techniques."
    manager.store(doc1, service.embed(doc1), {"source": "reflections.md"})
    manager.store(doc2, service.embed(doc2), {"source": "math.md"})
    manager.save_metadata({"status": "fresh", "indexed_vectors": 2})

    retriever = LanceDBVectorRetriever(embedding_service=service, db_manager=manager, max_candidates=2)
    assert retriever.name == "lancedb_vector"

    query_unit = KnowledgeUnit(
        id="q1",
        source="query.md",
        original_text="A calm morning observation on deep focus.",
        meaning="A calm morning observation on deep focus.",
        cleaned_text="A calm morning observation on deep focus.",
        normalized_text="a calm morning observation on deep focus.",
        position=0,
    )

    res = retriever.retrieve(query_unit, corpus=[])
    assert res.query_unit_id == "q1"
    assert len(res.candidates) > 0
    assert res.candidates[0].retriever == "lancedb_vector"
    # Searched collection size matches persistent index (2), not in-memory corpus
    assert res.corpus_size == 2
    assert res.metadata.get("corpus_size_reporting") == "vector_index_rows"


def test_lancedb_vector_retriever_corpus_size_ignores_unsearched_in_memory_corpus(tmp_path):
    """Corpus size must reflect the table collection actually searched, not an in-memory corpus list."""
    isolated_db = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(isolated_db))
    manager.create_knowledge_vector_table()
    service = DeterministicEmbedder(dimension=VECTOR_DIMENSION)

    # Store 1 document in the vector store
    doc = "Authentic clarity and disciplined inquiry."
    manager.store(doc, service.embed(doc), {"source": "truth.md"})
    manager.save_metadata({"status": "fresh", "indexed_vectors": 1})

    retriever = LanceDBVectorRetriever(embedding_service=service, db_manager=manager, max_candidates=2)
    query_unit = KnowledgeUnit(
        id="q-probe",
        source="q.md",
        original_text="Clarity and inquiry in practice.",
        meaning="Clarity and inquiry in practice.",
        cleaned_text="Clarity and inquiry in practice.",
        normalized_text="clarity and inquiry in practice.",
        position=0,
    )

    # Pass in-memory corpus of 10 items
    dummy_corpus = [
        KnowledgeUnit(
            id=f"c-{i}",
            source="dummy.md",
            original_text=f"dummy text {i}",
            meaning=f"dummy text {i}",
            cleaned_text=f"dummy text {i}",
            normalized_text=f"dummy text {i}",
            position=i,
        )
        for i in range(10)
    ]

    res = retriever.retrieve(query_unit, corpus=dummy_corpus)
    # Must report the collection actually searched (1 row in LanceDB), NOT len(dummy_corpus) == 10
    assert res.corpus_size == 1
    assert res.metadata.get("corpus_size_reporting") == "vector_index_rows"


def test_lancedb_vector_retriever_unknown_corpus_size_handling(tmp_path):
    """When collection size cannot be determined, uncertainty is explicitly represented."""
    isolated_db = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(isolated_db))
    table = manager.create_knowledge_vector_table()
    service = DeterministicEmbedder(dimension=VECTOR_DIMENSION)

    # Mock table where count_rows raises
    class BrokenTableProxy:
        def __init__(self, inner):
            self._inner = inner

        def search(self, *args, **kwargs):
            return self._inner.search(*args, **kwargs)

        @property
        def version(self):
            return self._inner.version

        def count_rows(self):
            raise RuntimeError("size unmeasurable")

        def __len__(self):
            raise RuntimeError("length unmeasurable")

    class BrokenManagerProxy(LanceDBManager):
        def __init__(self, inner_mgr, broken_tbl):
            self._broken_tbl = broken_tbl
            self.db_path = inner_mgr.db_path

        def get_table(self):
            return self._broken_tbl

    retriever = LanceDBVectorRetriever(
        embedding_service=service,
        db_manager=BrokenManagerProxy(manager, BrokenTableProxy(table)),
        max_candidates=2,
    )
    query_unit = KnowledgeUnit(
        id="q-probe-2",
        source="q.md",
        original_text="Clarity and inquiry in practice.",
        meaning="Clarity and inquiry in practice.",
        cleaned_text="Clarity and inquiry in practice.",
        normalized_text="clarity and inquiry in practice.",
        position=0,
    )

    res = retriever.retrieve(query_unit, corpus=[])
    assert res.corpus_size is None
    assert res.metadata.get("corpus_size_unknown") is True
    assert res.metadata.get("corpus_size_reporting") == "unknown"


def test_lancedb_vector_retriever_relevance_assessment_real_work_and_events(tmp_path):
    """Relevance assessment performs real transformation of vector distances into explainable assessments."""
    isolated_db = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(isolated_db))
    manager.create_knowledge_vector_table()
    service = DeterministicEmbedder(dimension=VECTOR_DIMENSION)

    doc1 = "The quiet morning facilitates deep reflection and research."
    doc2 = "Mathematical logic and formal verification techniques."
    manager.store(doc1, service.embed(doc1), {"source": "reflections.md", "position": 1})
    manager.store(doc2, service.embed(doc2), {"source": "math.md", "position": 2})
    manager.save_metadata({"status": "fresh", "indexed_vectors": 2})

    events = []
    def observer(evt):
        events.append(evt)

    retriever = LanceDBVectorRetriever(embedding_service=service, db_manager=manager, max_candidates=5)
    query_unit = KnowledgeUnit(
        id="q-events",
        source="query.md",
        original_text="Deep reflection in the quiet morning.",
        meaning="Deep reflection in the quiet morning.",
        cleaned_text="Deep reflection in the quiet morning.",
        normalized_text="deep reflection in the quiet morning.",
        position=0,
    )

    res = retriever.retrieve(query_unit, corpus=[], observer=observer)
    operations = [e.get("operation") for e in events if e.get("stage") == "retrieval"]

    # All 5 capabilities observed in order
    expected_order = [
        "query_preparation",
        "candidate_retrieval",
        "relevance_assessment",
        "candidate_identification",
        "retrieval_evidence_recording",
    ]
    seen_unique_ops = []
    for op in operations:
        if op and (not seen_unique_ops or seen_unique_ops[-1] != op):
            seen_unique_ops.append(op)
    assert seen_unique_ops == expected_order

    # Relevance assessment completed event reported assessed candidate count
    rel_completed = [
        e for e in events
        if e.get("operation") == "relevance_assessment" and e.get("phase") == "completed"
    ]
    assert len(rel_completed) == 1
    assert rel_completed[0]["assessed_count"] == 2

    # Candidates contain vector distance and canonical identity format <source>:<position>:<unit_id>
    assert len(res.candidates) > 0
    top = res.candidates[0]
    assert top.vector_distance is not None
    assert top.candidate_identity_key.startswith("reflections.md:") or top.candidate_identity_key.startswith("math.md:")
    parts = top.candidate_identity_key.split(":")
    assert len(parts) >= 3


def test_empty_query_retrieval_event_ordering(tmp_path):
    """Empty query must emit operations in order, starting evidence recording before result construction and completing it only after."""
    isolated_db = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(isolated_db))
    service = DeterministicEmbedder(dimension=VECTOR_DIMENSION)

    retriever = LanceDBVectorRetriever(embedding_service=service, db_manager=manager, max_candidates=5)
    query_unit = KnowledgeUnit(
        id="q-empty",
        source="empty.md",
        original_text="   \n  \t  ",
        meaning="",
        cleaned_text="",
        normalized_text="",
        position=0,
    )

    events = []
    def observer(evt):
        events.append(evt)

    res = retriever.retrieve(query_unit, corpus=[], observer=observer)
    assert res.candidates == ()
    assert res.corpus_size is None

    # Verify event ordering
    event_pairs = [(e.get("operation"), e.get("phase")) for e in events if e.get("stage") == "retrieval"]
    expected_pairs = [
        ("query_preparation", "started"),
        ("query_preparation", "completed"),
        ("candidate_retrieval", "started"),
        ("candidate_retrieval", "completed"),
        ("relevance_assessment", "started"),
        ("relevance_assessment", "completed"),
        ("candidate_identification", "started"),
        ("candidate_identification", "completed"),
        ("retrieval_evidence_recording", "started"),
        ("retrieval_evidence_recording", "completed"),
    ]
    assert event_pairs == expected_pairs


def test_empty_query_result_construction_failure_emits_failed_not_completed(tmp_path, monkeypatch):
    """If RetrievalResult construction fails for empty query, retrieval_evidence_recording failed is emitted and completed is not."""
    isolated_db = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(isolated_db))
    service = DeterministicEmbedder(dimension=VECTOR_DIMENSION)

    retriever = LanceDBVectorRetriever(embedding_service=service, db_manager=manager, max_candidates=5)
    query_unit = KnowledgeUnit(
        id="q-empty-err",
        source="empty.md",
        original_text="",
        meaning="",
        cleaned_text="",
        normalized_text="",
        position=0,
    )

    import src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever as lancedb_mod

    def failing_retrieval_result(*args, **kwargs):
        raise RuntimeError("simulated empty result construction failure")

    monkeypatch.setattr(lancedb_mod, "RetrievalResult", failing_retrieval_result)

    events = []
    def observer(evt):
        events.append(evt)

    import pytest
    with pytest.raises(RuntimeError, match="simulated empty result construction failure"):
        retriever.retrieve(query_unit, corpus=[], observer=observer)

    rec_events = [e for e in events if e.get("operation") == "retrieval_evidence_recording"]
    assert len(rec_events) == 2
    assert rec_events[0]["phase"] == "started"
    assert rec_events[1]["phase"] == "failed"
    assert "simulated empty result construction failure" in rec_events[1]["error"]
    assert not any(e.get("phase") == "completed" and e.get("operation") == "retrieval_evidence_recording" for e in events)


# ==============================================================================
# Precision Regression Suite: Default Multiplier (0.30 / 1.30x) & Store Anti-Bypass (8 Direct-Call Tests)
# ==============================================================================

def test_default_core_boost_factor_multiplier_exact_1_30x(tmp_path):
    """The default core_boost_factor must be 0.30, resulting in exactly a 1.30x multiplier."""
    from tests._test_core_helpers import (
        build_test_sync_authorization,
        build_test_validated_core,
    )
    from src.living_authenticity.knowledge.vault.core_validator import compute_core_record_id
    import pytest

    db_dir = tmp_path / "boost_test_db"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DeterministicEmbedder(dimension=VECTOR_DIMENSION)

    core_text = "Foundational authenticity calibration standard."
    core_unit = KnowledgeUnit(
        id="core-u1",
        source="01 Core/core.md",
        original_text=core_text,
        meaning=core_text,
        cleaned_text=core_text,
        normalized_text=core_text.lower(),
        position=0,
    )
    val_core = build_test_validated_core(
        units=[core_unit],
        source_path="01 Core/core.md",
        core_hash="sha256_core_boost_test",
    )
    rec_id = compute_core_record_id(core_unit, val_core)

    store.store(
        text=core_text,
        embedding=embedder.embed(core_text),
        metadata={
            "source": "01 Core/core.md",
            "position": 0,
            "is_core": True,
            "core_hash": "sha256_core_boost_test",
        },
        record_id=rec_id,
        validated_core=val_core,
    )
    val_auth = build_test_sync_authorization(val_core)
    store.save_metadata(
        {"status": "fresh", "indexed_vectors": 1},
        validated_core=val_core,
        sync_auth=val_auth,
    )

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=val_core,
    )
    assert retriever.core_boost_factor == pytest.approx(0.30)

    query = KnowledgeUnit(
        id="q-boost",
        source="query.md",
        original_text=core_text,
        meaning=core_text,
        cleaned_text=core_text,
        normalized_text=core_text.lower(),
        position=0,
    )

    res = retriever.retrieve(query, corpus=[])
    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.is_core is True
    assert cand.core_hash == "sha256_core_boost_test"


@pytest.mark.parametrize(
    "case_num,source,text_override,position,record_id_override,core_hash,use_context,expected_is_core",
    [
        (1, "02 Notes/ordinary.md", None, 0, None, "hash123", True, False),   # Valid context with ordinary source
        (2, "01 Core/core.md", "arbitrary text mismatch", 0, None, "hash123", True, False), # Valid context with arbitrary text
        (3, "01 Core/core.md", None, 99, None, "hash123", True, False),       # Valid context with mismatched position
        (4, "01 Core/core.md", None, 0, "core_vec_forged_id", "hash123", True, False), # Forged deterministic ID
        (5, "01 Core/core.md", None, 0, None, "wrong_hash", True, False),      # Mismatched core_hash
        (6, "01 Core/core.md", None, 0, None, "hash123", True, True),         # Valid Core unit with correct metadata
        (7, "01 Core/core.md", None, 0, "core_vec_fake", "hash123", False, False), # Ordinary unit carrying Core flags (context=None)
        (8, "01 Core/core.md", "core text with mutated suffix", 0, None, "hash123", True, False), # Cloned/mutated Core unit
    ],
)
def test_lancedb_manager_store_anti_bypass_eight_cases(
    tmp_path, case_num, source, text_override, position, record_id_override, core_hash, use_context, expected_is_core
):
    """Direct-call anti-bypass verification on LanceDBManager.store across all 8 adversarial cases."""
    from tests._test_core_helpers import (
        build_test_sync_authorization,
        build_test_validated_core,
    )
    from src.living_authenticity.knowledge.vault.core_validator import compute_core_record_id

    db_dir = tmp_path / f"db_antibypass_{case_num}"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DeterministicEmbedder(dimension=VECTOR_DIMENSION)

    genuine_text = "Authentic core unit thesis for testing anti-bypass mechanisms."
    core_unit = KnowledgeUnit(
        id="unit-antibypass",
        source="01 Core/core.md",
        original_text=genuine_text,
        meaning=genuine_text,
        cleaned_text=genuine_text,
        normalized_text=genuine_text.lower(),
        position=0,
    )
    val_core = build_test_validated_core(
        units=[core_unit],
        source_path="01 Core/core.md",
        core_hash="hash123",
    )
    canonical_rec_id = compute_core_record_id(core_unit, val_core)

    active_text = text_override if text_override is not None else genuine_text
    active_rec_id = record_id_override if record_id_override is not None else canonical_rec_id
    active_context = val_core if use_context else None

    stored_id = store.store(
        text=active_text,
        embedding=embedder.embed(active_text),
        metadata={
            "source": source,
            "position": position,
            "is_core": True,  # Caller always attempts to claim Core
            "core_hash": core_hash,
        },
        record_id=active_rec_id,
        validated_core=active_context,
    )

    rows = store.show_all()
    assert len(rows) == 1
    row = rows[0]

    if expected_is_core:
        assert row["is_core"] is True
        assert row["core_hash"] == "hash123"
        assert row["id"] == canonical_rec_id
        assert stored_id == canonical_rec_id
    else:
        assert row["is_core"] is False
        assert row["core_hash"] == ""
        # Caller CANNOT forge core_vec_ IDs
        assert not str(row["id"]).startswith("core_vec_")
