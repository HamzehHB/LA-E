"""Final targeted regression tests for Phase 3:
1. Issue A: Prove Ordinary-Note Vector Completeness at Chunk Level (17 scenarios)
2. Issue B: Fail-Closed Retrieval for Missing Metadata & Invalid States (10 scenarios)
"""
import copy
import hashlib
import json
from pathlib import Path
from typing import Any
import numpy as np
import pytest

from src.living_authenticity.database.lancedb_manager import (
    LanceDBManager,
    DatabaseOperationError,
    VaultMetadataMissingError,
    VaultMetadataCorruptionError,
)
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
    LanceDBVectorRetriever,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
from src.living_authenticity.knowledge.vault.core_validator import (
    validate_and_load_core,
    compute_core_record_id,
)
from src.living_authenticity.knowledge.vault.context import snapshot_vault
from src.living_authenticity.knowledge.vault.sync import (
    VaultSyncSession,
    VaultSyncError,
    VaultSyncUnauthorizedError,
    SYNC_STATE_FRESH,
    SYNC_STATE_STALE,
)


class DummyVectorEmbedder:
    dimension = 1024

    def embed(self, text: str) -> list[float]:
        vec = np.zeros(self.dimension, dtype=np.float32)
        h = abs(hash(text)) % self.dimension
        vec[h] = 1.0
        return vec.tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _setup_vault_and_index(
    tmp_path: Path, note_content: str | None = None
) -> tuple[Path, LanceDBManager, VaultSyncSession, EvidenceFirstPipeline, DummyVectorEmbedder, Path, Path]:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    core_dir = vault / "01 Core"
    core_dir.mkdir(parents=True, exist_ok=True)
    core_file = core_dir / "01-core-anchor.md"
    core_file.write_text(
        "---\n"
        "canonical_type: Core\n"
        "draft: false\n"
        "title: \"Authoritative Core\"\n"
        "tags: [core, research]\n"
        "---\n\n"
        "## Core Thesis\n"
        "Authentic living begins with disciplined awareness and self-clarity.\n\n"
        "## Guiding Tenet\n"
        "Direct observation takes precedence over ungrounded speculation.\n",
        encoding="utf-8",
    )

    if note_content is None:
        # Multi-chunk ordinary note producing 5 chunks
        note_content = (
            "First observation regarding morning reflection.\n\n"
            "Second observation on disciplined inquiry.\n\n"
            "Third observation on analytical consistency.\n\n"
            "Fourth observation regarding structural stability.\n\n"
            "Fifth observation regarding authentic living practice.\n"
        )
    obs_file = vault / "observation.md"
    obs_file.write_text(note_content, encoding="utf-8")

    pipeline = EvidenceFirstPipeline()
    embedder = DummyVectorEmbedder()
    vdb_dir = tmp_path / "lancedb"
    store = LanceDBManager(str(vdb_dir))
    store.create_knowledge_vector_table()

    session = VaultSyncSession()
    auth_rep = session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_dir),
        embedder=embedder,
        pipeline=pipeline,
    )
    assert auth_rep["authorized"] is True
    reconcile_rep = session.reconcile()
    assert reconcile_rep["fresh"] is True
    assert session.is_fresh is True
    assert session._store is not None

    return vault, session._store, session, pipeline, embedder, obs_file, core_file


# ==============================================================================
# ISSUE A: 17 Regression Test Scenarios
# ==============================================================================

def test_ordinary_note_all_expected_chunk_ids_exist(tmp_path):
    """1. All expected ordinary-note Chunk IDs and positions exist -> Baseline consistency passes."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is True
    assert reason == ""


def test_ordinary_note_one_chunk_missing_detected(tmp_path):
    """2. One Chunk is missing while other Chunks from the same note remain (5 expected vs 4 existing)."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    # Ingest to get expected knowledge units
    ing = pipeline.ingestion.ingest(str(obs_file))
    units = [u for u in ing.knowledge_units if (u.cleaned_text or u.original_text).strip()]
    assert len(units) == 5, f"Expected 5 units, got {len(units)}"

    # Delete fifth chunk (position 4) from LanceDB
    tbl = store.get_table()
    norm_src = str(obs_file.resolve()).replace("'", "''")
    tbl.delete(f"source = '{norm_src}' AND position = 4")

    # Rows now have 4 ordinary records instead of 5
    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "has 5 expected chunk record(s), but found 4" in reason or "missing chunk position" in reason or "missing 1 record" in reason


