"""Focused Regression Suite: Generic Indexing Governance & Store Freshness Protection.

Verifies the governance invariants established to prevent generic indexing from
certifying Store freshness:
Scenario A: Generic indexing (index_units, index_corpus) writes operational metadata, never Fresh.
Scenario B: Existing Fresh store cannot survive unverified mutation.
Scenario C: Failed or interrupted generic indexing leaves store unverified, never Fresh.
Scenario D: Retrieval protection: LanceDBVectorRetriever rejects unverified stores fail-closed.
Scenario E: Authorized synchronization (VaultSyncSession) establishes Fresh capability and allows retrieval.
"""

from pathlib import Path
import time
from unittest.mock import patch
import pyarrow as pa
import pytest

from src.living_authenticity.database.lancedb_manager import (
    DatabaseOperationError,
    LanceDBManager,
    VaultMetadataCorruptionError,
    VaultMetadataMissingError,
)
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
    LanceDBVectorRetriever,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.integration.indexing import (
    index_corpus,
    index_units,
)
from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
from src.living_authenticity.knowledge.vault.sync import (
    SYNC_STATE_FRESH,
    VaultSyncError,
    VaultSyncSession,
    VaultSyncUnauthorizedError,
)


class DummyTestEmbedder:
    """Deterministic embedding service stub for testing."""
    dimension = 1024

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.01 * (i + 1)] * self.dimension for i in range(len(texts))]

    def embed_query(self, text: str) -> list[float]:
        return [0.01] * self.dimension

    def embed(self, text: str) -> list[float]:
        return self.embed_query(text)


def _create_core_note(
    vault_path: Path,
    filename: str = "core.md",
    title: str = "Core Note",
    body: str = "Authentic core unit text.",
) -> Path:
    core_dir = vault_path / "01 Core"
    core_dir.mkdir(parents=True, exist_ok=True)
    note = core_dir / filename
    content = (
        f"---\ncanonical_type: Core\ndraft: false\ntitle: \"{title}\"\ntags: [core]\n---\n"
        f"# {title}\n{body}\n"
    )
    note.write_text(content, encoding="utf-8")
    return note


# ==============================================================================
# Scenario A: Generic Indexing Writes Operational Metadata, Never Fresh
# ==============================================================================

def test_index_units_persists_unverified_metadata_never_fresh(tmp_path: Path):
    """index_units() must record status='unverified', unverified=True, never 'fresh'."""
    db_dir = tmp_path / "db_a_units"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()

    unit = KnowledgeUnit(
        id="unit-a",
        source="notes/a.md",
        original_text="Empirical observation of sensory phenomena.",
        meaning="Empirical observation of sensory phenomena.",
        position=0,
    )

    indexed_count = index_units([unit], embedder, store)
    assert indexed_count[0] == 1

    # Verify store metadata
    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("unverified") is True
    assert meta.get("indexing_source") == "index_units"
    assert meta.get("indexed_vectors") == 1
    assert "status" in meta and meta["status"] != "fresh"

    # Verify inspect_store_state exposes unverified status
    state_info = store.inspect_store_state()
    assert state_info.get("metadata", {}).get("status") == "unverified"
    assert state_info.get("metadata", {}).get("unverified") is True


def test_index_corpus_persists_unverified_metadata_never_fresh(tmp_path: Path):
    """index_corpus() must record status='unverified', unverified=True, never 'fresh'."""
    corpus_dir = tmp_path / "corpus_a"
    corpus_dir.mkdir()
    note = corpus_dir / "observation.md"
    note.write_text("# Observation\nCognitive pattern analysis.", encoding="utf-8")

    db_dir = tmp_path / "db_a_corpus"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    pipe = EvidenceFirstPipeline()

    figures = index_corpus(str(corpus_dir), pipe, embedder, store, max_files=5)
    assert figures[0] == 1  # 1 note ingested
    assert figures[2] >= 1  # >=1 vector indexed

    # Verify store metadata
    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("unverified") is True
    assert meta.get("indexing_source") == "index_corpus"
    assert meta.get("indexed_notes") == figures[0]
    assert meta.get("indexed_vectors") == figures[2]
    assert meta.get("status") != "fresh"

    # Verify inspect_store_state exposes unverified status
    state_info = store.inspect_store_state()
    assert state_info.get("metadata", {}).get("status") == "unverified"
    assert state_info.get("metadata", {}).get("unverified") is True


# ==============================================================================
# Scenario B: Existing Fresh Store Cannot Survive Unverified Mutation
# ==============================================================================

def test_existing_fresh_store_invalidated_by_index_units(tmp_path: Path):
    """An existing fresh store must be transitioned to unverified before/after index_units()."""
    db_dir = tmp_path / "db_b_units"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()

    # Pre-seed store with certified Fresh metadata
    store.save_metadata({
        "status": "fresh",
        "indexed_vectors": 0,
        "vault_id": "mock_vault_b",
        "core_hash": "mock_core_b",
    })
    assert store.get_metadata().get("status") == "fresh"

    unit = KnowledgeUnit(
        id="unit-b",
        source="notes/b.md",
        original_text="New unverified knowledge unit text.",
        meaning="New unverified knowledge unit text.",
        position=0,
    )

    index_units([unit], embedder, store)

    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("unverified") is True
    assert meta.get("indexing_source") == "index_units"
    assert meta.get("status") != "fresh"


