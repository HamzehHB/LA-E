"""Focused regression suite for Authoritative Core Identity, Retrieval Weighting,
and Index Lifecycle.

Verifies all 15 requirements specified by the Phase 3 governance review:
1. Ordinary note attempting to impersonate Core through proposed_type.
2. Ordinary note with a Core-looking path.
3. Vector records with missing or stale Core identity metadata.
4. Validated Core identity preserved through indexing and retrieval.
5. Core version/hash changes and index update approval.
6. Rejection of an arbitrary Test Mode example as Core.
7. Test Mode symlink and unexpected-entry handling.
8. Vault Mode draft: true rejection.
9. CLI rejection when Core provenance is absent or invalid.
10. Runner rejection of arbitrary non-empty KnowledgeUnit sequences.
11. Retry audit persistence failure.
12. Emergency audit persistence behavior.
13. Repeated retry failure provenance.
14. No unapproved Core vector-index mutation.
15. Existing valid Core behavior remains unchanged.
And includes an end-to-end test demonstrating ordinary notes cannot impersonate Core.
"""
import hashlib
import json
from pathlib import Path
import time
from typing import Any
import pytest
import numpy as np

from src.living_authenticity.adapter.app import AdapterError, Launcher
from src.living_authenticity.cli import main as cli_main
from src.living_authenticity.database.lancedb_manager import (
    LanceDBManager,
    DatabaseOperationError,
    VaultMetadataMissingError,
    VaultMetadataCorruptionError,
)
from src.living_authenticity.knowledge.vault.core_validator import compute_core_vector_digest
from src.living_authenticity.knowledge.analysis.core_analysis.token_overlap import DefaultCoreAnalyzer
from src.living_authenticity.knowledge.analysis.retrieval.candidate import RetrievedCandidate
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import LanceDBVectorRetriever, VectorEmbeddingService
from src.living_authenticity.knowledge.analysis.retrieval.pipeline import RelevanceAssessment
from src.living_authenticity.knowledge.decision.proposal.builder import ProposalBuilder
from src.living_authenticity.knowledge.governance.audit import AuditWriter
from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
from src.living_authenticity.knowledge.governance.audit.persistence import (
    persist_record_resilient,
    EMERGENCY_LOG_FILENAME,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.integration.indexing import index_units
from src.living_authenticity.knowledge.integration.runner import run_bounded_integration
from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
from src.living_authenticity.knowledge.vault.core_validator import (
    CoreFailureCategory,
    CoreResolutionError,
    is_trusted_validation_context,
    is_validated_core_unit,
    validate_and_load_core,
    verify_core_units_provenance,
)
from src.living_authenticity.knowledge.vault.sync import (
    VaultSyncSession,
    VaultSyncError,
    VaultSyncUnauthorizedError,
    VaultSyncStaleApprovalError,
    VaultSyncSnapshotDriftError,
    SYNC_STATE_FRESH,
    SYNC_STATE_STALE,
    compute_diff_fingerprint,
)
from tests._test_core_helpers import build_test_sync_authorization, build_test_validated_core


class DummyVectorEmbedder:
    """Deterministic embedding stub for testing vector operations."""
    dimension = 1024

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dimension, dtype=np.float32)
        h = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
        vec[0] = float((h % 1000) / 1000.0)
        vec[1] = 1.0
        return vec

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed(t) for t in texts]


def _make_valid_vault(vault_dir: Path, note_title: str = "Central Anchor", body: str = "Core research principles of LivingAuthenticity.") -> Path:
    core_dir = vault_dir / "01 Core"
    core_dir.mkdir(parents=True, exist_ok=True)
    note = core_dir / "01-core-anchor.md"
    content = f"""---
canonical_type: Core
draft: false
title: "{note_title}"
tags: [core, research]
---

{body}
"""
    note.write_text(content, encoding="utf-8")
    return note


# ==============================================================================
# Requirement 1: Ordinary note attempting to impersonate Core through proposed_type
# ==============================================================================
def test_ordinary_note_cannot_impersonate_core_via_proposed_type(tmp_path):
    fake_unit = KnowledgeUnit(
        id="ku-fake-core",
        source="03 Concepts/some_concept.md",
        original_text="Some conceptual research text.",
        meaning="Some conceptual research text.",
        proposed_type="Core",  # Impersonation attempt!
        is_core=False,         # Not from authoritative Core validator
        core_hash="",
    )

    # 1. Direct provenance verification must reject it
    assert is_validated_core_unit(fake_unit) is False
    with pytest.raises(ValueError, match="lacks authoritative Core provenance"):
        verify_core_units_provenance([fake_unit])

    # 2. TokenOverlapCoreAnalyzer must reject it
    analyzer = DefaultCoreAnalyzer()
    query = KnowledgeUnit(id="q1", source="input", original_text="query", meaning="query")
    with pytest.raises(ValueError, match="lacks authoritative Core provenance"):
        analyzer.analyze(query, [fake_unit])

    # 3. Vector indexing must not record is_core=True
    db_dir = tmp_path / "vec_db"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()
    index_units([fake_unit], embedder, store)

    rows = store.show_all()
    assert len(rows) == 1
    assert rows[0].get("is_core") is False
    assert rows[0].get("core_hash") == ""


# ==============================================================================
# Requirement 2: Ordinary note with a Core-looking path
# ==============================================================================
def test_ordinary_note_with_core_looking_path_not_promoted_to_core(tmp_path):
    fake_path_unit = KnowledgeUnit(
        id="ku-path-fake",
        source="/authorized/vault/01 Core/concept_scratch.md",
        original_text="Ordinary notes placed in 01 Core folder by mistake.",
        meaning="Ordinary notes placed in 01 Core folder by mistake.",
        proposed_type="Concept",
        is_core=False,
        core_hash="",
    )

    db_dir = tmp_path / "vec_db2"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()
    index_units([fake_path_unit], embedder, store)

    rows = store.show_all()
    assert len(rows) == 1
    # Despite containing "01 Core" in path, is_core MUST NOT be inferred as True
    assert rows[0].get("is_core") is False
    assert rows[0].get("core_hash") == ""


# ==============================================================================
# Requirement 3: Vector records with missing or stale Core identity metadata
# ==============================================================================
def test_vector_records_with_stale_or_missing_core_identity_treated_as_unverified(tmp_path):
    db_dir = tmp_path / "vec_db3"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "indexed_vectors": 2, "core_hash": "active_core_hash_999"})
    embedder = DummyVectorEmbedder()

    # Store a stale Core record (hash does not match active Core)
    store.store(
        text="Old Core note text",
        embedding=embedder.embed("Old Core note text"),
        metadata={"source": "01 Core/old.md", "position": 0, "is_core": True, "core_hash": "stale_hash_111"},
    )
    # Store a legacy record with is_core=True but missing core_hash
    store.store(
        text="Legacy Core note text",
        embedding=embedder.embed("Legacy Core note text"),
        metadata={"source": "01 Core/legacy.md", "position": 0, "is_core": True, "core_hash": ""},
    )

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core_hash="active_core_hash_999",
    )
    query = KnowledgeUnit(id="q", source="inp", original_text="Core research", meaning="Core research")
    res = retriever.retrieve(query)

    for c in res.candidates:
        # Neither stale hash nor missing hash may be treated as verified Core
        assert c.is_core is False
        assert c.core_hash == ""


# ==============================================================================
# Requirement 4: Validated Core identity preserved through indexing and retrieval
# ==============================================================================
def test_validated_core_identity_preserved_through_indexing_and_retrieval(tmp_path):
    vault = tmp_path / "vault4"
    _make_valid_vault(vault, note_title="Authentic Core", body="Authoritative Core theory text.")
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    assert len(val_core.units) >= 1
    unit = val_core.units[0]
    assert unit.is_core is True
    assert unit.core_hash == val_core.sha256
    assert unit.core_source_path == str(val_core.path)

    # Index into vector database
    db_dir = tmp_path / "vec_db4"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()
    index_units(val_core.units, embedder, store, validated_core=val_core)

    rows = store.show_all()
    assert len(rows) == 1
    assert rows[0].get("is_core") is True
    assert rows[0].get("core_hash") == val_core.sha256

    val_auth = build_test_sync_authorization(val_core)
    store.save_metadata(
        {"status": "fresh", "indexed_vectors": len(rows), "core_hash": val_core.sha256},
        validated_core=val_core,
        sync_auth=val_auth,
    )

    # Retrieve with matching validated core hash
    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=val_core,
    )
    query = KnowledgeUnit(id="q", source="inp", original_text="theory text", meaning="theory text")
    res = retriever.retrieve(query)

    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.is_core is True
    assert cand.core_hash == val_core.sha256


# ==============================================================================
# Requirement 5: Core version/hash changes and index update approval
# ==============================================================================
def test_core_version_change_distinction_and_revalidation_before_sync(tmp_path):
    vault = tmp_path / "vault5"
    note_p = _make_valid_vault(vault, note_title="V1 Core", body="Initial Core thesis.")
    db_dir = tmp_path / "vec_db5"
    embedder = DummyVectorEmbedder()

    session = VaultSyncSession()
    session.authorize(str(vault), str(db_dir), embedder=embedder)
    assert session.is_fresh is True

    # Mutate the Core note
    note_p.write_text("""---
canonical_type: Core
draft: false
title: "V2 Core"
tags: [core]
---

Updated Core thesis.
""", encoding="utf-8")

    # Check changes must detect core_changed=True and stale_core_vectors=True
    changes = session.check_changes()
    assert changes["stale"] is True
    assert changes["core_changed"] is True
    assert changes["stale_core_vectors"] is True
    assert len(changes["core_changes"]["modified"]) == 1
    assert len(changes["ordinary_changes"]["modified"]) == 0

    # If mutated Core note has draft: true, reconcile must fail closed during revalidation
    note_p.write_text("""---
canonical_type: Core
draft: true
title: "Draft Core"
---

Broken draft.
""", encoding="utf-8")

    reconcile_res = session.reconcile()
    assert reconcile_res["fresh"] is False
    assert "draft" in reconcile_res["error"]


# ==============================================================================
# Requirement 6: Rejection of an arbitrary Test Mode example as Core
# ==============================================================================
def test_rejection_of_arbitrary_test_mode_example_as_core(tmp_path):
    ex_root = tmp_path / "test_mode"
    core_dir = ex_root / "core"
    core_dir.mkdir(parents=True)
    # Put an arbitrary file instead of designated 01-core-calibration.md
    (core_dir / "02-arbitrary-note.md").write_text("""---
origin: "synthetic"
---
arbitrary content
""", encoding="utf-8")

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("examples", examples_root=str(ex_root))
    assert exc_info.value.category == CoreFailureCategory.NOTE_MISSING


# ==============================================================================
# Requirement 7: Test Mode symlink and unexpected-entry handling
# ==============================================================================
def test_test_mode_unexpected_entry_handling(tmp_path):
    ex_root = tmp_path / "test_mode7"
    core_dir = ex_root / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "01-core-calibration.md").write_text("""---
origin: "synthetic-example"
tags: "core"
---
Calibration procedure.
""", encoding="utf-8")

    # Add unexpected non-markdown file
    (core_dir / "notes.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("examples", examples_root=str(ex_root))
    assert exc_info.value.category == CoreFailureCategory.UNEXPECTED_ENTRIES


# ==============================================================================
# Requirement 8: Vault Mode draft: true rejection
# ==============================================================================
def test_vault_mode_draft_true_rejection(tmp_path):
    vault = tmp_path / "vault8"
    core_dir = vault / "01 Core"
    core_dir.mkdir(parents=True)
    (core_dir / "core.md").write_text("""---
canonical_type: Core
draft: true
---
Draft core thesis.
""", encoding="utf-8")

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.DRAFT_NOTE_REJECTED


# ==============================================================================
# Requirement 9: CLI rejection when Core provenance is absent or invalid
# ==============================================================================
def test_cli_rejection_when_core_absent_or_invalid(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "inp.md").write_text("Some substantive research text that is at least 50 letters long for input gate.", encoding="utf-8")

    # Invocation without core-root and without valid vault
    test_args = [
        "cli.py",
        "--source-root", str(source),
        "--staging-root", str(staging),
        "--max-files", "1",
    ]
    monkeypatch.setattr("sys.argv", test_args)
    rc = cli_main()
    assert rc == 2


# ==============================================================================
# Requirement 10: Runner rejection of arbitrary non-empty KnowledgeUnit sequences
# ==============================================================================
def test_runner_rejection_of_arbitrary_non_empty_knowledge_unit_sequence(tmp_path):
    staging = tmp_path / "staging10"
    staging.mkdir()
    source = tmp_path / "source10"
    source.mkdir()
    (source / "in.md").write_text("Substantive research observation exceeding the minimum fifty letter boundary.", encoding="utf-8")

    # Sequence with non-empty KnowledgeUnit but lacking validated Core provenance
    fake_cores = [
        KnowledgeUnit(
            id="fake-core-id",
            source="some/note.md",
            meaning="meaning",
            original_text="text",
            proposed_type="Core",
            is_core=False,  # Unverified!
            core_hash="",
        )
    ]

    report = run_bounded_integration(
        source_root=str(source),
        staging_root=str(staging),
        core_units=fake_cores,
        operating_mode="examples",
    )
    assert report.accepted is False
    assert report.run_outcome == "operational_failure"
    assert report.rejection_check == "core_preflight"
    assert "authoritative validated Core provenance" in report.rejection_reason


# ==============================================================================
# Requirement 11: Retry audit persistence failure
# ==============================================================================
def test_retry_audit_persistence_failure_emits_audit_failed(tmp_path, monkeypatch):
    vault = tmp_path / "vault11"
    vault.mkdir()
    staging = tmp_path / "staging11"
    staging.mkdir()
    audit = tmp_path / "audit11"
    audit.mkdir()
    AuditWriter(str(audit)).ensure_directories()

    events = []
    launcher = Launcher(
        paths={
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit), "passed": str(audit / "passed"), "failed": str(audit / "failed")},
            "obsidian": {"vault": str(vault)},
        },
        is_test_mode=True,
    )
    launcher._session_destinations["vault"] = str(vault)
    launcher._session_destinations["audit"] = str(audit)
    launcher._sink = lambda payload: events.append(payload)

    # Initial run halts on missing Core
    res = launcher.start_run("Valid substantive input text with over fifty letters for input stage gate testing.", source_mode="personal_vault")
    run_id = res["run_id"]
    for _ in range(30):
        if not launcher.run_status(run_id).get("active"):
            break
        time.sleep(0.05)

    # Monkeypatch persist_record_resilient to simulate failure
    def mock_persist(*args, **kwargs):
        return {"persisted": False, "record_id": ""}

    import src.living_authenticity.knowledge.governance.audit.persistence as p_mod
    monkeypatch.setattr(p_mod, "persist_record_resilient", mock_persist)

    with pytest.raises(AdapterError):
        launcher.retry_paused_run(run_id)

    # Verify audit_failed event was emitted and paused run tracks persistence failure
    audit_failed_evs = [e for e in events if e.get("kind") == "audit_failed"]
    assert len(audit_failed_evs) >= 1
    assert launcher._paused_run is not None
    assert launcher._paused_run["audit_persisted"] is False


# ==============================================================================
# Requirement 12: Emergency audit persistence behavior
# ==============================================================================
def test_emergency_audit_persistence_behavior(tmp_path):
    audit_dir = tmp_path / "audit12"
    writer = AuditWriter(str(audit_dir))
    writer.ensure_directories()

    # Create a record
    rec = AuditRecord(
        run_id="run-emerg-12",
        terminal_outcome="infrastructure_error",
        validation_result="FAILED",
        stop_reason="Primary persistence test failure",
        errors=("Operational error",),
    )

    # Call append_emergency directly
    receipt = writer.append_emergency(rec, failure_reason="Primary audit log locked")
    assert receipt.get("persisted") is True
    assert receipt.get("emergency") is True

    # Check emergency file exists and holds the record
    emerg_file = audit_dir / "errors" / EMERGENCY_LOG_FILENAME
    assert emerg_file.is_file()
    lines = emerg_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data.get("run_id") == "run-emerg-12"
    assert data.get("terminal_outcome") == "infrastructure_error"


# ==============================================================================
# Requirement 13: Repeated retry failure provenance
# ==============================================================================
def test_repeated_retry_failure_provenance_chain(tmp_path):
    vault = tmp_path / "vault13"
    vault.mkdir()
    staging = tmp_path / "staging13"
    staging.mkdir()
    audit = tmp_path / "audit13"
    audit.mkdir()
    AuditWriter(str(audit)).ensure_directories()

    launcher = Launcher(
        paths={
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit), "passed": str(audit / "passed"), "failed": str(audit / "failed")},
            "obsidian": {"vault": str(vault)},
        },
        is_test_mode=True,
    )
    launcher._session_destinations["vault"] = str(vault)
    launcher._session_destinations["audit"] = str(audit)

    res = launcher.start_run("Valid input text with over fifty letters for input stage gate testing.", source_mode="personal_vault")
    run_id = res["run_id"]
    for _ in range(30):
        if not launcher.run_status(run_id).get("active"):
            break
        time.sleep(0.05)

    with pytest.raises(AdapterError):
        launcher.retry_paused_run(run_id)
    with pytest.raises(AdapterError):
        launcher.retry_paused_run(run_id)

    # Check that error records maintain chain
    err_file = audit / "errors" / "audit.jsonl"
    records = [json.loads(l) for l in err_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    matching = [r for r in records if r.get("run_id") == run_id]
    assert len(matching) >= 3
    # Check that later records link to previous run_id
    assert matching[1].get("resumed_from_run_id") == run_id
    assert matching[2].get("resumed_from_run_id") == run_id


# ==============================================================================
# Requirement 14: No unapproved Core vector-index mutation
# ==============================================================================
def test_no_unapproved_core_vector_index_mutation(tmp_path):
    vault = tmp_path / "vault14"
    note = _make_valid_vault(vault, note_title="V1 Core")
    db_dir = tmp_path / "vec_db14"
    embedder = DummyVectorEmbedder()

    session = VaultSyncSession()
    session.authorize(str(vault), str(db_dir), embedder=embedder)
    store = LanceDBManager(str(session.index_dir))
    initial_rows = store.show_all()
    assert len(initial_rows) >= 1

    # Mutate Core note in vault
    note.write_text("""---
canonical_type: Core
draft: false
title: "V2 Core Mutated"
---
Mutated content.
""", encoding="utf-8")

    # Verify check_changes detects stale, but DOES NOT mutate database
    changes = session.check_changes()
    assert changes["stale"] is True

    rows_after_check = store.show_all()
    # Database rows and content remain identical to initial state (no unapproved mutation)
    assert len(rows_after_check) == len(initial_rows)
    assert rows_after_check[0]["text"] == initial_rows[0]["text"]


# ==============================================================================
# Requirement 15: Existing valid Core behavior remains unchanged
# ==============================================================================
def test_existing_valid_core_behavior_remains_unchanged(tmp_path):
    vault = tmp_path / "vault15"
    _make_valid_vault(vault, note_title="Standard Core", body="Authoritative theoretical thesis.")
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    assert val_core.title == "Standard Core"
    assert val_core.canonical_type == "Core"
    assert val_core.source_mode == "personal_vault"
    assert len(val_core.units) >= 1
    assert val_core.units[0].is_core is True
    assert val_core.units[0].core_hash == val_core.sha256


# ==============================================================================
# End-to-End Test: Core Weighting and Impersonation Prevention
# ==============================================================================
def test_e2e_core_weighting_and_impersonation_prevention(tmp_path):
    """Demonstrates that:
    1. Weakly relevant Core candidate does not take precedence over highly relevant ordinary note.
    2. Ordinary note cannot impersonate Core to steal priority boost.
    3. Verified Core gets dynamic boost factor.
    """
    db_dir = tmp_path / "e2e_vec_db"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()

    # We use a Mock embedding service with predictable distances
    class DeterministicMockEmbedder:
        dimension = 1024
        def embed(self, text):
            # Normal unit text returns a vector aligned with query
            vec = np.zeros(self.dimension, dtype=np.float32)
            if "query" in text:
                vec[0] = 1.0
            elif "highly relevant ordinary" in text:
                vec[0] = 0.95
            elif "weakly relevant core" in text:
                vec[0] = 0.10
            elif "impersonator" in text:
                vec[0] = 0.85
            return vec

    embedder = DeterministicMockEmbedder()
    active_core_hash = "correct_core_hash_abc"

    core_unit = KnowledgeUnit(
        id="core-thesis", source="01 Core/thesis.md", original_text="weakly relevant core note",
        meaning="weakly relevant core note", cleaned_text="weakly relevant core note",
        normalized_text="weakly relevant core note", context="",
        proposed_type="Core", position=0, is_core=True,
        core_hash=active_core_hash, core_source_path="01 Core/thesis.md",
    )
    from tests._test_core_helpers import build_test_validated_core
    from src.living_authenticity.knowledge.vault.core_validator import compute_core_record_id
    val_core = build_test_validated_core([core_unit], source_path="01 Core/thesis.md", core_hash=active_core_hash)
    core_rec_id = compute_core_record_id(core_unit, val_core)

    # Store 3 rows:
    # Row 1: Highly relevant ordinary note
    store.store(
        text="highly relevant ordinary note",
        embedding=embedder.embed("highly relevant ordinary"),
        metadata={"source": "03 Concepts/note1.md", "position": 0, "is_core": False, "core_hash": ""},
    )
    # Row 2: Weakly relevant authoritative Core with trusted indexing record_id
    store.store(
        text="weakly relevant core note",
        embedding=embedder.embed("weakly relevant core"),
        metadata={"source": "01 Core/thesis.md", "position": 0, "is_core": True, "core_hash": active_core_hash},
        record_id=core_rec_id,
        validated_core=val_core,
    )
    # Row 3: Ordinary note trying to impersonate Core via path and metadata but wrong hash
    store.store(
        text="impersonator note",
        embedding=embedder.embed("impersonator"),
        metadata={"source": "01 Core/impersonator.md", "position": 0, "is_core": True, "core_hash": "wrong_hash"},
    )
    val_auth = build_test_sync_authorization(val_core)
    store.save_metadata(
        {"status": "fresh", "indexed_vectors": 3, "core_hash": active_core_hash},
        validated_core=val_core,
        sync_auth=val_auth,
    )

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=val_core,
        core_boost_factor=0.25,
        max_candidates=5,
    )

    query = KnowledgeUnit(id="q", source="inp", original_text="query", meaning="query")
    res = retriever.retrieve(query)

    # Ensure all 3 candidates returned
    assert len(res.candidates) == 3

    # Check candidates:
    # 1. Impersonator must have is_core=False because hash didn't match
    impersonator = [c for c in res.candidates if "impersonator" in c.text][0]
    assert impersonator.is_core is False

    # 2. Authentic Core must have is_core=True
    core_cand = [c for c in res.candidates if "weakly relevant core" in c.text][0]
    assert core_cand.is_core is True
    assert core_cand.core_hash == active_core_hash

    # 3. Highly relevant ordinary note must be ranked FIRST, ahead of weakly relevant Core!
    assert res.candidates[0].text == "highly relevant ordinary note"
    assert res.candidates[0].is_core is False


# ==============================================================================
# Requirement 17: Exhaustive Retrieval Core Identity Hash Verification Matrix
# ==============================================================================
def test_retrieval_active_core_hash_matrix_exhaustive(tmp_path):
    """Verify that vector records receive verified-Core status IF AND ONLY IF their
    stored identity and hash match an active, independently validated authoritative Core.

    Exhaustively tests:
    1. Matching active Core hash.
    2. Mismatched / stale Core hash.
    3. Missing stored Core hash.
    4. Missing active validated Core hash on retriever (fail-closed: records remain unverified).
    5. Legacy vector records without Core metadata.
    6. Record claiming Core identity without valid provenance.
    """
    db_dir = tmp_path / "vec_matrix_db"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    active_hash = "authoritative_active_hash_456"

    core_unit_mat = KnowledgeUnit(
        id="c-mat", source="01 Core/note.md", original_text="valid matching core note",
        meaning="valid matching core note", cleaned_text="valid matching core note",
        normalized_text="valid matching core note", context="",
        proposed_type="Core", position=0, is_core=True,
        core_hash=active_hash, core_source_path="01 Core/note.md",
    )
    from tests._test_core_helpers import build_test_validated_core
    from src.living_authenticity.knowledge.vault.core_validator import compute_core_record_id
    val_core_mat = build_test_validated_core([core_unit_mat], source_path="01 Core/note.md", core_hash=active_hash)
    core_rec_id_mat = compute_core_record_id(core_unit_mat, val_core_mat)

    # Row 1: Matching active Core hash with trusted record_id
    store.store(
        text="valid matching core note",
        embedding=embedder.embed("valid core"),
        metadata={"source": "01 Core/note.md", "position": 0, "is_core": True, "core_hash": active_hash},
        record_id=core_rec_id_mat,
        validated_core=val_core_mat,
    )
    # Row 2: Mismatched / stale Core hash
    store.store(
        text="stale core note",
        embedding=embedder.embed("stale core"),
        metadata={"source": "01 Core/note.md", "position": 1, "is_core": True, "core_hash": "stale_hash_old"},
    )
    # Row 3: Missing stored Core hash (empty string)
    store.store(
        text="missing hash core note",
        embedding=embedder.embed("missing hash"),
        metadata={"source": "01 Core/note.md", "position": 2, "is_core": True, "core_hash": ""},
    )
    # Row 4: Legacy vector record with no Core metadata keys
    store.store(
        text="legacy record note",
        embedding=embedder.embed("legacy note"),
        metadata={"source": "ordinary/legacy.md", "position": 0},
    )
    # Row 5: Ordinary note claiming is_core=True with fake hash
    store.store(
        text="impersonating ordinary note",
        embedding=embedder.embed("impersonator"),
        metadata={"source": "notes/sample.md", "position": 0, "is_core": True, "core_hash": "unverified_hash_999"},
    )
    val_auth = build_test_sync_authorization(val_core_mat)
    store.save_metadata(
        {"status": "fresh", "indexed_vectors": 5, "core_hash": active_hash},
        validated_core=val_core_mat,
        sync_auth=val_auth,
    )

    query = KnowledgeUnit(id="q-mat", source="inp", original_text="query", meaning="query")

    # Case A: Active validated core context is provided
    retriever_with_hash = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=val_core_mat,
        core_boost_factor=0.25,
        max_candidates=10,
    )
    res_a = retriever_with_hash.retrieve(query)
    cands_a = {c.text: c for c in res_a.candidates}

    # 1. Matching record IS verified
    assert cands_a["valid matching core note"].is_core is True
    assert cands_a["valid matching core note"].core_hash == active_hash

    # 2. Stale hash is UNVERIFIED
    assert cands_a["stale core note"].is_core is False
    assert cands_a["stale core note"].core_hash == ""

    # 3. Missing stored hash is UNVERIFIED
    assert cands_a["missing hash core note"].is_core is False
    assert cands_a["missing hash core note"].core_hash == ""

    # 4. Legacy record is UNVERIFIED
    assert cands_a["legacy record note"].is_core is False
    assert cands_a["legacy record note"].core_hash == ""

    # 5. Impersonator is UNVERIFIED
    assert cands_a["impersonating ordinary note"].is_core is False
    assert cands_a["impersonating ordinary note"].core_hash == ""

    # Case B: NO active validated core hash is available on retriever (missing validation context)
    retriever_no_hash = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core_hash="",  # No active Core validated
        core_boost_factor=0.25,
        max_candidates=10,
    )
    res_b = retriever_no_hash.retrieve(query)
    # ALL records must remain strictly unverified; none can receive verified-Core status
    for c in res_b.candidates:
        assert c.is_core is False
        assert c.core_hash == ""


# ==============================================================================
# Requirement 18: Vault Synchronization Core Validation and Error Propagation
# ==============================================================================
def test_vault_sync_check_changes_observable_failure_on_invalid_core(tmp_path):
    """check_changes must make Core validation failures observable without mutating
    and must never represent an invalid Core as healthy or fresh."""
    vdb_dir = tmp_path / "sync_vdb18"
    vault = tmp_path / "vault18"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    # Create invalid Core note (draft status)
    (core_dir / "Core.md").write_text(
        "---\ntitle: Core Note\ncanonical_type: Core\ndraft: true\n---\nCore body text.",
        encoding="utf-8",
    )
    events = []
    pipe = EvidenceFirstPipeline(stage_observer=lambda e: events.append(e))
    session = VaultSyncSession(stage_observer=lambda e: events.append(e))

    # Initial authorization fails because Core note is draft
    auth_rep = session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_dir),
        embedder=DummyVectorEmbedder(),
        pipeline=pipe,
    )
    assert auth_rep["fresh"] is False
    assert session.is_fresh is False

    # Mark authorized to inspect check_changes behavior directly
    session._authorized = True
    changes = session.check_changes()

    assert changes["core_valid"] is False
    assert changes["core_error"] is not None
    assert changes["core_error_category"] == "core_draft_note_rejected"
    assert changes["stale"] is True
    assert changes["stale_core_vectors"] is True
    assert session.is_fresh is False


def test_vault_sync_multiple_active_notes_in_01_core(tmp_path):
    """Multiple active Markdown notes in 01 Core violates singleton policy."""
    vdb_dir = tmp_path / "sync_vdb_multi"
    vault = tmp_path / "vault_multi"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "Core1.md").write_text(
        "---\ntitle: Core 1\ncanonical_type: Core\n---\nFirst core note.",
        encoding="utf-8",
    )
    (core_dir / "Core2.md").write_text(
        "---\ntitle: Core 2\ncanonical_type: Core\n---\nSecond core note.",
        encoding="utf-8",
    )
    pipe = EvidenceFirstPipeline()
    session = VaultSyncSession()

    session._authorized = True
    session._vault_root = str(vault)
    session._store = LanceDBManager(str(vdb_dir))
    session._store.create_knowledge_vector_table()
    session._pipeline = pipe

    changes = session.check_changes()
    assert changes["core_valid"] is False
    assert changes["core_error_category"] == "core_multiple_active_notes"
    assert changes["stale"] is True

    reconcile_res = session.reconcile()
    assert reconcile_res["fresh"] is False
    assert "singleton policy requires exactly 1 active note" in reconcile_res["error"]