def test_ordinary_note_multiple_chunks_missing_detected(tmp_path):
    """3. Multiple Chunks missing from one Note."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    tbl = store.get_table()
    norm_src = str(obs_file.resolve()).replace("'", "''")
    # Delete positions 2 and 3
    tbl.delete(f"source = '{norm_src}' AND position IN (2, 3)")

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "missing" in reason.lower()


def test_ordinary_note_all_chunks_missing_detected(tmp_path):
    """4. All Chunks from one Note missing."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    store.delete_source(str(obs_file.resolve()))

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "has 5 expected chunk record(s), but found 0" in reason or "missing vector records" in reason


def test_ordinary_note_unexpected_vector_record_detected(tmp_path):
    """5. An unexpected vector record exists (unmanifested source or extra record)."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    # Insert an unexpected row for an unmanifested source
    store.store(
        text="Ghost record not in vault",
        embedding=embedder.embed("Ghost record not in vault"),
        metadata={"source": str(vault / "ghost_unmanifested.md"), "position": 0, "is_core": False},
    )

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "unexpected vector record" in reason or "unmanifested source" in reason


def test_ordinary_note_aggregate_count_matches_but_identities_differ(tmp_path):
    """6. Aggregate vector counts match, but record identities/positions do not."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    # Delete chunk position 4, and insert a bogus chunk position 99 for the same source
    tbl = store.get_table()
    norm_src = str(obs_file.resolve()).replace("'", "''")
    tbl.delete(f"source = '{norm_src}' AND position = 4")

    store.store(
        text="Bogus position chunk",
        embedding=embedder.embed("Bogus position chunk"),
        metadata={"source": str(obs_file.resolve()), "position": 99, "is_core": False},
    )

    # Total row count is still 5, but position 4 is missing and position 99 is unexpected
    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "missing chunk position" in reason or "4" in reason


def test_ordinary_note_duplicate_records_cannot_conceal_missing_chunks(tmp_path):
    """7. Duplicate records cannot conceal missing expected identities (e.g. pos 0 duplicate, pos 1 missing)."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    tbl = store.get_table()
    norm_src = str(obs_file.resolve()).replace("'", "''")
    # Delete position 4
    tbl.delete(f"source = '{norm_src}' AND position = 4")
    # Duplicate position 0
    store.store(
        text="Duplicate chunk 0",
        embedding=embedder.embed("Duplicate chunk 0"),
        metadata={"source": str(obs_file.resolve()), "position": 0, "is_core": False},
    )

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "duplicate chunk position" in reason or "missing chunk position" in reason


def test_ordinary_note_misattributed_source_identity_detected(tmp_path):
    """8. A vector record is associated with an incorrect source identity."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    # Delete 1 record from obs_file and add 1 record with another source
    tbl = store.get_table()
    norm_src = str(obs_file.resolve()).replace("'", "''")
    tbl.delete(f"source = '{norm_src}' AND position = 4")
    store.store(
        text="Wrong source chunk",
        embedding=embedder.embed("Wrong source chunk"),
        metadata={"source": str(vault / "another_file.md"), "position": 0, "is_core": False},
    )

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "unexpected vector record" in reason or "has 5 expected chunk record(s)" in reason


def test_ordinary_note_core_record_cannot_be_counted_as_ordinary(tmp_path):
    """9. A Core record cannot be counted as an ordinary-note record."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    # Infiltrate a row belonging to Core but marked is_core=False
    norm_core = str(core_file.resolve())
    store.store(
        text="Core note disguised as ordinary",
        embedding=embedder.embed("Core note disguised as ordinary"),
        metadata={"source": norm_core, "position": 99, "is_core": False},
    )

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "Core vector record" in reason and "misclassified as ordinary" in reason


def test_ordinary_note_record_cannot_impersonate_core(tmp_path):
    """10. An ordinary-note record cannot impersonate Core (is_core=True for ordinary note)."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    # Manually insert row into raw table to bypass LanceDBManager.store protection
    raw_tbl = store.get_table()
    raw_tbl.add([{
        "id": "forged_ordinary_core",
        "text": "Impersonator text",
        "source": str(obs_file.resolve()),
        "position": 99,
        "is_core": True,
        "core_hash": "active_core_hash",
        "embedding": embedder.embed("Impersonator text"),
    }])

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "impersonating Core" in reason or "lacking deterministic 'core_vec_' prefix" in reason