def test_existing_fresh_store_invalidated_by_index_corpus(tmp_path: Path):
    """An existing fresh store must be transitioned to unverified before/after index_corpus()."""
    corpus_dir = tmp_path / "corpus_b"
    corpus_dir.mkdir()
    (corpus_dir / "note.md").write_text("# Note B\nUnverified corpus text.", encoding="utf-8")

    db_dir = tmp_path / "db_b_corpus"
    store = LanceDBManager(str(db_dir))
    embedder = DummyTestEmbedder()
    pipe = EvidenceFirstPipeline()

    # Pre-seed store with certified Fresh metadata
    store.create_knowledge_vector_table()
    store.save_metadata({
        "status": "fresh",
        "indexed_vectors": 0,
        "vault_id": "mock_vault_corpus_b",
    })
    assert store.get_metadata().get("status") == "fresh"

    index_corpus(str(corpus_dir), pipe, embedder, store, max_files=1)

    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("unverified") is True
    assert meta.get("indexing_source") == "index_corpus"
    assert meta.get("status") != "fresh"


# ==============================================================================
# Scenario C: Failed or Interrupted Generic Indexing
# ==============================================================================

def test_interrupted_index_units_leaves_store_unverified_never_fresh(tmp_path: Path):
    """When index_units() fails mid-execution, pre-mutation invalidation ensures store is unverified."""
    db_dir = tmp_path / "db_c_units"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()

    # Pre-seed store with Fresh status
    store.save_metadata({
        "status": "fresh",
        "indexed_vectors": 0,
        "vault_id": "mock_vault_c",
    })
    assert store.get_metadata().get("status") == "fresh"

    class FailingEmbedder:
        dimension = 1024
        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("Simulated crash during batch embedding generation")
        def embed(self, text: str) -> list[float]:
            raise RuntimeError("Simulated crash during embedding")

    unit = KnowledgeUnit(
        id="unit-c",
        source="notes/c.md",
        original_text="Failing unit text.",
        meaning="Failing unit text.",
        position=0,
    )

    with pytest.raises(RuntimeError) as exc_info:
        index_units([unit], FailingEmbedder(), store)
    assert "Simulated crash" in str(exc_info.value)

    # Store must NOT remain fresh! Pre-mutation invalidation must have persisted unverified
    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("unverified") is True
    assert meta.get("unverified_reason") == "modified_by_generic_indexing"
    assert meta.get("status") != "fresh"


def test_interrupted_index_corpus_leaves_store_unverified_never_fresh(tmp_path: Path):
    """When index_corpus() fails mid-execution, pre-mutation invalidation ensures store is unverified."""
    corpus_dir = tmp_path / "corpus_c"
    corpus_dir.mkdir()
    (corpus_dir / "note.md").write_text("# Note C\nInterrupted corpus text.", encoding="utf-8")

    db_dir = tmp_path / "db_c_corpus"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()

    # Pre-seed store with Fresh status
    store.save_metadata({
        "status": "fresh",
        "indexed_vectors": 0,
        "vault_id": "mock_vault_corpus_c",
    })
    assert store.get_metadata().get("status") == "fresh"

    class FailingCorpusEmbedder:
        dimension = 1024
        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("Simulated mid-corpus embedding failure")
        def embed(self, text: str) -> list[float]:
            raise RuntimeError("Simulated mid-corpus embedding failure")

    pipe = EvidenceFirstPipeline()
    with pytest.raises(RuntimeError) as exc_info:
        index_corpus(str(corpus_dir), pipe, FailingCorpusEmbedder(), store, max_files=1)
    assert "Simulated mid-corpus" in str(exc_info.value)

    # Store must NOT remain fresh!
    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("unverified") is True
    assert meta.get("unverified_reason") == "modified_by_generic_indexing"
    assert meta.get("status") != "fresh"


# ==============================================================================
# Scenario D: Retrieval Protection
# ==============================================================================

def test_retrieval_protection_rejects_unverified_store_with_vectors(tmp_path: Path):
    """LanceDBVectorRetriever.retrieve() must raise DatabaseOperationError when store is unverified."""
    db_dir = tmp_path / "db_d_vectors"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()

    unit = KnowledgeUnit(
        id="unit-d",
        source="notes/d.md",
        original_text="Unverified content for retrieval testing.",
        meaning="Unverified content for retrieval testing.",
        position=0,
    )
    index_units([unit], embedder, store)

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
    )

    query = KnowledgeUnit(id="q", source="inbox", original_text="retrieval query", meaning="retrieval query")
    with pytest.raises(DatabaseOperationError) as exc_info:
        retriever.retrieve(query)

    assert "Cannot retrieve from an unverified" in str(exc_info.value)
    assert "unverified" in str(exc_info.value)


def test_retrieval_protection_rejects_unverified_store_empty(tmp_path: Path):
    """LanceDBVectorRetriever.retrieve() must raise DatabaseOperationError on an unverified empty store."""
    db_dir = tmp_path / "db_d_empty"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({
        "status": "unverified",
        "unverified": True,
        "indexed_vectors": 0,
    })
    embedder = DummyTestEmbedder()

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
    )

    query = KnowledgeUnit(id="q", source="inbox", original_text="retrieval query", meaning="retrieval query")
    with pytest.raises(DatabaseOperationError) as exc_info:
        retriever.retrieve(query)

    assert "Cannot retrieve from an unverified" in str(exc_info.value)


# ==============================================================================
# Scenario E: Authorized Synchronization Maintains Fresh Capability
# ==============================================================================

