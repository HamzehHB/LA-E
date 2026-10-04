"""Synthetic counterpart to tests/local/test_real_bge_m3_lancedb_local.py.

Demonstrates the same embedding and isolated LanceDB retrieval lifecycle
using portable synthetic test doubles without accessing local models or
production databases.
"""
from pathlib import Path
import numpy as np

from src.living_authenticity.database.lancedb_manager import LanceDBManager
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
    LanceDBVectorRetriever,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)


class SyntheticEmbeddingService:
    def __init__(self, dimension=1024):
        self.dimension = dimension

    def embed(self, text: str):
        vec = np.zeros(self.dimension, dtype=np.float32)
        vec[0] = 1.0
        return vec


def test_synthetic_embedding_and_lancedb_lifecycle(tmp_path):
    service = SyntheticEmbeddingService()
    isolated_db = tmp_path / "synthetic_lancedb"
    manager = LanceDBManager(db_path=str(isolated_db))
    manager.create_knowledge_vector_table()

    manager.store(
        text="Synthetic document content.",
        embedding=service.embed("Synthetic document content."),
        metadata={"source": "synthetic.md"}
    )

    records = manager.show_all()
    assert len(records) == 1
    assert records[0]["source"] == "synthetic.md"

    retriever = LanceDBVectorRetriever(embedding_service=service, db_manager=manager)
    query = KnowledgeUnit(
        id="q_synth",
        source="query.md",
        original_text="Synthetic query.",
        meaning="Synthetic query.",
        cleaned_text="Synthetic query.",
        normalized_text="synthetic query.",
        position=0,
    )
    res = retriever.retrieve(query, corpus=[])
    assert res.query_unit_id == "q_synth"
    assert len(res.candidates) == 1