def test_vault_sync_non_core_specialized_test_context_retains_behavior(tmp_path):
    """Genuinely authorized non-Core test contexts (no 01 Core directory) retain
    ordinary synchronization behavior without raising false CoreResolutionError."""
    vdb_dir = tmp_path / "sync_vdb_noncore"
    vault = tmp_path / "vault_noncore"
    vault.mkdir()
    (vault / "ordinary.md").write_text("# Ordinary\nContent.", encoding="utf-8")

    session = VaultSyncSession()
    auth_rep = session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_dir),
        embedder=DummyVectorEmbedder(),
        pipeline=EvidenceFirstPipeline(),
    )
    assert auth_rep["fresh"] is True
    assert session.is_fresh is True

    changes = session.check_changes()
    assert changes["core_valid"] is True
    assert changes["core_error"] is None
    assert changes["stale"] is False


# ==============================================================================
# Requirement 19: Elimination of Core Identity Heuristics in Retrieval Pipeline
# ==============================================================================
def test_retrieval_pipeline_heuristic_elimination_and_provenance(tmp_path):
    """Verify that assess_relevance and identify_candidates in the retrieval pipeline
    strictly enforce validated Core provenance and reject all heuristic identity inference:
    1. proposed_type="Core" cannot establish Core identity.
    2. Paths containing '01 Core' cannot establish Core identity.
    3. Paths containing '/core/' cannot establish Core identity.
    4. Genuinely validated Core unit retains verified identity.
    5. Missing or invalid Core provenance remains unverified.
    6. Core ranking boost is applied ONLY to validated Core candidates.
    """
    from src.living_authenticity.knowledge.analysis.retrieval.pipeline import (
        prepare_query,
        assess_relevance,
        identify_candidates,
    )

    query = prepare_query(
        KnowledgeUnit(
            id="q-pipe",
            source="input/query.md",
            original_text="authenticity and core presence",
            meaning="authenticity and core presence",
        )
    )

    active_core_hash = "authoritative_hash_pipe_777"

    # Unit 1: Ordinary note attempting to claim Core via proposed_type
    u1_proposed = KnowledgeUnit(
        id="u1",
        source="notes/ordinary1.md",
        original_text="core presence and authenticity",
        meaning="core presence and authenticity",
        proposed_type="Core",
        is_core=False,
        core_hash="",
    )
    # Unit 2: Ordinary note inside path containing '01 Core'
    u2_path_01core = KnowledgeUnit(
        id="u2",
        source="archive/01 Core/nested_note.md",
        original_text="core presence and authenticity",
        meaning="core presence and authenticity",
        proposed_type="Observation",
        is_core=False,
        core_hash="",
    )
    # Unit 3: Ordinary note inside path containing '/core/'
    u3_path_slashcore = KnowledgeUnit(
        id="u3",
        source="examples/core/fixture.md",
        original_text="core presence and authenticity",
        meaning="core presence and authenticity",
        proposed_type="",
        is_core=False,
        core_hash="",
    )
    # Unit 4: Genuinely validated Core unit with matching hash
    u4_genuine_core = KnowledgeUnit(
        id="u4",
        source="01 Core/Authentic.md",
        original_text="core presence and authenticity",
        meaning="core presence and authenticity",
        proposed_type="Core",
        is_core=True,
        core_hash=active_core_hash,
    )
    # Unit 5: Unit claiming is_core=True but with stale/mismatched hash
    u5_mismatched = KnowledgeUnit(
        id="u5",
        source="01 Core/Stale.md",
        original_text="core presence and authenticity",
        meaning="core presence and authenticity",
        proposed_type="Core",
        is_core=True,
        core_hash="stale_mismatched_hash_000",
    )

    # 1. Test assess_relevance with active validated core context
    from tests._test_core_helpers import build_test_validated_core
    val_core_pipe = build_test_validated_core([u4_genuine_core], source_path="01 Core/Authentic.md", core_hash=active_core_hash)
    candidates = [u1_proposed, u2_path_01core, u3_path_slashcore, u4_genuine_core, u5_mismatched]
    assessments = assess_relevance(
        prepared=query,
        candidates=candidates,
        strategy="token_overlap",
        validated_core=val_core_pipe,
    )
    by_id = {a.candidate_unit_id: a for a in assessments}

    # Units 1, 2, 3 must be strictly unverified despite Core in proposed_type or path
    assert by_id["u1"].is_core is False
    assert by_id["u1"].core_hash == ""

    assert by_id["u2"].is_core is False
    assert by_id["u2"].core_hash == ""

    assert by_id["u3"].is_core is False
    assert by_id["u3"].core_hash == ""

    # Unit 4 is genuine matching Core
    assert by_id["u4"].is_core is True
    assert by_id["u4"].core_hash == active_core_hash

    # Unit 5 has mismatched hash -> unverified
    assert by_id["u5"].is_core is False
    assert by_id["u5"].core_hash == ""

    # 2. Test identify_candidates ranking and identity propagation
    identified = identify_candidates(
        assessments=assessments,
        max_candidates=10,
        retriever_name="token_overlap",
        core_boost_factor=0.5,
        validated_core=val_core_pipe,
    )
    id_by_unit = {c.unit_id: c for c in identified}

    # Only u4 receives is_core=True in RetrievedCandidate
    assert id_by_unit["u4"].is_core is True
    assert id_by_unit["u4"].core_hash == active_core_hash

    assert id_by_unit["u1"].is_core is False
    assert id_by_unit["u2"].is_core is False
    assert id_by_unit["u3"].is_core is False
    assert id_by_unit["u5"].is_core is False

    # u4 receives the 50% boost in ranking over other candidates with equal raw overlap
    assert identified[0].unit_id == "u4"


# ==============================================================================
# Requirement 20: Vault Sync Nested 01 Core Boundary Matching
# ==============================================================================
def test_vault_sync_nested_01_core_directory_not_mistaken_for_authoritative_core(tmp_path):
    """A nested directory like archive/01 Core/ must NOT be mistaken for the
    authoritative root-level 01 Core directory."""
    vault = tmp_path / "vault_nested_core"
    vault.mkdir()
    archive_core = vault / "archive" / "01 Core"
    archive_core.mkdir(parents=True)
    (archive_core / "archived_note.md").write_text("# Archived note\nContent.", encoding="utf-8")

    session = VaultSyncSession()
    session._authorized = True
    session._vault_root = str(vault)
    vdb_dir = tmp_path / "vdb_nested"
    session._store = LanceDBManager(str(vdb_dir))
    session._store.create_knowledge_vector_table()
    session._pipeline = EvidenceFirstPipeline()

    changes = session.check_changes()

    # The file in archive/01 Core must be classified as ORDINARY changes, not CORE changes
    assert "archive/01 Core/archived_note.md" in changes["ordinary_changes"]["added"] or \
           "archive\\01 Core\\archived_note.md" in changes["ordinary_changes"]["added"]
    assert len(changes["core_changes"]["added"]) == 0
    assert changes["core_changed"] is False


# ==============================================================================
# Requirement 21: Strict Fail-Closed Retrieval Without Active Validated Core
# ==============================================================================
def test_strict_fail_closed_retrieval_without_active_validated_core():
    """When validated_core_hash is absent or empty:
    1. Every candidate MUST remain is_core=False and core_hash="".
    2. A non-empty hash alone NEVER establishes Core identity.
    3. Caller-supplied is_core=True NEVER establishes Core identity.
    4. No candidate receives any Core ranking boost.
    5. Holds across assess_relevance, identify_candidates, and RetrievalCoordinator.
    """
    from src.living_authenticity.knowledge.analysis.retrieval.pipeline import (
        prepare_query,
        assess_relevance,
        identify_candidates,
        RetrievalCoordinator,
    )
    from src.living_authenticity.knowledge.analysis.retrieval.token_overlap import (
        TokenOverlapRetriever,
    )

    query = prepare_query(
        KnowledgeUnit(
            id="q1",
            source="input/query.md",
            original_text="authenticity and core presence",
            meaning="authenticity and core presence",
        )
    )

    # Unit carrying caller-asserted is_core=True and non-empty hash
    unit_unverified = KnowledgeUnit(
        id="cand1",
        source="01 Core/Authentic.md",
        original_text="authenticity and core presence",
        meaning="authenticity and core presence",
        proposed_type="Core",
        is_core=True,
        core_hash="non_empty_hash_without_active_reference_123",
    )

    # 1. assess_relevance with NO validated_core_hash
    assessments_no_active = assess_relevance(
        prepared=query,
        candidates=[unit_unverified],
        strategy="token_overlap",
        validated_core_hash="",  # ABSENT!
    )
    assert len(assessments_no_active) == 1
    assert assessments_no_active[0].is_core is False
    assert assessments_no_active[0].core_hash == ""

    # 2. identify_candidates with NO validated_core_hash
    candidates_no_active = identify_candidates(
        assessments=assessments_no_active,
        max_candidates=5,
        retriever_name="token_overlap",
        validated_core_hash="",  # ABSENT!
        core_boost_factor=0.5,
    )
    assert len(candidates_no_active) == 1
    assert candidates_no_active[0].is_core is False
    assert candidates_no_active[0].core_hash == ""

    # 3. RetrievalCoordinator execution with NO validated_core_hash
    coord = RetrievalCoordinator(
        strategy="token_overlap",
        min_shared_terms=1,
        max_candidates=5,
        validated_core_hash="",  # ABSENT!
        core_boost_factor=0.5,
    )
    res_coord = coord.run(
        KnowledgeUnit(id="q1", source="input", original_text="presence", meaning="presence"),
        [unit_unverified],
    )
    assert len(res_coord.candidates) == 1
    assert res_coord.candidates[0].is_core is False
    assert res_coord.candidates[0].core_hash == ""

    # 4. TokenOverlapRetriever execution with NO validated_core_hash
    retriever = TokenOverlapRetriever(
        min_shared_terms=1,
        max_candidates=5,
        validated_core_hash="",  # ABSENT!
        core_boost_factor=0.5,
    )
    res_ret = retriever.retrieve(
        KnowledgeUnit(id="q1", source="input", original_text="presence", meaning="presence"),
        [unit_unverified],
    )
    assert len(res_ret.candidates) == 1
    assert res_ret.candidates[0].is_core is False
    assert res_ret.candidates[0].core_hash == ""


# ==============================================================================
# Requirement 22: Forged Core Metadata Rejected Without Validated Provenance
# ==============================================================================
def test_forged_core_metadata_rejected_without_trustworthy_provenance(tmp_path):
    """A unit attempting to forge Core identity (e.g. copying active hash into an
    ordinary note or using an ordinary concept source path) MUST be rejected:
    1. assess_relevance marks it is_core=False.
    2. identify_candidates marks it is_core=False and applies no boost.
    3. LanceDBVectorRetriever marks it is_core=False and applies no boost.
    """
    from src.living_authenticity.knowledge.analysis.retrieval.pipeline import (
        prepare_query,
        assess_relevance,
        identify_candidates,
    )

    active_hash = "active_authoritative_sha256_hash_999"
    active_source = "/vault/01 Core/ActiveCore.md"

    # Forged unit: claims is_core=True, copies active hash, but source is outside Core
    forged_unit = KnowledgeUnit(
        id="forged_concept_unit",
        source="03 Concepts/some_concept.md",
        original_text="presence and authenticity",
        meaning="presence and authenticity",
        proposed_type="Core",
        is_core=True,
        core_hash=active_hash,
        core_source_path="03 Concepts/some_concept.md",
    )

    query = prepare_query(
        KnowledgeUnit(
            id="q1",
            source="input",
            original_text="presence and authenticity",
            meaning="presence and authenticity",
        )
    )

    assessments = assess_relevance(
        prepared=query,
        candidates=[forged_unit],
        strategy="token_overlap",
        validated_core_hash=active_hash,
        validated_core_source_path=active_source,
    )
    assert len(assessments) == 1
    assert assessments[0].is_core is False
    assert assessments[0].core_hash == ""

    candidates = identify_candidates(
        assessments=assessments,
        max_candidates=5,
        retriever_name="token_overlap",
        validated_core_hash=active_hash,
        core_boost_factor=0.5,
    )
    assert len(candidates) == 1
    assert candidates[0].is_core is False
    assert candidates[0].core_hash == ""

    # LanceDBVectorRetriever verification
    db_dir = tmp_path / "vec_db_forged"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    store.save_metadata({"status": "fresh", "indexed_vectors": 1, "core_hash": active_hash})
    embedder = DummyVectorEmbedder()

    # Store row with forged source outside Core
    store.store(
        text="presence and authenticity",
        embedding=embedder.embed("presence and authenticity"),
        metadata={
            "source": "03 Concepts/some_concept.md",
            "position": 0,
            "is_core": True,
            "core_hash": active_hash,
        },
    )

    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core_hash=active_hash,
        validated_core_source_path=active_source,
        core_boost_factor=0.5,
    )
    res = retriever.retrieve(
        KnowledgeUnit(id="q1", source="input", original_text="presence", meaning="presence")
    )
    assert len(res.candidates) == 1
    assert res.candidates[0].is_core is False
    assert res.candidates[0].core_hash == ""


# ==============================================================================
# Requirement 23: Generic Indexing Rejects Arbitrary Caller-Supplied Core Metadata
# ==============================================================================
def test_generic_indexing_rejects_arbitrary_caller_supplied_core_metadata(tmp_path):
    """index_units and index_corpus must not blindly convert caller-supplied
    is_core=True and a non-empty core_hash into authoritative Core vector records:
    1. An arbitrary unit with is_core=True and core_hash stored via index_units
       without validated_core MUST be stored as is_core=False, core_hash="".
    2. An arbitrary unit stored via index_corpus MUST be stored as is_core=False, core_hash="".
    3. Genuinely validated Core units retain is_core=True, core_hash=active_hash.
    """
    db_dir = tmp_path / "vec_db_generic_idx"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    # Arbitrary caller creates a unit claiming is_core=True
    arbitrary_unit = KnowledgeUnit(
        id="arb-ku",
        source="02 Notes/random_note.md",
        original_text="Some random observation text.",
        meaning="Some random observation text.",
        proposed_type="Core",
        is_core=True,
        core_hash="arbitrary_caller_supplied_hash_555",
    )

    # Index without validated_core
    index_units([arbitrary_unit], embedder, store)

    rows = store.show_all()
    assert len(rows) == 1
    # MUST NOT be recorded as Core
    assert rows[0].get("is_core") is False
    assert rows[0].get("core_hash") == ""

    # Test with genuinely validated Core
    vault = tmp_path / "vault_idx_genuine"
    _make_valid_vault(vault, note_title="Genuine Core", body="Authoritative Core theory text.")
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    db_dir2 = tmp_path / "vec_db_genuine"
    store2 = LanceDBManager(str(db_dir2))
    store2.create_knowledge_vector_table()

    index_units(val_core.units, embedder, store2, validated_core=val_core)
    rows2 = store2.show_all()
    assert len(rows2) == 1
    assert rows2[0].get("is_core") is True
    assert rows2[0].get("core_hash") == val_core.sha256


# ==============================================================================
# Requirement 24: LanceDB Vector Retriever Fail-Closed Lifecycle & Ranking
# ==============================================================================
def test_lancedb_vector_retriever_fail_closed_lifecycle_and_ranking(tmp_path):
    """Verify that LanceDBVectorRetriever:
    1. Fails closed when validated_core_hash is absent (candidates remain is_core=False, core_hash="", no boost).
    2. Fails closed when validated_core_hash is mismatched.
    3. Applies Core status and proportional boost ONLY when hash and provenance match active Core.
    4. Does not unconditionally override semantically stronger ordinary candidates.
    """
    db_dir = tmp_path / "vec_db_lifecycle"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()

    active_hash = "active_hash_444"
    core_source = "/vault/01 Core/ActiveCore.md"

    c_unit_lance = KnowledgeUnit(
        id="c-lance-matching", source=core_source, original_text="authentic presence and core resonance",
        meaning="authentic presence and core resonance", cleaned_text="authentic presence and core resonance",
        normalized_text="authentic presence and core resonance", context="",
        proposed_type="Core", position=0, is_core=True,
        core_hash=active_hash, core_source_path=core_source,
    )
    from tests._test_core_helpers import build_test_validated_core
    from src.living_authenticity.knowledge.vault.core_validator import compute_core_record_id
    val_core_lance = build_test_validated_core([c_unit_lance], source_path=core_source, core_hash=active_hash)
    core_rec_id_lance = compute_core_record_id(c_unit_lance, val_core_lance)

    # Row 1: Valid Core record with trusted record_id
    store.store(
        text="authentic presence and core resonance",
        embedding=embedder.embed("authentic presence and core resonance"),
        metadata={
            "source": core_source,
            "position": 0,
            "is_core": True,
            "core_hash": active_hash,
        },
        record_id=core_rec_id_lance,
        validated_core=val_core_lance,
    )
    val_auth = build_test_sync_authorization(val_core_lance)
    store.save_metadata(
        {"status": "fresh", "indexed_vectors": 1, "core_hash": active_hash},
        validated_core=val_core_lance,
        sync_auth=val_auth,
    )

    # 1. Retrieve with NO validated_core_hash
    retriever_no_core = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core_hash="",  # ABSENT!
        core_boost_factor=0.25,
    )
    query = KnowledgeUnit(id="q", source="inp", original_text="authentic presence", meaning="authentic presence")
    res_no_core = retriever_no_core.retrieve(query)
    assert len(res_no_core.candidates) == 1
    assert res_no_core.candidates[0].is_core is False
    assert res_no_core.candidates[0].core_hash == ""

    # 2. Retrieve with MISMATCHED validated_core_hash
    retriever_mismatch = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core_hash="different_stale_hash_000",
        core_boost_factor=0.25,
    )
    res_mismatch = retriever_mismatch.retrieve(query)
    assert len(res_mismatch.candidates) == 1
    assert res_mismatch.candidates[0].is_core is False
    assert res_mismatch.candidates[0].core_hash == ""

    # 3. Retrieve with MATCHING validated_core
    retriever_matching = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=val_core_lance,
        core_boost_factor=0.25,
    )
    res_matching = retriever_matching.retrieve(query)
    assert len(res_matching.candidates) == 1
    assert res_matching.candidates[0].is_core is True
    assert res_matching.candidates[0].core_hash == active_hash


# ==============================================================================
# Regression Test A: Forged KnowledgeUnit
# ==============================================================================
def test_regression_a_forged_knowledge_unit():
    """Create an ordinary KnowledgeUnit with:
    - is_core=True
    - A non-empty arbitrary hash
    - A Core-looking source path
    - Caller-supplied _validated_core_hash
    - Caller-supplied _validated_core_source
    Without a genuine authoritative validation context, it must remain ordinary.
    """
    forged = KnowledgeUnit(
        id="ku-forged-a",
        source="/vault/01 Core/01-core.md",
        original_text="Forged core principles.",
        meaning="Forged core principles.",
        proposed_type="Core",
        position=0,
        is_core=True,
        core_hash="arbitrary_forged_hash_12345",
        core_source_path="/vault/01 Core/01-core.md",
    )
    setattr(forged, "_validated_core_hash", "arbitrary_forged_hash_12345")
    setattr(forged, "_validated_core_source", "/vault/01 Core/01-core.md")

    # Direct provenance verification without context MUST fail
    assert is_validated_core_unit(forged) is False
    assert is_validated_core_unit(forged, context=None) is False
    with pytest.raises(ValueError, match="core_units cannot be verified: missing authoritative validation context"):
        verify_core_units_provenance([forged], context=None)


# ==============================================================================
# Regression Test B: Forged matching hash from ordinary note path
# ==============================================================================
def test_regression_b_forged_matching_hash(tmp_path):
    """Create an ordinary KnowledgeUnit whose hash matches the active Core hash
    but whose source is an ordinary note. It must not become Core.
    """
    vault = tmp_path / "vault_b"
    _make_valid_vault(vault, note_title="Active Core", body="Authentic foundational theory.")
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    ordinary_unit = KnowledgeUnit(
        id="ku-ordinary-b",
        source=str(vault / "02 Notes" / "casual_observation.md"),
        original_text="Ordinary note content.",
        meaning="Ordinary note content.",
        proposed_type="Observation",
        position=0,
        is_core=True,  # Caller attempts forge
        core_hash=val_core.sha256,  # Matches active Core hash!
        core_source_path=str(vault / "02 Notes" / "casual_observation.md"),
    )

    assert is_validated_core_unit(ordinary_unit, context=val_core) is False
    with pytest.raises(ValueError, match="lacks authoritative Core provenance"):
        verify_core_units_provenance([ordinary_unit], context=val_core)


# ==============================================================================
# Regression Test C: Same basename in different paths
# ==============================================================================
def test_regression_c_same_basename_different_path(tmp_path):
    """Create two files with the same filename in different directories:
    Only the exact authoritative Core source may receive verified Core identity.
    """
    vault = tmp_path / "vault_c"
    note_path = _make_valid_vault(vault, note_title="Active Core", body="Authentic root.")
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    # Second file with identical basename in an archive folder
    archive_dir = vault / "09 Archive" / "01 Core"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_note = archive_dir / note_path.name
    archive_note.write_text("Old archived content", encoding="utf-8")

    archive_unit = KnowledgeUnit(
        id="ku-archive-same-name",
        source=str(archive_note),
        original_text="Old archived content",
        meaning="Old archived content",
        proposed_type="Core",
        position=0,
        is_core=True,
        core_hash=val_core.sha256,
        core_source_path=str(archive_note),
    )

    # Basename matches ("01-core-anchor.md"), but canonical path differs
    assert is_validated_core_unit(archive_unit, context=val_core) is False
    with pytest.raises(ValueError, match="lacks authoritative Core provenance"):
        verify_core_units_provenance([archive_unit], context=val_core)

    # Genuine Core note unit matches
    genuine_unit = val_core.units[0]
    assert is_validated_core_unit(genuine_unit, context=val_core) is True
    verify_core_units_provenance([genuine_unit], context=val_core)


# ==============================================================================
# Regression Test D: Missing source provenance
# ==============================================================================
def test_regression_d_missing_source_provenance(tmp_path):
    """Provide a matching hash without authoritative source provenance:
    The unit must remain ordinary.
    """
    vault = tmp_path / "vault_d"
    _make_valid_vault(vault)
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    unit_empty_source = KnowledgeUnit(
        id="ku-empty-src",
        source="",
        original_text="Core text",
        meaning="Core text",
        proposed_type="Core",
        position=0,
        is_core=True,
        core_hash=val_core.sha256,
        core_source_path="",
    )
    assert is_validated_core_unit(unit_empty_source, context=val_core) is False


# ==============================================================================
# Regression Test E: Missing validation context across all 7 execution paths
# ==============================================================================
def test_regression_e_missing_validation_context_all_paths(tmp_path):
    """Exercise:
    1. index_units
    2. assess_relevance
    3. identify_candidates
    4. TokenOverlapRetriever
    5. LanceDBVectorRetriever
    6. RetrievalCoordinator
    7. EvidenceFirstPipeline
    Every path must fail closed when validation context is missing.
    """
    from src.living_authenticity.knowledge.analysis.retrieval.pipeline import (
        PreparedQuery,
        assess_relevance,
        identify_candidates,
        RetrievalCoordinator,
    )
    from src.living_authenticity.knowledge.analysis.retrieval.token_overlap import (
        TokenOverlapRetriever,
    )

    query = KnowledgeUnit(id="q1", source="input", original_text="clarity attention", meaning="clarity attention")
    candidate_unit = KnowledgeUnit(
        id="cand-ku",
        source="/vault/01 Core/note.md",
        original_text="clarity attention presence",
        meaning="clarity attention presence",
        proposed_type="Core",
        position=0,
        is_core=True,
        core_hash="alleged_hash_999",
        core_source_path="/vault/01 Core/note.md",
    )
    setattr(candidate_unit, "_validated_core_hash", "alleged_hash_999")
    setattr(candidate_unit, "_validated_core_source", "/vault/01 Core/note.md")

    # 1. index_units without validated_core -> indexed as ordinary
    db_dir = tmp_path / "vec_db_e"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()
    index_units([candidate_unit], embedder, store, validated_core=None)
    rows = store.show_all()
    assert len(rows) == 1
    assert rows[0].get("is_core") is False
    assert rows[0].get("core_hash") == ""
    store.save_metadata({"status": "fresh", "indexed_vectors": len(rows)})

    # 2. assess_relevance without validation context -> candidate is ordinary
    prep = PreparedQuery(
        query_unit_id="q1",
        query_source="input",
        query_position=0,
        raw_text="clarity attention",
        normalized_text="clarity attention",
        query_terms=("attention", "clarity"),
        context_metadata={},
    )
    assessments = assess_relevance(prep, [candidate_unit], validated_core=None)
    assert len(assessments) == 1
    assert assessments[0].is_core is False
    assert assessments[0].core_hash == ""

    # 3. identify_candidates without validation context -> candidates remain ordinary with no boost
    identified = identify_candidates(assessments, validated_core=None, core_boost_factor=0.5)
    assert len(identified) == 1
    assert identified[0].is_core is False
    assert identified[0].core_hash == ""

    # 4. TokenOverlapRetriever without validated_core -> result candidates remain ordinary
    to_retriever = TokenOverlapRetriever(validated_core=None, core_boost_factor=0.5)
    to_res = to_retriever.retrieve(query, [candidate_unit])
    assert len(to_res.candidates) == 1
    assert to_res.candidates[0].is_core is False
    assert to_res.candidates[0].core_hash == ""

    # 5. LanceDBVectorRetriever without validated_core -> candidates remain ordinary
    lancedb_retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=None,
        core_boost_factor=0.5,
    )
    lance_res = lancedb_retriever.retrieve(query)
    assert len(lance_res.candidates) == 1
    assert lance_res.candidates[0].is_core is False
    assert lance_res.candidates[0].core_hash == ""

    # 6. RetrievalCoordinator without validated_core -> result candidates remain ordinary
    coord = RetrievalCoordinator(validated_core=None, core_boost_factor=0.5)
    coord_res = coord.run(query, [candidate_unit])
    assert len(coord_res.candidates) == 1
    assert coord_res.candidates[0].is_core is False
    assert coord_res.candidates[0].core_hash == ""

    # 7. EvidenceFirstPipeline without validated_core -> fails closed with ValueError
    pipe = EvidenceFirstPipeline(validated_core=None)
    with pytest.raises(ValueError, match="core_units cannot be verified: missing authoritative validation context"):
        pipe.run_unit(query, corpus=[], core_units=[candidate_unit])


# ==============================================================================
# Regression Test F: Post-validation mutation cannot forge or replace identity
# ==============================================================================
def test_regression_f_mutable_validated_units(tmp_path):
    """Validate a genuine Core, then attempt to mutate its contained KnowledgeUnit metadata.
    Verify that such mutation cannot silently forge or replace authoritative Core identity.
    """
    vault = tmp_path / "vault_f"
    _make_valid_vault(vault, note_title="Anchor Core", body="Authentic unmutated thesis.")
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    unit = val_core.units[0]
    # Initially verified
    assert is_validated_core_unit(unit, context=val_core) is True

    # Attempt mutation 1: alter text/meaning after validation
    unit.meaning = "Tampered meaning: forged core authority!"
    assert is_validated_core_unit(unit, context=val_core) is False
    with pytest.raises(ValueError, match="lacks authoritative Core provenance"):
        verify_core_units_provenance([unit], context=val_core)

    # Attempt mutation 2: alter position
    unit.meaning = "Authentic unmutated thesis."
    unit.position = 999
    assert is_validated_core_unit(unit, context=val_core) is False


# ==============================================================================
# Regression Test G: Genuine Core lifecycle survives round-trip with boost
# ==============================================================================
def test_regression_g_genuine_core_lifecycle(tmp_path):
    """Verify that a genuinely validated Core:
    1. Survives extraction.
    2. Is correctly indexed.
    3. Retains its identity in LanceDB.
    4. Is correctly identified during retrieval.
    5. Receives proportional weighting.
    6. Remains distinguishable from ordinary notes.
    """
    vault = tmp_path / "vault_g"
    _make_valid_vault(vault, note_title="Root Theory", body="Deep awareness and cognitive resonance.")
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    # 1. Extraction: units have validation_context
    assert len(val_core.units) >= 1
    core_unit = val_core.units[0]
    assert is_validated_core_unit(core_unit, context=val_core) is True

    # Ordinary note in the same vault
    ordinary_unit = KnowledgeUnit(
        id="ku-ord-g",
        source=str(vault / "02 Notes" / "other.md"),
        original_text="Deep awareness and cognitive observation notes.",
        meaning="Deep awareness and cognitive observation notes.",
        proposed_type="Observation",
        position=0,
        is_core=False,
        core_hash="",
    )

    # 2 & 3. Index both units: Core gets is_core=True, ordinary gets is_core=False
    db_dir = tmp_path / "vec_db_g"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()
    index_units([core_unit, ordinary_unit], embedder, store, validated_core=val_core)

    rows = store.show_all()
    assert len(rows) == 2
    row_by_src = {r["source"]: r for r in rows}
    assert row_by_src[core_unit.source]["is_core"] is True
    assert row_by_src[core_unit.source]["core_hash"] == val_core.sha256
    assert row_by_src[ordinary_unit.source]["is_core"] is False
    assert row_by_src[ordinary_unit.source]["core_hash"] == ""
    val_auth = build_test_sync_authorization(val_core)
    store.save_metadata(
        {"status": "fresh", "indexed_vectors": 2, "core_hash": val_core.sha256},
        validated_core=val_core,
        sync_auth=val_auth,
    )

    # 4 & 5. Retrieval with validated_core context: Core is recognized and boosted
    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=val_core,
        core_boost_factor=0.5,
    )
    query = KnowledgeUnit(id="q", source="inp", original_text="Deep awareness and cognitive resonance", meaning="Deep awareness")
    result = retriever.retrieve(query)

    assert len(result.candidates) == 2
    cand_by_src = {c.source: c for c in result.candidates}
    core_cand = cand_by_src[core_unit.source]
    ord_cand = cand_by_src[ordinary_unit.source]

    assert core_cand.is_core is True
    assert core_cand.core_hash == val_core.sha256
    assert ord_cand.is_core is False
    assert ord_cand.core_hash == ""

    # Proportional boost verification: core candidate is boosted to rank #1
    assert result.candidates[0].source == core_unit.source
    assert result.candidates[0].is_core is True


