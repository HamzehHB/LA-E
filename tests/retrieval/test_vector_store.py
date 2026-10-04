"""Tests for vector-store eligibility, isolated indexing, and tables."""
from pathlib import Path

import numpy as np
import pytest

from src.living_authenticity.database.lancedb_manager import LanceDBManager
from src.living_authenticity.database.schema import KNOWLEDGE_VECTOR_SCHEMA
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.integration.indexing import (
    index_corpus,
    ingest_units,
)
from src.living_authenticity.knowledge.integration.vector import (
    build_vector_components,
    check_vector_store_eligible,
    resolve_production_vector_db,
)
from src.living_authenticity.knowledge.orchestration.orchestrator import (
    EvidenceFirstPipeline,
)


SAMPLE_NOTE = ("Observation:\nI observed that harbour moorings swing with "
               "the evening tide.\n")


def _write(directory, name, text=SAMPLE_NOTE):
    target = Path(directory) / name
    target.write_text(text, encoding="utf-8")
    return target


def _embedder():
    from tests.retrieval.test_lancedb_retriever import DeterministicEmbedder
    return DeterministicEmbedder()


def test_vector_store_rejects_repository_and_production(tmp_path):
    from Config.settings import ROOT as REPO_ROOT

    eligible, reason, _c = check_vector_store_eligible(str(REPO_ROOT), "")
    assert eligible is False
    assert "repository" in reason

    production = tmp_path / "production-db"
    production.mkdir()
    candidate = production / "index"
    candidate.mkdir()
    eligible, reason, _c = check_vector_store_eligible(
        str(candidate), str(production))
    assert eligible is False
    assert "production" in reason

    isolated = tmp_path / "isolated-db"
    eligible, _r, _c = check_vector_store_eligible(
        str(isolated), str(production))
    assert eligible is True


def test_vector_store_symlink_is_rejected(tmp_path):
    real = tmp_path / "real-db"
    real.mkdir()
    link = tmp_path / "db-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    eligible, reason, _c = check_vector_store_eligible(str(link), "")
    assert eligible is False
    assert "symlink" in reason


def test_production_vector_db_key_resolves_from_config():
    assert resolve_production_vector_db() != ""
    import src.living_authenticity.knowledge.integration.vector as vector_mod
    assert vector_mod.PRODUCTION_VECTOR_DB_KEY == "vector_db.lancedb"


def test_schema_records_optional_position():
    names = KNOWLEDGE_VECTOR_SCHEMA.names
    assert names == ["id", "text", "source", "position", "embedding"]
    assert KNOWLEDGE_VECTOR_SCHEMA.field("position").nullable is True


def test_store_preserves_position_on_new_tables(tmp_path):
    manager = LanceDBManager(db_path=str(tmp_path / "vdb"))
    manager.create_knowledge_vector_table()
    vector = np.zeros(1024, dtype=np.float32)
    vector[0] = 1.0
    manager.store("some text", vector,
                  {"source": "note.md", "position": 3})
    rows = manager.show_all()
    assert len(rows) == 1
    assert rows[0]["position"] == 3


def test_store_tolerates_tables_without_position(tmp_path):
    import lancedb
    import pyarrow as pa

    legacy = pa.schema([
        pa.field("id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("embedding", pa.list_(pa.float32(), 1024)),
    ])
    db = lancedb.connect(str(tmp_path / "legacy-db"))
    db.create_table("knowledge_vectors", schema=legacy, exist_ok=True)
    manager = LanceDBManager(db_path=str(tmp_path / "legacy-db"))
    vector = np.zeros(1024, dtype=np.float32)
    vector[1] = 1.0
    # Must not raise against the older table layout.
    manager.store("legacy text", vector,
                  {"source": "old.md", "position": 7})
    rows = manager.show_all()
    assert len(rows) == 1
    assert rows[0]["text"] == "legacy text"
    assert rows[0]["source"] == "old.md"


def test_indexing_preserves_provenance_and_counts(tmp_path):
    source = tmp_path / "corpus"
    source.mkdir()
    _write(source, "note.md")
    pipe = EvidenceFirstPipeline()
    _service, store, _retriever = build_vector_components(
        str(tmp_path / "index-db"), embedder=_embedder())
    figures = index_corpus(str(source), pipe, _embedder(), store, 1)
    assert figures[0] == 1
    assert figures[1] >= 1 and figures[1] == figures[2]
    assert figures[3] == 0
    rows = store.show_all()
    assert len(rows) == figures[2]
    assert all(row["source"] == str(source / "note.md") for row in rows)


def test_vector_retrieval_round_trip_through_pipeline(tmp_path):
    source = tmp_path / "corpus"
    source.mkdir()
    _write(source, "note.md")
    pipe = EvidenceFirstPipeline()
    _service, store, retriever = build_vector_components(
        str(tmp_path / "index-db"), embedder=_embedder())
    index_corpus(str(source), pipe, _embedder(), store, 1)
    pipe = EvidenceFirstPipeline(retriever=retriever)
    result = pipe.run_file(str(source / "note.md"), corpus=[], core_units=[])
    retrieval = result.units[0].retrieval_result
    assert retrieval.strategy == "lancedb_vector"
    assert retrieval.candidates, "indexed corpus must be retrievable"


def test_indexing_refuses_to_double_index(tmp_path):
    source = tmp_path / "corpus"
    source.mkdir()
    _write(source, "note.md")
    pipe = EvidenceFirstPipeline()
    _service, store, _retriever = build_vector_components(
        str(tmp_path / "index-db"), embedder=_embedder())
    first = index_corpus(str(source), pipe, _embedder(), store, 1)
    assert first[2] >= 1
    rows_after_first = len(store.show_all())
    with pytest.raises(ValueError) as exc:
        index_corpus(str(source), pipe, _embedder(), store, 1)
    assert "refusing to index again" in str(exc.value)
    assert len(store.show_all()) == rows_after_first


def test_ingest_units_respects_bounds(tmp_path):
    directory = tmp_path / "units"
    directory.mkdir()
    _write(directory, "a.md")
    _write(directory, "b.md", "Method:\nA method for tending garden beds.\n")
    pipe = EvidenceFirstPipeline()
    units = ingest_units(str(directory), pipe, 1)
    first_files = {unit.source for unit in units}
    assert first_files == {str(directory / "a.md")}