def test_authorized_synchronization_establishes_fresh_and_enables_retrieval(tmp_path: Path):
    """VaultSyncSession establishes certified Fresh status, which allows retrieval."""
    vault = tmp_path / "vault_e"
    vault.mkdir()
    _create_core_note(vault, title="Core Architecture", body="Authoritative Core theory text.")
    (vault / "note.md").write_text("# Ordinary Note\nEmpirical observation.", encoding="utf-8")

    vdb = tmp_path / "vdb_e"
    embedder = DummyTestEmbedder()

    # 1. Authorize genuine first initialization
    session = VaultSyncSession()
    auth_rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=embedder)
    assert auth_rep["fresh"] is True
    assert session.is_fresh is True

    # Check store metadata is certified fresh
    assert session._store is not None
    meta = session._store.get_metadata()
    assert meta.get("status") == "fresh"
    assert meta.get("core_hash") is not None
    indexed_vectors = meta.get("indexed_vectors")
    assert isinstance(indexed_vectors, int) and indexed_vectors >= 1

    # 2. Retrieval against the authorized Fresh store succeeds
    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=session._store,
    )
    query = KnowledgeUnit(id="q", source="inbox", original_text="theory text", meaning="theory text")
    res = retriever.retrieve(query)
    assert len(res.candidates) >= 1
    assert res.strategy == "lancedb_vector"

    # 3. Modify note to create changes and verify reconciliation with approval ticket
    time.sleep(0.05)
    (vault / "note2.md").write_text("# Note 2\nAdditional observation.", encoding="utf-8")

    changes = session.check_changes()
    assert changes["stale"] is True

    # Reconcile without ticket is blocked
    with pytest.raises(VaultSyncUnauthorizedError):
        session.reconcile()

    # Reconcile with ticket succeeds and maintains Fresh certification
    req = session.request_approval(run_id="run_e")
    session.resolve_approval(req["approval_id"], "approve")
    assert session._active_ticket is not None
    rec_rep = session.reconcile(ticket=session._active_ticket)
    assert rec_rep["fresh"] is True
    assert session.is_fresh is True

    meta_after = session._store.get_metadata()
    assert meta_after.get("status") == "fresh"

    # Retrieval continues to succeed
    res_after = retriever.retrieve(query)
    assert len(res_after.candidates) >= 1
    assert res_after.strategy == "lancedb_vector"


# ==============================================================================
# Phase 3 Deterministic Concurrency & Governance Test Matrix (16 Scenarios)
# ==============================================================================

import os
import subprocess
import sys
import threading
from src.living_authenticity.database.lancedb_manager import (
    DatabaseLockError,
    VaultMetadataCorruptionError,
    VaultMetadataError,
    VaultMetadataMissingError,
)
from src.living_authenticity.knowledge.vault.sync import (
    VaultSyncApprovalTicket,
    VaultSyncError,
    VaultSyncUnauthorizedError,
)


def test_scenario_5_metadata_corruption_fails_closed(tmp_path: Path):
    """5. Metadata corruption fails closed."""
    db_dir = tmp_path / "db_scen_5"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "manifest": {}})
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    tbl = store.db.open_table("index_metadata")
    tbl.delete("id IS NOT NULL")
    tbl.add([{"id": "meta", "payload": "{not_valid_json: 123"}])

    state = store.inspect_store_state()
    assert state["state"] == "corrupted_metadata"

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=store)
    with pytest.raises(VaultMetadataCorruptionError):
        retriever.retrieve(KnowledgeUnit(id="q", source="q", original_text="Sample", meaning="Sample"))


def test_scenario_6_empty_metadata_with_populated_vectors_fails_closed(tmp_path: Path):
    """6. Empty metadata with populated vectors fails closed."""
    db_dir = tmp_path / "db_scen_6"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "manifest": {}})
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    tbl = store.db.open_table("index_metadata")
    tbl.delete("id IS NOT NULL")

    state = store.inspect_store_state()
    assert state["state"] == "missing_metadata"

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=store)
    with pytest.raises(VaultMetadataMissingError):
        retriever.retrieve(KnowledgeUnit(id="q", source="q", original_text="Sample", meaning="Sample"))


def test_scenario_7_missing_metadata_with_populated_vectors_fails_closed(tmp_path: Path):
    """7. Missing metadata with populated vectors fails closed."""
    db_dir = tmp_path / "db_scen_7"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    store.drop_table("index_metadata")

    state = store.inspect_store_state()
    assert state["state"] == "missing_metadata"

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=store)
    with pytest.raises(VaultMetadataMissingError):
        retriever.retrieve(KnowledgeUnit(id="q", source="q", original_text="Sample", meaning="Sample"))


def test_scenario_8_interrupted_initialization_cannot_receive_initial_sync_privileges(tmp_path: Path):
    """8. Interrupted initialization cannot receive initial-sync privileges."""
    vault = tmp_path / "vault_scen_8"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nText.", encoding="utf-8")
    vdb = tmp_path / "vdb_scen_8"
    session = VaultSyncSession()
    embedder = DummyTestEmbedder()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=embedder)

    assert session._store is not None
    store = session._store
    meta = dict(store.get_metadata())
    meta["status"] = "sync_in_progress"
    store.save_metadata(meta)

    session2 = VaultSyncSession()
    session2.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=embedder)
    assert session2._is_genuine_first_init() is False
    with pytest.raises(VaultSyncUnauthorizedError):
        session2.reconcile()


def test_scenario_9_metadata_commit_interruption_preserves_safe_state(tmp_path: Path):
    """9. Metadata commit interruption preserves a safe state."""
    db_dir = tmp_path / "db_scen_9"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    initial_meta = {"status": "fresh", "manifest": {"entries": {}}, "indexed_vectors": 0}
    store.save_metadata(initial_meta)

    meta_update = {"status": "unverified", "indexing_source": "test", "indexed_vectors": 0}
    store.save_metadata(meta_update)

    read_back = store.get_metadata()
    assert read_back.get("status") == "unverified"
    assert read_back.get("indexing_source") == "test"
    tbl = store.db.open_table("index_metadata")
    assert tbl.count_rows() == 1