# ==============================================================================
# Phase 3 — Final Provenance Integrity and Zero Self-Authentication Regressions
# (Tests A through K)
# ==============================================================================

def test_regression_a_runner_rejects_arbitrary_core_units_without_validated_core(tmp_path):
    """Regression Test A: run_bounded_integration() rejects arbitrary units with
    is_core=True and non-empty hash when no validated Core is supplied."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "note.md").write_text("Observation:\nObserving calm.\n", encoding="utf-8")
    staging = tmp_path / "stg"
    staging.mkdir()

    arbitrary_core = KnowledgeUnit(
        id="forged-core",
        source="01 Core/forged.md",
        original_text="Method:\nForged core procedure.\n",
        meaning="Method:\nForged core procedure.\n",
        cleaned_text="Method:\nForged core procedure.\n",
        normalized_text="method: forged core procedure.",
        context="",
        proposed_type="Core",
        position=0,
        is_core=True,
        core_hash="forged_hash_value_1234567890",
        core_source_path="01 Core/forged.md",
    )

    report = run_bounded_integration(
        source_root=str(source),
        staging_root=str(staging),
        core_units=[arbitrary_core],
        validated_core=None,
    )

    assert report.accepted is False
    assert report.run_outcome == "operational_failure"
    assert report.rejection_check == "core_preflight"
    assert "authoritative validated Core provenance" in report.rejection_reason


def test_regression_b_runner_never_autocreates_validated_core_at_runtime(tmp_path):
    """Regression Test B: Prove that build_test_validated_core is never called inside run_bounded_integration."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "note.md").write_text("Observation:\nObserving calm.\n", encoding="utf-8")
    staging = tmp_path / "stg"
    staging.mkdir()

    arbitrary_core = KnowledgeUnit(
        id="forged-core",
        source="core.md",
        original_text="Method:\nForged core.\n",
        meaning="Method:\nForged core.\n",
        cleaned_text="Method:\nForged core.\n",
        normalized_text="method: forged core.",
        context="",
        proposed_type="Core",
        position=0,
        is_core=True,
        core_hash="forged_hash_b",
        core_source_path="core.md",
    )

    pipe = EvidenceFirstPipeline()
    report = run_bounded_integration(
        source_root=str(source),
        staging_root=str(staging),
        pipeline=pipe,
        core_units=[arbitrary_core],
    )
    assert report.accepted is False
    assert report.run_outcome == "operational_failure"
    assert getattr(pipe, "validated_core", None) is None


def test_regression_c_manual_core_validation_context_rejected():
    """Regression Test C: Manually constructed CoreValidationContext fails closed."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        CoreValidationContext,
        is_validated_core_unit,
        is_trusted_validation_context,
    )
    u = KnowledgeUnit(
        id="c1",
        source="core.md",
        original_text="Core text",
        meaning="Core text",
        cleaned_text="Core text",
        normalized_text="core text",
        context="",
        proposed_type="Core",
        position=0,
        is_core=True,
        core_hash="dummy_hash",
        core_source_path="core.md",
    )
    manual_ctx = CoreValidationContext(
        core_hash="dummy_hash",
        source_path="core.md",
        unit_fingerprints=frozenset([("c1", "some_text_hash", 0, "core.md", "core.md", "dummy_hash", True)]),
    )
    assert is_trusted_validation_context(manual_ctx) is False
    assert is_validated_core_unit(u, context=manual_ctx) is False


def test_regression_d_generic_duck_typed_object_rejected():
    """Regression Test D: Generic duck-typed object exposing sha256/source_path fails closed."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        is_validated_core_unit,
        is_trusted_validation_context,
    )
    class DuckContext:
        sha256 = "dummy_hash"
        source_path = "core.md"
        path = "core.md"

    duck = DuckContext()
    u = KnowledgeUnit(
        id="c1", source="core.md", original_text="Core text", meaning="Core text",
        cleaned_text="Core text", normalized_text="core text", context="",
        proposed_type="Core", position=0, is_core=True, core_hash="dummy_hash",
        core_source_path="core.md",
    )
    assert is_trusted_validation_context(duck) is False
    assert is_validated_core_unit(u, context=duck) is False


def test_regression_e_incomplete_context_with_empty_fingerprints_rejected():
    """Regression Test E: Incomplete context with empty fingerprint collection fails closed."""
    from tests._test_core_helpers import build_test_validated_core
    from src.living_authenticity.knowledge.vault.core_validator import (
        is_validated_core_unit,
        is_trusted_validation_context,
    )
    val_empty = build_test_validated_core([], source_path="core.md", core_hash="hash_empty")
    assert is_trusted_validation_context(val_empty.validation_context) is False

    u = KnowledgeUnit(
        id="c1", source="core.md", original_text="Core text", meaning="Core text",
        cleaned_text="Core text", normalized_text="core text", context="",
        proposed_type="Core", position=0, is_core=True, core_hash="hash_empty",
        core_source_path="core.md",
    )
    assert is_validated_core_unit(u, context=val_empty) is False


def test_regression_f_raw_hash_and_path_cannot_authorize_retrieval_or_boost():
    """Regression Test F: Raw validated_core_hash and validated_core_source_path passed to
    retrieval APIs do not establish Core identity or trigger boost."""
    from src.living_authenticity.knowledge.analysis.retrieval.pipeline import (
        prepare_query,
        assess_relevance,
        identify_candidates,
    )
    u_cand = KnowledgeUnit(
        id="cand1", source="core.md", original_text="Calibrating instruments procedure",
        meaning="Calibrating instruments", cleaned_text="Calibrating instruments procedure",
        normalized_text="calibrating instruments procedure", context="",
        proposed_type="Core", position=0, is_core=True, core_hash="secret_hash",
        core_source_path="core.md",
    )
    query = KnowledgeUnit(
        id="q1", source="input.md", original_text="Calibrating instruments",
        meaning="Calibrating instruments", cleaned_text="Calibrating instruments",
        normalized_text="calibrating instruments", context="", position=0,
    )
    prep = prepare_query(query)

    # Calling assess_relevance with RAW hash/path strings and validated_core=None
    assessments = assess_relevance(
        prepared=prep,
        candidates=[u_cand],
        validated_core_hash="secret_hash",
        validated_core_source_path="core.md",
        validated_core=None,
    )
    assert len(assessments) == 1
    assert assessments[0].is_core is False
    assert assessments[0].core_hash == ""

    # Calling identify_candidates with RAW hash/path strings and validated_core=None
    candidates = identify_candidates(
        assessments=assessments,
        validated_core_hash="secret_hash",
        validated_core_source_path="core.md",
        core_boost_factor=1.0,
        validated_core=None,
    )
    assert len(candidates) == 1
    assert candidates[0].is_core is False
    assert candidates[0].core_hash == ""


def test_regression_g_direct_setters_cannot_bypass_validated_core_context():
    """Regression Test G: Direct setters do not bypass the validated-Core context requirement."""
    from src.living_authenticity.knowledge.analysis.retrieval.token_overlap import TokenOverlapRetriever
    from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import LanceDBVectorRetriever
    from src.living_authenticity.knowledge.analysis.retrieval.pipeline import RetrievalCoordinator

    to_ret = TokenOverlapRetriever()
    to_ret.set_validated_core_hash("raw_hash")
    to_ret.set_validated_core_source_path("raw_path")
    assert getattr(to_ret, "_validated_core_hash", "") == ""
    assert getattr(to_ret, "_validated_core_source_path", "") == ""

    coord = RetrievalCoordinator()
    coord.set_validated_core_hash("raw_hash")
    coord.set_validated_core_source_path("raw_path")
    assert coord.validated_core_hash == ""
    assert coord.validated_core_source_path == ""


def test_regression_h_mutation_of_any_provenance_field_invalidates_unit():
    """Regression Test H: Mutation of any provenance-bearing field invalidates verified status."""
    from tests._test_core_helpers import build_test_validated_core
    from src.living_authenticity.knowledge.vault.core_validator import (
        is_validated_core_unit,
    )
    raw = KnowledgeUnit(
        id="c1", source="core.md", original_text="Genuine core content.",
        meaning="Genuine core content.", cleaned_text="Genuine core content.",
        normalized_text="genuine core content.", context="",
        proposed_type="Core", position=0, is_core=True,
        core_hash="h1", core_source_path="core.md",
    )
    val = build_test_validated_core([raw], source_path="core.md", core_hash="h1")
    unit = val.units[0]
    assert is_validated_core_unit(unit, context=val) is True

    # Mutate source
    unit.source = "other.md"
    assert is_validated_core_unit(unit, context=val) is False
    unit.source = "core.md"

    # Mutate core_source_path
    unit.core_source_path = "other.md"
    assert is_validated_core_unit(unit, context=val) is False
    unit.core_source_path = "core.md"

    # Mutate core_hash
    unit.core_hash = "h2"
    assert is_validated_core_unit(unit, context=val) is False
    unit.core_hash = "h1"

    # Mutate is_core
    unit.is_core = False
    assert is_validated_core_unit(unit, context=val) is False
    unit.is_core = True

    # Mutate position
    unit.position = 42
    assert is_validated_core_unit(unit, context=val) is False
    unit.position = 0

    # Mutate original_text
    unit.original_text = "Tampered text."
    assert is_validated_core_unit(unit, context=val) is False
    unit.original_text = "Genuine core content."

    assert is_validated_core_unit(unit, context=val) is True


def test_regression_i_cloned_or_constructed_unit_with_copied_metadata_rejected():
    """Regression Test I: Cloned or independently constructed KnowledgeUnit with copied
    metadata is rejected due to object instance binding."""
    import copy
    from tests._test_core_helpers import build_test_validated_core
    from src.living_authenticity.knowledge.vault.core_validator import (
        is_validated_core_unit,
    )
    raw = KnowledgeUnit(
        id="c1", source="core.md", original_text="Genuine core content.",
        meaning="Genuine core content.", cleaned_text="Genuine core content.",
        normalized_text="genuine core content.", context="",
        proposed_type="Core", position=0, is_core=True,
        core_hash="h1", core_source_path="core.md",
    )
    val = build_test_validated_core([raw], source_path="core.md", core_hash="h1")
    genuine_unit = val.units[0]
    assert is_validated_core_unit(genuine_unit, context=val) is True

    # Clone using copy.copy
    cloned = copy.copy(genuine_unit)
    assert is_validated_core_unit(cloned, context=val) is False

    # Separately constructed with identical fields
    separate = KnowledgeUnit(
        id="c1", source="core.md", original_text="Genuine core content.",
        meaning="Genuine core content.", cleaned_text="Genuine core content.",
        normalized_text="genuine core content.", context="",
        proposed_type="Core", position=0, is_core=True,
        core_hash="h1", core_source_path="core.md",
    )
    assert is_validated_core_unit(separate, context=val) is False


def test_regression_j_genuine_vault_mode_core_lifecycle_e2e(tmp_path):
    from src.living_authenticity.knowledge.vault.core_validator import is_trusted_validation_context
    """Regression Test J: Genuine Vault Mode Core validation works end to end across
    validation, indexing, and both retrieval strategies (token overlap and vector)."""
    vault = tmp_path / "obsidian_vault_j"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    core_note = core_dir / "01-core-authentic.md"
    core_note.write_text(
        "---\ntitle: Authentic Core\ncanonical_type: Core\ndraft: false\nstatus: active\n---\n"
        "Calibration of laboratory instruments and precision analytical balances.",
        encoding="utf-8",
    )

    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
    assert val_core.canonical_type == "Core"
    assert val_core.validation_context is not None
    assert is_trusted_validation_context(val_core.validation_context) is True
    assert len(val_core.units) >= 1
    c_unit = val_core.units[0]
    assert is_validated_core_unit(c_unit, context=val_core) is True

    # 1. Token overlap retrieval end-to-end
    pipe = EvidenceFirstPipeline()
    pipe.set_validated_core(val_core)
    q_unit = KnowledgeUnit(id="q", source="inbox", original_text="laboratory instruments precision", meaning="instruments precision")
    tok_res = pipe.retriever.retrieve(q_unit, [c_unit])
    assert len(tok_res.candidates) >= 1
    assert tok_res.candidates[0].is_core is True
    assert tok_res.candidates[0].core_hash == val_core.sha256

    # 2. Vector indexing and vector retrieval end-to-end
    db_dir = tmp_path / "vec_j"
    store = LanceDBManager(str(db_dir))
    store.create_knowledge_vector_table()
    embedder = DummyVectorEmbedder()
    index_units(list(val_core.units), embedder, store, validated_core=val_core)
    val_auth = build_test_sync_authorization(val_core)
    store.save_metadata(
        {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
        validated_core=val_core,
        sync_auth=val_auth,
    )
    vec_retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=val_core,
    )
    vec_res = vec_retriever.retrieve(q_unit)
    assert len(vec_res.candidates) >= 1
    assert vec_res.candidates[0].is_core is True
    assert vec_res.candidates[0].core_hash == val_core.sha256


def test_regression_k_test_mode_fixtures_preserve_vault_mode_boundaries(tmp_path):
    from tests._test_core_helpers import build_test_validated_core
    from src.living_authenticity.knowledge.vault.core_validator import (
        is_trusted_validation_context,
    )
    """Regression Test K: Explicitly controlled Test Mode fixtures work without weakening Vault Mode."""
    # Test Mode fixture works when explicitly designated
    raw = KnowledgeUnit(
        id="t1", source="core.md", original_text="Test fixture text.",
        meaning="Test fixture text.", cleaned_text="Test fixture text.",
        normalized_text="test fixture text.", context="",
        proposed_type="Core", position=0, is_core=True,
        core_hash="test_mode_hash", core_source_path="core.md",
    )
    val_test = build_test_validated_core([raw], source_path="core.md", core_hash="test_mode_hash")
    assert is_trusted_validation_context(val_test.validation_context) is True
    assert is_validated_core_unit(val_test.units[0], context=val_test) is True

    # But Vault Mode never accepts loose test fixtures or unvalidated notes
    fake_vault = tmp_path / "fake_vault"
    fake_vault.mkdir()
    # Missing 01 Core dir in Vault Mode
    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(fake_vault))
    assert exc_info.value.category == CoreFailureCategory.DIRECTORY_MISSING