def test_ordinary_note_read_permission_error_fails_closed(tmp_path, monkeypatch):
    """11. A Note read fails due to a filesystem permission error -> Explicit failure state, never fresh."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    real_read_bytes = Path.read_bytes
    def mock_read_bytes(path_self):
        if "observation.md" in str(path_self):
            raise PermissionError("EACCES: permission denied accessing observation.md")
        return real_read_bytes(path_self)

    monkeypatch.setattr(Path, "read_bytes", mock_read_bytes)

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "filesystem error" in reason or "permission denied" in reason


def test_ordinary_note_read_io_error_fails_closed(tmp_path, monkeypatch):
    """12. A Note read fails due to an unexpected I/O error -> Explicit failure state, never fresh."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    real_read_bytes = Path.read_bytes
    def mock_read_bytes(path_self):
        if "observation.md" in str(path_self):
            raise OSError("EIO: hardware disk read error")
        return real_read_bytes(path_self)

    monkeypatch.setattr(Path, "read_bytes", mock_read_bytes)

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "filesystem error" in reason or "EIO" in reason


def test_ordinary_note_source_file_changed_during_verification(tmp_path):
    """13. A source file changes during integrity verification (sha256 mismatch with manifest)."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    # Modify observation.md on disk after index was written
    obs_file.write_text("# Modified Content\nDrifted without synchronization.\n", encoding="utf-8")

    meta = store.get_metadata()
    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "sha256 mismatch" in reason or "content modified" in reason


def test_ordinary_note_manifest_information_insufficient(tmp_path):
    """14. Manifest information is insufficient (missing or malformed entries)."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)

    meta = dict(store.get_metadata())
    # Malform manifest entries
    meta["manifest"] = {"vault_root": str(vault)}  # Missing "entries" key

    consistent, reason = session._check_index_baseline_consistency(meta, val_core)
    assert consistent is False
    assert "manifest is missing or malformed" in reason


def test_case_a_rejects_incomplete_ordinary_note_index(tmp_path):
    """15. Case A rejects incomplete ordinary-note indexes (does not perform recovery or declare Fresh)."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)

    # Delete 1 ordinary chunk from LanceDB (pos 4)
    tbl = store.get_table()
    norm_src = str(obs_file.resolve()).replace("'", "''")
    tbl.delete(f"source = '{norm_src}' AND position = 4")

    # Also drop 1 Core record so Case A would normally be considered
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)
    core_unit = val_core.units[0]
    rec_id = compute_core_record_id(core_unit, val_core)
    tbl.delete(f"id = '{rec_id}'")

    # Reconcile without ticket must reject
    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        session.reconcile(ticket=None)

    assert any(m in str(exc_info.value).lower() for m in ("index inconsistency detected", "missing core vector records"))
    assert session.is_fresh is False


def test_case_a_succeeds_when_all_expected_chunk_conditions_met(tmp_path):
    """16. Case A succeeds when all expected ordinary-note and Core integrity conditions are met."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)

    # All ordinary chunks (0..4) are intact.
    # Drop ONLY 1 Core vector record
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipeline)
    core_unit = val_core.units[0]
    rec_id = compute_core_record_id(core_unit, val_core)
    tbl = store.get_table()
    tbl.delete(f"id = '{rec_id}'")

    # Reconcile without ticket must fail closed: operator approval ticket required
    with pytest.raises(VaultSyncUnauthorizedError, match="Missing Core vector records detected in store; explicit operator approval ticket is required"):
        session.reconcile(ticket=None)

    req = session.request_approval(run_id="run_case_a_test16")
    session.resolve_approval(req["approval_id"], "approve")
    rep = session.reconcile(ticket=session._active_ticket)
    assert rep["fresh"] is True
    assert rep.get("case_a_recovery") is True
    assert rep.get("recovered_vectors") == 1
    assert session.is_fresh is True

    # Re-verify that Core record is restored and all ordinary chunks remain intact
    post_meta = store.get_metadata()
    consistent, _ = session._check_index_baseline_consistency(post_meta, val_core)
    assert consistent is True