def test_scenario_10_generic_indexing_preserves_authoritative_manifest_fields(tmp_path: Path):
    """10. Generic indexing preserves authoritative manifest fields."""
    db_dir = tmp_path / "db_scen_10"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()

    auth_manifest = {"entries": {"note1.md": {"sha256": "abc123", "size": 100}}}
    baseline_meta = {
        "status": "fresh",
        "vault_id": "vault-scen-10",
        "manifest": auth_manifest,
        "last_synced_at": "2026-11-09T00:00:00Z",
        "core_hash": "corehash999",
        "embedding_dimension": 1024,
    }
    store.save_metadata(baseline_meta)

    unit = KnowledgeUnit(id="u1", source="u1.md", original_text="Obs", meaning="Obs")
    index_units([unit], embedder, store)

    meta_after = store.get_metadata()
    assert meta_after.get("status") == "unverified"
    assert meta_after.get("vault_id") == "vault-scen-10"
    assert meta_after.get("manifest") == auth_manifest
    assert meta_after.get("last_synced_at") == "2026-11-09T00:00:00Z"
    assert meta_after.get("core_hash") == "corehash999"
    assert meta_after.get("embedding_dimension") == 1024


def test_scenario_11_failed_invalidation_prevents_vector_mutation(tmp_path: Path):
    """11. Failed invalidation prevents vector mutation."""
    db_dir = tmp_path / "db_scen_11"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    store.save_metadata({"status": "fresh", "manifest": {}})

    def failing_save(metadata: dict) -> None:
        raise DatabaseOperationError("Simulated disk error during invalidation")

    store.save_metadata = failing_save  # type: ignore[assignment]

    unit = KnowledgeUnit(id="u_fail", source="fail.md", original_text="Obs", meaning="Obs")
    with pytest.raises(DatabaseOperationError):
        index_units([unit], embedder, store)

    tbl = store.get_table()
    assert tbl.count_rows() == 0


def test_scenario_12_clean_rebuild_requires_explicit_clean_rebuild_approval(tmp_path: Path):
    """12. Clean rebuild requires explicit clean_rebuild approval."""
    vault = tmp_path / "vault_scen_12"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nText.", encoding="utf-8")
    vdb = tmp_path / "vdb_scen_12"
    session = VaultSyncSession()
    embedder = DummyTestEmbedder()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=embedder)

    assert session._store is not None
    meta = dict(session._store.get_metadata())
    meta["status"] = "unverified"
    meta["unverified"] = True
    session._store.save_metadata(meta)

    req = session.request_approval(run_id="run_12")
    assert session._pending_approval is not None
    assert session._pending_approval.operation_type == "clean_rebuild"

    session.resolve_approval(req["approval_id"], "approve")
    ticket = session._active_ticket
    assert ticket is not None
    assert ticket.operation_type == "clean_rebuild"

    rec_rep = session.reconcile(ticket=ticket)
    assert rec_rep["fresh"] is True
    assert session.is_fresh is True
    assert session._store.get_metadata().get("status") == "fresh"


def test_scenario_13_incremental_tickets_cannot_authorize_clean_rebuild(tmp_path: Path):
    """13. Incremental tickets cannot authorize clean rebuild."""
    vault = tmp_path / "vault_scen_13"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nText.", encoding="utf-8")
    vdb = tmp_path / "vdb_scen_13"
    session = VaultSyncSession()
    embedder = DummyTestEmbedder()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=embedder)

    assert session._store is not None
    meta = dict(session._store.get_metadata())
    meta["status"] = "unverified"
    meta["unverified"] = True
    session._store.save_metadata(meta)

    req = session.request_approval(run_id="run_13")
    session.resolve_approval(req["approval_id"], "approve")
    valid_ticket = session._active_ticket
    assert valid_ticket is not None
    # Tamper operation_type to incremental_sync
    valid_ticket.operation_type = "incremental_sync"
    bad_ticket = valid_ticket

    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        session.reconcile(ticket=bad_ticket)
    assert "clean_rebuild" in str(exc_info.value)


def test_scenario_14_ordinary_notes_cannot_impersonate_core(tmp_path: Path):
    """14. Ordinary notes cannot impersonate Core."""
    vault = tmp_path / "vault_scen_14"
    vault.mkdir()
    core_file = _create_core_note(vault, title="Genuine Core", body="Authentic thesis.")
    impostor = vault / "impostor.md"
    impostor.write_text(
        "---\ncanonical_type: Core\ndraft: false\ntitle: Impostor\ntags: [core]\n---\nFake Core thesis.",
        encoding="utf-8",
    )
    vdb = tmp_path / "vdb_scen_14"
    session = VaultSyncSession()
    embedder = DummyTestEmbedder()
    auth_rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=embedder)
    assert auth_rep["fresh"] is True

    assert session._store is not None
    rows = session._store.show_all()
    core_rows = [r for r in rows if r.get("is_core") is True]
    impostor_rows = [r for r in rows if "impostor.md" in str(r.get("source", ""))]

    assert len(core_rows) >= 1
    for cr in core_rows:
        assert "01 Core" in str(cr.get("source", ""))
    for ir in impostor_rows:
        assert ir.get("is_core") is False


def test_scenario_15_test_mode_and_vault_mode_core_identity_remain_isolated(tmp_path: Path):
    """15. Test Mode and Vault Mode Core identity remain isolated."""
    vault = tmp_path / "vault_scen_15"
    vault.mkdir()
    core_file = _create_core_note(vault, title="Vault Core", body="Vault Core Thesis.")
    vdb = tmp_path / "vdb_scen_15"
    session = VaultSyncSession()
    embedder = DummyTestEmbedder()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=embedder)

    from src.living_authenticity.knowledge.vault.core_validator import (
        validate_and_load_core,
        CoreResolutionError,
    )
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
    assert val_core.source_mode == "personal_vault"
    assert val_core.path.resolve() == core_file.resolve()

    with pytest.raises(CoreResolutionError):
        validate_and_load_core("personal_vault", vault_root=str(core_file), pipeline=EvidenceFirstPipeline())