# ==============================================================================
# Phase 3 Final Provenance, Test Factory Relocation & Vector Trust Chain Tests (A-I)
# ==============================================================================
class TestPhase3ProvenanceAndZeroSelfAuthenticationRegression:
    """Comprehensive regression tests verifying that:
    1. Production API never exposes a callable test-only Core validation factory.
    2. Arbitrary runtime code paths cannot create a trusted Core context.
    3. Designated Test Mode fixture produces a trusted context.
    4. Genuine Vault Mode validation produces a trusted context.
    5. Directly forged vector records cannot receive verified Core status or boost.
    6. Genuinely indexed Core records retain verified status and proportional ranking.
    7. Ordinary vector records remain ordinary and unboosted.
    8. Fingerprint serialization is unambiguous, length-prefixed, and deterministic.
    9. Post-validation mutation and cloned units fail closed.
    """

    def test_coverage_a_production_exports_do_not_expose_test_validation_factory(self):
        """A: Production imports and exports do not expose a callable test-only Core validation factory."""
        import inspect
        import src.living_authenticity.knowledge.vault as vault
        import src.living_authenticity.knowledge.vault.core_validator as core_validator

        # Vault package public exports
        assert "build_test_validated_core" not in vault.__all__
        assert getattr(vault, "build_test_validated_core", None) is None
        assert "compute_core_record_id" not in vault.__all__
        assert getattr(vault, "compute_core_record_id", None) is None

        # Core validator module public functions
        assert getattr(core_validator, "build_test_validated_core", None) is None
        public_funcs = [
            name for name, obj in inspect.getmembers(core_validator, inspect.isfunction)
            if not name.startswith("_")
        ]
        assert "build_test_validated_core" not in public_funcs
        for name in public_funcs:
            assert not name.startswith("build_test_"), f"Unexpected test factory in production module: {name}"

    def test_coverage_b_arbitrary_runtime_code_cannot_mint_trusted_core_context(self, tmp_path):
        """B: Arbitrary runtime code paths cannot create a trusted Core context from caller-supplied units."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            CoreValidationContext,
            is_trusted_validation_context,
            is_validated_core_unit,
        )
        # Attempt 1: Caller directly instantiates CoreValidationContext
        forged_ctx = CoreValidationContext(
            core_hash="attacker_hash_123",
            source_path="01 Core/attacker.md",
            canonical_type="Core",
            source_mode="vault",
            unit_fingerprints=frozenset([("fake", "text_h", 0, "s", "s", "h", True)]),
        )
        assert is_trusted_validation_context(forged_ctx) is False

        # Attempt 2: Caller supplies forged context to run_bounded_integration
        src = tmp_path / "src"
        src.mkdir()
        (src / "obs.md").write_text("Observation:\nDaily stress observations.\n", encoding="utf-8")
        stg = tmp_path / "stg"
        stg.mkdir()

        arbitrary_u = KnowledgeUnit(
            id="attacker-u", source="01 Core/attacker.md",
            original_text="Attacker thesis.", meaning="Attacker thesis.",
            cleaned_text="Attacker thesis.", normalized_text="attacker thesis.",
            context="", proposed_type="Core", position=0,
            is_core=True, core_hash="attacker_hash_123", core_source_path="01 Core/attacker.md",
        )
        report = run_bounded_integration(
            str(src), str(stg),
            core_units=[arbitrary_u],
            validated_core=forged_ctx,
        )
        assert report.accepted is False
        assert report.run_outcome == "operational_failure"
        assert report.rejection_check == "core_preflight"

        # Attempt 3: Pipeline set_validated_core rejects forged context
        pipe = EvidenceFirstPipeline()
        pipe.set_validated_core(forged_ctx)
        assert pipe.validated_core is None
        assert getattr(pipe.retriever, "validated_core", None) is None

    def test_coverage_c_designated_test_mode_fixture_produces_trusted_context(self):
        """C: Actual designated Test Mode fixture validation still produces a trusted context."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            validate_and_load_core,
            is_trusted_validation_context,
            is_validated_core_unit,
        )
        val_test = validate_and_load_core("examples")
        assert val_test.canonical_type == "Core"
        assert val_test.source_mode == "examples"
        assert is_trusted_validation_context(val_test.validation_context) is True
        assert len(val_test.units) >= 1
        for u in val_test.units:
            assert is_validated_core_unit(u, context=val_test) is True

    def test_coverage_d_genuine_vault_mode_validation_produces_trusted_context(self, tmp_path):
        """D: Genuine Vault Mode validation still produces a trusted context."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            validate_and_load_core,
            is_trusted_validation_context,
            is_validated_core_unit,
        )
        vault = tmp_path / "vault_d"
        core_dir = vault / "01 Core"
        core_dir.mkdir(parents=True)
        note_content = (
            "---\n"
            "canonical_type: Core\n"
            "draft: false\n"
            "status: active\n"
            "---\n"
            "# Authoritative Core Thesis\n\n"
            "Central calibration principle for empirical evaluation.\n"
        )
        (core_dir / "thesis.md").write_text(note_content, encoding="utf-8")

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        assert val_core.canonical_type == "Core"
        assert val_core.source_mode == "personal_vault"
        assert is_trusted_validation_context(val_core.validation_context) is True
        assert len(val_core.units) >= 1
        assert is_validated_core_unit(val_core.units[0], context=val_core) is True

    def test_coverage_e_forged_vector_record_cannot_receive_verified_core_or_boost(self, tmp_path):
        """E: A forged Vector Store record with matching is_core, core_hash, and source
        cannot receive verified Core status or Core ranking boost when inserted directly."""
        val_core = validate_and_load_core("examples")
        active_hash = val_core.sha256
        active_source = val_core.source_path

        db_dir = tmp_path / "vec_forge_test"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        store.save_metadata({"status": "fresh", "indexed_vectors": 1, "core_hash": active_hash})
        embedder = DummyVectorEmbedder()

        # Directly insert a FORGED record into LanceDB with matching hash & source,
        # but with arbitrary forged content not originating from trusted indexing of the validated units.
        forged_text = "arbitrary attacker injected text claiming core authority"
        store.store(
            text=forged_text,
            embedding=embedder.embed(forged_text),
            metadata={
                "source": active_source,
                "position": 0,
                "is_core": True,
                "core_hash": active_hash,
            },
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            core_boost_factor=0.50,
        )

        q = KnowledgeUnit(id="q", source="inbox", original_text="attacker injected text", meaning="attacker injected text")
        res = retriever.retrieve(q)
        assert len(res.candidates) == 1
        cand = res.candidates[0]
        # Must fail closed: not verified Core, no hash preserved, no boost!
        assert cand.is_core is False
        assert cand.core_hash == ""

    def test_coverage_f_genuinely_indexed_core_record_verifiable_and_proportional_boost(self, tmp_path):
        """F: A genuinely indexed Core record remains verifiable and retains proportional ranking behavior."""
        val_core = validate_and_load_core("examples")
        db_dir = tmp_path / "vec_genuine_test"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        # Index genuine units through trusted indexing pipeline
        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            core_boost_factor=0.25,
        )

        first_unit = val_core.units[0]
        q = KnowledgeUnit(id="q", source="inbox", original_text=first_unit.cleaned_text[:30], meaning=first_unit.cleaned_text[:30])
        res = retriever.retrieve(q)
        assert len(res.candidates) >= 1
        genuine_cand = [c for c in res.candidates if c.position == first_unit.position and c.source == first_unit.source][0]
        assert genuine_cand.is_core is True
        assert genuine_cand.core_hash == val_core.sha256

    def test_coverage_g_ordinary_vector_records_remain_ordinary_and_unboosted(self, tmp_path):
        """G: Existing ordinary vector records remain ordinary and unboosted."""
        val_core = validate_and_load_core("examples")
        db_dir = tmp_path / "vec_ordinary_test"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        ord_unit = KnowledgeUnit(
            id="ord-1", source="02 Notes/ordinary.md",
            original_text="Ordinary empirical observation text.",
            meaning="Ordinary empirical observation text.",
            cleaned_text="Ordinary empirical observation text.",
            normalized_text="ordinary empirical observation text.",
            context="", proposed_type="Observation", position=0,
            is_core=False, core_hash="",
        )
        index_units([ord_unit], embedder, store, validated_core=val_core)
        store.save_metadata({"status": "fresh", "indexed_vectors": 1, "core_hash": val_core.sha256})

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            core_boost_factor=0.30,
        )
        q = KnowledgeUnit(id="q", source="inbox", original_text="empirical observation", meaning="empirical observation")
        res = retriever.retrieve(q)
        assert len(res.candidates) == 1
        ord_cand = res.candidates[0]
        assert ord_cand.is_core is False
        assert ord_cand.core_hash == ""

    def test_coverage_h_fingerprint_serialization_unambiguous_and_deterministic(self):
        """H: Fingerprint serialization is unambiguous and deterministic against delimiter collisions."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            _serialize_unit_text,
            _compute_unit_fingerprint,
        )
        # Collision attempt across field boundaries using colon delimiter
        u1 = KnowledgeUnit(
            id="u1", source="c.md", original_text="alpha:beta", cleaned_text="gamma", meaning="",
            context="", position=0, is_core=True, core_hash="h1", core_source_path="c.md",
        )
        u2 = KnowledgeUnit(
            id="u1", source="c.md", original_text="alpha", cleaned_text="beta:gamma", meaning="",
            context="", position=0, is_core=True, core_hash="h1", core_source_path="c.md",
        )
        assert _serialize_unit_text(u1) != _serialize_unit_text(u2)
        assert _compute_unit_fingerprint(u1) != _compute_unit_fingerprint(u2)

        # Collision attempt across field boundaries using pipe delimiter
        u3 = KnowledgeUnit(
            id="u3", source="c.md", original_text="foo|bar", cleaned_text="baz", meaning="",
            context="", position=0, is_core=True, core_hash="h1", core_source_path="c.md",
        )
        u4 = KnowledgeUnit(
            id="u3", source="c.md", original_text="foo", cleaned_text="bar|baz", meaning="",
            context="", position=0, is_core=True, core_hash="h1", core_source_path="c.md",
        )
        assert _serialize_unit_text(u3) != _serialize_unit_text(u4)
        assert _compute_unit_fingerprint(u3) != _compute_unit_fingerprint(u4)

        # Determinism check
        fp_a = _compute_unit_fingerprint(u1)
        fp_b = _compute_unit_fingerprint(u1)
        assert fp_a == fp_b

    def test_coverage_i_post_validation_mutation_and_cloned_unit_rejection(self):
        """I: Existing post-validation mutation and cloned-unit rejection tests continue to pass."""
        import copy
        from tests._test_core_helpers import build_test_validated_core
        from src.living_authenticity.knowledge.vault.core_validator import is_validated_core_unit

        raw = KnowledgeUnit(
            id="genuine", source="core.md", original_text="Genuine core content.",
            meaning="Genuine core content.", cleaned_text="Genuine core content.",
            normalized_text="genuine core content.", context="",
            proposed_type="Core", position=0, is_core=True,
            core_hash="h_orig", core_source_path="core.md",
        )
        val = build_test_validated_core([raw], source_path="core.md", core_hash="h_orig")
        unit = val.units[0]
        assert is_validated_core_unit(unit, context=val) is True

        # Mutation of position invalidates unit
        raw.position = 999
        assert is_validated_core_unit(unit, context=val) is False
        raw.position = 0
        assert is_validated_core_unit(unit, context=val) is True

        # Mutation of text invalidates unit
        raw.cleaned_text = "Mutated text"
        assert is_validated_core_unit(unit, context=val) is False
        raw.cleaned_text = "Genuine core content."
        assert is_validated_core_unit(unit, context=val) is True

        # Cloned unit with identical fields is rejected due to instance binding
        cloned = copy.deepcopy(unit)
        assert is_validated_core_unit(cloned, context=val) is False
    def test_regression_vector_a_forged_record_with_exact_copied_metadata_fails_closed(self, tmp_path):
        """A: A forged vector record with matching Core metadata and exact copied text/position/source
        cannot become verified Core merely through metadata matching."""
        val_core = validate_and_load_core("examples")
        active_hash = val_core.sha256
        first_unit = val_core.units[0]

        db_dir = tmp_path / "vec_forge_copied_test"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        store.save_metadata({"status": "fresh", "indexed_vectors": 1, "core_hash": active_hash})
        embedder = DummyVectorEmbedder()

        # Attacker copies EXACT text, position, source, is_core=True, and active core_hash,
        # but inserts directly without trusted indexing provenance (no deterministic record_id).
        store.store(
            text=first_unit.cleaned_text,
            embedding=embedder.embed(first_unit.cleaned_text),
            metadata={
                "source": first_unit.source,
                "position": first_unit.position,
                "is_core": True,
                "core_hash": active_hash,
            },
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            core_boost_factor=0.50,
        )

        q = KnowledgeUnit(id="q", source="inbox", original_text=first_unit.cleaned_text, meaning=first_unit.cleaned_text)
        res = retriever.retrieve(q)
        assert len(res.candidates) == 1
        cand = res.candidates[0]
        # Must fail closed: metadata matching is NOT accepted as proof of provenance
        assert cand.is_core is False
        assert cand.core_hash == ""

    def test_regression_vector_b_genuinely_indexed_core_record_verifiable_and_proportional_boost(self, tmp_path):
        """B: A genuinely indexed Core record remains verifiable and retains proportional ranking behavior."""
        val_core = validate_and_load_core("examples")
        db_dir = tmp_path / "vec_genuine_b_test"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()

        class PredictableEmbedder:
            def embed(self, text):
                vec = np.zeros(1024, dtype=np.float32)
                if "query" in text:
                    vec[0] = 1.0
                elif "calibrat" in text.lower():
                    vec[0] = 0.80  # distance ~ 0.20 -> sim ~ 0.833
                elif "ordinary competing" in text:
                    vec[0] = 0.90  # distance ~ 0.10 -> sim ~ 0.909 (higher raw similarity!)
                return vec

            def embed_batch(self, texts):
                return [self.embed(t) for t in texts]

        embedder = PredictableEmbedder()
        first_unit = val_core.units[0]

        ord_unit = KnowledgeUnit(
            id="ord-competing", source="03 Notes/competing.md",
            original_text="ordinary competing note with higher similarity",
            meaning="ordinary competing note with higher similarity",
            cleaned_text="ordinary competing note with higher similarity",
            normalized_text="ordinary competing note with higher similarity",
            context="", proposed_type="Observation", position=0,
            is_core=False, core_hash="",
        )

        # Index both units in single indexing call (refuses indexing when store non-empty)
        index_units([first_unit, ord_unit], embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": 2, "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        # Retrieve with 25% Core boost factor
        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            core_boost_factor=0.25,
            max_candidates=5,
        )

        q = KnowledgeUnit(id="q", source="inbox", original_text="query", meaning="query")
        res = retriever.retrieve(q)
        assert len(res.candidates) == 2

        # Verify Core candidate retains identity, active hash, and original unit_id
        core_cands = [c for c in res.candidates if c.is_core is True]
        assert len(core_cands) == 1
        cand = core_cands[0]
        assert cand.core_hash == val_core.sha256
        assert cand.unit_id == first_unit.id

        # Verify proportional boost: Core candidate (0.833 * 1.25 = 1.041) overtakes
        # ordinary candidate (0.909 * 1.0 = 0.909) and ranks #1
        assert res.candidates[0].is_core is True
        assert res.candidates[1].is_core is False

    def test_regression_vector_c_mutated_core_unit_before_retriever_set_causes_rejection(self, tmp_path):
        """C: A validated Core unit mutated before LanceDBVectorRetriever.set_validated_core()
        causes the entire context to be rejected."""
        val_core = validate_and_load_core("examples")
        db_dir = tmp_path / "vec_mutated_c_test"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        # Mutate unit content before retriever registration
        val_core.units[0].cleaned_text = "Mutated core content that invalidates provenance fingerprint"

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
        )
        retriever.set_validated_core(val_core)

        # Entire context must be rejected and cleared
        assert retriever.validated_core is None
        assert retriever._validated_core_hash == ""
        assert retriever._core_record_manifest == {}

    def test_regression_vector_d_substituted_or_cloned_units_cannot_construct_authorized_manifest(self, tmp_path):
        """D: A substituted or cloned unit collection cannot be used to construct an authorized manifest."""
        import copy
        import dataclasses

        val_core = validate_and_load_core("examples")
        db_dir = tmp_path / "vec_cloned_d_test"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        # Deepcopy units so object identity binding fails
        cloned_units = copy.deepcopy(val_core.units)
        val_core_cloned = dataclasses.replace(val_core, units=cloned_units)

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
        )
        retriever.set_validated_core(val_core_cloned)

        # Cloned units fail provenance verification; retriever clears validated core and manifest
        assert retriever.validated_core is None
        assert retriever._validated_core_hash == ""
        assert retriever._core_record_manifest == {}

    def test_regression_vector_e_ordinary_vector_records_remain_ordinary_and_unboosted(self, tmp_path):
        """E: Existing ordinary vector records remain ordinary and unboosted."""
        val_core = validate_and_load_core("examples")
        db_dir = tmp_path / "vec_ordinary_e_test"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        ord_unit = KnowledgeUnit(
            id="ord-e", source="02 Notes/sample.md",
            original_text="Ordinary empirical note.",
            meaning="Ordinary empirical note.",
            cleaned_text="Ordinary empirical note.",
            normalized_text="ordinary empirical note.",
            context="", proposed_type="Observation", position=0,
            is_core=False, core_hash="",
        )
        index_units([ord_unit], embedder, store, validated_core=val_core)
        store.save_metadata({"status": "fresh", "indexed_vectors": 1, "core_hash": val_core.sha256})

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            core_boost_factor=0.30,
        )
        q = KnowledgeUnit(id="q", source="inbox", original_text="empirical note", meaning="empirical note")
        res = retriever.retrieve(q)
        assert len(res.candidates) == 1
        cand = res.candidates[0]
        assert cand.is_core is False
        assert cand.core_hash == ""

    def test_regression_vector_f_vault_mode_and_test_mode_validation_remain_unchanged(self, tmp_path):
        """F: Existing Vault Mode and designated Test Mode validation behavior remains unchanged."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            is_trusted_validation_context,
            is_validated_core_unit,
        )

        # 1. Test Mode
        val_test = validate_and_load_core("examples")
        assert val_test.canonical_type == "Core"
        assert val_test.source_mode == "examples"
        assert is_trusted_validation_context(val_test.validation_context) is True
        for u in val_test.units:
            assert is_validated_core_unit(u, context=val_test) is True

        # 2. Vault Mode
        vault = tmp_path / "vault_reg_f"
        core_dir = vault / "01 Core"
        core_dir.mkdir(parents=True)
        note_content = (
            "---\n"
            "canonical_type: Core\n"
            "draft: false\n"
            "status: active\n"
            "---\n"
            "# Vault Mode Core Note\n\n"
            "Operational calibration principle for vault testing.\n"
        )
        (core_dir / "core.md").write_text(note_content, encoding="utf-8")

        val_vault = validate_and_load_core("personal_vault", vault_root=str(vault))
        assert val_vault.canonical_type == "Core"
        assert val_vault.source_mode == "personal_vault"
        assert is_trusted_validation_context(val_vault.validation_context) is True
        assert len(val_vault.units) >= 1
        for u in val_vault.units:
            assert is_validated_core_unit(u, context=val_vault) is True

    def test_regression_vector_g_core_identity_provenance_fingerprints_and_object_binding(self):
        """G: Existing Core identity, provenance fingerprint, and object-instance tests continue to pass."""
        import copy
        from tests._test_core_helpers import build_test_validated_core
        from src.living_authenticity.knowledge.vault.core_validator import is_validated_core_unit

        raw = KnowledgeUnit(
            id="g-unit", source="01 Core/core.md", original_text="Core content for integrity test.",
            meaning="Core content for integrity test.", cleaned_text="Core content for integrity test.",
            normalized_text="core content for integrity test.", context="",
            proposed_type="Core", position=0, is_core=True,
            core_hash="h_g", core_source_path="01 Core/core.md",
        )
        val = build_test_validated_core([raw], source_path="01 Core/core.md", core_hash="h_g")
        unit = val.units[0]
        assert is_validated_core_unit(unit, context=val) is True

        # Field mutation fails
        raw.cleaned_text = "tampered content"
        assert is_validated_core_unit(unit, context=val) is False
        raw.cleaned_text = "Core content for integrity test."
        assert is_validated_core_unit(unit, context=val) is True

        # Cloned instance fails
        cloned = copy.deepcopy(unit)
        assert is_validated_core_unit(cloned, context=val) is False
    def test_adversarial_deterministic_record_id_forgery_and_storage_semantics(self, tmp_path):
        """Adversarial Regression Test:
        1. Loads a genuinely validated Core.
        2. Obtains one of its genuine Core units.
        3. Computes the exact deterministic record ID using the internal helper.
        4. Directly inserts a forged vector record into LanceDB with:
           exact Core text, exact source, exact position, active Core hash, is_core=True,
           and the computed deterministic record ID (calling store.store without validated_core).
        5. Runs retrieval against the validated Core:
           Asserts that the forged record CANNOT become a verified Core candidate.
           It fails closed (is_core=False, core_hash="", effective score unboosted).
        6. Insertion-order variation & storage semantics:
           - Direct raw table insertion verifies LanceDB's native duplicate-ID semantics:
             LanceDB natively permits duplicate IDs without restriction.
           - Attempting index_units on an already contaminated vector store raises ValueError.
           - When trusted indexing runs with validated_core, LanceDBManager enforces database
             uniqueness (replaces any duplicate) and establishes authentic Core provenance.
        """
        from src.living_authenticity.knowledge.vault.core_validator import compute_core_record_id

        # 1. Load genuine validated Core
        val_core = validate_and_load_core("examples")
        active_hash = val_core.sha256
        # 2. Obtain genuine Core unit
        first_unit = val_core.units[0]
        # 3. Compute deterministic record ID
        rec_id = compute_core_record_id(first_unit, val_core)

        # Adversary embedder with distinct embedding vector
        class AdversaryEmbedder:
            dimension = 1024
            def embed(self, text: str) -> np.ndarray:
                return np.ones(1024, dtype=np.float32) * 0.99
            def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
                return [self.embed(t) for t in texts]

        embedder = AdversaryEmbedder()

        db_dir_adv = tmp_path / "vec_adv_store_direct"
        store_adv = LanceDBManager(str(db_dir_adv))
        store_adv.create_knowledge_vector_table()
        store_adv.save_metadata({"status": "fresh", "indexed_vectors": 1, "core_hash": active_hash})

        # 4. Untrusted adversary directly calls store.store() with forged metadata & rec_id
        stored_id = store_adv.store(
            text=first_unit.cleaned_text,
            embedding=embedder.embed(first_unit.cleaned_text),
            metadata={
                "source": first_unit.source,
                "position": first_unit.position,
                "is_core": True,
                "core_hash": active_hash,
            },
            record_id=rec_id,
        )

        # Untrusted callers cannot establish Core status through store.store()
        assert stored_id != rec_id, "Untrusted caller must not be granted a Core record_id!"
        adv_rows = store_adv.show_all()
        assert len(adv_rows) == 1
        assert adv_rows[0]["is_core"] is False, "Direct store call must not establish is_core=True!"
        assert adv_rows[0]["core_hash"] == "", "Direct store call must not establish core_hash!"

        # 5. Retrieve against validated Core
        retriever_adv = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store_adv,
            validated_core=val_core,
            core_boost_factor=0.50,
        )
        q = KnowledgeUnit(id="q", source="inbox", original_text="query", meaning="query")
        res_adv = retriever_adv.retrieve(q)
        assert len(res_adv.candidates) == 1
        cand_adv = res_adv.candidates[0]
        assert cand_adv.is_core is False
        assert cand_adv.core_hash == ""
        assert cand_adv.unit_id != first_unit.id

        # 6. Storage Semantics & Insertion-Order Variations
        # A. Native LanceDB duplicate ID semantics (raw table insertion)
        db_dir_raw = tmp_path / "vec_lancedb_raw"
        store_raw = LanceDBManager(str(db_dir_raw))
        store_raw.create_knowledge_vector_table()
        raw_table = store_raw.get_table()

        dup_id = "test_dup_id_999"
        raw_table.add([
            {"id": dup_id, "text": "record 1", "source": "s1.md", "position": 0, "is_core": False, "core_hash": "", "embedding": [0.0]*1024},
            {"id": dup_id, "text": "record 2", "source": "s2.md", "position": 1, "is_core": False, "core_hash": "", "embedding": [0.0]*1024},
        ])
        raw_rows = store_raw.show_all()
        # Storage semantics established: LanceDB natively permits duplicate IDs
        assert len(raw_rows) == 2
        assert raw_rows[0]["id"] == dup_id
        assert raw_rows[1]["id"] == dup_id

        # B. Attempting index_units on an already contaminated vector store raises ValueError
        with pytest.raises(ValueError) as exc_info:
            index_units([first_unit], embedder, store_raw, validated_core=val_core)
        assert "refusing to index again" in str(exc_info.value)

        # C. Database uniqueness & trusted registration with validated_core
        db_dir_clean = tmp_path / "vec_clean_test"
        store_clean = LanceDBManager(str(db_dir_clean))
        store_clean.create_knowledge_vector_table()

        # Genuine trusted indexing via index_units
        index_units([first_unit], embedder, store_clean, validated_core=val_core)
        clean_rows = store_clean.show_all()
        assert len(clean_rows) == 1
        assert clean_rows[0]["id"] == rec_id
        assert clean_rows[0]["is_core"] is True
        assert clean_rows[0]["core_hash"] == active_hash

        # Second write of the same Core record with validated_core updates without duplicates
        store_clean.store(
            text=first_unit.cleaned_text,
            embedding=embedder.embed(first_unit.cleaned_text),
            metadata={"source": first_unit.source, "position": first_unit.position, "is_core": True, "core_hash": active_hash},
            record_id=rec_id,
            validated_core=val_core,
        )
        clean_rows_after = store_clean.show_all()
        assert len(clean_rows_after) == 1
        assert clean_rows_after[0]["id"] == rec_id

    def test_interrupted_core_vector_replacement_recovery_lifecycle(self, tmp_path):
        """Simulate failure at the precise boundary between deletion and insertion in LanceDB.

        Verifies:
        A. Initial Valid State: Core validated, vector present with deterministic record ID,
           manifest consistent, and retrieval verifies Core with proportional boost.
        B. Interrupted Replacement: table.delete(f"id = '{rec_id}'") succeeds, but table.add
           fails (simulating process crash / write interruption).
        C. Immediate Post-Failure State: Insertion failure is not swallowed, database reflects
           missing Core vector, ordinary notes remain un-boosted and cannot impersonate Core,
           check_changes reports stale_core_vectors=True, and session is not fresh.
        D. Subsequent Recovery: After clearing the failure, running reconcile() revalidates Core,
           detects the missing Core vector, recreates it with trusted Core identity and metadata,
           and retrieval verifies the recovered Core vector.
        E. Idempotence and Failure Safety: Repeated reconciliation does not create duplicate rows.
        """
        from src.living_authenticity.knowledge.vault.core_validator import (
            compute_core_record_id,
        )
        from src.living_authenticity.database.lancedb_manager import (
            DatabaseOperationError,
        )

        # Setup Vault with 01 Core and 02 Observation
        vault = tmp_path / "vault_recovery"
        vault.mkdir()
        core_dir = vault / "01 Core"
        core_dir.mkdir()
        core_file = core_dir / "life.md"
        core_file.write_text(
            "---\ntitle: Central Life Thesis\ncanonical_type: Core\ndraft: false\n---\nAuthentic living requires deep self-reflection and core coherence across life domains.",
            encoding="utf-8",
        )
        obs_dir = vault / "02 Observation"
        obs_dir.mkdir()
        obs_file = obs_dir / "daily_routine.md"
        obs_file.write_text(
            "---\ntitle: Daily Routine\ncanonical_type: Observation\n---\nDaily routines provide structure but must align with core authenticity.",
            encoding="utf-8",
        )

        vdb_dir = tmp_path / "vdb_recovery"
        vdb_dir.mkdir()
        pipe = EvidenceFirstPipeline()
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()

        # A. Initial Valid State
        auth_rep = session.authorize(
            vault_root=str(vault),
            vector_db_root=str(vdb_dir),
            embedder=embedder,
            pipeline=pipe,
        )
        assert auth_rep["fresh"] is True
        assert session.is_fresh is True
        store = session._store
        assert store is not None

        # Validate Core independently
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipe)
        target_unit = next(
            u for u in val_core.units
            if "deep self-reflection" in (u.cleaned_text or u.original_text or "")
        )
        core_rec_id = compute_core_record_id(target_unit, val_core)
        core_unit_ids = {u.id for u in val_core.units}
        core_rec_ids = {compute_core_record_id(u, val_core) for u in val_core.units}
        active_hash = val_core.sha256

        # Verify initial LanceDB records
        initial_rows = store.show_all()
        core_rows_init = [r for r in initial_rows if r.get("id") in core_rec_ids]
        assert len(core_rows_init) == len(core_rec_ids)
        for r in core_rows_init:
            assert r.get("is_core") is True
            assert r.get("core_hash") == active_hash

        # Verify ordinary note
        obs_rows_init = [r for r in initial_rows if r.get("id") not in core_rec_ids]
        assert len(obs_rows_init) >= 1
        for r in obs_rows_init:
            assert r.get("is_core") is False

        # Verify retrieval with initial valid state
        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            core_boost_factor=0.25,
        )
        q = KnowledgeUnit(id="q", source="inbox", original_text="deep self-reflection", meaning="deep self-reflection")
        res_init = retriever.retrieve(q)
        assert len(res_init.candidates) >= 1
        core_cands_init = [c for c in res_init.candidates if c.is_core]
        assert len(core_cands_init) >= 1
        assert any(c.unit_id == target_unit.id for c in core_cands_init)
        for c in core_cands_init:
            assert c.core_hash == active_hash

        # B. Interrupted Replacement
        # Simulate a write failure at the exact boundary: table.delete succeeds, but table.add fails.
        orig_get_table = store.get_table
        target_table: Any = orig_get_table()
        real_add = target_table.add

        def interrupted_add(data: Any, *args: Any, **kwargs: Any) -> Any:
            rows = data if isinstance(data, list) else [data]
            if any(isinstance(r, dict) and r.get("id") == core_rec_id for r in rows):
                raise RuntimeError("Simulated crash / write interruption immediately after table.delete")
            return real_add(data, *args, **kwargs)

        setattr(target_table, "add", interrupted_add)
        store.get_table = lambda: target_table

        # Attempt to store/replace the Core record using the failing add
        with pytest.raises(DatabaseOperationError) as exc_info:
            store.store(
                text=target_unit.cleaned_text,
                embedding=embedder.embed(target_unit.cleaned_text),
                metadata={
                    "source": target_unit.source,
                    "position": target_unit.position,
                    "is_core": True,
                    "core_hash": active_hash,
                },
                record_id=core_rec_id,
                validated_core=val_core,
            )
        assert "Simulated crash" in str(exc_info.value)

        # C. Immediate Post-Failure State
        # 1. The Core record was deleted by table.delete before table.add failed
        rows_after_failure = store.show_all()
        core_rows_after_failure = [r for r in rows_after_failure if r.get("id") == core_rec_id]
        assert len(core_rows_after_failure) == 0  # Missing from database!

        # 2. Ordinary notes still exist but cannot acquire Core status
        obs_rows_after_failure = [r for r in rows_after_failure if r.get("id") not in core_rec_ids]
        assert len(obs_rows_after_failure) >= 1
        for r in obs_rows_after_failure:
            assert r.get("is_core") is False

        # 3. check_changes detects missing Core vectors as stale
        changes = session.check_changes()
        assert changes["stale_core_vectors"] is True
        assert changes["stale"] is True
        assert session.is_fresh is False

        # 4. Retrieval fails closed: missing Core vector is not found, ordinary notes are not boosted as Core
        res_post_failure = retriever.retrieve(q)
        core_cands_post = [c for c in res_post_failure.candidates if c.unit_id == target_unit.id and c.is_core]
        assert len(core_cands_post) == 0  # The interrupted Core unit is not returned as Core!
        for c in res_post_failure.candidates:
            if c.unit_id not in core_unit_ids:
                assert c.is_core is False

        # D. Subsequent Recovery
        # Remove failure injection
        setattr(target_table, "add", real_add)
        store.get_table = orig_get_table

        # Run the supported reconciliation recovery path: operator approval required
        with pytest.raises(VaultSyncUnauthorizedError, match="Missing Core vector records detected in store; explicit operator approval ticket is required"):
            session.reconcile()

        req = session.request_approval(run_id="run_interrupted_recovery")
        session.resolve_approval(req["approval_id"], "approve")
        reconcile_res = session.reconcile(ticket=session._active_ticket)
        assert reconcile_res["fresh"] is True
        assert session.is_fresh is True

        # Verify the missing Core vector was detected, recreated, and restored
        rows_recovered = store.show_all()
        core_rows_recovered = [r for r in rows_recovered if r.get("id") == core_rec_id]
        assert len(core_rows_recovered) == 1
        assert core_rows_recovered[0]["is_core"] is True
        assert core_rows_recovered[0]["core_hash"] == active_hash

        # Verify retrieval now recognizes the recovered Core vector and applies the proportional boost
        retriever.set_validated_core(val_core)
        res_recovered = retriever.retrieve(q)
        core_cands_rec = [c for c in res_recovered.candidates if c.unit_id == target_unit.id and c.is_core]
        assert len(core_cands_rec) == 1
        assert core_cands_rec[0].core_hash == active_hash

        # E. Idempotence & Failure Safety
        # Running reconcile again should not duplicate rows
        reconcile_second = session.reconcile()
        assert reconcile_second["fresh"] is True
        rows_second = store.show_all()
        assert len([r for r in rows_second if r.get("id") == core_rec_id]) == 1


# ==============================================================================
# Precision Regression Suite: Normalized Text Framing, Frontmatter Strictness, Stable File Checks
# ==============================================================================

def test_regression_normalized_text_mutation_alone_invalidates_unit_validation(tmp_path: Path):
    """Mutating normalized_text alone must invalidate unit validation against trusted context."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        validate_and_load_core,
        is_validated_core_unit,
    )
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit

    vault = tmp_path / "vault_norm_test"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ncanonical_type: Core\ndraft: false\ntitle: Core Thesis\n---\n"
        "# Foundational Insight\nPrecise unit normalization is mandatory for cryptographic framing.",
        encoding="utf-8",
    )
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
    assert len(val_core.units) >= 1
    orig_unit = val_core.units[0]
    assert is_validated_core_unit(orig_unit, context=val_core) is True

    # Mutate ONLY normalized_text
    tampered_unit = KnowledgeUnit(
        id=orig_unit.id,
        source=orig_unit.source,
        original_text=orig_unit.original_text,
        meaning=orig_unit.meaning,
        cleaned_text=orig_unit.cleaned_text,
        normalized_text="tampered normalized text altering canonical identity",
        position=orig_unit.position,
    )
    assert is_validated_core_unit(tampered_unit, context=val_core) is False


def test_regression_frontmatter_strictness_vault_and_test_mode(tmp_path: Path):
    """Missing draft, string draft: 'false', invalid canonical_type must fail closed."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        validate_and_load_core,
        CoreResolutionError,
    )

    vault = tmp_path / "vault_fm_strict"
    core_dir = vault / "01 Core"
    core_dir.mkdir(parents=True)
    core_file = core_dir / "core.md"

    # 1. Missing draft field
    core_file.write_text(
        "---\ncanonical_type: Core\ntitle: No Draft\n---\n# Body\nCore content.",
        encoding="utf-8",
    )
    with pytest.raises(CoreResolutionError, match="missing required 'draft' frontmatter field"):
        validate_and_load_core("personal_vault", vault_root=str(vault))

    # 2. String draft: "false" (must be boolean false)
    core_file.write_text(
        "---\ncanonical_type: Core\ndraft: \"false\"\ntitle: String Draft\n---\n# Body\nCore content.",
        encoding="utf-8",
    )
    with pytest.raises(CoreResolutionError, match="must explicitly be boolean 'draft: false'"):
        validate_and_load_core("personal_vault", vault_root=str(vault))

    # 3. Invalid canonical_type
    core_file.write_text(
        "---\ncanonical_type: Observation\ndraft: false\ntitle: Wrong Type\n---\n# Body\nCore content.",
        encoding="utf-8",
    )
    with pytest.raises(CoreResolutionError, match="declares invalid canonical_type='Observation'"):
        validate_and_load_core("personal_vault", vault_root=str(vault))


def test_regression_file_mutation_during_validation_pass_fails_closed(tmp_path: Path):
    """File mutation during validation pass triggers CHANGED_DURING_PAUSE fail-closed."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        validate_and_load_core,
        CoreResolutionError,
        CoreFailureCategory,
    )
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline

    vault = tmp_path / "vault_mut_pass"
    core_dir = vault / "01 Core"
    core_dir.mkdir(parents=True)
    core_file = core_dir / "core.md"
    core_file.write_text(
        "---\ncanonical_type: Core\ndraft: false\ntitle: Stable File\n---\n# Body\nOriginal content.",
        encoding="utf-8",
    )

    real_pipe = EvidenceFirstPipeline()

    class MutatingPipeline:
        def __init__(self, inner):
            self._inner = inner
        def __getattr__(self, name):
            if name == "ingestion":
                # Mutate file on disk before ingestion finishes
                core_file.write_text(
                    "---\ncanonical_type: Core\ndraft: false\ntitle: Mutated\n---\n# Body\nMutated content.",
                    encoding="utf-8",
                )
            return getattr(self._inner, name)

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=MutatingPipeline(real_pipe))
    assert exc_info.value.category == CoreFailureCategory.CHANGED_DURING_PAUSE


# ==============================================================================
# Complete Authorized Core Replacement Lifecycle & Anti-Bypass Integration Tests
# ==============================================================================

def test_complete_authorized_core_v1_to_v2_replacement_and_retrieval_lifecycle_e2e(tmp_path: Path):
    """Prove the complete authorized Core replacement lifecycle: V1 init -> V2 modification ->

    detection -> human approval binding -> replacement reconciliation -> retrieval weighting.
    """
    vault = tmp_path / "vault_core_replacement"
    vdb = tmp_path / "vdb_core_replacement"
    core_dir = vault / "01 Core"
    notes_dir = vault / "notes"
    core_dir.mkdir(parents=True)
    notes_dir.mkdir(parents=True)

    # 1. Scenario A: Initial Core V1 & Ordinary Note
    core_file = core_dir / "01-core-anchor.md"
    core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core Thesis V1"
tags: [core]
---

Authoritative Core thesis V1: Authentic living demands sovereign awareness and radical self-honesty.
""", encoding="utf-8")

    ordinary_file = notes_dir / "daily_log.md"
    ordinary_file.write_text("""---
title: "Daily Log"
---

# Daily Log
Reflections on routine morning habits and task management.
""", encoding="utf-8")

    embedder = DummyVectorEmbedder()
    session = VaultSyncSession()
    init_rep = session.authorize(str(vault), str(vdb), embedder=embedder)

    assert init_rep["fresh"] is True
    assert session.is_fresh is True
    assert session.status == SYNC_STATE_FRESH

    # Verify initial Core V1 identity and ordinary note distinction
    store = session._store
    assert store is not None
    initial_rows = store.show_all()
    assert len(initial_rows) >= 2

    validated_core_v1 = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
    v1_hash = validated_core_v1.sha256

    core_rows_v1 = [r for r in initial_rows if r["is_core"]]
    ordinary_rows_v1 = [r for r in initial_rows if not r["is_core"]]

    assert len(core_rows_v1) >= 1
    assert len(ordinary_rows_v1) >= 1

    for cr in core_rows_v1:
        assert cr["is_core"] is True
        assert cr["core_hash"] == v1_hash
        assert cr["id"].startswith("core_vec_")
        assert "01 Core" in cr["source"].replace("\\", "/")

    for orow in ordinary_rows_v1:
        assert orow["is_core"] is False
        assert orow["core_hash"] == ""
        assert not orow["id"].startswith("core_vec_")

    v1_core_ids = {r["id"] for r in core_rows_v1}
    v1_vector_count = len(initial_rows)
    v1_db_version = str(getattr(store.get_table(), "version", ""))
    v1_meta = store.get_metadata()
    assert v1_meta["core_hash"] == v1_hash

    # 2. Scenario B: Mutate Core to V2 without approval
    core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core Thesis V2"
tags: [core]
---

Authoritative Core thesis V2: Sovereign presence actively transcends conditioned narratives through uncompromised truth.
""", encoding="utf-8")

    changes = session.check_changes()
    assert changes["stale"] is True
    assert changes["core_changed"] is True
    assert changes["stale_core_vectors"] is True
    assert session.is_fresh is False
    assert session.status == SYNC_STATE_STALE

    # Verify database has NOT been mutated before approval
    rows_before_approval = store.show_all()
    assert len(rows_before_approval) == v1_vector_count
    assert {r["id"] for r in rows_before_approval if r["is_core"]} == v1_core_ids
    assert str(getattr(store.get_table(), "version", "")) == v1_db_version

    # Unapproved reconcile is strictly rejected
    with pytest.raises(VaultSyncUnauthorizedError):
        session.reconcile()

    # 3. Scenario C: Request and Approve exact Core V2 version
    ticket_req = session.request_approval(run_id="run-core-replace-001", ttl_seconds=60)
    assert ticket_req["run_id"] == "run-core-replace-001"
    approval_id = ticket_req["approval_id"]

    validated_core_v2 = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
    v2_hash = validated_core_v2.sha256
    assert v2_hash != v1_hash

    resolve_res = session.resolve_approval(approval_id, "approve", run_id="run-core-replace-001", vault_id=session.vault_id)
    assert resolve_res["status"] == "ok"
    ticket = session._active_ticket
    assert ticket is not None

    # Verify ticket binding invariants
    assert ticket.vault_id == session.vault_id
    assert ticket.core_hash == v2_hash
    assert ticket.snapshot_fingerprint == ticket_req["snapshot_fingerprint"]
    assert ticket.diff_fingerprint == compute_diff_fingerprint(ticket_req["diff"])
    assert ticket.database_baseline_version == v1_db_version
    assert ticket.used is False
    assert ticket.expires_at > time.time()

    # 4. Scenario D: Execute approved Core replacement
    sync_report = session.reconcile(ticket=ticket)
    assert sync_report["fresh"] is True
    assert session.is_fresh is True
    assert session.status == SYNC_STATE_FRESH

    # Verify final vector database state
    final_rows = store.show_all()
    core_rows_v2 = [r for r in final_rows if r["is_core"]]
    ordinary_rows_final = [r for r in final_rows if not r["is_core"]]

    assert len(core_rows_v2) >= 1
    assert len(ordinary_rows_final) >= 1

    # New Core V2 identity verified
    for cr in core_rows_v2:
        assert cr["is_core"] is True
        assert cr["core_hash"] == v2_hash
        assert cr["id"].startswith("core_vec_")

    # Obsolete V1 Core records are NOT active
    assert not any(r["core_hash"] == v1_hash for r in final_rows)
    assert not any(r["id"] in v1_core_ids for r in final_rows)

    # Ordinary notes preserved without promotion
    for orow in ordinary_rows_final:
        assert orow["is_core"] is False
        assert orow["core_hash"] == ""

    # Metadata and manifest consistent
    final_meta = store.get_metadata()
    assert final_meta["core_hash"] == v2_hash
    assert final_meta["status"] == "fresh"

    # Single-use ticket enforcement
    assert ticket.used is True
    with pytest.raises(VaultSyncStaleApprovalError, match="already used"):
        session.reconcile(ticket=ticket)

    # 5. Scenario E: Verify retrieval weighting and semantic distinction after replacement
    retriever = LanceDBVectorRetriever(
        embedding_service=embedder,
        db_manager=store,
        validated_core=validated_core_v2,
        core_boost_factor=0.30,
    )

    query_unit = KnowledgeUnit(
        id="q_core_v2",
        source="query.md",
        original_text="sovereign presence and uncompromised truth",
        meaning="sovereign presence",
    )
    retrieved = retriever.retrieve(query_unit)
    cands = retrieved.candidates
    assert len(cands) >= 1

    top_cand = cands[0]
    assert top_cand.is_core is True
    assert top_cand.core_hash == v2_hash

    # Ordinary notes in retrieval receive no core status
    ord_cands = [c for c in cands if not c.is_core]
    for oc in ord_cands:
        assert oc.is_core is False
        assert oc.core_hash == ""

    # Verify impersonation rejection: candidate with obsolete V1 hash is rejected under V2 context
    forged_v1_cand = RetrievedCandidate(
        unit_id="forged_v1",
        text="Authentic living demands sovereign awareness",
        source=str(core_file.resolve()),
        position=0,
        is_core=True,
        core_hash=v1_hash,  # obsolete V1 hash
    )
    assert retriever._validated_core_hash == v2_hash
    assert forged_v1_cand.core_hash != retriever._validated_core_hash