def test_retrieval_remains_blocked_when_index_integrity_not_established(tmp_path):
    """17. Retrieval remains blocked when index integrity is not established."""
    vault, store, session, pipeline, embedder, obs_file, core_file = _setup_vault_and_index(tmp_path)

    # Corrupt metadata status to stale/failed
    meta = dict(store.get_metadata())
    meta["status"] = "failed"
    store.save_metadata(meta)

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=store)
    q = KnowledgeUnit(id="q", source="inp", original_text="morning reflection", meaning="morning reflection")

    with pytest.raises(DatabaseOperationError) as exc_info:
        retriever.retrieve(q)

    assert "incomplete or uncommitted vector store" in str(exc_info.value)


# ==============================================================================
# ISSUE B: 10 Regression Test Scenarios
# ==============================================================================

def test_retrieval_rejects_missing_metadata(tmp_path):
    """1. Retrieval rejects missing_metadata state with VaultMetadataMissingError."""
    db_dir = tmp_path / "vec_missing_meta"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    # Store a row, but do NOT save index_metadata
    manager.store("Some note", embedder.embed("Some note"), {"source": "note.md", "position": 0})
    assert manager.inspect_store_state()["state"] == "missing_metadata"

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=manager)
    q = KnowledgeUnit(id="q", source="inp", original_text="query", meaning="query")

    with pytest.raises(VaultMetadataMissingError) as exc_info:
        retriever.retrieve(q)
    assert "missing metadata" in str(exc_info.value)


def test_retrieval_rejects_corrupted_metadata(tmp_path):
    """2. Retrieval rejects corrupted_metadata state with VaultMetadataCorruptionError."""
    db_dir = tmp_path / "vec_corrupt_meta"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    # Store a row
    manager.store("Some note", embedder.embed("Some note"), {"source": "note.md", "position": 0})
    # Save corrupted metadata
    import pyarrow as pa
    meta_tbl = manager.db.create_table(
        "index_metadata",
        schema=pa.schema([pa.field("id", pa.string()), pa.field("payload", pa.string())]),
        exist_ok=True,
    )
    meta_tbl.add([{"id": "meta", "payload": "{invalid_json: true"}])

    assert manager.inspect_store_state()["state"] == "corrupted_metadata"

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=manager)
    q = KnowledgeUnit(id="q", source="inp", original_text="query", meaning="query")

    with pytest.raises(VaultMetadataCorruptionError) as exc_info:
        retriever.retrieve(q)
    assert "corrupted" in str(exc_info.value).lower()


def test_retrieval_rejects_error_state(tmp_path, monkeypatch):
    """3. Retrieval rejects error state with DatabaseOperationError."""
    db_dir = tmp_path / "vec_error_state"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    # Mock inspect_store_state to return state='error'
    monkeypatch.setattr(manager, "inspect_store_state", lambda: {"state": "error", "error": "Disk I/O failure"})

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=manager)
    q = KnowledgeUnit(id="q", source="inp", original_text="query", meaning="query")

    with pytest.raises(DatabaseOperationError) as exc_info:
        retriever.retrieve(q)
    assert "failed vector store" in str(exc_info.value)


def test_retrieval_rejects_unrecognized_invalid_state(tmp_path, monkeypatch):
    """4. Retrieval rejects an unrecognized or invalid store state."""
    db_dir = tmp_path / "vec_unrecognized_state"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    # Mock inspect_store_state to return arbitrary unrecognized state
    monkeypatch.setattr(manager, "inspect_store_state", lambda: {"state": "partially_migrated_unknown"})

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=manager)
    q = KnowledgeUnit(id="q", source="inp", original_text="query", meaning="query")

    with pytest.raises(DatabaseOperationError) as exc_info:
        retriever.retrieve(q)
    assert "unverified or invalid vector store" in str(exc_info.value)


def test_retrieval_does_not_execute_vector_search_after_metadata_validation_failure(tmp_path, monkeypatch):
    """5. Retrieval does not execute the vector search after metadata validation fails."""
    db_dir = tmp_path / "vec_no_search_exec"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    manager.store("Note", embedder.embed("Note"), {"source": "note.md", "position": 0})
    # Ensure missing_metadata
    assert manager.inspect_store_state()["state"] == "missing_metadata"

    search_called = []
    real_open = manager.db.open_table
    def mock_open_table(name):
        tbl = real_open(name)
        if name == "knowledge_vectors":
            real_search = tbl.search
            def mock_search(*args, **kwargs):
                search_called.append(True)
                return real_search(*args, **kwargs)
            monkeypatch.setattr(tbl, "search", mock_search)
        return tbl

    monkeypatch.setattr(manager.db, "open_table", mock_open_table)

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=manager)
    q = KnowledgeUnit(id="q", source="inp", original_text="query", meaning="query")

    with pytest.raises(VaultMetadataMissingError):
        retriever.retrieve(q)

    assert len(search_called) == 0, "Vector search MUST NOT be executed when metadata validation fails!"