def test_scenario_16_fresh_certification_requires_successful_candidate_validation(tmp_path: Path):
    """16. Fresh certification requires successful candidate validation."""
    vault = tmp_path / "vault_scen_16"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nInitial text.", encoding="utf-8")
    vdb = tmp_path / "vdb_scen_16"
    session = VaultSyncSession()
    embedder = DummyTestEmbedder()
    auth_rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=embedder)
    assert auth_rep["fresh"] is True

    orig_validate = session._validate_sync_candidate_consistency

    def sabotaged_validate(*args, **kwargs):
        assert session._store is not None
        session._store.get_table().delete("id IS NOT NULL")
        return orig_validate(*args, **kwargs)

    session._validate_sync_candidate_consistency = sabotaged_validate

    (vault / "note2.md").write_text("# Note 2\nNew text.", encoding="utf-8")
    req = session.request_approval(run_id="run_16")
    session.resolve_approval(req["approval_id"], "approve")

    assert session._active_ticket is not None
    rec_rep = session.reconcile(ticket=session._active_ticket)
    assert rec_rep["fresh"] is False
    assert "Pre-commit consistency validation failed" in rec_rep["error"]
    assert session.is_fresh is False
    assert session._store is not None
    assert session._store.get_metadata().get("status") != "fresh"


# ==============================================================================
# Finding 7: Generic Indexing In-Memory Fallback & TypeError Safety
# ==============================================================================


def test_generic_indexing_in_memory_test_double_without_db_path_succeeds():
    """In-memory test doubles without db_path and without exclusive_lock use nullcontext safely."""
    from src.living_authenticity.knowledge.integration.indexing import index_units

    stored_records = []

    class InMemoryTestDoubleStore:
        def show_all(self):
            return list(stored_records)
        def store(self, text, vec, meta):
            stored_records.append({"text": text, "vec": vec, "meta": meta})

    store = InMemoryTestDoubleStore()
    embedder = DummyTestEmbedder()
    unit = KnowledgeUnit(id="u1", source="u1.md", original_text="Test", meaning="Test")

    count, stored = index_units([unit], embedder, store)
    assert count == 1
    assert stored == 1
    assert len(stored_records) == 1


def test_generic_indexing_store_internal_type_error_propagates_without_retrying():
    """Internal TypeError from store.store() must propagate immediately without retry loop."""
    from src.living_authenticity.knowledge.integration.indexing import index_units

    call_count = 0

    class FailingStore:
        def show_all(self):
            return []
        def store(self, text, vec, meta, **kwargs):
            nonlocal call_count
            call_count += 1
            raise TypeError("Arrow conversion internal type error: int not string")

    store = FailingStore()
    embedder = DummyTestEmbedder()
    unit = KnowledgeUnit(id="u1", source="u1.md", original_text="Test", meaning="Test")

    with pytest.raises(TypeError, match="Arrow conversion internal type error"):
        index_units([unit], embedder, store)

    # Store was called exactly once — zero unsafe blind retries
    assert call_count == 1


def test_generic_save_metadata_fails_closed_on_corrupted_metadata(tmp_path: Path):
    """save_metadata() and get_metadata() must fail closed with VaultMetadataCorruptionError when index_metadata is corrupted."""
    db_dir = tmp_path / "db_corrupt_save"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "manifest": {}})
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    # Corrupt index_metadata table payload
    tbl = store.db.open_table("index_metadata")
    tbl.delete("id IS NOT NULL")
    tbl.add([{"id": "meta", "payload": "{invalid_json_payload: [}"}])

    # 1. get_metadata() fails closed with VaultMetadataCorruptionError
    with pytest.raises(VaultMetadataCorruptionError):
        store.get_metadata()

    # 2. save_metadata() fails closed with VaultMetadataCorruptionError
    with pytest.raises(VaultMetadataCorruptionError):
        store.save_metadata({"status": "unverified"})

    # Verify corrupt payload was not overwritten (database not mutated)
    tbl = store.db.open_table("index_metadata")
    rows = tbl.to_arrow().to_pylist()
    assert rows[0]["payload"] == "{invalid_json_payload: [}"


def test_generic_save_metadata_fails_closed_on_missing_metadata_in_populated_store(tmp_path: Path):
    """save_metadata() and get_metadata() must fail closed with VaultMetadataMissingError when metadata table has no payload in populated store."""
    db_dir = tmp_path / "db_missing_save"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "manifest": {}})
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    # Empty the index_metadata table so it has populated vectors but missing metadata
    tbl = store.db.open_table("index_metadata")
    tbl.delete("id IS NOT NULL")

    # 1. get_metadata() fails closed with VaultMetadataMissingError
    with pytest.raises(VaultMetadataMissingError):
        store.get_metadata()

    # 2. save_metadata() fails closed with VaultMetadataMissingError
    with pytest.raises(VaultMetadataMissingError):
        store.save_metadata({"status": "unverified"})

    # Verify empty table was not overwritten (database not mutated)
    tbl = store.db.open_table("index_metadata")
    assert len(tbl.to_arrow()) == 0