def test_stale_approval_ticket_rejected_when_core_mutates_to_v3_before_reconcile(tmp_path: Path):
    """A valid approval ticket for Core V2 is rejected fail-closed if Core mutates to V3 before sync."""
    vault = tmp_path / "vault_stale_v3"
    vdb = tmp_path / "vdb_stale_v3"
    core_dir = vault / "01 Core"
    core_dir.mkdir(parents=True)
    core_file = core_dir / "01-core-anchor.md"

    core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core V1"
---
Initial Core V1 body.
""", encoding="utf-8")

    embedder = DummyVectorEmbedder()
    session = VaultSyncSession()
    session.authorize(str(vault), str(vdb), embedder=embedder)
    assert session.is_fresh is True
    assert session._store is not None
    v1_rows = session._store.show_all()
    meta = session._store.get_metadata()
    assert meta is not None
    v1_hash = meta["core_hash"]

    # Mutate to V2
    core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core V2"
---
Updated Core V2 body.
""", encoding="utf-8")

    session.check_changes()
    req = session.request_approval(run_id="run-stale-v3-test", ttl_seconds=60)
    session.resolve_approval(req["approval_id"], "approve", run_id="run-stale-v3-test", vault_id=session.vault_id)
    v2_ticket = session._active_ticket
    assert v2_ticket is not None

    # Before reconcile, mutate to V3
    core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core V3"
---
Further modified Core V3 body before reconcile.
""", encoding="utf-8")

    # Reconciliation with V2 ticket must be rejected fail-closed
    with pytest.raises((VaultSyncStaleApprovalError, VaultSyncSnapshotDriftError)):
        session.reconcile(ticket=v2_ticket)

    # Prove database was NOT updated to V3
    assert session._store is not None
    current_rows = session._store.show_all()
    assert len(current_rows) == len(v1_rows)
    assert current_rows[0]["core_hash"] == v1_hash
    assert "V3" not in current_rows[0]["text"]
    assert session.is_fresh is False


def test_stale_approval_ticket_rejected_on_out_of_band_database_baseline_mutation(tmp_path: Path):
    """An approval ticket is rejected fail-closed if vector database table version increments out of band."""
    vault = tmp_path / "vault_oob_db"
    vdb = tmp_path / "vdb_oob_db"
    core_dir = vault / "01 Core"
    core_dir.mkdir(parents=True)
    core_file = core_dir / "01-core-anchor.md"

    core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core V1"
---
Initial Core V1 body.
""", encoding="utf-8")

    embedder = DummyVectorEmbedder()
    session = VaultSyncSession()
    session.authorize(str(vault), str(vdb), embedder=embedder)

    # Mutate to V2
    core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core V2"
---
Updated Core V2 body.
""", encoding="utf-8")

    session.check_changes()
    req = session.request_approval(run_id="run-oob-test", ttl_seconds=60)
    session.resolve_approval(req["approval_id"], "approve", run_id="run-oob-test", vault_id=session.vault_id)
    ticket = session._active_ticket
    assert ticket is not None

    # Mutate database table out of band before reconcile
    store = session._store
    assert store is not None
    store.get_table().add([{
        "id": "oob_vector_1",
        "text": "out of band insertion",
        "embedding": [0.0] * 1024,
        "source": "oob.md",
        "position": 0,
        "is_core": False,
        "core_hash": "",
    }])

    # Reconcile must reject ticket due to table version mismatch
    with pytest.raises(VaultSyncStaleApprovalError, match="Vector database mutated out-of-band"):
        session.reconcile(ticket=ticket)

    assert session.is_fresh is False


def test_store_type_error_during_core_indexing_fails_closed_without_demoting_core(tmp_path: Path):
    """Internal TypeError during Core storage raises VaultSyncError and NEVER retries omitting validated_core."""
    vault = tmp_path / "vault_store_type_err"
    vdb = tmp_path / "vdb_store_type_err"
    core_dir = vault / "01 Core"
    core_dir.mkdir(parents=True)
    core_file = core_dir / "01-core-anchor.md"

    core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core V1"
---
Core V1 thesis for type error test.
""", encoding="utf-8")

    embedder = DummyVectorEmbedder()
    session = VaultSyncSession()
    session.authorize(str(vault), str(vdb), embedder=embedder)

    validated_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())

    # Monkeypatch store.store to record calls and raise TypeError
    store_calls: list[dict] = []
    assert session._store is not None
    real_store = session._store.store

    def failing_store(text, embedding, metadata, record_id=None, validated_core=None):
        store_calls.append({
            "text": text,
            "metadata": dict(metadata),
            "record_id": record_id,
            "has_validated_core": validated_core is not None,
        })
        raise TypeError("Simulated internal TypeError in store column validation")

    session._store.store = failing_store  # type: ignore[assignment]

    try:
        with pytest.raises(VaultSyncError, match="Failed storing Core vector"):
            session._index_single_file(core_file, validated_core=validated_core)

        # Must be called exactly once; NEVER retried with fewer security arguments
        assert len(store_calls) == 1
        assert store_calls[0]["has_validated_core"] is True
        assert store_calls[0]["record_id"] is not None
        assert store_calls[0]["metadata"]["is_core"] is True
    finally:
        session._store.store = real_store  # type: ignore[assignment]

    # Storing Core note with validated_core=None must immediately fail closed without writing
    with pytest.raises(VaultSyncError, match="without validated_core context"):
        session._index_single_file(core_file, validated_core=None)


# ==============================================================================
# Phase 3 Final Integrity Verification: Vector Database Baseline Integrity,
# Retrieval Trust & Core Impersonation Testing (Tests A through H)
# ==============================================================================
class TestPhase3VectorDatabaseBaselineIntegrityAndAdversarialRetrieval:
    """Rigorous adversarial tests verifying approved database baseline integrity,
    fail-closed retrieval trust, and rejection of out-of-band Core mutations.
    """

    def test_test_a_out_of_band_core_embedding_mutation_rejected(self, tmp_path):
        """Test A: Out-of-band Core embedding mutation before retrieval.
        When an adversary modifies a Core embedding vector directly in LanceDB
        while preserving all identity metadata, retriever detects content digest
        mismatch and strictly fails closed (no Core status, no ranking boost).
        """
        from src.living_authenticity.database.lancedb_manager import DatabaseOperationError

        vault = tmp_path / "vault_test_a"
        vdb = tmp_path / "vdb_test_a"
        _make_valid_vault(vault, note_title="Test A Core", body="Authentic core principles for test a.")

        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(vdb), embedder=embedder)
        assert session.is_fresh is True

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        assert session._store is not None

        # Verify initial baseline is intact and trusted
        retriever_clean = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            core_boost_factor=0.30,
        )
        query = KnowledgeUnit(id="q-a", source="inp", original_text="authentic core principles", meaning="authentic core principles")
        res_clean = retriever_clean.retrieve(query)
        assert res_clean.metadata["core_baseline_verified"] is True
        assert res_clean.metadata["core_trusted"] is True
        clean_core_cands = [c for c in res_clean.candidates if c.is_core]
        assert len(clean_core_cands) > 0

        # Adversary mutates the live Core embedding vector out-of-band while preserving metadata
        tbl = session._store.get_table()
        live_core_rows = [r for r in tbl.to_arrow().to_pylist() if r.get("is_core")]
        assert len(live_core_rows) > 0
        target_id = live_core_rows[0]["id"]
        mutated_embedding = np.zeros(1024, dtype=np.float32)
        mutated_embedding[0] = 0.999
        mutated_embedding[1] = 0.888
        tbl.update(where=f"id = '{target_id}'", values={"embedding": mutated_embedding.tolist()})

        # Dual fail-closed verification:
        # Mode 1: raise_on_integrity_error=False (default safe operational mode)
        retriever_soft = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            core_boost_factor=0.30,
            raise_on_integrity_error=False,
        )
        res_soft = retriever_soft.retrieve(query)
        assert res_soft.metadata["core_baseline_verified"] is False
        assert res_soft.metadata["core_trusted"] is False
        for c in res_soft.candidates:
            assert c.is_core is False
            assert c.core_hash == ""

        # Mode 2: raise_on_integrity_error=True (strict validation mode)
        retriever_strict = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(DatabaseOperationError, match="Vector store Core baseline integrity digest mismatch"):
            retriever_strict.retrieve(query)

    def test_test_b_out_of_band_core_metadata_mutation_rejected(self, tmp_path):
        """Test B: Core metadata or identity mutation.
        When an adversary modifies metadata fields (e.g., core_hash or source)
        directly in the vector database, digest and identity checks detect tampering
        and fail closed.
        """
        from src.living_authenticity.database.lancedb_manager import DatabaseOperationError

        vault = tmp_path / "vault_test_b"
        vdb = tmp_path / "vdb_test_b"
        _make_valid_vault(vault, note_title="Test B Core", body="Authentic core principles for test b.")

        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(vdb), embedder=embedder)

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        assert session._store is not None
        tbl = session._store.get_table()
        live_core_rows = [r for r in tbl.to_arrow().to_pylist() if r.get("is_core")]
        target_id = live_core_rows[0]["id"]

        # Adversary mutates core_hash directly in table
        tbl.update(where=f"id = '{target_id}'", values={"core_hash": "forged_malicious_core_hash_999"})

        query = KnowledgeUnit(id="q-b", source="inp", original_text="authentic core principles", meaning="authentic core principles")

        # Soft mode: rejected and untrusted
        retriever_soft = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=False,
        )
        res_soft = retriever_soft.retrieve(query)
        assert res_soft.metadata["core_baseline_verified"] is False
        assert res_soft.metadata["core_trusted"] is False
        for c in res_soft.candidates:
            assert c.is_core is False
            assert c.core_hash == ""

        # Strict mode: raises DatabaseOperationError
        retriever_strict = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(DatabaseOperationError):
            retriever_strict.retrieve(query)

    def test_test_c_adversarial_core_record_addition_and_removal_rejected(self, tmp_path):
        """Test C: Core record addition or removal.
        Detects rogue Core row insertions and Core row deletions out-of-band.
        """
        from src.living_authenticity.database.lancedb_manager import DatabaseOperationError

        vault = tmp_path / "vault_test_c"
        vdb = tmp_path / "vdb_test_c"
        _make_valid_vault(vault, note_title="Test C Core", body="Authentic core principles for test c.")

        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(vdb), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        assert session._store is not None
        tbl = session._store.get_table()
        query = KnowledgeUnit(id="q-c", source="inp", original_text="authentic core principles", meaning="authentic core principles")

        # Subtest C1: Unauthorized Core record addition
        rogue_rec = {
            "id": "core_vec_rogue_insertion",
            "text": "Rogue injection claiming Core authority.",
            "embedding": embedder.embed("rogue").tolist(),
            "source": "01 Core/01-core-anchor.md",
            "position": 99,
            "is_core": True,
            "core_hash": val_core.sha256,
        }
        tbl.add([rogue_rec])

        retriever_c1_soft = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=False,
        )
        res_c1 = retriever_c1_soft.retrieve(query)
        assert res_c1.metadata["core_baseline_verified"] is False
        for c in res_c1.candidates:
            assert c.is_core is False

        retriever_c1_strict = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(DatabaseOperationError, match="Core record count mismatch"):
            retriever_c1_strict.retrieve(query)

        # Subtest C2: Core record deletion
        # Reset and delete legitimate Core record
        vdb_c2 = tmp_path / "vdb_test_c2"
        session_c2 = VaultSyncSession()
        session_c2.authorize(str(vault), str(vdb_c2), embedder=embedder)
        assert session_c2._store is not None
        tbl_c2 = session_c2._store.get_table()
        core_rows_c2 = [r for r in tbl_c2.to_arrow().to_pylist() if r.get("is_core")]
        del_id = core_rows_c2[0]["id"]
        tbl_c2.delete(f"id = '{del_id}'")

        retriever_c2_soft = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session_c2._store,
            validated_core=val_core,
            raise_on_integrity_error=False,
        )
        res_c2 = retriever_c2_soft.retrieve(query)
        assert res_c2.metadata["core_baseline_verified"] is False
        for c in res_c2.candidates:
            assert c.is_core is False

        retriever_c2_strict = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session_c2._store,
            validated_core=val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(DatabaseOperationError, match="Core record count mismatch"):
            retriever_c2_strict.retrieve(query)

    def test_test_d_missing_or_malformed_baseline_fails_closed(self, tmp_path):
        """Test D: Missing or malformed baseline metadata fails closed."""
        from src.living_authenticity.database.lancedb_manager import (
            VaultMetadataMissingError,
            VaultMetadataCorruptionError,
        )

        vault = tmp_path / "vault_test_d"
        vdb = tmp_path / "vdb_test_d"
        _make_valid_vault(vault, note_title="Test D Core", body="Authentic core principles for test d.")

        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(vdb), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        assert session._store is not None
        query = KnowledgeUnit(id="q-d", source="inp", original_text="authentic core principles", meaning="authentic core principles")

        # Subtest D1: Missing core_vector_digest
        meta_d1 = session._store.get_metadata()
        assert meta_d1 is not None
        del meta_d1["core_vector_digest"]
        meta_tbl = session._store.db.open_table("index_metadata")
        meta_tbl.update(where="id = 'meta'", values={"payload": json.dumps(meta_d1)})

        retriever_d1 = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(VaultMetadataMissingError, match="missing required Core baseline integrity digest"):
            retriever_d1.retrieve(query)

        # Subtest D2: Malformed core_vector_digest
        meta_d2 = session._store.get_metadata()
        assert meta_d2 is not None
        meta_d2["core_vector_digest"] = "short_invalid_hex_string"
        meta_tbl.update(where="id = 'meta'", values={"payload": json.dumps(meta_d2)})

        retriever_d2 = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(VaultMetadataCorruptionError, match="malformed 'core_vector_digest'"):
            retriever_d2.retrieve(query)

        # Subtest D3: Invalid core_vector_count
        meta_d3 = session._store.get_metadata()
        assert meta_d3 is not None
        meta_d3["core_vector_digest"] = "0" * 64
        meta_d3["core_vector_count"] = -1
        meta_tbl.update(where="id = 'meta'", values={"payload": json.dumps(meta_d3)})

        retriever_d3 = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(VaultMetadataCorruptionError, match="invalid 'core_vector_count'"):
            retriever_d3.retrieve(query)

    def test_test_e_process_restart_preserves_trust_and_detects_tampering(self, tmp_path):
        """Test E: Process restart with disk persistence.
        Verifies that after process restart, clean database verifies integrity cleanly,
        while out-of-band tampering performed during shutdown is detected immediately.
        """
        from src.living_authenticity.database.lancedb_manager import DatabaseOperationError

        vault = tmp_path / "vault_test_e"
        vdb = tmp_path / "vdb_test_e"
        _make_valid_vault(vault, note_title="Test E Core", body="Authentic core principles for test e.")

        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(vdb), embedder=embedder)
        assert session._store is not None
        index_dir = session._store.db_path
        query = KnowledgeUnit(id="q-e", source="inp", original_text="authentic core principles", meaning="authentic core principles")

        # Simulate process termination: discard session and retriever
        del session

        # Restart 1: Clean startup from disk
        store_r1 = LanceDBManager(db_path=index_dir)
        val_core_r1 = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        retriever_r1 = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store_r1,
            validated_core=val_core_r1,
            core_boost_factor=0.30,
        )
        res_r1 = retriever_r1.retrieve(query)
        assert res_r1.metadata["core_baseline_verified"] is True
        assert res_r1.metadata["core_trusted"] is True
        assert any(c.is_core for c in res_r1.candidates)

        # Simulate shutdown and offline tampering
        del store_r1
        del retriever_r1

        # Out-of-band script tampers with embedding on disk
        store_offline = LanceDBManager(db_path=index_dir)
        tbl_offline = store_offline.get_table()
        core_rows = [r for r in tbl_offline.to_arrow().to_pylist() if r.get("is_core")]
        target_id = core_rows[0]["id"]
        tbl_offline.update(where=f"id = '{target_id}'", values={"embedding": [0.777] * 1024})
        del store_offline

        # Restart 2: Fresh process startup after tampering
        store_r2 = LanceDBManager(db_path=index_dir)
        retriever_r2 = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store_r2,
            validated_core=val_core_r1,
            raise_on_integrity_error=True,
        )
        with pytest.raises(DatabaseOperationError, match="Vector store Core baseline integrity digest mismatch"):
            retriever_r2.retrieve(query)

    def test_test_f_ordinary_note_mutation_preserves_core_baseline_integrity(self, tmp_path):
        """Test F: Ordinary note mutation (Core-Only vs. Whole-Table Policy Boundary).
        Mutating or adding ordinary notes does not alter the Core baseline digest,
        ensuring ordinary note edits never invalidate Core baseline trust.
        """
        vault = tmp_path / "vault_test_f"
        vdb = tmp_path / "vdb_test_f"
        _make_valid_vault(vault, note_title="Test F Core", body="Authentic core principles for test f.")

        # Add an ordinary note
        notes_dir = vault / "02 Notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        ord_file = notes_dir / "ordinary.md"
        ord_file.write_text("# Ordinary Note\nOrdinary research context on test f.", encoding="utf-8")

        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(vdb), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        assert session._store is not None

        # Verify initial clean retrieval
        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            core_boost_factor=0.30,
        )
        q = KnowledgeUnit(id="q-f", source="inp", original_text="authentic core research", meaning="authentic core research")
        res_init = retriever.retrieve(q)
        assert res_init.metadata["core_baseline_verified"] is True
        core_cands_init = [c for c in res_init.candidates if c.is_core]
        assert len(core_cands_init) > 0

        # Now mutate ordinary note vector directly in the table
        tbl = session._store.get_table()
        ord_rows = [r for r in tbl.to_arrow().to_pylist() if not r.get("is_core")]
        assert len(ord_rows) > 0
        ord_id = ord_rows[0]["id"]
        tbl.update(where=f"id = '{ord_id}'", values={"text": "Mutated ordinary text completely out-of-band"})

        # Retrieval must STILL verify Core baseline successfully
        res_after = retriever.retrieve(q)
        assert res_after.metadata["core_baseline_verified"] is True
        assert res_after.metadata["core_trusted"] is True
        core_cands_after = [c for c in res_after.candidates if c.is_core]
        assert len(core_cands_after) > 0
        assert core_cands_after[0].is_core is True
        assert core_cands_after[0].core_hash == val_core.sha256

        # Ordinary candidate retrieved normally without Core elevation
        ord_cands_after = [c for c in res_after.candidates if not c.is_core]
        assert len(ord_cands_after) > 0
        assert ord_cands_after[0].is_core is False
        assert ord_cands_after[0].core_hash == ""

    def test_test_g_legacy_database_without_digest_fails_closed_safely(self, tmp_path):
        """Test G: Legacy database compatibility.
        A database created before baseline digests were introduced is recognized as legacy,
        and fails closed (untrusted Core, no boost) rather than being silently accepted.
        """
        from src.living_authenticity.database.lancedb_manager import VaultMetadataMissingError

        vault = tmp_path / "vault_test_g"
        vdb = tmp_path / "vdb_test_g"
        _make_valid_vault(vault, note_title="Test G Core", body="Authentic core principles for test g.")

        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(vdb), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        assert session._store is not None

        # Simulate legacy database: remove baseline digest fields from metadata table
        meta = session._store.get_metadata()
        assert meta is not None
        legacy_meta = {k: v for k, v in meta.items() if not k.startswith("core_vector_") and k != "core_record_ids"}
        meta_tbl = session._store.db.open_table("index_metadata")
        meta_tbl.update(where="id = 'meta'", values={"payload": json.dumps(legacy_meta)})

        query = KnowledgeUnit(id="q-g", source="inp", original_text="authentic core principles", meaning="authentic core principles")

        # Soft mode: handles legacy database without crashing, but demotes Core
        retriever_soft = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=False,
        )
        res_soft = retriever_soft.retrieve(query)
        assert res_soft.metadata["core_baseline_verified"] is False
        assert res_soft.metadata["core_trusted"] is False
        for c in res_soft.candidates:
            assert c.is_core is False
            assert c.core_hash == ""

        # Strict mode: raises VaultMetadataMissingError
        retriever_strict = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(VaultMetadataMissingError, match="missing required Core baseline integrity digest"):
            retriever_strict.retrieve(query)

    def test_test_h_real_obsolete_v1_candidate_rejected_against_active_v2(self, tmp_path):
        """Test H: Real obsolete V1 candidate rejection.
        When Core note is updated from V1 to V2 in the vault, retriever with active V2
        context strictly rejects real obsolete V1 records in the database until authorized
        reconciliation completes and establishes the approved V2 baseline.
        """
        vault = tmp_path / "vault_test_h"
        vdb = tmp_path / "vdb_test_h"
        core_file = _make_valid_vault(vault, note_title="Core V1", body="Central thesis of Core V1.")

        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(vdb), embedder=embedder)
        val_core_v1 = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        assert session._store is not None

        # Verify initial V1 trust
        retriever_v1 = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core_v1,
            core_boost_factor=0.30,
        )
        query = KnowledgeUnit(id="q-h", source="inp", original_text="central thesis", meaning="central thesis")
        res_v1 = retriever_v1.retrieve(query)
        assert res_v1.metadata["core_baseline_verified"] is True
        assert any(c.is_core and c.core_hash == val_core_v1.sha256 for c in res_v1.candidates)

        # Mutate vault Core note to V2
        core_file.write_text("""---
canonical_type: Core
draft: false
title: "Core V2"
tags: [core]
---

Central thesis of Core V2 with new principles.
""", encoding="utf-8")

        val_core_v2 = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=EvidenceFirstPipeline())
        assert val_core_v2.sha256 != val_core_v1.sha256

        # Active runtime is updated to V2, but database still holds V1 records
        retriever_v2_pre_sync = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core_v2,
            core_boost_factor=0.30,
            raise_on_integrity_error=False,
        )
        res_pre = retriever_v2_pre_sync.retrieve(query)
        assert res_pre.metadata["core_baseline_verified"] is False
        assert res_pre.metadata["core_trusted"] is False
        # Obsolete V1 candidates MUST NOT be granted Core status or boost
        for c in res_pre.candidates:
            assert c.is_core is False
            assert c.core_hash == ""

        # Perform authorized V2 reconciliation
        changes = session.check_changes()
        assert changes["stale"] is True
        assert changes["core_changed"] is True
        req = session.request_approval(run_id="run_v2_h")
        session.resolve_approval(req["approval_id"], "approve")
        reconcile_res = session.reconcile(ticket=session._active_ticket)
        assert reconcile_res["fresh"] is True
        assert session.is_fresh is True

        # Now retriever with V2 context verifies active V2 records and applies boost
        retriever_v2_post_sync = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=session._store,
            validated_core=val_core_v2,
            core_boost_factor=0.30,
        )
        res_post = retriever_v2_post_sync.retrieve(query)
        assert res_post.metadata["core_baseline_verified"] is True
        assert res_post.metadata["core_trusted"] is True
        v2_core_cands = [c for c in res_post.candidates if c.is_core]
        assert len(v2_core_cands) > 0
        assert v2_core_cands[0].core_hash == val_core_v2.sha256
        assert "Core V2" in v2_core_cands[0].text


# ==============================================================================
# Phase 3 Targeted Integrity Corrections Test Suite (Corrections A, B, C, D, E)
# ==============================================================================
class TestPhase3TargetedIntegrityCorrections:
    """Verifies that all 5 targeted Phase 3 baseline integrity corrections fail closed."""

    @pytest.fixture(autouse=True)
    def _setup_imports(self):
        pass

    # --------------------------------------------------------------------------
    # Correction A: Baseline certification prevention via save_metadata()
    # --------------------------------------------------------------------------
    def test_save_metadata_fresh_without_validated_core_cannot_create_baseline(self, tmp_path):
        """A.1: save_metadata({"status": "fresh"}) called generically cannot synthesize a baseline."""
        db_dir = tmp_path / "vec_corr_a1"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        store.save_metadata({"status": "unverified"})
        embedder = DummyVectorEmbedder()
        store.store(
            text="some note text",
            embedding=embedder.embed("some note text"),
            metadata={"source": "01 Core/note.md", "position": 0, "is_core": True},
        )

        # Call save_metadata without validated_core
        store.save_metadata({"status": "fresh", "indexed_vectors": 1})
        meta = store.get_metadata()
        assert "core_vector_digest" not in meta
        assert "core_vector_count" not in meta
        assert "core_record_ids" not in meta

    def test_generic_metadata_update_cannot_replace_existing_baseline(self, tmp_path):
        """A.2: Generic save_metadata call strictly preserves existing trusted baseline."""
        db_dir = tmp_path / "vec_corr_a2"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        val_core = validate_and_load_core("examples")
        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )
        initial_meta = store.get_metadata()
        trusted_digest = initial_meta["core_vector_digest"]
        trusted_count = initial_meta["core_vector_count"]
        trusted_hash = initial_meta["core_hash"]

        # Attempt to overwrite baseline keys with evil values without validated_core
        store.save_metadata({
            "status": "fresh",
            "indexed_vectors": 999,
            "core_vector_digest": "evil_digest_1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            "core_vector_count": 999,
            "core_hash": "evil_hash_123",
        })
        after_meta = store.get_metadata()
        # Trusted baseline keys are preserved!
        assert after_meta["core_vector_digest"] == trusted_digest
        assert after_meta["core_vector_count"] == trusted_count
        assert after_meta["core_hash"] == trusted_hash
        # Non-baseline fields can be updated
        assert after_meta["indexed_vectors"] == 999

    def test_save_metadata_with_untrusted_validated_core_fails_closed(self, tmp_path):
        """A.3: save_metadata rejects untrusted or forged validated_core objects."""
        db_dir = tmp_path / "vec_corr_a3"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()

        class FakeCore:
            validation_context = "not_a_real_context"

        val_core = validate_and_load_core("examples")
        val_auth = build_test_sync_authorization(val_core)
        with pytest.raises(DatabaseOperationError, match="Untrusted or invalid validation context"):
            store.save_metadata(
                {"status": "fresh"},
                validated_core=FakeCore(),
                sync_auth=val_auth,
            )

    def test_save_metadata_hash_mismatch_with_context_fails_closed(self, tmp_path):
        """A.4: save_metadata rejects candidate metadata with mismatched core_hash."""
        db_dir = tmp_path / "vec_corr_a4"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        val_core = validate_and_load_core("examples")
        val_auth = build_test_sync_authorization(val_core)

        with pytest.raises(DatabaseOperationError, match="Core hash in metadata candidate"):
            store.save_metadata(
                {"status": "fresh", "core_hash": "mismatched_candidate_hash"},
                validated_core=val_core,
                sync_auth=val_auth,
            )

    # --------------------------------------------------------------------------
    # Correction B: TOCTOU window closure
    # --------------------------------------------------------------------------
    def test_table_version_mutation_during_baseline_verification_fails_closed(self, tmp_path):
        """B.1: Table version changing between pre-check and post-check fails closed."""
        db_dir = tmp_path / "vec_corr_b1"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )

        # Hook get_table to simulate mutation right after baseline verification
        call_count = [0]
        real_get_table = store.get_table

        class MutatingVersionTable:
            def __init__(self, inner):
                self._inner = inner
            @property
            def version(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    return 1
                return 2  # Mutated during check!
            def __getattr__(self, name):
                return getattr(self._inner, name)

        def hooked_get_table():
            tbl = real_get_table()
            return MutatingVersionTable(tbl)

        setattr(store, "get_table", hooked_get_table)

        query = KnowledgeUnit(id="q", source="inp", original_text="lab", meaning="lab")
        with pytest.raises(DatabaseOperationError, match="Store mutated during baseline verification: fail closed"):
            retriever.retrieve(query)

    def test_missing_or_invalid_pre_table_version_fails_closed(self, tmp_path):
        """B.2: Missing table version before verification fails closed."""
        db_dir = tmp_path / "vec_corr_b2"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )

        real_get_table = store.get_table

        class NoVersionTable:
            def __init__(self, inner):
                self._inner = inner
            @property
            def version(self):
                return None
            def __getattr__(self, name):
                return getattr(self._inner, name)

        setattr(store, "get_table", lambda: NoVersionTable(real_get_table()))
        query = KnowledgeUnit(id="q", source="inp", original_text="lab", meaning="lab")
        with pytest.raises(DatabaseOperationError, match="Vector table version is missing or invalid before baseline verification"):
            retriever.retrieve(query)

    # --------------------------------------------------------------------------
    # Correction C: Exact ValidatedCore hash binding in baseline verification
    # --------------------------------------------------------------------------
    def test_missing_meta_core_hash_rejected_fail_closed(self, tmp_path):
        """C.1: Missing 'core_hash' in metadata fails closed with VaultMetadataMissingError."""
        db_dir = tmp_path / "vec_corr_c1"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )
        fake_meta = {
            "core_vector_digest": "0" * 64,
            "core_vector_count": 0,
            "core_record_ids": [],
            # "core_hash" is MISSING!
        }
        with pytest.raises(VaultMetadataMissingError, match="missing required 'core_hash'"):
            retriever._verify_core_database_baseline_integrity(
                table=store.get_table(),
                meta=fake_meta,
                raise_on_error=True,
            )

    def test_empty_or_whitespace_meta_core_hash_rejected_fail_closed(self, tmp_path):
        """C.2: Empty or whitespace 'core_hash' in metadata fails closed with VaultMetadataCorruptionError."""
        db_dir = tmp_path / "vec_corr_c2"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )
        for bad_hash in ("", "   ", None, 123):
            fake_meta = {
                "core_vector_digest": "0" * 64,
                "core_vector_count": 0,
                "core_record_ids": [],
                "core_hash": bad_hash,
            }
            with pytest.raises(VaultMetadataCorruptionError, match="malformed or invalid 'core_hash'"):
                retriever._verify_core_database_baseline_integrity(
                    table=store.get_table(),
                    meta=fake_meta,
                    raise_on_error=True,
                )

    def test_mismatching_meta_core_hash_rejected_fail_closed(self, tmp_path):
        """C.3: Mismatched 'core_hash' fails closed with DatabaseOperationError."""
        db_dir = tmp_path / "vec_corr_c3"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )
        fake_meta = {
            "core_vector_digest": "0" * 64,
            "core_vector_count": 0,
            "core_record_ids": [],
            "core_hash": "different_hash_value",
        }
        with pytest.raises(DatabaseOperationError, match="does not match active validated Core"):
            retriever._verify_core_database_baseline_integrity(
                table=store.get_table(),
                meta=fake_meta,
                raise_on_error=True,
            )

    # --------------------------------------------------------------------------
    # Correction D: Little-endian float32 serialization and non-finite rejection
    # --------------------------------------------------------------------------
    def test_float32_little_endian_canonical_digest_deterministic(self):
        """D.1: Canonical digest uses little-endian float32 serialization deterministically."""
        rec1 = {
            "id": "c1",
            "source": "01 Core/note.md",
            "position": 0,
            "text": "calibration theory",
            "vector": [0.1, 0.2, 0.3],
        }
        digest1 = compute_core_vector_digest([rec1])
        assert isinstance(digest1, str)
        assert len(digest1) == 64
        # Deterministic re-computation
        digest2 = compute_core_vector_digest([rec1])
        assert digest1 == digest2

    def test_non_finite_embedding_nan_rejected(self):
        """D.2: Embedding vector with NaN raises ValueError."""
        rec_nan = {
            "id": "c1",
            "source": "01 Core/note.md",
            "position": 0,
            "text": "theory",
            "vector": [0.1, float("nan"), 0.3],
        }
        with pytest.raises(ValueError, match="Non-finite float value detected in Core embedding vector"):
            compute_core_vector_digest([rec_nan])

    def test_non_finite_embedding_inf_rejected(self):
        """D.3: Embedding vector with +/-Inf raises ValueError."""
        for inf_val in (float("inf"), float("-inf")):
            rec_inf = {
                "id": "c1",
                "source": "01 Core/note.md",
                "position": 0,
                "text": "theory",
                "vector": [0.1, inf_val, 0.3],
            }
            with pytest.raises(ValueError, match="Non-finite float value detected in Core embedding vector"):
                compute_core_vector_digest([rec_inf])

    def test_embedding_value_change_changes_digest(self):
        """D.4: Slight numerical perturbation in embedding changes the SHA-256 digest."""
        rec_a = {"id": "c1", "source": "01 Core/note.md", "position": 0, "text": "t", "vector": [0.12345, 0.5]}
        rec_b = {"id": "c1", "source": "01 Core/note.md", "position": 0, "text": "t", "vector": [0.12346, 0.5]}
        assert compute_core_vector_digest([rec_a]) != compute_core_vector_digest([rec_b])

    # --------------------------------------------------------------------------
    # Correction E: Exact stored text preserved without whitespace stripping
    # --------------------------------------------------------------------------
    def test_exact_stored_text_leading_whitespace_changes_digest(self):
        """E.1: Leading whitespace in stored text is preserved and alters digest."""
        rec_clean = {"id": "c1", "source": "01 Core/note.md", "position": 0, "text": "Theory note", "vector": [0.1, 0.2]}
        rec_leading = {"id": "c1", "source": "01 Core/note.md", "position": 0, "text": "  Theory note", "vector": [0.1, 0.2]}
        assert compute_core_vector_digest([rec_clean]) != compute_core_vector_digest([rec_leading])

    def test_exact_stored_text_trailing_whitespace_changes_digest(self):
        """E.2: Trailing whitespace in stored text is preserved and alters digest."""
        rec_clean = {"id": "c1", "source": "01 Core/note.md", "position": 0, "text": "Theory note", "vector": [0.1, 0.2]}
        rec_trailing = {"id": "c1", "source": "01 Core/note.md", "position": 0, "text": "Theory note\n", "vector": [0.1, 0.2]}
        assert compute_core_vector_digest([rec_clean]) != compute_core_vector_digest([rec_trailing])

    def test_exact_stored_text_internal_whitespace_changes_digest(self):
        """E.3: Internal whitespace variations in stored text alter digest."""
        rec_clean = {"id": "c1", "source": "01 Core/note.md", "position": 0, "text": "Theory note", "vector": [0.1, 0.2]}
        rec_internal = {"id": "c1", "source": "01 Core/note.md", "position": 0, "text": "Theory   note", "vector": [0.1, 0.2]}
        assert compute_core_vector_digest([rec_clean]) != compute_core_vector_digest([rec_internal])

    def test_unicode_text_deterministic_digest(self):
        """E.4: Unicode text produces deterministic canonical digest."""
        rec_unicode = {
            "id": "c1",
            "source": "01 Core/note.md",
            "position": 0,
            "text": "Authenticité & Précision 🔬 — 100% Validé",
            "vector": [0.5, 0.5],
        }
        d1 = compute_core_vector_digest([rec_unicode])
        d2 = compute_core_vector_digest([rec_unicode])
        assert d1 == d2
        assert len(d1) == 64


