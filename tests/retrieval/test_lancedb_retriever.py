"""Tests for embedding service and LanceDB vector retriever integration."""
import numpy as np

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