def test_generic_save_metadata_fails_closed_when_metadata_table_absent_in_populated_store(tmp_path: Path):
    """Generic save_metadata() and get_metadata() must fail closed with VaultMetadataMissingError when index_metadata is absent in initialized populated store."""
    db_dir = tmp_path / "db_no_meta_table"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    # Establish initial metadata record
    store.save_metadata({"status": "unverified", "indexed_vectors": 1})
    assert "index_metadata" in store._table_names()
    assert "_store_lifecycle" in store._table_names()

    # Drop index_metadata table to simulate loss of metadata in populated store
    store.drop_table("index_metadata")
    assert "index_metadata" not in store._table_names()

    # 1. get_metadata() fails closed with VaultMetadataMissingError
    with pytest.raises(VaultMetadataMissingError):
        store.get_metadata()

    # 2. save_metadata() fails closed with VaultMetadataMissingError
    with pytest.raises(VaultMetadataMissingError):
        store.save_metadata({"status": "unverified"})

    # Verify no index_metadata table was created as a side effect (database not mutated)
    assert "index_metadata" not in store._table_names()


def test_generic_save_metadata_succeeds_on_new_empty_store(tmp_path: Path):
    """Generic save_metadata() succeeds on a genuinely new empty store."""
    db_dir = tmp_path / "db_first_init_empty"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()

    # Genuinely uninitialized store
    assert "index_metadata" not in store._table_names()
    assert "_store_lifecycle" not in store._table_names()

    store.save_metadata({"status": "unverified", "indexed_vectors": 0})
    assert "index_metadata" in store._table_names()
    assert "_store_lifecycle" in store._table_names()

    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("indexed_vectors") == 0


def test_generic_save_metadata_succeeds_on_new_store_with_vectors_before_first_metadata(tmp_path: Path):
    """Generic save_metadata() succeeds on a genuinely new store with vectors inserted before first metadata initialization."""
    db_dir = tmp_path / "db_first_init_with_vectors"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    # Genuinely uninitialized store with vectors
    assert "index_metadata" not in store._table_names()
    assert "_store_lifecycle" not in store._table_names()

    store.save_metadata({"status": "unverified", "indexed_vectors": 1})
    assert "index_metadata" in store._table_names()
    assert "_store_lifecycle" in store._table_names()

    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("indexed_vectors") == 1


def test_generic_save_metadata_fails_closed_on_operational_read_failure(tmp_path: Path):
    """save_metadata() and get_metadata() must fail closed with DatabaseOperationError on operational read error without mutating database."""
    db_dir = tmp_path / "db_op_fail"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "manifest": {}})
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    # Snapshot pre-failure table state
    pre_tbl = store.db.open_table("index_metadata")
    pre_payload = pre_tbl.to_arrow().to_pylist()[0]["payload"]

    orig_open = store.db.open_table

    def failing_open(name):
        if name == "index_metadata":
            raise OSError("Simulated disk read I/O error")
        return orig_open(name)

    with patch.object(store.db, "open_table", side_effect=failing_open):
        # 1. get_metadata() fails closed with DatabaseOperationError
        with pytest.raises(DatabaseOperationError, match="Simulated disk read I/O error"):
            store.get_metadata()

        # 2. save_metadata() fails closed with DatabaseOperationError
        with pytest.raises(DatabaseOperationError, match="Simulated disk read I/O error"):
            store.save_metadata({"status": "unverified"})

    # Database not mutated: pre-failure payload is preserved intact
    post_tbl = store.db.open_table("index_metadata")
    assert post_tbl.to_arrow().to_pylist()[0]["payload"] == pre_payload


def test_save_metadata_rollback_on_lifecycle_marker_creation_failure(tmp_path: Path):
    """If lifecycle-marker creation fails during initial save_metadata(), newly created index_metadata is rolled back."""
    db_dir = tmp_path / "db_marker_fail"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    # Fault injection: _mark_initialized fails during first initialization
    with patch.object(store, "_mark_initialized", side_effect=DatabaseOperationError("Simulated disk full on marker")):
        with pytest.raises(DatabaseOperationError, match="Simulated disk full on marker"):
            store.save_metadata({"status": "unverified", "indexed_vectors": 1})

    # Fail-closed rollback verification:
    # 1. index_metadata table was rolled back (not left orphaned)
    assert "index_metadata" not in store._table_names()
    # 2. _store_lifecycle table was not created
    assert "_store_lifecycle" not in store._table_names()
    # 3. Vector records are intact
    assert len(store.show_all()) == 1

    # Recovery verification: subsequent retry succeeds cleanly
    store.save_metadata({"status": "unverified", "indexed_vectors": 1})
    assert "index_metadata" in store._table_names()
    assert "_store_lifecycle" in store._table_names()
    meta = store.get_metadata()
    assert meta.get("status") == "unverified"
    assert meta.get("indexed_vectors") == 1


def test_validate_lifecycle_marker_fails_closed_on_empty_or_corrupted_marker(tmp_path: Path):
    """_validate_lifecycle_marker() fails closed with DatabaseOperationError if marker table is empty or invalid."""
    db_dir = tmp_path / "db_corrupt_marker"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "unverified", "indexed_vectors": 0})
    assert "_store_lifecycle" in store._table_names()

    # Empty the lifecycle marker table
    tbl = store.db.open_table("_store_lifecycle")
    tbl.delete("id IS NOT NULL")
    assert len(tbl.to_arrow()) == 0

    # Validation must fail closed
    with pytest.raises(DatabaseOperationError, match="Invalid _store_lifecycle marker"):
        store._validate_lifecycle_marker()

    # get_metadata and save_metadata must fail closed
    with pytest.raises(DatabaseOperationError):
        store.get_metadata()
    with pytest.raises(DatabaseOperationError):
        store.save_metadata({"status": "unverified"})