def test_retrieval_does_not_return_empty_result_after_metadata_validation_failure(tmp_path):
    """6. Retrieval does not return an apparently successful empty result after metadata validation fails."""
    db_dir = tmp_path / "vec_no_empty_result"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    manager.store("Note", embedder.embed("Note"), {"source": "note.md", "position": 0})

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=manager)
    q = KnowledgeUnit(id="q", source="inp", original_text="query", meaning="query")

    # Must raise, NEVER return RetrievalResult(candidates=())
    with pytest.raises(VaultMetadataMissingError):
        res = retriever.retrieve(q)


def test_genuine_empty_store_initialization_possible(tmp_path):
    """7. Genuine empty-store initialization remains possible through the authorized initialization path."""
    db_dir = tmp_path / "vec_empty_init"
    manager = LanceDBManager(str(db_dir))
    # No tables created yet
    state_info = manager.inspect_store_state()
    assert state_info["state"] == "genuine_first_init"
    assert state_info["has_vectors_table"] is False

    # Create table with 0 rows
    manager.create_knowledge_vector_table()
    state_info2 = manager.inspect_store_state()
    assert state_info2["state"] == "genuine_first_init"
    assert state_info2["has_vectors_table"] is True
    assert state_info2["vector_count"] == 0


def test_non_empty_store_with_missing_metadata_cannot_be_treated_as_first_init(tmp_path):
    """8. A non-empty store with missing metadata cannot be treated as genuine first initialization."""
    db_dir = tmp_path / "vec_non_empty_no_meta"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    manager.store("Note 1", embedder.embed("Note 1"), {"source": "note1.md", "position": 0})
    state_info = manager.inspect_store_state()

    assert state_info["state"] == "missing_metadata"
    assert state_info["state"] != "genuine_first_init"
    assert state_info["state"] != "empty"


def test_valid_initialized_store_remains_retrievable(tmp_path):
    """9. A valid initialized store remains retrievable."""
    db_dir = tmp_path / "vec_valid_retrievable"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    manager.save_metadata({"status": "fresh", "indexed_vectors": 1})
    embedder = DummyVectorEmbedder()

    doc = "Disciplined reflection and inquiry."
    manager.store(doc, embedder.embed(doc), {"source": "valid.md", "position": 0})

    assert manager.inspect_store_state()["state"] == "valid"

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=manager)
    q = KnowledgeUnit(id="q", source="inp", original_text="inquiry", meaning="inquiry")
    res = retriever.retrieve(q)

    assert len(res.candidates) == 1
    assert res.candidates[0].source == "valid.md"


def test_valid_store_becoming_corrupted_rejected_subsequently(tmp_path):
    """10. A valid store that becomes corrupted after initialization is rejected on subsequent Retrieval."""
    db_dir = tmp_path / "vec_subsequent_corrupt"
    manager = LanceDBManager(str(db_dir))
    manager.create_knowledge_vector_table()
    manager.save_metadata({"status": "fresh", "indexed_vectors": 1})
    embedder = DummyVectorEmbedder()

    doc = "Disciplined reflection."
    manager.store(doc, embedder.embed(doc), {"source": "valid.md", "position": 0})

    retriever = LanceDBVectorRetriever(embedding_service=embedder, db_manager=manager)
    q = KnowledgeUnit(id="q", source="inp", original_text="reflection", meaning="reflection")

    # First retrieval succeeds
    res1 = retriever.retrieve(q)
    assert len(res1.candidates) == 1

    # Now corrupt metadata
    manager.save_metadata({"status": "corrupted_incomplete"})
    # Overwrite payload with broken JSON in raw table
    raw_meta = manager.db.open_table("index_metadata")
    raw_meta.delete("id IS NOT NULL")
    raw_meta.add([{"id": "meta", "payload": "{corrupted json"}])

    # Second retrieval is rejected fail-closed
    with pytest.raises(VaultMetadataCorruptionError):
        retriever.retrieve(q)