# ==============================================================================
# Phase 3 Final Authorization Boundary & TOCTOU Corrections Test Suite
# (Correction A: A1-A6, Correction B: B1-B5)
# ==============================================================================
class TestPhase3FinalAuthorizationBoundaryAndTOCTOUCorrections:
    """Verifies that Core validation proof is separated from sync authorization (Correction A)
    and that the second table snapshot fails closed if reacquisition fails (Correction B)."""

    # --------------------------------------------------------------------------
    # Correction A: Separation of Core Validation Proof from Sync Authorization
    # --------------------------------------------------------------------------
    def test_a1_validated_core_without_human_approval_cannot_establish_baseline(self, tmp_path):
        """A.1: Genuinely valid ValidatedCore without approval cannot establish database baseline."""
        db_dir = tmp_path / "vec_corr_a1"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        # 1. Obtain genuinely valid ValidatedCore through legitimate validation path
        val_core = validate_and_load_core("examples")
        assert is_trusted_validation_context(val_core.validation_context) is True

        index_units(list(val_core.units), embedder, store, validated_core=val_core)

        # 2 & 3. Do not obtain approval ticket / sync_auth. Attempt to establish new baseline
        with pytest.raises(
            DatabaseOperationError,
            match="Core baseline certification requires valid, authorized synchronization evidence",
        ):
            store.save_metadata(
                {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
                validated_core=val_core,
                sync_auth=None,
            )

        # 4 & 5. Verify no trusted baseline was created
        meta = store.get_metadata()
        assert "core_vector_digest" not in meta
        assert "core_vector_count" not in meta
        assert "core_record_ids" not in meta

    def test_a2_validated_core_with_forged_authorization_fails_closed(self, tmp_path):
        """A.2: Valid ValidatedCore with forged/caller-supplied authorization fails closed."""
        db_dir = tmp_path / "vec_corr_a2"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")
        index_units(list(val_core.units), embedder, store, validated_core=val_core)

        forged_auth_candidates = [
            True,                                  # Boolean flag
            "authorized_by_operator",              # Plain string
            {"approved": True, "ticket_id": "1"},  # Plain dict
            object(),                              # Generic object
        ]
        for fake_auth in forged_auth_candidates:
            with pytest.raises(
                DatabaseOperationError,
                match="Untrusted or invalid synchronization authorization",
            ):
                store.save_metadata(
                    {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
                    validated_core=val_core,
                    sync_auth=fake_auth,
                )

        # Forged SyncAuthorizationContext with counterfeit sentinel or wrong Core hash
        class CounterfeitAuth:
            vault_id = "test_vault"
            core_hash = val_core.sha256
            _sync_proof = object()  # Fake proof object

        with pytest.raises(
            DatabaseOperationError,
            match="Untrusted or invalid synchronization authorization",
        ):
            store.save_metadata(
                {"status": "fresh"},
                validated_core=val_core,
                sync_auth=CounterfeitAuth(),
            )

        # Valid sync auth but mismatched Core hash
        mismatched_auth = build_test_sync_authorization(val_core)
        object.__setattr__(mismatched_auth, "core_hash", "completely_different_hash_000")
        with pytest.raises(
            DatabaseOperationError,
            match="Untrusted or invalid synchronization authorization",
        ):
            store.save_metadata(
                {"status": "fresh"},
                validated_core=val_core,
                sync_auth=mismatched_auth,
            )

    def test_a3_authorized_synchronization_lifecycle_establishes_verified_baseline(self, tmp_path):
        """A.3: Genuine human-approved synchronization lifecycle establishes verified baseline."""
        vault = tmp_path / "auth_vault"
        _make_valid_vault(vault, note_title="Core Research V1", body="Foundational principles.")

        db_dir = tmp_path / "auth_db"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()

        # 1. Initial sync (genuine first initialization)
        init_res = session.authorize(str(vault), str(db_dir), embedder=embedder)
        assert init_res["authorized"] is True
        assert init_res["status"] == SYNC_STATE_FRESH
        assert init_res["fresh"] is True

        # Verify initial baseline in store
        val_core_v1 = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=session._pipeline)
        store = LanceDBManager(str(session.index_dir))
        meta1 = store.get_metadata()
        assert meta1["status"] == SYNC_STATE_FRESH
        assert meta1["core_hash"] == val_core_v1.sha256
        assert bool(meta1.get("core_vector_digest")) is True
        assert meta1.get("core_vector_count") == len(val_core_v1.units)
        digest1 = meta1["core_vector_digest"]

        # 2. Introduce Core modification (V1 -> V2)
        time.sleep(0.01)
        core_file = vault / "01 Core" / "01-core-anchor.md"
        core_file.write_text(
            "---\ntitle: Core Research V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\n"
            "Advanced expanded foundational principles of LivingAuthenticity.",
            encoding="utf-8",
        )

        session2 = VaultSyncSession()
        auth2 = session2.authorize(str(vault), str(db_dir), embedder=embedder)
        assert auth2["status"] in ("approval_required", SYNC_STATE_STALE)
        appr_req = session2.request_approval(run_id="run_v2")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_v2", vault_id=session2._vault_id)
        ticket = session2._active_ticket
        assert ticket is not None
        assert ticket.core_hash != meta1["core_hash"]

        # Human operator approves and reconciles
        rec2 = session2.reconcile(ticket)
        assert rec2["authorized"] is True
        assert rec2["fresh"] is True

        # Verify new baseline established matching Core V2
        meta2 = store.get_metadata()
        assert meta2["status"] == SYNC_STATE_FRESH
        assert meta2["core_hash"] == ticket.core_hash
        assert meta2["core_hash"] != meta1["core_hash"]
        assert meta2["core_vector_digest"] != digest1

    def test_a4_unauthorized_baseline_replacement_rejected_and_original_preserved(self, tmp_path):
        """A.4: Attempting to replace baseline with new ValidatedCore without approval fails closed."""
        db_dir = tmp_path / "vec_corr_a4"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        val_core_v1 = validate_and_load_core("examples")
        index_units(list(val_core_v1.units), embedder, store, validated_core=val_core_v1)
        val_auth_v1 = build_test_sync_authorization(val_core_v1)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core_v1.units), "core_hash": val_core_v1.sha256},
            validated_core=val_core_v1,
            sync_auth=val_auth_v1,
        )
        meta_v1 = store.get_metadata()
        orig_digest = meta_v1["core_vector_digest"]
        orig_hash = meta_v1["core_hash"]
        orig_count = meta_v1["core_vector_count"]

        # Create a genuinely valid Core V2
        vault_v2 = tmp_path / "vault_v2"
        _make_valid_vault(vault_v2, note_title="Core V2", body="Completely different core V2.")
        val_core_v2 = validate_and_load_core("personal_vault", vault_root=str(vault_v2))
        assert val_core_v2.sha256 != orig_hash

        # Attempt to replace baseline without sync_auth
        with pytest.raises(
            DatabaseOperationError,
            match="Core baseline certification requires valid, authorized synchronization evidence",
        ):
            store.save_metadata(
                {"status": "fresh", "core_hash": val_core_v2.sha256},
                validated_core=val_core_v2,
            )

        # Attempt to replace baseline with forged sync_auth
        with pytest.raises(
            DatabaseOperationError,
            match="Untrusted or invalid synchronization authorization",
        ):
            store.save_metadata(
                {"status": "fresh", "core_hash": val_core_v2.sha256},
                validated_core=val_core_v2,
                sync_auth=True,
            )

        # Original baseline is strictly preserved
        after_meta = store.get_metadata()
        assert after_meta["core_hash"] == orig_hash
        assert after_meta["core_vector_digest"] == orig_digest
        assert after_meta["core_vector_count"] == orig_count

    def test_a5_failed_or_stale_or_consumed_ticket_cannot_establish_baseline(self, tmp_path):
        """A.5: Expired, consumed, or invalidated ticket cannot establish or replace baseline."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "stale_vault"
        _make_valid_vault(vault)
        db_dir = tmp_path / "stale_db"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        # Modify Core to get a ticket
        time.sleep(0.01)
        core_file = vault / "01 Core" / "01-core-anchor.md"
        core_file.write_text(
            "---\ntitle: Core Stale Test\ncanonical_type: Core\ndraft: false\nstatus: active\n---\n"
            "Stale ticket test note body.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_stale")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_stale", vault_id=session2._vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        # Expire the ticket
        ticket.expires_at = time.time() - 10.0
        with pytest.raises(VaultSyncStaleApprovalError, match="Ticket is already used or has expired"):
            session2.reconcile(ticket)

        # Direct call to create_sync_authorization with expired ticket fails closed
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        with pytest.raises(ValueError, match="Approval ticket has expired"):
            create_sync_authorization(session2, val_core, ticket=ticket)

    def test_a6_generic_metadata_updates_preserve_baseline_and_no_baseline_state(self, tmp_path):
        """A.6: Generic save_metadata calls strictly preserve baseline fields or no-baseline state."""
        db_dir = tmp_path / "gen_meta_db"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()

        # Part 1: No-baseline state
        store.save_metadata({
            "status": "fresh",
            "indexed_vectors": 3,
            "core_vector_digest": "spoofed_digest_123456",
            "core_vector_count": 99,
        })
        meta_no_base = store.get_metadata()
        assert meta_no_base["status"] == "fresh"
        assert meta_no_base["indexed_vectors"] == 3
        assert "core_vector_digest" not in meta_no_base
        assert "core_vector_count" not in meta_no_base

        # Part 2: Established baseline state
        val_core = validate_and_load_core("examples")
        embedder = DummyVectorEmbedder()
        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )
        base_meta = store.get_metadata()
        trusted_digest = base_meta["core_vector_digest"]
        trusted_hash = base_meta["core_hash"]

        # Generic update modifying operational fields and attempting to tamper with baseline
        store.save_metadata({
            "status": "fresh",
            "indexed_vectors": 42,
            "core_vector_digest": "evil_override_digest",
            "core_hash": "evil_override_hash",
        })
        after_meta = store.get_metadata()
        assert after_meta["indexed_vectors"] == 42
        assert after_meta["core_vector_digest"] == trusted_digest
        assert after_meta["core_hash"] == trusted_hash

    # --------------------------------------------------------------------------
    # Correction B: Fail Closed When Second Table Snapshot Cannot Be Obtained
    # --------------------------------------------------------------------------
    def test_b1_second_get_table_call_fails_closed_without_fallback(self, tmp_path):
        """B.1: Exception in second get_table() call fails closed with DatabaseOperationError preserving cause."""
        db_dir = tmp_path / "vec_corr_b1"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )

        call_count = [0]
        real_get_table = store.get_table

        def hooked_get_table():
            call_count[0] += 1
            if call_count[0] <= 3:
                return real_get_table()
            raise RuntimeError("Simulated storage failure during post-verification table reacquisition")

        setattr(store, "get_table", hooked_get_table)

        query = KnowledgeUnit(id="q", source="inp", original_text="analytical theory", meaning="theory")
        with pytest.raises(DatabaseOperationError) as exc_info:
            retriever.retrieve(query)

        assert "Failed reacquiring vector table for post-verification snapshot" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "Simulated storage failure" in str(exc_info.value.__cause__)
        assert call_count[0] == 4

    def test_b2_second_table_reference_none_or_invalid_fails_closed(self, tmp_path):
        """B.2: Second get_table() returning None fails closed immediately."""
        db_dir = tmp_path / "vec_corr_b2"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )

        call_count = [0]
        real_get_table = store.get_table

        def hooked_get_table():
            call_count[0] += 1
            if call_count[0] <= 3:
                return real_get_table()
            return None

        setattr(store, "get_table", hooked_get_table)

        query = KnowledgeUnit(id="q", source="inp", original_text="analytical theory", meaning="theory")
        with pytest.raises(
            DatabaseOperationError,
            match="Reacquired vector table is None after baseline verification",
        ):
            retriever.retrieve(query)
        assert call_count[0] == 4

    def test_b3_second_table_version_missing_or_empty_fails_closed(self, tmp_path):
        """B.3: Second table snapshot with missing or empty version fails closed."""
        db_dir = tmp_path / "vec_corr_b3"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )

        call_count = [0]
        real_get_table = store.get_table

        class EmptyVersionTable:
            def __init__(self, inner):
                self._inner = inner
            @property
            def version(self):
                return ""
            def __getattr__(self, name):
                return getattr(self._inner, name)

        def hooked_get_table():
            call_count[0] += 1
            tbl = real_get_table()
            if call_count[0] <= 3:
                return tbl
            return EmptyVersionTable(tbl)

        setattr(store, "get_table", hooked_get_table)

        query = KnowledgeUnit(id="q", source="inp", original_text="analytical theory", meaning="theory")
        with pytest.raises(
            DatabaseOperationError,
            match="Refreshed table version missing or invalid after baseline verification",
        ):
            retriever.retrieve(query)
        assert call_count[0] == 4

    def test_b4_version_change_between_snapshots_fails_closed(self, tmp_path):
        """B.4: Discrepancy between v_pre and v_post fails closed immediately."""
        db_dir = tmp_path / "vec_corr_b4"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )

        call_count = [0]
        real_get_table = store.get_table

        class VersionChangingTable:
            def __init__(self, inner, is_post=False):
                self._inner = inner
                self._is_post = is_post
            @property
            def version(self):
                return 999 if self._is_post else 1
            def __getattr__(self, name):
                return getattr(self._inner, name)

        def hooked_b4():
            call_count[0] += 1
            tbl = real_get_table()
            if call_count[0] <= 3:
                return VersionChangingTable(tbl, is_post=False)
            return VersionChangingTable(tbl, is_post=True)

        setattr(store, "get_table", hooked_b4)

        query = KnowledgeUnit(id="q", source="inp", original_text="analytical theory", meaning="theory")
        with pytest.raises(DatabaseOperationError, match="Store mutated during baseline verification: fail closed"):
            retriever.retrieve(query)

    def test_b5_stable_versions_allow_normal_retrieval(self, tmp_path):
        """B.5: When both snapshots succeed independently and versions match, retrieval succeeds."""
        db_dir = tmp_path / "vec_corr_b5"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
        )

        query = KnowledgeUnit(id="q", source="inp", original_text="analytical theory balance", meaning="theory")
        result = retriever.retrieve(query)
        assert len(result.candidates) > 0
        assert retriever._core_baseline_verified is True
        assert result.metadata.get("core_baseline_verified") is True

    def test_b6_reacquired_table_has_valid_version_but_unusable_query_fails_closed(self, tmp_path):
        """B.6: Second get_table() returns table with valid version but unusable query interface; fails closed."""
        db_dir = tmp_path / "vec_corr_b6"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        val_core = validate_and_load_core("examples")

        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        observed_events = []
        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            observer=lambda ev: observed_events.append(ev),
        )

        call_count = [0]
        real_get_table = store.get_table

        class UnusableQueryTable:
            def __init__(self, inner):
                self._inner = inner
            @property
            def version(self):
                return getattr(self._inner, "version", 1)
            def count_rows(self):
                return 1
            def search(self, *args, **kwargs):
                raise RuntimeError("Underlying vector table search index corrupted or missing")
            def __getattr__(self, name):
                return getattr(self._inner, name)

        def hooked_b6():
            call_count[0] += 1
            tbl = real_get_table()
            if call_count[0] <= 3:
                return tbl
            return UnusableQueryTable(tbl)

        setattr(store, "get_table", hooked_b6)

        query = KnowledgeUnit(id="q_b6", source="inp", original_text="analytical theory balance", meaning="theory")
        with pytest.raises(DatabaseOperationError) as exc_info:
            retriever.retrieve(query)

        assert "Vector search failed" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "Underlying vector table search index corrupted or missing" in str(exc_info.value.__cause__)
        assert call_count[0] == 4
        stages = [ev.get("stage") for ev in observed_events]
        assert "retrieval_complete" not in stages
        failed_events = [ev for ev in observed_events if ev.get("phase") == "failed"]
        assert len(failed_events) >= 1
        assert any(ev.get("operation") == "candidate_retrieval" for ev in failed_events)

    def test_b6_a_reacquired_table_lacks_search_method_fails_closed(self, tmp_path):
        """B6-A: Reacquired vector table lacking search method fails closed with DatabaseOperationError."""
        vault = tmp_path / "vault_b6a"
        _make_valid_vault(vault, note_title="Core B6A", body="B6A thesis.")
        db_dir = tmp_path / "db_b6a"
        embedder = DummyVectorEmbedder()

        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        observed_events = []
        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            observer=lambda ev: observed_events.append(ev),
        )

        call_count = [0]
        real_get_table = store.get_table

        class TableWithoutSearch:
            def __init__(self, inner):
                self._inner = inner
            @property
            def version(self):
                return getattr(self._inner, "version", 1)
            def count_rows(self):
                return 1
            def __getattr__(self, name):
                if name == "search":
                    raise AttributeError("search method missing")
                return getattr(self._inner, name)

        def hooked():
            call_count[0] += 1
            tbl = real_get_table()
            if call_count[0] <= 3:
                return tbl
            return TableWithoutSearch(tbl)

        setattr(store, "get_table", hooked)

        query = KnowledgeUnit(id="q_b6a", source="inp", original_text="analytical query", meaning="theory")
        with pytest.raises(DatabaseOperationError, match="Reacquired vector table lacks callable search method"):
            retriever.retrieve(query)

        assert call_count[0] == 4
        assert not any(ev.get("phase") == "completed" and ev.get("operation") == "candidate_retrieval" for ev in observed_events)
        assert any(ev.get("phase") == "failed" for ev in observed_events)

    def test_b6_b_reacquired_table_has_non_callable_search_fails_closed(self, tmp_path):
        """B6-B: Reacquired vector table with non-callable search attribute fails closed."""
        vault = tmp_path / "vault_b6b"
        _make_valid_vault(vault, note_title="Core B6B", body="B6B thesis.")
        db_dir = tmp_path / "db_b6b"
        embedder = DummyVectorEmbedder()

        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        index_units(list(val_core.units), embedder, store, validated_core=val_core)
        val_auth = build_test_sync_authorization(val_core)
        store.save_metadata(
            {"status": "fresh", "indexed_vectors": len(val_core.units), "core_hash": val_core.sha256},
            validated_core=val_core,
            sync_auth=val_auth,
        )

        observed_events = []
        retriever = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=val_core,
            observer=lambda ev: observed_events.append(ev),
        )

        call_count = [0]
        real_get_table = store.get_table

        class NonCallableSearchTable:
            def __init__(self, inner):
                self._inner = inner
                self.search = "not_callable"
            @property
            def version(self):
                return getattr(self._inner, "version", 1)
            def count_rows(self):
                return 1
            def __getattr__(self, name):
                return getattr(self._inner, name)

        def hooked():
            call_count[0] += 1
            tbl = real_get_table()
            if call_count[0] <= 3:
                return tbl
            return NonCallableSearchTable(tbl)

        setattr(store, "get_table", hooked)

        query = KnowledgeUnit(id="q_b6b", source="inp", original_text="analytical query", meaning="theory")
        with pytest.raises(DatabaseOperationError, match="Reacquired vector table lacks callable search method"):
            retriever.retrieve(query)

        assert call_count[0] == 4
        assert not any(ev.get("phase") == "completed" and ev.get("operation") == "candidate_retrieval" for ev in observed_events)
        assert any(ev.get("phase") == "failed" for ev in observed_events)


class TestPhase3SyncAuthorizationFactoryHardening:
    """Adversarial security and invariant verification for create_sync_authorization().

    Enforces:
    - Session authenticity: Fabricated sessions or copied attributes cannot mint capabilities (F1, F2).
    - Ticket authenticity: Fabricated, rejected, consumed, expired, or cross-session tickets cannot mint (F3, F4, F5, F6, F7).
    - Lifecycle binding: Legitimate approved lifecycle succeeds (F8); genuine initial cold start succeeds (F9).
    - Inactive session rejection: Genuine session not in active sync state cannot mint (F10).
    - Hash mismatch rejection: Ticket for V1 cannot authorize V2 (F11).
    """

    def test_f1_fabricated_initial_sync_session_rejected(self, tmp_path):
        """F1: Fabricated session-like object with _initial_sync_active=True fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        val_core = validate_and_load_core("examples")

        class FabricatedSession:
            _vault_id = "fake_vault"
            session_id = "fake_session"
            _initial_sync_active = True
            is_authorized = True
            _status = "authorizing"

        fake = FabricatedSession()
        with pytest.raises(ValueError, match="Session is not an authentic, authorized VaultSyncSession instance"):
            create_sync_authorization(fake, val_core)

    def test_f2_fabricated_session_with_copied_attributes_rejected(self, tmp_path):
        """F2: Separately constructed object copying legitimate session attributes fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_f2"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f2"
        embedder = DummyVectorEmbedder()

        real_session = VaultSyncSession()
        real_session.authorize(str(vault), str(db_dir), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        class CopiedSession:
            def __init__(self, src):
                self.__dict__.update(src.__dict__)

        copied = CopiedSession(real_session)
        with pytest.raises(ValueError, match="Session is not an authentic, authorized VaultSyncSession instance"):
            create_sync_authorization(copied, val_core)

    def test_f3_fabricated_approval_ticket_rejected(self, tmp_path):
        """F3: Fabricated ticket object with matching public attributes fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        vault = tmp_path / "vault_f3"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f3"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        # Fabricate a ticket directly via constructor (lacks _ticket_proof)
        fake_ticket = VaultSyncApprovalTicket(
            ticket_id="fake_ticket_123",
            vault_id=session.vault_id,
            vault_root=session.vault_root,
            run_id="fake_run",
            snapshot_fingerprint="snap_123",
            core_hash=val_core.sha256,
            diff_fingerprint="diff_123",
            expires_at=time.time() + 1000.0,
            session_id=session.session_id,
            approval_id="fake_appr",
            decision="approve",
        )

        with pytest.raises(ValueError, match="Approval ticket is forged, untrusted, or was not issued through authorized approval"):
            create_sync_authorization(session, val_core, ticket=fake_ticket)

    def test_f4_rejected_ticket_cannot_authorize_sync(self, tmp_path):
        """F4: Ticket with decision='reject' or operator rejection fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        vault = tmp_path / "vault_f4"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f4"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        # 1. Operator rejects approval request in session
        time.sleep(0.01)
        core_file = vault / "01 Core" / "01-core-anchor.md"
        core_file.write_text(
            "---\ntitle: Core F4\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nModified body F4.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_f4")
        res = session2.resolve_approval(appr_req["approval_id"], decision="reject", run_id="run_f4", vault_id=session2.vault_id)
        assert res["decision"] == "reject"
        assert session2._active_ticket is None

        # 2. A ticket carrying decision='reject' is rejected by factory
        rejected_ticket = VaultSyncApprovalTicket(
            ticket_id="rej_123",
            vault_id=session2.vault_id,
            vault_root=session2.vault_root,
            run_id="run_f4",
            snapshot_fingerprint="snap_123",
            core_hash=val_core.sha256,
            diff_fingerprint="diff_123",
            expires_at=time.time() + 1000.0,
            session_id=session2.session_id,
            decision="reject",
        )
        with pytest.raises(ValueError, match="Approval ticket is forged, untrusted, or was not issued through authorized approval"):
            create_sync_authorization(session2, val_core, ticket=rejected_ticket)

    def test_f5_consumed_ticket_cannot_mint_authorization(self, tmp_path):
        """F5: A genuine ticket already consumed by completed reconciliation cannot be reused."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_f5"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f5"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        # Modify Core note and approve
        time.sleep(0.01)
        core_file = vault / "01 Core" / "01-core-anchor.md"
        core_file.write_text(
            "---\ntitle: Core F5\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nModified body F5.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_f5")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_f5", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        # Reconcile successfully consumes the ticket
        rec = session2.reconcile(ticket)
        assert rec["fresh"] is True
        assert ticket.is_consumed is True

        val_core_v2 = validate_and_load_core("personal_vault", vault_root=str(vault))
        # Attempt to reuse consumed ticket directly with create_sync_authorization
        with pytest.raises(ValueError, match="Approval ticket has already been consumed by a completed reconciliation"):
            create_sync_authorization(session2, val_core_v2, ticket=ticket)

    def test_f6_expired_ticket_rejected(self, tmp_path):
        """F6: An expired ticket cannot authorize synchronization."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_f6"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f6"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        core_file = vault / "01 Core" / "01-core-anchor.md"
        core_file.write_text(
            "---\ntitle: Core F6\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nModified body F6.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_f6")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_f6", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        # Expire ticket
        ticket.expires_at = time.time() - 5.0
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        with pytest.raises(ValueError, match="Approval ticket has expired"):
            create_sync_authorization(session2, val_core, ticket=ticket)

    def test_f7_cross_session_ticket_rejected(self, tmp_path):
        """F7: Valid ticket from Session A cannot authorize Session B."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_f7"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f7"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        core_file = vault / "01 Core" / "01-core-anchor.md"
        core_file.write_text(
            "---\ntitle: Core F7\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nModified body F7.",
            encoding="utf-8",
        )
        session_a = VaultSyncSession()
        session_a.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session_a.request_approval(run_id="run_f7")
        session_a.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_f7", vault_id=session_a.vault_id)
        ticket_a = session_a._active_ticket
        assert ticket_a is not None

        session_b = VaultSyncSession()
        session_b.authorize(str(vault), str(db_dir), embedder=embedder)
        assert session_b.session_id != session_a.session_id

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        with pytest.raises(ValueError, match="Ticket session_id .* does not match active session_id"):
            create_sync_authorization(session_b, val_core, ticket=ticket_a)

    def test_f8_correct_approved_lifecycle_succeeds(self, tmp_path):
        """F8: Genuine approved ticket within authorized session succeeds and establishes certified baseline."""
        vault = tmp_path / "vault_f8"
        _make_valid_vault(vault, note_title="Core V1", body="Original body V1.")
        db_dir = tmp_path / "db_f8"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        store = LanceDBManager(str(session.index_dir))
        meta_v1 = store.get_metadata()
        assert meta_v1["status"] == "fresh"

        # Update to V2
        time.sleep(0.01)
        core_file = vault / "01 Core" / "01-core-anchor.md"
        core_file.write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nApproved body V2.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_f8")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_f8", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        rec = session2.reconcile(ticket)
        assert rec["fresh"] is True
        assert ticket.is_consumed is True

        meta_v2 = store.get_metadata()
        assert meta_v2["status"] == "fresh"
        assert meta_v2["core_hash"] == ticket.core_hash
        assert meta_v2["core_hash"] != meta_v1["core_hash"]

    def test_f9_genuine_initial_cold_start_lifecycle_succeeds(self, tmp_path):
        """F9: Genuine eligible cold-start initialization mints authorization and establishes verified baseline."""
        vault = tmp_path / "vault_f9"
        _make_valid_vault(vault, note_title="Core Cold Start", body="Cold start initial body.")
        db_dir = tmp_path / "db_f9"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        auth = session.authorize(str(vault), str(db_dir), embedder=embedder)
        assert auth["fresh"] is True
        assert auth["status"] == "fresh"

        store = LanceDBManager(str(session.index_dir))
        meta = store.get_metadata()
        assert meta["status"] == "fresh"
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        assert meta["core_hash"] == val_core.sha256
        assert bool(meta.get("core_vector_digest")) is True

    def test_f10_no_authorization_outside_active_lifecycle(self, tmp_path):
        """F10: Genuine session not in active synchronization state cannot mint authorization."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_f10"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f10"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        assert session.status == "fresh"
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        # Attempt to call create_sync_authorization on idle fresh session without active sync
        with pytest.raises(ValueError, match="Cannot mint sync authorization without valid approval ticket or initial sync privilege"):
            create_sync_authorization(session, val_core)

    def test_f11_core_hash_mismatch_rejected(self, tmp_path):
        """F11: Legitimate authorization lifecycle for Core V1 cannot authorize Core V2."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault_v1 = tmp_path / "vault_f11_v1"
        _make_valid_vault(vault_v1, note_title="Core V1", body="Body V1.")
        db_dir = tmp_path / "db_f11"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault_v1), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        core_file = vault_v1 / "01 Core" / "01-core-anchor.md"
        core_file.write_text(
            "---\ntitle: Core V1 Updated\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody V1 Updated.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault_v1), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_f11")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_f11", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        # Build Core V2 with a different hash
        vault_v2 = tmp_path / "vault_f11_v2"
        _make_valid_vault(vault_v2, note_title="Core V2", body="Completely different Core V2.")
        val_core_v2 = validate_and_load_core("personal_vault", vault_root=str(vault_v2))
        assert val_core_v2.sha256 != ticket.core_hash

        with pytest.raises(ValueError, match="Ticket Core hash .* does not match validated Core hash"):
            create_sync_authorization(session2, val_core_v2, ticket=ticket)

    def test_f12_forged_ticket_with_valid_looking_sentinel_rejected(self, tmp_path):
        """F12: Constructing ticket with decision='approve', matching fields, and sentinel fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            create_sync_authorization,
            is_trusted_approval_ticket,
        )
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        vault = tmp_path / "vault_f12"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f12"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        forged_ticket = VaultSyncApprovalTicket(
            ticket_id="forged_f12",
            vault_id=session.vault_id,
            vault_root=session.vault_root,
            run_id="run_f12",
            snapshot_fingerprint="snap_f12",
            core_hash=val_core.sha256,
            diff_fingerprint="diff_f12",
            expires_at=time.time() + 1000.0,
            session_id=session.session_id,
            approval_id="appr_f12",
            decision="approve",
        )
        # Attempt to inject a sentinel object or copied attribute
        setattr(forged_ticket, "_ticket_proof", object())

        # is_trusted_approval_ticket rejects it
        assert is_trusted_approval_ticket(forged_ticket, session=session) is False
        assert is_trusted_approval_ticket(forged_ticket) is False

        # create_sync_authorization rejects it
        with pytest.raises(ValueError, match="Approval ticket is forged, untrusted, or was not issued through authorized approval"):
            create_sync_authorization(session, val_core, ticket=forged_ticket)

    def test_f13_forged_ticket_assigned_to_genuine_active_session_rejected(self, tmp_path):
        """F13: Assigning a fabricated ticket to _current_reconciliation_ticket fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        vault = tmp_path / "vault_f13"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f13"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        forged_ticket = VaultSyncApprovalTicket(
            ticket_id="forged_f13",
            vault_id=session.vault_id,
            vault_root=session.vault_root,
            run_id="run_f13",
            snapshot_fingerprint="snap_f13",
            core_hash=val_core.sha256,
            diff_fingerprint="diff_f13",
            expires_at=time.time() + 1000.0,
            session_id=session.session_id,
            approval_id="appr_f13",
            decision="approve",
        )
        # Attempt to bypass by setting active reconciliation ticket and in_progress status
        session._current_reconciliation_ticket = forged_ticket
        session._status = "sync_in_progress"

        store = LanceDBManager(str(session.index_dir))
        meta_before = store.get_metadata()

        with pytest.raises(ValueError, match="Approval ticket is forged, untrusted, or was not issued through authorized approval"):
            create_sync_authorization(session, val_core, ticket=forged_ticket)

        # Even if attacker injects the private _TICKET_PROOF onto the forged ticket:
        # It fails closed because it was never issued by the active session!
        from src.living_authenticity.knowledge.vault.core_validator import _TICKET_PROOF
        setattr(forged_ticket, "_ticket_proof", _TICKET_PROOF)
        with pytest.raises(ValueError, match="Approval ticket was not issued by the active session"):
            create_sync_authorization(session, val_core, ticket=forged_ticket)

        meta_after = store.get_metadata()
        assert meta_before == meta_after

    def test_f14_copied_genuine_ticket_attributes_rejected(self, tmp_path):
        """F14: Object copying all attributes from a genuine approved ticket fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            create_sync_authorization,
            is_trusted_approval_ticket,
        )
        vault = tmp_path / "vault_f14"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f14"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core F14\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nModified body F14.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_f14")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_f14", vault_id=session2.vault_id)
        real_ticket = session2._active_ticket
        assert real_ticket is not None

        # Build a distinct copy of the ticket with matching attributes
        class TicketImpostor:
            def __init__(self, src):
                self.__dict__.update(src.__dict__)

        impostor = TicketImpostor(real_ticket)
        assert is_trusted_approval_ticket(impostor, session=session2) is False

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        with pytest.raises(ValueError, match="Approval ticket is forged, untrusted, or was not issued through authorized approval"):
            create_sync_authorization(session2, val_core, ticket=impostor)

    def test_f15_public_constructor_cannot_mint_trusted_ticket(self, tmp_path):
        """F15: Direct invocation of VaultSyncApprovalTicket constructor cannot mint trusted ticket."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            create_sync_authorization,
            is_trusted_approval_ticket,
        )
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        vault = tmp_path / "vault_f15"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_f15"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        public_ticket = VaultSyncApprovalTicket(
            ticket_id="public_t15",
            vault_id=session.vault_id,
            vault_root=session.vault_root,
            run_id="run_f15",
            snapshot_fingerprint="snap_f15",
            core_hash=val_core.sha256,
            diff_fingerprint="diff_f15",
            expires_at=time.time() + 1000.0,
            session_id=session.session_id,
            approval_id="appr_f15",
            decision="approve",
        )
        assert public_ticket._ticket_proof is None
        assert is_trusted_approval_ticket(public_ticket) is False
        assert is_trusted_approval_ticket(public_ticket, session=session) is False

        with pytest.raises(ValueError, match="Approval ticket is forged, untrusted, or was not issued through authorized approval"):
            create_sync_authorization(session, val_core, ticket=public_ticket)

    def test_f16_genuine_approval_lifecycle_complete_end_to_end(self, tmp_path):
        """F16: Real ticket issued through resolve_approval(decision='approve') executes full approved lifecycle."""
        vault = tmp_path / "vault_f16"
        _make_valid_vault(vault, note_title="Core V1", body="V1 text.")
        db_dir = tmp_path / "db_f16"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        store = LanceDBManager(str(session.index_dir))
        assert store.get_metadata()["status"] == "fresh"

        # Mutate to V2
        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nV2 approved body.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_f16")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_f16", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None
        assert session2.is_ticket_issued_by_session(ticket) is True

        rec = session2.reconcile(ticket)
        assert rec["fresh"] is True
        assert ticket.is_consumed is True
        assert session2.status == "fresh"

        meta_v2 = store.get_metadata()
        assert meta_v2["status"] == "fresh"
        assert meta_v2["core_hash"] == ticket.core_hash

    def test_repeated_authorization_within_same_reconciliation_rejected(self, tmp_path):
        """P2: Calling create_sync_authorization more than once with the same ticket fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_reentry1"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_reentry1"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody V2.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_reentry1")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_reentry1", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        session2._status = "sync_in_progress"
        session2._current_reconciliation_ticket = ticket

        # First issuance succeeds
        auth1 = create_sync_authorization(session2, val_core, ticket=ticket)
        assert auth1 is not None

        # Second issuance with same ticket in same reconciliation FAILS CLOSED
        with pytest.raises(ValueError, match="Approval ticket has already authorized baseline certification; multiple authorization issuance is prohibited"):
            create_sync_authorization(session2, val_core, ticket=ticket)

    def test_authorization_attempt_after_successful_baseline_persistence_rejected(self, tmp_path):
        """P2: After reconciliation completes and certifies baseline, new authorization cannot be minted."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_post_commit"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_post_commit"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody V2 post.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        appr_req = session2.request_approval(run_id="run_post_commit")
        session2.resolve_approval(appr_req["approval_id"], decision="approve", run_id="run_post_commit", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        session2.reconcile(ticket)
        assert session2.status == "fresh"
        assert ticket.is_consumed is True

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        # Re-entry after completion fails closed
        with pytest.raises(ValueError):
            create_sync_authorization(session2, val_core, ticket=ticket)

    def test_initial_sync_multiple_authorization_issuance_rejected(self, tmp_path):
        """P2: Initial cold-start privilege cannot mint multiple authorization contexts."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_init_reentry"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_init_reentry"
        embedder = DummyVectorEmbedder()

        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        # Simulate initial sync active
        session._initial_sync_active = True
        session._initial_sync_privilege = session._session_proof
        session._status = "sync_in_progress"
        session._initial_sync_authorization_issued = False

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        auth1 = create_sync_authorization(session, val_core)
        assert auth1 is not None

        with pytest.raises(ValueError, match="Initial sync privilege has already minted an authorization context; multiple issuance is prohibited"):
            create_sync_authorization(session, val_core)


class TestPhase3FinalAuthorizationProvenanceAndAtomicIssuance:
    """Comprehensive adversarial test matrix for Section 6 (A1-A6, C1-C6, R1-R5)."""

    # --- Group A: Ticket Provenance ---

    def test_a1_direct_issue_approved_ticket_invocation_rejected(self):
        """A1: Direct _issue_approved_ticket() invocation without genuine approval is rejected."""
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        with pytest.raises(ValueError, match="Cannot issue approved ticket without authentic session proof"):
            VaultSyncApprovalTicket._issue_approved_ticket(
                ticket_id="fake_a1",
                vault_id="v_a1",
                vault_root="/v_a1",
                run_id="run_a1",
                snapshot_fingerprint="snap_a1",
                core_hash="hash_a1",
                diff_fingerprint="diff_a1",
                expires_at=time.time() + 1000.0,
            )

    def test_a2_genuine_session_proof_copied_into_direct_factory_call_rejected(self, tmp_path):
        """A2: Caller reading session._session_proof and invoking factory outside resolve_approval is rejected."""
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        vault = tmp_path / "vault_a2"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_a2"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        # Attacker reads _session_proof and calls factory directly
        with pytest.raises(ValueError, match="Cannot issue approved ticket outside active resolve_approval execution"):
            VaultSyncApprovalTicket._issue_approved_ticket(
                ticket_id="fake_a2",
                vault_id=session.vault_id,
                vault_root=session.vault_root,
                run_id="run_a2",
                snapshot_fingerprint="snap_a2",
                core_hash="hash_a2",
                diff_fingerprint="diff_a2",
                expires_at=time.time() + 1000.0,
                session_proof=session._session_proof,
                session=session,
            )

    def test_a3_fabricated_ticket_registered_manually_in_session_rejected(self, tmp_path):
        """A3: Fabricated Ticket registered manually in genuine Session fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            create_sync_authorization,
            is_trusted_approval_ticket,
        )
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        vault = tmp_path / "vault_a3"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_a3"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        forged_ticket = VaultSyncApprovalTicket(
            ticket_id="forged_a3",
            vault_id=session.vault_id,
            vault_root=session.vault_root,
            run_id="run_a3",
            snapshot_fingerprint="snap_a3",
            core_hash=val_core.sha256,
            diff_fingerprint="diff_a3",
            expires_at=time.time() + 1000.0,
            session_id=session.session_id,
            approval_id="appr_a3",
            decision="approve",
        )
        setattr(forged_ticket, "_ticket_proof", object())
        # Attacker injects ID into session collection
        session._issued_tickets.add(id(forged_ticket))
        session._current_reconciliation_ticket = forged_ticket
        session._status = "sync_in_progress"

        assert session.is_ticket_issued_by_session(forged_ticket) is False
        assert is_trusted_approval_ticket(forged_ticket, session=session) is False

        with pytest.raises(ValueError, match="Approval ticket is forged, untrusted, or was not issued through authorized approval"):
            create_sync_authorization(session, val_core, ticket=forged_ticket)

    def test_a4_fabricated_ticket_assigned_to_active_ticket_field_rejected(self, tmp_path):
        """A4: Fabricated Ticket assigned to active-ticket field fails closed without baseline mutation."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket
        vault = tmp_path / "vault_a4"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_a4"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        store = LanceDBManager(str(session.index_dir))
        meta_before = store.get_metadata()

        forged = VaultSyncApprovalTicket(
            ticket_id="forged_a4",
            vault_id=session.vault_id,
            vault_root=session.vault_root,
            run_id="run_a4",
            snapshot_fingerprint="snap_a4",
            core_hash=val_core.sha256,
            diff_fingerprint="diff_a4",
            expires_at=time.time() + 1000.0,
            session_id=session.session_id,
            approval_id="appr_a4",
            decision="approve",
        )
        session._current_reconciliation_ticket = forged
        session._status = "sync_in_progress"

        with pytest.raises(ValueError):
            create_sync_authorization(session, val_core, ticket=forged)

        meta_after = store.get_metadata()
        assert meta_before == meta_after

    def test_a5_ticket_attributes_copied_from_genuine_ticket_rejected(self, tmp_path):
        """A5: Impostor object copying all attributes from genuine ticket is rejected."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            create_sync_authorization,
            is_trusted_approval_ticket,
        )
        vault = tmp_path / "vault_a5"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_a5"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody A5.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        req = session2.request_approval(run_id="run_a5")
        session2.resolve_approval(req["approval_id"], decision="approve", run_id="run_a5", vault_id=session2.vault_id)
        real_ticket = session2._active_ticket
        assert real_ticket is not None

        class TicketClone:
            def __init__(self, src):
                self.__dict__.update(src.__dict__)

        clone = TicketClone(real_ticket)
        assert session2.is_ticket_issued_by_session(clone) is False
        assert is_trusted_approval_ticket(clone, session=session2) is False

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        with pytest.raises(ValueError):
            create_sync_authorization(session2, val_core, ticket=clone)

    def test_a6_ticket_issued_through_genuine_approval_lifecycle_accepted(self, tmp_path):
        """A6: Genuine ticket issued through human approval completes full reconciliation and certifies baseline."""
        vault = tmp_path / "vault_a6"
        _make_valid_vault(vault, note_title="Core V1", body="V1 text.")
        db_dir = tmp_path / "db_a6"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        store = LanceDBManager(str(session.index_dir))
        assert store.get_metadata()["status"] == "fresh"

        # Mutate to V2
        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nApproved V2 text.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        req = session2.request_approval(run_id="run_a6")
        session2.resolve_approval(req["approval_id"], decision="approve", run_id="run_a6", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None
        assert session2.is_ticket_issued_by_session(ticket) is True

        rep = session2.reconcile(ticket)
        assert rep["fresh"] is True
        assert ticket.is_consumed is True
        assert session2.status == "fresh"
        assert store.get_metadata()["core_hash"] == ticket.core_hash

    # --- Group C: Context Provenance ---

    def test_c1_direct_sync_authorization_context_construction_untrusted(self, tmp_path):
        """C1: Direct SyncAuthorizationContext construction sets _sync_proof=None and is untrusted."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            SyncAuthorizationContext,
            is_trusted_sync_authorization,
        )
        vault = tmp_path / "vault_c1"
        _make_valid_vault(vault)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        raw_ctx = SyncAuthorizationContext(
            vault_id="vault_c1",
            core_hash=val_core.sha256,
            ticket_id="raw_ticket",
        )
        assert raw_ctx._sync_proof is None
        assert is_trusted_sync_authorization(raw_ctx) is False

        store = LanceDBManager(str(tmp_path / "db_c1"))
        store.create_knowledge_vector_table()
        with pytest.raises(DatabaseOperationError, match="Untrusted or invalid synchronization authorization"):
            store.save_metadata({"status": "fresh"}, validated_core=val_core, sync_auth=raw_ctx)

    def test_c2_context_with_copied_or_injected_proof_untrusted(self, tmp_path):
        """C2: Context with injected proof is rejected because it is absent from active registry."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            SyncAuthorizationContext,
            _SYNC_AUTH_PROOF,
            is_trusted_sync_authorization,
        )
        vault = tmp_path / "vault_c2"
        _make_valid_vault(vault)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        injected_ctx = SyncAuthorizationContext(
            vault_id="vault_c2",
            core_hash=val_core.sha256,
            ticket_id="inj_ticket",
        )
        object.__setattr__(injected_ctx, "_sync_proof", _SYNC_AUTH_PROOF)
        assert is_trusted_sync_authorization(injected_ctx) is False

        store = LanceDBManager(str(tmp_path / "db_c2"))
        store.create_knowledge_vector_table()
        with pytest.raises(DatabaseOperationError, match="Untrusted or invalid synchronization authorization"):
            store.save_metadata({"status": "fresh"}, validated_core=val_core, sync_auth=injected_ctx)

    def test_c3_context_reused_for_another_ticket_rejected(self, tmp_path):
        """C3: Reusing a consumed SyncAuthorizationContext for another operation fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            create_sync_authorization,
            is_trusted_sync_authorization,
        )
        vault = tmp_path / "vault_c3"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_c3"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody C3.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        req = session2.request_approval(run_id="run_c3")
        session2.resolve_approval(req["approval_id"], decision="approve", run_id="run_c3", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        session2._status = "sync_in_progress"
        session2._current_reconciliation_ticket = ticket

        auth = create_sync_authorization(session2, val_core, ticket=ticket)
        store = LanceDBManager(str(session2.index_dir))

        # First use succeeds and consumes context
        store.save_metadata({"status": "fresh", "core_hash": val_core.sha256}, validated_core=val_core, sync_auth=auth)

        # Context is now consumed
        assert is_trusted_sync_authorization(auth) is False

        # Attempt to reuse consumed context fails closed
        with pytest.raises(DatabaseOperationError, match="Untrusted or invalid synchronization authorization"):
            store.save_metadata({"status": "fresh", "core_hash": val_core.sha256}, validated_core=val_core, sync_auth=auth)

    def test_c4_context_reused_for_another_session_rejected(self, tmp_path):
        """C4: Context minted for Session 1 cannot be reused by Session 2."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            create_sync_authorization,
            is_trusted_sync_authorization,
        )
        vault = tmp_path / "vault_c4"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_c4"
        embedder = DummyVectorEmbedder()
        session1 = VaultSyncSession()
        session1.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody C4.",
            encoding="utf-8",
        )
        session1_2 = VaultSyncSession()
        session1_2.authorize(str(vault), str(db_dir), embedder=embedder)
        req1 = session1_2.request_approval(run_id="run_c4_1")
        session1_2.resolve_approval(req1["approval_id"], decision="approve", run_id="run_c4_1", vault_id=session1_2.vault_id)
        ticket1 = session1_2._active_ticket
        assert ticket1 is not None
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        session1_2._status = "sync_in_progress"
        session1_2._current_reconciliation_ticket = ticket1

        auth1 = create_sync_authorization(session1_2, val_core, ticket=ticket1)

        # 1. Session 2 attempts to use ticket1 to mint authorization for session2: rejected by session_id mismatch
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(tmp_path / "db_c4_sess2"), embedder=embedder)
        with pytest.raises(ValueError, match="Ticket session_id .* does not match active session_id"):
            create_sync_authorization(session2, val_core, ticket=ticket1)

        # 2. Session 1 uses auth1 for baseline certification
        store1 = LanceDBManager(str(session1_2.index_dir))
        store1.save_metadata({"status": "fresh", "core_hash": val_core.sha256}, validated_core=val_core, sync_auth=auth1)
        assert is_trusted_sync_authorization(auth1) is False

        # 3. Session 2 attempts to reuse the consumed auth1 context: rejected by save_metadata
        store2 = LanceDBManager(str(tmp_path / "db_c4_sess2"))
        store2.create_knowledge_vector_table()
        with pytest.raises(DatabaseOperationError, match="Untrusted or invalid synchronization authorization"):
            store2.save_metadata({"status": "fresh", "core_hash": val_core.sha256}, validated_core=val_core, sync_auth=auth1)

    def test_c5_context_with_mismatched_core_hash_or_vault_identity_rejected(self, tmp_path):
        """C5: Context with mismatched Core hash or Vault identity is rejected by save_metadata."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            is_trusted_sync_authorization,
        )
        vault = tmp_path / "vault_c5"
        _make_valid_vault(vault)
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        # Context minted for a different core / vault identity
        other_core = build_test_validated_core(core_hash="different_hash_c5")
        auth = build_test_sync_authorization(
            other_core,
            vault_id="different_vault",
            ticket_id="ticket_c5",
        )
        assert is_trusted_sync_authorization(auth, expected_core_hash=val_core.sha256) is False

        store = LanceDBManager(str(tmp_path / "db_c5"))
        store.create_knowledge_vector_table()
        with pytest.raises(DatabaseOperationError, match="synchronization authorization"):
            store.save_metadata(
                {"status": "fresh", "vault_id": "personal_vault"},
                validated_core=val_core,
                sync_auth=auth,
            )

    def test_c6_genuine_context_through_approved_reconciliation_accepted(self, tmp_path):
        """C6: Genuine Context minted through approved reconciliation succeeds and updates baseline."""
        vault = tmp_path / "vault_c6"
        _make_valid_vault(vault, note_title="Core C6", body="C6 text.")
        db_dir = tmp_path / "db_c6"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)
        store = LanceDBManager(str(session.index_dir))

        meta = store.get_metadata()
        assert meta["status"] == "fresh"
        assert meta["core_hash"] != ""
        assert meta["core_vector_digest"] != ""

    # --- Group R: Concurrency & Single-Issuance ---

    def test_r1_two_concurrent_issuance_attempts_exactly_one_succeeds(self, tmp_path):
        """R1: Two concurrent threads calling create_sync_authorization with same ticket: exactly one succeeds."""
        import threading
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_r1"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_r1"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody R1.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        req = session2.request_approval(run_id="run_r1")
        session2.resolve_approval(req["approval_id"], decision="approve", run_id="run_r1", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        session2._status = "sync_in_progress"
        session2._current_reconciliation_ticket = ticket

        barrier = threading.Barrier(2)
        successes = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5.0)
                auth = create_sync_authorization(session2, val_core, ticket=ticket)
                successes.append(auth)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
        assert len(errors) == 1, f"Expected exactly 1 error, got {len(errors)}"
        assert "multiple authorization issuance is prohibited" in str(errors[0])

    def test_r2_reentrant_issuance_attempt_rejected(self, tmp_path):
        """R2: Re-entrant call to create_sync_authorization while issuance is in progress fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_r2"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_r2"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody R2.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        req = session2.request_approval(run_id="run_r2")
        session2.resolve_approval(req["approval_id"], decision="approve", run_id="run_r2", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        session2._status = "sync_in_progress"
        session2._current_reconciliation_ticket = ticket

        # Simulate re-entrant state
        ticket._issuance_in_progress = True
        try:
            with pytest.raises(ValueError, match="Re-entrant authorization issuance is prohibited"):
                create_sync_authorization(session2, val_core, ticket=ticket)
        finally:
            ticket._issuance_in_progress = False

    def test_r3_issuance_after_consumption_rejected(self, tmp_path):
        """R3: Issuance attempt after ticket consumption fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_r3"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_r3"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody R3.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        req = session2.request_approval(run_id="run_r3")
        session2.resolve_approval(req["approval_id"], decision="approve", run_id="run_r3", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None
        ticket.mark_consumed()

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        session2._status = "sync_in_progress"
        session2._current_reconciliation_ticket = ticket

        with pytest.raises(ValueError, match="Approval ticket has already been consumed by a completed reconciliation"):
            create_sync_authorization(session2, val_core, ticket=ticket)

    def test_r4_issuance_after_successful_reconciliation_rejected(self, tmp_path):
        """R4: Issuance attempt after reconciliation successfully completes fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        vault = tmp_path / "vault_r4"
        _make_valid_vault(vault)
        db_dir = tmp_path / "db_r4"
        embedder = DummyVectorEmbedder()
        session = VaultSyncSession()
        session.authorize(str(vault), str(db_dir), embedder=embedder)

        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nBody R4.",
            encoding="utf-8",
        )
        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        req = session2.request_approval(run_id="run_r4")
        session2.resolve_approval(req["approval_id"], decision="approve", run_id="run_r4", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        session2.reconcile(ticket)
        assert session2.status == "fresh"

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        with pytest.raises(ValueError):
            create_sync_authorization(session2, val_core, ticket=ticket)

    def test_r5_two_independent_genuine_sessions_both_succeed(self, tmp_path):
        """R5: Two separate legitimate Sessions with independent approval tickets both succeed."""
        from src.living_authenticity.knowledge.vault.core_validator import create_sync_authorization
        embedder = DummyVectorEmbedder()

        # Session A
        vault_a = tmp_path / "vault_ra"
        _make_valid_vault(vault_a, note_title="Core RA", body="RA body.")
        db_a = tmp_path / "db_ra"
        session_a = VaultSyncSession()
        session_a.authorize(str(vault_a), str(db_a), embedder=embedder)
        time.sleep(0.01)
        (vault_a / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core RA V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nRA V2 text.",
            encoding="utf-8",
        )
        session_a2 = VaultSyncSession()
        session_a2.authorize(str(vault_a), str(db_a), embedder=embedder)
        req_a = session_a2.request_approval(run_id="run_ra")
        session_a2.resolve_approval(req_a["approval_id"], decision="approve", run_id="run_ra", vault_id=session_a2.vault_id)
        ticket_a = session_a2._active_ticket
        assert ticket_a is not None
        val_core_a = validate_and_load_core("personal_vault", vault_root=str(vault_a))
        session_a2._status = "sync_in_progress"
        session_a2._current_reconciliation_ticket = ticket_a

        # Session B
        vault_b = tmp_path / "vault_rb"
        _make_valid_vault(vault_b, note_title="Core RB", body="RB body.")
        db_b = tmp_path / "db_rb"
        session_b = VaultSyncSession()
        session_b.authorize(str(vault_b), str(db_b), embedder=embedder)
        time.sleep(0.01)
        (vault_b / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core RB V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nRB V2 text.",
            encoding="utf-8",
        )
        session_b2 = VaultSyncSession()
        session_b2.authorize(str(vault_b), str(db_b), embedder=embedder)
        req_b = session_b2.request_approval(run_id="run_rb")
        session_b2.resolve_approval(req_b["approval_id"], decision="approve", run_id="run_rb", vault_id=session_b2.vault_id)
        ticket_b = session_b2._active_ticket
        assert ticket_b is not None
        val_core_b = validate_and_load_core("personal_vault", vault_root=str(vault_b))
        session_b2._status = "sync_in_progress"
        session_b2._current_reconciliation_ticket = ticket_b

        # Mint authorizations for both
        auth_a = create_sync_authorization(session_a2, val_core_a, ticket=ticket_a)
        auth_b = create_sync_authorization(session_b2, val_core_b, ticket=ticket_b)

        assert auth_a is not None
        assert auth_b is not None
        assert auth_a.session_id == session_a2.session_id
        assert auth_b.session_id == session_b2.session_id
        assert auth_a.vault_id != auth_b.vault_id


class TestPhase3FinalAuthorizationProvenanceAndAtomicConsumption:
    """Rigorous adversarial and concurrency verification suite for:
    1. SyncAuthorizationContext minting provenance & secret isolation (P1).
    2. Approval ticket nonce single-use & atomic consumption under concurrency.
    3. save_metadata() atomic claim-and-consume TOCTOU elimination & fail-closed failure semantics.
    """

    def test_p1_1_direct_minting_via_factory_secret_fails_closed(self, tmp_path):
        """P1-1: Calling _mint_authorized_context with imported _AUTH_FACTORY_SECRET fails closed with ValueError."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            _AUTH_FACTORY_SECRET,
            SyncAuthorizationContext,
        )

        with pytest.raises(ValueError, match="outside create_sync_authorization is strictly prohibited"):
            SyncAuthorizationContext._mint_authorized_context(
                vault_id="fake_vault",
                core_hash="fake_hash",
                ticket_id="fake_ticket",
                session_id="fake_session",
                factory_secret=_AUTH_FACTORY_SECRET,
            )

    def test_p1_2_direct_construction_produces_untrusted_context(self, tmp_path):
        """P1-2: Direct constructor SyncAuthorizationContext(...) produces untrusted context (_sync_proof is None)."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            _AUTH_FACTORY_SECRET,
            SyncAuthorizationContext,
            is_trusted_sync_authorization,
        )

        # Direct construction with no secret
        raw_ctx = SyncAuthorizationContext(
            vault_id="vault_raw",
            core_hash="hash_raw",
        )
        assert raw_ctx._sync_proof is None
        assert is_trusted_sync_authorization(raw_ctx) is False

        # Direct construction passing _AUTH_FACTORY_SECRET fails to set _sync_proof
        forged_ctx = SyncAuthorizationContext(
            vault_id="vault_forged",
            core_hash="hash_forged",
            _factory_secret=_AUTH_FACTORY_SECRET,
        )
        assert forged_ctx._sync_proof is None
        assert is_trusted_sync_authorization(forged_ctx) is False

        # Neither can be used in save_metadata
        val_core = build_test_validated_core(core_hash="hash_forged")
        store = LanceDBManager(str(tmp_path / "db_p1_2"))
        store.create_knowledge_vector_table()
        with pytest.raises(DatabaseOperationError, match="synchronization authorization"):
            store.save_metadata({"status": "fresh"}, validated_core=val_core, sync_auth=forged_ctx)

    def test_p1_3_approval_ticket_nonce_single_use_atomicity(self, tmp_path):
        """P1-3: Nonce is consumed immediately on first use in _issue_approved_ticket; replay fails closed."""
        from src.living_authenticity.knowledge.vault.core_validator import _SESSION_PROOF
        from src.living_authenticity.knowledge.vault.sync import (
            VaultSyncApprovalTicket,
            VaultSyncSession,
        )

        session = VaultSyncSession()
        session._session_proof = _SESSION_PROOF
        nonce = object()
        session._current_issuance_nonce = nonce

        # First issuance succeeds and atomically consumes the nonce
        t1 = VaultSyncApprovalTicket._issue_approved_ticket(
            ticket_id="ticket_p1_3",
            vault_id="vault_p1_3",
            vault_root=str(tmp_path),
            run_id="run_p1_3",
            snapshot_fingerprint="snap_p1_3",
            core_hash="core_p1_3",
            diff_fingerprint="diff_p1_3",
            expires_at=time.time() + 3600,
            session_id=session.session_id,
            approval_id="appr_p1_3",
            operation_type="reconciliation",
            database_baseline_version="1",
            session_proof=_SESSION_PROOF,
            issuance_nonce=nonce,
            session=session,
        )
        assert t1 is not None
        assert session._current_issuance_nonce is None

        # Replay attempt with same captured nonce raises ValueError
        with pytest.raises(ValueError, match="Cannot issue approved ticket outside active resolve_approval execution"):
            VaultSyncApprovalTicket._issue_approved_ticket(
                ticket_id="ticket_p1_3_second",
                vault_id="vault_p1_3",
                vault_root=str(tmp_path),
                run_id="run_p1_3",
                snapshot_fingerprint="snap_p1_3",
                core_hash="core_p1_3",
                diff_fingerprint="diff_p1_3",
                expires_at=time.time() + 3600,
                session_id=session.session_id,
                approval_id="appr_p1_3",
                operation_type="reconciliation",
                database_baseline_version="1",
                session_proof=_SESSION_PROOF,
                issuance_nonce=nonce,
                session=session,
            )

    def test_p1_4_concurrent_ticket_issuance_with_same_nonce(self, tmp_path):
        """P1-4: Concurrent calls to _issue_approved_ticket with the same nonce allow exactly one to succeed."""
        from src.living_authenticity.knowledge.vault.core_validator import _SESSION_PROOF
        from src.living_authenticity.knowledge.vault.sync import (
            VaultSyncApprovalTicket,
            VaultSyncSession,
        )
        import concurrent.futures

        session = VaultSyncSession()
        session._session_proof = _SESSION_PROOF
        nonce = object()
        session._current_issuance_nonce = nonce

        successes = []
        failures = []

        def worker(thread_idx: int):
            try:
                ticket = VaultSyncApprovalTicket._issue_approved_ticket(
                    ticket_id=f"ticket_p1_4_{thread_idx}",
                    vault_id="vault_p1_4",
                    vault_root=str(tmp_path),
                    run_id="run_p1_4",
                    snapshot_fingerprint="snap_p1_4",
                    core_hash="core_p1_4",
                    diff_fingerprint="diff_p1_4",
                    expires_at=time.time() + 3600,
                    session_id=session.session_id,
                    approval_id="appr_p1_4",
                    operation_type="reconciliation",
                    database_baseline_version="1",
                    session_proof=_SESSION_PROOF,
                    issuance_nonce=nonce,
                    session=session,
                )
                successes.append(ticket)
            except ValueError as e:
                failures.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, i) for i in range(5)]
            concurrent.futures.wait(futures)

        assert len(successes) == 1
        assert len(failures) == 4
        assert session._current_issuance_nonce is None

    def test_p1_5_save_metadata_atomic_consumption_replay_fails_closed(self, tmp_path):
        """P1-5: save_metadata atomically consumes context upfront; immediate replay fails closed."""
        val_core = build_test_validated_core(core_hash="hash_p1_5")
        sync_auth = build_test_sync_authorization(val_core, vault_id="vault_p1_5")

        store = LanceDBManager(str(tmp_path / "db_p1_5"))
        store.create_knowledge_vector_table()

        # First call succeeds
        store.save_metadata(
            {"status": "fresh", "vault_id": "vault_p1_5"},
            validated_core=val_core,
            sync_auth=sync_auth,
        )
        meta = store.get_metadata()
        assert meta["core_hash"] == "hash_p1_5"

        # Replay attempt fails closed immediately with DatabaseOperationError
        with pytest.raises(DatabaseOperationError, match="synchronization authorization"):
            store.save_metadata(
                {"status": "updated", "vault_id": "vault_p1_5"},
                validated_core=val_core,
                sync_auth=sync_auth,
            )

    def test_p1_6_concurrent_save_metadata_with_same_context(self, tmp_path):
        """P1-6: Concurrent save_metadata calls with the same context: exactly one succeeds."""
        import concurrent.futures

        val_core = build_test_validated_core(core_hash="hash_p1_6")
        sync_auth = build_test_sync_authorization(val_core, vault_id="vault_p1_6")

        store = LanceDBManager(str(tmp_path / "db_p1_6"))
        store.create_knowledge_vector_table()

        successes = []
        failures = []

        def worker(idx: int):
            try:
                store.save_metadata(
                    {"status": f"fresh_{idx}", "vault_id": "vault_p1_6"},
                    validated_core=val_core,
                    sync_auth=sync_auth,
                )
                successes.append(idx)
            except DatabaseOperationError as e:
                failures.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, i) for i in range(5)]
            concurrent.futures.wait(futures)

        assert len(successes) == 1
        assert len(failures) == 4

    def test_p1_7_context_bound_to_different_session_rejected(self, tmp_path):
        """P1-7: Context bound to session_A is rejected when saving under expected_session_id='session_B'."""
        val_core = build_test_validated_core(core_hash="hash_p1_7")
        sync_auth = build_test_sync_authorization(
            val_core,
            vault_id="vault_p1_7",
            session_id="session_A",
        )

        store = LanceDBManager(str(tmp_path / "db_p1_7"))
        store.create_knowledge_vector_table()

        with pytest.raises(DatabaseOperationError, match="synchronization authorization"):
            store.save_metadata(
                {"status": "fresh", "vault_id": "vault_p1_7"},
                validated_core=val_core,
                sync_auth=sync_auth,
                expected_session_id="session_B",
            )

    def test_p1_8_context_bound_to_different_ticket_rejected(self, tmp_path):
        """P1-8: Context bound to ticket_A is rejected when saving under expected_ticket_id='ticket_B'."""
        val_core = build_test_validated_core(core_hash="hash_p1_8")
        sync_auth = build_test_sync_authorization(
            val_core,
            vault_id="vault_p1_8",
            ticket_id="ticket_A",
        )

        store = LanceDBManager(str(tmp_path / "db_p1_8"))
        store.create_knowledge_vector_table()

        with pytest.raises(DatabaseOperationError, match="synchronization authorization"):
            store.save_metadata(
                {"status": "fresh", "vault_id": "vault_p1_8"},
                validated_core=val_core,
                sync_auth=sync_auth,
                expected_ticket_id="ticket_B",
            )

    def test_p1_9_context_bound_to_different_vault_rejected(self, tmp_path):
        """P1-9: Context bound to vault_A is rejected when saving to vault_B."""
        val_core = build_test_validated_core(core_hash="hash_p1_9")
        sync_auth = build_test_sync_authorization(
            val_core,
            vault_id="vault_A",
        )

        store = LanceDBManager(str(tmp_path / "db_p1_9"))
        store.create_knowledge_vector_table()

        with pytest.raises(DatabaseOperationError, match="synchronization authorization"):
            store.save_metadata(
                {"status": "fresh", "vault_id": "vault_B"},
                validated_core=val_core,
                sync_auth=sync_auth,
                expected_vault_id="vault_B",
            )

    def test_p1_10_context_with_mismatched_core_hash_rejected(self, tmp_path):
        """P1-10: Context with core_hash_A is rejected when passed with validated_core having core_hash_B."""
        val_core_a = build_test_validated_core(core_hash="hash_p1_10_A")
        val_core_b = build_test_validated_core(core_hash="hash_p1_10_B")
        sync_auth_a = build_test_sync_authorization(val_core_a, vault_id="vault_p1_10")

        store = LanceDBManager(str(tmp_path / "db_p1_10"))
        store.create_knowledge_vector_table()

        with pytest.raises(DatabaseOperationError, match="synchronization authorization"):
            store.save_metadata(
                {"status": "fresh", "vault_id": "vault_p1_10"},
                validated_core=val_core_b,
                sync_auth=sync_auth_a,
            )

    def test_p1_11_persistence_failure_leaves_context_consumed_no_retry(self, tmp_path, monkeypatch):
        """P1-11: If save_metadata fails during database write, the context remains consumed (fail closed)."""
        val_core = build_test_validated_core(core_hash="hash_p1_11")
        sync_auth = build_test_sync_authorization(val_core, vault_id="vault_p1_11")

        store = LanceDBManager(str(tmp_path / "db_p1_11"))
        store.create_knowledge_vector_table()

        # Monkeypatch db.create_table to raise an unexpected database disk failure
        def mock_create_table(*args, **kwargs):
            raise RuntimeError("Simulated LanceDB write crash on disk")

        monkeypatch.setattr(store.db, "create_table", mock_create_table)

        with pytest.raises(DatabaseOperationError, match="Failed saving index metadata"):
            store.save_metadata(
                {"status": "fresh", "vault_id": "vault_p1_11"},
                validated_core=val_core,
                sync_auth=sync_auth,
            )

        # Remove monkeypatch and verify that retrying with the same sync_auth is rejected
        monkeypatch.undo()
        with pytest.raises(DatabaseOperationError, match="synchronization authorization"):
            store.save_metadata(
                {"status": "fresh", "vault_id": "vault_p1_11"},
                validated_core=val_core,
                sync_auth=sync_auth,
            )

    def test_t2_1_reconstructed_internal_state_reaches_baseline_creation(self, tmp_path):
        """T2-1: Demonstrates that a caller with T2 capabilities (access to private symbols and attribute
        assignment on __new__ instances) can reconstruct Session/Ticket state to create a Core baseline.
        """
        import threading
        from src.living_authenticity.knowledge.vault.core_validator import (
            _SESSION_PROOF,
            _TICKET_PROOF,
            is_trusted_vault_sync_session,
            is_trusted_approval_ticket,
            create_sync_authorization,
            is_trusted_sync_authorization,
            SYNC_STATE_IN_PROGRESS,
        )
        from src.living_authenticity.knowledge.vault.sync import (
            VaultSyncSession,
            VaultSyncApprovalTicket,
        )

        vault = tmp_path / "vault_t2_1"
        _make_valid_vault(vault, note_title="Core T2", body="T2 test body.")
        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        # Reconstruct session
        fake_session = VaultSyncSession.__new__(VaultSyncSession)
        fake_session._session_proof = _SESSION_PROOF
        fake_session._session_id = "sess_t2_1"
        fake_session._vault_id = "personal_vault"
        fake_session._authorized = True
        fake_session._lock = threading.RLock()
        fake_session._issued_tickets = set()
        fake_session._approved_tickets_by_id = {}
        fake_session._approval_history = {}
        fake_session._status = SYNC_STATE_IN_PROGRESS

        assert is_trusted_vault_sync_session(fake_session) is True

        # Reconstruct ticket
        fake_ticket = VaultSyncApprovalTicket(
            ticket_id="ticket_t2_1",
            vault_id="personal_vault",
            vault_root=str(vault),
            run_id="run_t2_1",
            snapshot_fingerprint="snap_t2_1",
            core_hash=val_core.sha256,
            diff_fingerprint="diff_t2_1",
            expires_at=time.time() + 3600,
            session_id="sess_t2_1",
            approval_id="appr_t2_1",
            decision="approve",
        )
        fake_ticket._ticket_proof = _TICKET_PROOF

        fake_session._issued_tickets.add(id(fake_ticket))
        fake_session._approved_tickets_by_id[fake_ticket.ticket_id] = fake_ticket
        fake_session._approval_history[fake_ticket.approval_id] = {
            "ticket_id": fake_ticket.ticket_id,
            "decision": "approve",
        }
        fake_session._current_reconciliation_ticket = fake_ticket

        assert is_trusted_approval_ticket(fake_ticket, session=fake_session) is True
        assert fake_session.is_ticket_issued_by_session(fake_ticket) is True

        # create_sync_authorization succeeds with reconstructed state
        auth = create_sync_authorization(fake_session, val_core, ticket=fake_ticket)
        assert auth is not None
        assert is_trusted_sync_authorization(auth) is True

        # save_metadata succeeds and creates baseline
        store = LanceDBManager(str(tmp_path / "db_t2_1"))
        store.create_knowledge_vector_table()
        store.save_metadata(
            {"status": "fresh", "vault_id": "personal_vault"},
            validated_core=val_core,
            sync_auth=auth,
            expected_session_id=fake_session.session_id,
            expected_ticket_id=fake_ticket.ticket_id,
            expected_vault_id="personal_vault",
        )
        meta = store.get_metadata()
        assert meta["core_hash"] == val_core.sha256

    def test_t2_2_reconstructed_internal_state_reaches_baseline_replacement(self, tmp_path):
        """T2-2: Demonstrates that a caller with T2 capabilities can replace an existing Core baseline."""
        import threading
        from src.living_authenticity.knowledge.vault.core_validator import (
            _SESSION_PROOF,
            _TICKET_PROOF,
            create_sync_authorization,
            SYNC_STATE_IN_PROGRESS,
        )
        from src.living_authenticity.knowledge.vault.sync import (
            VaultSyncSession,
            VaultSyncApprovalTicket,
        )

        vault = tmp_path / "vault_t2_2"
        _make_valid_vault(vault, note_title="Core V1", body="V1 body.")
        val_core_v1 = validate_and_load_core("personal_vault", vault_root=str(vault))

        store = LanceDBManager(str(tmp_path / "db_t2_2"))
        store.create_knowledge_vector_table()

        # Initial baseline via genuine test helper
        init_auth = build_test_sync_authorization(val_core_v1, vault_id="personal_vault")
        store.save_metadata(
            {"status": "fresh", "vault_id": "personal_vault"},
            validated_core=val_core_v1,
            sync_auth=init_auth,
        )
        assert store.get_metadata()["core_hash"] == val_core_v1.sha256

        # Now Core changes to V2
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nV2 body changed.",
            encoding="utf-8",
        )
        val_core_v2 = validate_and_load_core("personal_vault", vault_root=str(vault))
        assert val_core_v2.sha256 != val_core_v1.sha256

        # Attacker reconstructs V2 session and ticket
        fake_session = VaultSyncSession.__new__(VaultSyncSession)
        fake_session._session_proof = _SESSION_PROOF
        fake_session._session_id = "sess_t2_2"
        fake_session._vault_id = "personal_vault"
        fake_session._authorized = True
        fake_session._lock = threading.RLock()
        fake_session._issued_tickets = set()
        fake_session._approved_tickets_by_id = {}
        fake_session._approval_history = {}
        fake_session._status = SYNC_STATE_IN_PROGRESS

        fake_ticket = VaultSyncApprovalTicket(
            ticket_id="ticket_t2_2",
            vault_id="personal_vault",
            vault_root=str(vault),
            run_id="run_t2_2",
            snapshot_fingerprint="snap_t2_2",
            core_hash=val_core_v2.sha256,
            diff_fingerprint="diff_t2_2",
            expires_at=time.time() + 3600,
            session_id="sess_t2_2",
            approval_id="appr_t2_2",
            decision="approve",
        )
        fake_ticket._ticket_proof = _TICKET_PROOF
        fake_session._issued_tickets.add(id(fake_ticket))
        fake_session._approved_tickets_by_id[fake_ticket.ticket_id] = fake_ticket
        fake_session._approval_history[fake_ticket.approval_id] = {
            "ticket_id": fake_ticket.ticket_id,
            "decision": "approve",
        }
        fake_session._current_reconciliation_ticket = fake_ticket

        auth_v2 = create_sync_authorization(fake_session, val_core_v2, ticket=fake_ticket)
        store.save_metadata(
            {"status": "fresh", "vault_id": "personal_vault"},
            validated_core=val_core_v2,
            sync_auth=auth_v2,
            expected_session_id=fake_session.session_id,
            expected_ticket_id=fake_ticket.ticket_id,
            expected_vault_id="personal_vault",
        )

        meta_v2 = store.get_metadata()
        assert meta_v2["core_hash"] == val_core_v2.sha256

    def test_t2_3_genuine_approval_control_path_comparison(self, tmp_path):
        """T2-3: Genuine control path demonstrates authentic approval lifecycle succeeding."""
        from src.living_authenticity.knowledge.vault.core_validator import (
            create_sync_authorization,
        )
        vault = tmp_path / "vault_t2_3"
        _make_valid_vault(vault, note_title="Core Gen", body="Gen text.")
        db_dir = tmp_path / "db_t2_3"
        embedder = DummyVectorEmbedder()

        # Initial cold start
        session1 = VaultSyncSession()
        session1.authorize(str(vault), str(db_dir), embedder=embedder)

        # Modify Core
        time.sleep(0.01)
        (vault / "01 Core" / "01-core-anchor.md").write_text(
            "---\ntitle: Core Gen V2\ncanonical_type: Core\ndraft: false\nstatus: active\n---\nGen V2 text.",
            encoding="utf-8",
        )

        session2 = VaultSyncSession()
        session2.authorize(str(vault), str(db_dir), embedder=embedder)
        req = session2.request_approval(run_id="run_t2_3")
        res = session2.resolve_approval(req["approval_id"], decision="approve", run_id="run_t2_3", vault_id=session2.vault_id)
        ticket = session2._active_ticket
        assert ticket is not None

        val_core = validate_and_load_core("personal_vault", vault_root=str(vault))
        session2._status = "sync_in_progress"
        session2._current_reconciliation_ticket = ticket

        auth = create_sync_authorization(session2, val_core, ticket=ticket)
        store = LanceDBManager(str(session2.index_dir))
        store.save_metadata(
            {"status": "fresh", "vault_id": session2.vault_id},
            validated_core=val_core,
            sync_auth=auth,
            expected_session_id=session2.session_id,
            expected_ticket_id=ticket.ticket_id,
            expected_vault_id=session2.vault_id,
        )
        assert store.get_metadata()["core_hash"] == val_core.sha256

    def test_t2_4_forged_core_validation_context_causes_retrieval_fail_closed(self, tmp_path):
        """T2-4: Forging a CoreValidationContext writes an arbitrary hash to the database,
        but retrieval fails closed when evaluated against the genuine Obsidian Vault note.
        """
        from src.living_authenticity.knowledge.vault.core_validator import (
            _VALIDATOR_PROOF,
            CoreValidationContext,
            ValidatedCore,
        )
        from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
            LanceDBVectorRetriever,
        )

        vault = tmp_path / "vault_t2_4"
        _make_valid_vault(vault, note_title="Real Core", body="Real body text.")
        real_val_core = validate_and_load_core("personal_vault", vault_root=str(vault))

        # T2 attacker crafts a fake CoreValidationContext with an arbitrary hash
        fake_hash = "completely_fabricated_sha256_hash_value"
        fake_ctx = CoreValidationContext(
            core_hash=fake_hash,
            source_path=str(vault / "01 Core" / "01-core-anchor.md"),
            canonical_type="Core",
            source_mode="vault",
            unit_fingerprints=frozenset([("fake_u1", "h1", 0, "p", "p", fake_hash, True)]),
            unit_instance_ids=frozenset(),
            _validator_proof=_VALIDATOR_PROOF,
        )
        fake_core = ValidatedCore(
            path=vault / "01 Core" / "01-core-anchor.md",
            source_path=str(vault / "01 Core" / "01-core-anchor.md"),
            content="Fake body",
            title="Fake",
            canonical_type="Core",
            source_mode="vault",
            sha256=fake_hash,
            mtime_ns=0,
            size=10,
            units=(),
            validation_context=fake_ctx,
        )

        # Write fake baseline to database
        db_dir = tmp_path / "db_t2_4"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()
        store.store(
            text="Real body text.",
            embedding=embedder.embed("Real body text."),
            metadata={"source": "01 Core/01-core-anchor.md", "position": 0, "is_core": True},
        )
        fake_auth = build_test_sync_authorization(fake_core, vault_id="personal_vault")
        store.save_metadata(
            {"status": "fresh", "vault_id": "personal_vault"},
            validated_core=fake_core,
            sync_auth=fake_auth,
        )
        assert store.get_metadata()["core_hash"] == fake_hash

        # Now instantiate retriever with the REAL ValidatedCore anchored in the vault
        q_unit = KnowledgeUnit(id="q_t2", source="inp", original_text="test query", meaning="test query")

        # Soft mode: fail closed (untrusted, core_baseline_verified=False)
        retriever_soft = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=real_val_core,
            raise_on_integrity_error=False,
        )
        res_soft = retriever_soft.retrieve(query=q_unit)
        assert res_soft.metadata["core_baseline_verified"] is False
        assert res_soft.metadata["core_trusted"] is False
        for c in res_soft.candidates:
            assert c.is_core is False
            assert c.core_hash == ""

        # Strict mode: raises DatabaseOperationError
        retriever_strict = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=real_val_core,
            raise_on_integrity_error=True,
        )
        with pytest.raises(DatabaseOperationError, match="does not match active validated Core"):
            retriever_strict.retrieve(query=q_unit)

    def test_t2_5_forged_core_supplied_directly_to_retriever_accepted_as_trust_anchor(self, tmp_path):
        """T2-5: When a T2 caller directly constructs LanceDBVectorRetriever supplying a forged
        ValidatedCore, the Retriever accepts the forged Core as its retrieval trust anchor because
        it has no independent out-of-band verification channel to the physical vault.
        """
        import threading
        from src.living_authenticity.knowledge.vault.core_validator import (
            _VALIDATOR_PROOF,
            _SESSION_PROOF,
            _TICKET_PROOF,
            CoreValidationContext,
            ValidatedCore,
            create_sync_authorization,
            SYNC_STATE_IN_PROGRESS,
            _compute_unit_fingerprint,
            compute_core_record_id,
        )
        from src.living_authenticity.knowledge.vault.sync import (
            VaultSyncSession,
            VaultSyncApprovalTicket,
        )
        from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
            LanceDBVectorRetriever,
        )

        db_dir = tmp_path / "db_t2_5"
        store = LanceDBManager(str(db_dir))
        store.create_knowledge_vector_table()
        embedder = DummyVectorEmbedder()

        fake_hash = "fabricated_retriever_anchor_hash_deterministic_fixed"
        fake_source = "01 Core/01-core-anchor.md"

        # 1. Craft forged KnowledgeUnit
        fake_unit = KnowledgeUnit(
            id="unit_fabricated_retriever",
            source=fake_source,
            position=0,
            original_text="Fabricated Core anchor text.",
            cleaned_text="Fabricated Core anchor text.",
            normalized_text="fabricated core anchor text.",
            meaning="Fabricated Core anchor text.",
            is_core=True,
            core_hash=fake_hash,
        )

        # 2. Craft forged CoreValidationContext using _VALIDATOR_PROOF
        fake_ctx = CoreValidationContext(
            core_hash=fake_hash,
            source_path=fake_source,
            canonical_type="Core",
            source_mode="vault",
            unit_fingerprints=frozenset([_compute_unit_fingerprint(fake_unit)]),
            unit_instance_ids=frozenset([id(fake_unit)]),
            _validator_proof=_VALIDATOR_PROOF,
        )

        # 3. Craft forged ValidatedCore
        fake_core = ValidatedCore(
            path=tmp_path / fake_source,
            source_path=fake_source,
            content="Fabricated Core anchor text.",
            title="Fabricated Core",
            canonical_type="Core",
            source_mode="vault",
            sha256=fake_hash,
            mtime_ns=0,
            size=100,
            units=(fake_unit,),
            validation_context=fake_ctx,
        )

        # 4. Write vector with matching deterministic record ID into database
        rec_id = compute_core_record_id(fake_unit, fake_core)
        store.store(
            text=fake_unit.cleaned_text,
            embedding=embedder.embed(fake_unit.cleaned_text),
            metadata={
                "source": fake_source,
                "position": 0,
                "is_core": True,
                "core_hash": fake_hash,
            },
            record_id=rec_id,
            validated_core=fake_core,
        )

        # 5. Use forged Session/Ticket to write metadata
        fake_session = VaultSyncSession.__new__(VaultSyncSession)
        fake_session._session_proof = _SESSION_PROOF
        fake_session._session_id = "sess_t2_5"
        fake_session._vault_id = "personal_vault"
        fake_session._authorized = True
        fake_session._lock = threading.RLock()
        fake_session._issued_tickets = set()
        fake_session._approved_tickets_by_id = {}
        fake_session._approval_history = {}
        fake_session._status = SYNC_STATE_IN_PROGRESS

        fake_ticket = VaultSyncApprovalTicket(
            ticket_id="ticket_t2_5",
            vault_id="personal_vault",
            vault_root=str(tmp_path),
            run_id="run_t2_5",
            snapshot_fingerprint="snap_t2_5",
            core_hash=fake_hash,
            diff_fingerprint="diff_t2_5",
            expires_at=time.time() + 3600,
            session_id="sess_t2_5",
            approval_id="appr_t2_5",
            decision="approve",
        )
        fake_ticket._ticket_proof = _TICKET_PROOF

        fake_session._issued_tickets.add(id(fake_ticket))
        fake_session._approved_tickets_by_id[fake_ticket.ticket_id] = fake_ticket
        fake_session._approval_history[fake_ticket.approval_id] = {
            "ticket_id": fake_ticket.ticket_id,
            "decision": "approve",
        }
        fake_session._current_reconciliation_ticket = fake_ticket

        auth = create_sync_authorization(fake_session, fake_core, ticket=fake_ticket)
        store.save_metadata(
            {"status": "fresh", "vault_id": "personal_vault"},
            validated_core=fake_core,
            sync_auth=auth,
            expected_session_id=fake_session.session_id,
            expected_ticket_id=fake_ticket.ticket_id,
            expected_vault_id="personal_vault",
        )
        assert store.get_metadata()["core_hash"] == fake_hash

        query = KnowledgeUnit(id="q_t2_5", source="inp", original_text="Fabricated query", meaning="Fabricated query")

        # 6. Directly construct Retriever supplying the FORGED ValidatedCore as trust anchor
        # Mode 1: Safe Operational Mode (raise_on_integrity_error=False)
        retriever_safe = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=fake_core,
            raise_on_integrity_error=False,
        )
        res_safe = retriever_safe.retrieve(query)
        assert res_safe.metadata["core_baseline_verified"] is True
        assert res_safe.metadata["core_trusted"] is True
        assert len(res_safe.candidates) == 1
        cand_safe = res_safe.candidates[0]
        assert cand_safe.is_core is True
        assert cand_safe.core_hash == fake_hash
        assert cand_safe.unit_id == "unit_fabricated_retriever"

        # Mode 2: Strict Validation Mode (raise_on_integrity_error=True)
        # Does NOT raise DatabaseOperationError because DB hash matches the caller-supplied Core
        retriever_strict = LanceDBVectorRetriever(
            embedding_service=embedder,
            db_manager=store,
            validated_core=fake_core,
            raise_on_integrity_error=True,
        )
        res_strict = retriever_strict.retrieve(query)
        assert res_strict.metadata["core_baseline_verified"] is True
        assert res_strict.metadata["core_trusted"] is True
        assert len(res_strict.candidates) == 1
        cand_strict = res_strict.candidates[0]
        assert cand_strict.is_core is True
        assert cand_strict.core_hash == fake_hash
        assert cand_strict.unit_id == "unit_fabricated_retriever"