def test_generic_save_metadata_cannot_establish_core_baseline_fields(tmp_path: Path):
    """Generic save_metadata() cannot establish core vector baseline fields out of thin air."""
    db_dir = tmp_path / "db_no_baseline"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()

    store.save_metadata({
        "status": "unverified",
        "core_vector_digest": "fake_digest",
        "core_vector_count": 99,
        "core_record_ids": ["r1", "r2"],
    })

    meta = store.get_metadata()
    assert "core_vector_digest" not in meta
    assert "core_vector_count" not in meta
    assert "core_record_ids" not in meta


def test_index_corpus_surfaces_snapshot_failure_and_does_not_persist_empty_snapshot(tmp_path: Path):
    """index_corpus() must raise RuntimeError on snapshot failure and never persist an empty snapshot."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\nContent.", encoding="utf-8")
    db_dir = tmp_path / "db_snap_fail"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    pipe = EvidenceFirstPipeline()

    with patch(
        "src.living_authenticity.knowledge.vault.context.snapshot_vault",
        side_effect=OSError("Disk read failure during snapshot"),
    ):
        with pytest.raises(RuntimeError, match="Failed snapshotting corpus"):
            index_corpus(str(corpus), pipe, embedder, store, max_files=1)

    # Verify store metadata was not saved with an empty operational_corpus_snapshot
    meta = store.get_metadata()
    assert "operational_corpus_snapshot" not in meta


def test_index_corpus_genuinely_empty_vault_persists_valid_empty_snapshot(tmp_path: Path):
    """index_corpus() on genuinely empty vault succeeds and persists valid empty snapshot."""
    empty_corpus = tmp_path / "empty_corpus"
    empty_corpus.mkdir()
    db_dir = tmp_path / "db_empty_snap"
    store = LanceDBManager(str(db_dir))
    embedder = DummyTestEmbedder()
    pipe = EvidenceFirstPipeline()

    files, units, vecs, skipped = index_corpus(str(empty_corpus), pipe, embedder, store, max_files=1)
    assert files == 0
    assert units == 0

    meta = store.get_metadata()
    assert "operational_corpus_snapshot" in meta
    snap = meta["operational_corpus_snapshot"]
    assert snap.get("entries") == {}
    assert snap.get("files_discovered") == 0


def test_lifecycle_only_store_classified_as_inconsistent_and_denies_first_init(tmp_path: Path):
    """Lifecycle-only store (marker exists, vector table absent, metadata absent) is inconsistent and denies first init."""
    db_dir = tmp_path / "db_lifecycle_only"
    store = LanceDBManager(str(db_dir))
    store._mark_initialized()
    assert "_store_lifecycle" in store._table_names()
    assert "knowledge_vectors" not in store._table_names()
    assert "index_metadata" not in store._table_names()

    # 1. inspect_store_state() classifies as inconsistent_initialized, never genuine_first_init
    state_info = store.inspect_store_state()
    assert state_info["state"] == "inconsistent_initialized"
    assert state_info["state"] != "genuine_first_init"
    assert state_info["has_lifecycle_marker"] is True

    # 2. _is_genuine_first_init() denies first-initialization authorization
    session = VaultSyncSession()
    session._store = store
    with pytest.raises(VaultSyncError, match="inconsistent_initialized"):
        session._is_genuine_first_init()


def test_lifecycle_marker_with_empty_vectors_classified_as_inconsistent_and_denies_first_init(tmp_path: Path):
    """Lifecycle marker plus empty vector table with missing metadata is inconsistent and denies first init."""
    db_dir = tmp_path / "db_lifecycle_empty_vecs"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store._mark_initialized()
    assert "_store_lifecycle" in store._table_names()
    assert "knowledge_vectors" in store._table_names()
    assert "index_metadata" not in store._table_names()
    assert store.inspect_store_state()["vector_count"] == 0

    # 1. inspect_store_state() classifies as inconsistent_initialized, never genuine_first_init
    state_info = store.inspect_store_state()
    assert state_info["state"] == "inconsistent_initialized"
    assert state_info["state"] != "genuine_first_init"
    assert state_info["has_lifecycle_marker"] is True

    # 2. _is_genuine_first_init() denies first-initialization authorization
    session = VaultSyncSession()
    session._store = store
    with pytest.raises(VaultSyncError, match="inconsistent_initialized"):
        session._is_genuine_first_init()


def test_save_metadata_rollback_failure_propagates_both_errors_and_fails_closed(tmp_path: Path):
    """When rollback deletion fails after marker creation failure, both errors are propagated and store fails closed."""
    db_dir = tmp_path / "db_rb_fail"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    store.store("Sample", embedder.embed_query("Sample"), {"source": "s.md", "id": "s1"})

    # Fault injection: _mark_initialized fails, and drop_table also fails during rollback
    with patch.object(store, "_mark_initialized", side_effect=DatabaseOperationError("Marker write fault")):
        with patch.object(store, "drop_table", side_effect=DatabaseOperationError("Rollback permission denied")):
            with pytest.raises(DatabaseOperationError) as exc_info:
                store.save_metadata({"status": "unverified", "indexed_vectors": 1})

            # Verify both original error and rollback failure are reported
            err_msg = str(exc_info.value)
            assert "Marker write fault" in err_msg
            assert "Rollback permission denied" in err_msg

    # Vector records remain intact
    assert len(store.show_all()) == 1

    # index_metadata remains because rollback failed
    assert "index_metadata" in store._table_names()
    assert "_store_lifecycle" not in store._table_names()

    # 1. inspect_store_state() classifies as inconsistent_initialized, never valid or genuine_first_init
    state_info = store.inspect_store_state()
    assert state_info["state"] == "inconsistent_initialized"
    assert state_info["has_lifecycle_marker"] is False
    assert state_info["has_metadata_table"] is True

    # 2. _is_genuine_first_init() raises VaultSyncError (denies first-initialization privilege)
    session = VaultSyncSession()
    session._store = store
    with pytest.raises(VaultSyncError, match="inconsistent_initialized"):
        session._is_genuine_first_init()

    # 3. Subsequent generic save_metadata() fails closed and refuses to silently establish lifecycle marker
    with pytest.raises(DatabaseOperationError, match="lifecycle marker is absent"):
        store.save_metadata({"status": "unverified", "indexed_vectors": 1})
    assert "_store_lifecycle" not in store._table_names()

    # 4. Retrieval fails closed on unverified/inconsistent store
    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=store)
    with pytest.raises(DatabaseOperationError, match="unverified or invalid vector store"):
        retriever.retrieve(KnowledgeUnit(id="q", source="q", original_text="Sample", meaning="Sample"))


def test_post_rollback_failure_fresh_metadata_cannot_bypass_lifecycle_invariants(tmp_path: Path):
    """Even if index_metadata claims status 'fresh', absence of _store_lifecycle forces inconsistent_initialized."""
    db_dir = tmp_path / "db_fresh_no_lifecycle"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyTestEmbedder()
    store.store("Sample doc", embedder.embed_query("Sample doc"), {"source": "doc.md", "id": "d1"})

    # Simulate rollback failure where the attempted metadata had status="fresh"
    with patch.object(store, "_mark_initialized", side_effect=DatabaseOperationError("Marker disk full")):
        with patch.object(store, "drop_table", side_effect=DatabaseOperationError("Lock failure")):
            with pytest.raises(DatabaseOperationError):
                store.save_metadata({"status": "fresh", "indexed_vectors": 1})

    assert "index_metadata" in store._table_names()
    assert "_store_lifecycle" not in store._table_names()

    # 1. inspect_store_state() classifies as inconsistent_initialized, NEVER valid
    state_info = store.inspect_store_state()
    assert state_info["state"] == "inconsistent_initialized"
    assert state_info["state"] != "valid"
    assert state_info["has_lifecycle_marker"] is False

    # 2. Retrieval fails closed and refuses to retrieve
    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=store)
    with pytest.raises(DatabaseOperationError, match="unverified or invalid vector store"):
        retriever.retrieve(KnowledgeUnit(id="q", source="q", original_text="Sample", meaning="Sample"))

    # 3. Generic save_metadata() fails closed and cannot create lifecycle marker
    with pytest.raises(DatabaseOperationError, match="lifecycle marker is absent"):
        store.save_metadata({"status": "unverified", "indexed_vectors": 1})
    assert "_store_lifecycle" not in store._table_names()


def test_get_metadata_storage_corruption_exception_classification(tmp_path: Path):
    """get_metadata() classifies typed PyArrow and Lance storage corruption as VaultMetadataCorruptionError."""
    db_dir = tmp_path / "db_storage_corrupt"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "manifest": {}})

    orig_open = store.db.open_table

    # 1. pa.ArrowInvalid during table read -> VaultMetadataCorruptionError
    def failing_open_invalid(name, *args, **kwargs):
        if name == "index_metadata":
            raise pa.ArrowInvalid("Corrupted IPC header")
        return orig_open(name, *args, **kwargs)

    with patch.object(store.db, "open_table", side_effect=failing_open_invalid):
        with pytest.raises(VaultMetadataCorruptionError, match="Corrupted storage format"):
            store.get_metadata()

    # 2. pa.ArrowSerializationError during table read -> VaultMetadataCorruptionError
    def failing_open_serial(name, *args, **kwargs):
        if name == "index_metadata":
            raise pa.ArrowSerializationError("Malformed record batch")
        return orig_open(name, *args, **kwargs)

    with patch.object(store.db, "open_table", side_effect=failing_open_serial):
        with pytest.raises(VaultMetadataCorruptionError, match="Corrupted storage format"):
            store.get_metadata()

    # 3. Low-level LanceError(Corrupt) -> VaultMetadataCorruptionError
    def failing_open_corrupt(name, *args, **kwargs):
        if name == "index_metadata":
            raise RuntimeError("lance error: LanceError(Corrupt): bad checksum")
        return orig_open(name, *args, **kwargs)

    with patch.object(store.db, "open_table", side_effect=failing_open_corrupt):
        with pytest.raises(VaultMetadataCorruptionError, match="Low-level Lance storage corruption"):
            store.get_metadata()


def test_get_metadata_operational_io_exception_classification(tmp_path: Path):
    """get_metadata() classifies operational I/O and filesystem errors as DatabaseOperationError."""
    db_dir = tmp_path / "db_operational_io"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "manifest": {}})

    orig_open = store.db.open_table

    # 1. PermissionError -> DatabaseOperationError
    def failing_open_perm(name, *args, **kwargs):
        if name == "index_metadata":
            raise PermissionError("Access is denied")
        return orig_open(name, *args, **kwargs)

    with patch.object(store.db, "open_table", side_effect=failing_open_perm):
        with pytest.raises(DatabaseOperationError, match="Operational failure opening or reading index_metadata table"):
            store.get_metadata()

    # 2. TimeoutError -> DatabaseOperationError
    def failing_open_timeout(name, *args, **kwargs):
        if name == "index_metadata":
            raise TimeoutError("Lock wait timeout")
        return orig_open(name, *args, **kwargs)

    with patch.object(store.db, "open_table", side_effect=failing_open_timeout):
        with pytest.raises(DatabaseOperationError, match="Operational failure opening or reading index_metadata table"):
            store.get_metadata()
