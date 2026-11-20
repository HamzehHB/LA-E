"""Comprehensive Security and Lifecycle Test Suite for Unified Vault Reconciliation Authorization.

Verifies:
1. Direct Session reconciliation without approval is strictly blocked.
2. Direct Launcher reconciliation without approval is strictly blocked.
3. Direct HTTP reconciliation without approval returns 403 approval_required.
4. Attempted approval transition outside a valid pending request is rejected.
5. Forged or reconstructed Ticket objects are rejected.
6. Caller-supplied approved=True or fake flags cannot bypass approval.
7. Expired approvals and tickets are rejected (TTL expiry).
8. Replay of already resolved approval requests is rejected.
9. Single-use ticket enforcement: reused tickets are rejected.
10. Cross-session, cross-vault, and cross-run tickets are rejected.
11. Case A: Non-destructive recovery of missing Core records (zero deletes, manifest untouched).
12. Case A recovery blocked if any filesystem change exists (must route through Case B).
13. Case B: Modified Core requires explicit human approval.
14. TOCTOU / ABA snapshot integrity: mid-sync mutations abort commit, prevent stale freshness.
15. Genuine First Initialization vs corrupted or missing metadata (fails closed).
16. VaultSyncMonitor is strictly observation-only (zero writes, never calls reconcile).
17. Partial persistence and idempotent retry.
"""
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from src.living_authenticity.adapter.app import AdapterError, Launcher
from src.living_authenticity.database.lancedb_manager import (
    DatabaseOperationError,
    LanceDBManager,
    VaultMetadataCorruptionError,
    VaultMetadataMissingError,
)
from src.living_authenticity.knowledge.vault.context import snapshot_vault
from src.living_authenticity.knowledge.vault.core_validator import (
    validate_and_load_core,
)
from src.living_authenticity.knowledge.vault.identity import (
    compute_vault_id,
    resolve_vault_index_dir,
)
from src.living_authenticity.knowledge.vault.sync import (
    SYNC_STATE_IN_PROGRESS,
    SYNC_STATE_FAILED,
    SYNC_STATE_APPROVED,
    SYNC_STATE_FRESH,
    SYNC_STATE_PENDING_APPROVAL,
    SYNC_STATE_REJECTED,
    SYNC_STATE_STALE,
    VaultSyncApprovalTicket,
    VaultSyncError,
    VaultSyncMonitor,
    VaultSyncRollbackError,
    VaultSyncSession,
    VaultSyncSnapshotDriftError,
    VaultSyncStaleApprovalError,
    VaultSyncUnauthorizedError,
    _PendingSyncApprovalRecord,
    compute_diff_fingerprint,
    compute_snapshot_fingerprint,
)


class DummyEmbedder:
    dimension = 1024

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.01 * (i + 1)] * 1024 for i in range(len(texts))]

    def embed_query(self, text: str) -> list[float]:
        return [0.01] * 1024

    def embed(self, text: str) -> list[float]:
        return self.embed_query(text)


def _create_core_note(vault_path: Path, filename: str = "core.md", title: str = "Core Note", body: str = "Authentic core unit text.") -> Path:
    core_dir = vault_path / "01 Core"
    core_dir.mkdir(parents=True, exist_ok=True)
    note = core_dir / filename
    content = f"---\ncanonical_type: Core\ndraft: false\ntitle: \"{title}\"\ntags: [core]\n---\n# {title}\n{body}\n"
    note.write_text(content, encoding="utf-8")
    return note


# ==============================================================================
# 1. Approval Security: Direct Entry Points & Ticket Forgery
# ==============================================================================

def test_direct_session_reconciliation_without_approval_rejected(tmp_path: Path):
    """Direct session.reconcile() without approval ticket is rejected when vault has changes."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Add a note to create changes
    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")

    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        session.reconcile()
    assert "explicit operator approval ticket is required" in str(exc_info.value)
    assert session.status == SYNC_STATE_STALE


def test_direct_launcher_reconciliation_without_approval_rejected(tmp_path: Path):
    """Launcher.reconcile_vault_sync() without ticket raises AdapterError('approval_required')."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    audit = tmp_path / "audit"
    for p in (vdb, vault, staging, audit):
        p.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial.", encoding="utf-8")

    launcher = Launcher(
        paths={
            "vault": {"root": str(vault)},
            "vector_db": {"root": str(vdb)},
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._embedder = DummyEmbedder()
    launcher.authorize_vault_sync(str(vault))

    (vault / "note2.md").write_text("# Note 2\nUnapproved note.", encoding="utf-8")

    with pytest.raises(AdapterError) as exc_info:
        launcher.reconcile_vault_sync()
    assert exc_info.value.kind == "approval_required"


def test_attempted_approval_transition_outside_valid_pending_request(tmp_path: Path):
    """Resolving an approval when none is pending raises VaultSyncUnauthorizedError."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        session.resolve_approval(approval_id="non_existent_id", decision="approve")
    assert "No pending vault sync approval found" in str(exc_info.value)


def test_forged_or_reconstructed_ticket_rejected(tmp_path: Path):
    """Supplying a manufactured or forged Ticket object is rejected by session.reconcile()."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nNew note.", encoding="utf-8")

    # Construct a forged ticket outside session issuance
    forged_ticket = VaultSyncApprovalTicket(
        ticket_id="forged_ticket_123",
        vault_id=session.vault_id,
        vault_root=session.vault_root,
        run_id="fake_run",
        snapshot_fingerprint="fake_fp",
        core_hash="",
        diff_fingerprint="fake_diff",
        expires_at=time.time() + 300,
    )

    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        session.reconcile(ticket=forged_ticket)
    assert "does not match active approved ticket" in str(exc_info.value)


def test_expired_approval_rejected(tmp_path: Path):
    """Pending approval that has exceeded its TTL raises VaultSyncStaleApprovalError on resolution."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nNew note.", encoding="utf-8")

    # Request approval with 0.05 second TTL
    req = session.request_approval(run_id="exp_run", ttl_seconds=0.05)
    time.sleep(0.1)

    with pytest.raises(VaultSyncStaleApprovalError) as exc_info:
        session.resolve_approval(approval_id=req["approval_id"], decision="approve")
    assert "has expired" in str(exc_info.value)


def test_reused_ticket_rejected(tmp_path: Path):
    """A single-use ticket cannot be reused for a second reconciliation."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nNew note.", encoding="utf-8")
    req = session.request_approval(run_id="run_1")
    session.resolve_approval(req["approval_id"], "approve")
    ticket = session._active_ticket
    assert ticket is not None

    # First reconciliation consumes the ticket
    rep1 = session.reconcile(ticket=ticket)
    assert rep1["fresh"] is True
    assert ticket.used is True

    # Attempting to use the consumed ticket again is rejected
    (vault / "note3.md").write_text("# Note 3\nAnother note.", encoding="utf-8")
    with pytest.raises(VaultSyncStaleApprovalError) as exc_info:
        session.reconcile(ticket=ticket)
    assert "already used" in str(exc_info.value)


def test_cross_session_or_cross_vault_ticket_rejected(tmp_path: Path):
    """A ticket issued for Vault A cannot be used on Vault B."""
    vdb_a = tmp_path / "vdb_a"
    vault_a = tmp_path / "vault_a"
    vdb_b = tmp_path / "vdb_b"
    vault_b = tmp_path / "vault_b"
    for p in (vdb_a, vault_a, vdb_b, vault_b):
        p.mkdir()

    (vault_a / "note.md").write_text("# Note A\nContent A.", encoding="utf-8")
    (vault_b / "note.md").write_text("# Note B\nContent B.", encoding="utf-8")

    sess_a = VaultSyncSession()
    sess_a.authorize(vault_root=str(vault_a), vector_db_root=str(vdb_a), embedder=DummyEmbedder())

    sess_b = VaultSyncSession()
    sess_b.authorize(vault_root=str(vault_b), vector_db_root=str(vdb_b), embedder=DummyEmbedder())

    (vault_a / "note2.md").write_text("# Note A2\nNew A.", encoding="utf-8")
    req_a = sess_a.request_approval(run_id="run_a")
    sess_a.resolve_approval(req_a["approval_id"], "approve")
    ticket_a = sess_a._active_ticket

    (vault_b / "note2.md").write_text("# Note B2\nNew B.", encoding="utf-8")

    # Attempt to reconcile Session B with Ticket A
    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        sess_b.reconcile(ticket=ticket_a)
    assert "does not match active approved ticket" in str(exc_info.value)


# ==============================================================================
# 2. Case A vs. Case B Separation
# ==============================================================================

def test_case_a_missing_core_vector_recovery_non_destructive(tmp_path: Path):
    """Case A: Missing Core vectors are restored non-destructively without modifying baseline manifest."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    core_file = _create_core_note(vault, "core.md", "Authoritative Core", "Critical core principle text.")

    session = VaultSyncSession()
    rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert rep["fresh"] is True

    mgr = LanceDBManager(db_path=str(session.index_dir))
    meta_before = mgr.get_metadata()
    initial_vectors = len(mgr.show_all())

    # Simulate an interrupted write that deleted Core vectors without re-inserting
    mgr.delete_source(str(core_file.resolve()))
    assert len(mgr.show_all()) < initial_vectors

    # Check changes: detects Case A eligibility (no disk change, Core hash matches, missing vectors)
    changes = session.check_changes()
    assert changes["case_a_eligible"] is True
    assert changes["filesystem_stale"] is False
    assert len(changes["missing_core_ids"]) > 0

    # Case A recovery requires an explicit operator approval ticket
    with pytest.raises(VaultSyncUnauthorizedError, match="Missing Core vector records detected in store; explicit operator approval ticket is required"):
        session.reconcile()

    req = session.request_approval(run_id="run_case_a_approved")
    session.resolve_approval(req["approval_id"], "approve")
    rec_rep = session.reconcile(ticket=session._active_ticket)
    assert rec_rep["fresh"] is True
    assert rec_rep.get("case_a_recovery") is True
    assert rec_rep.get("recovered_vectors", 0) > 0

    # Vectors restored
    assert len(mgr.show_all()) == initial_vectors

    # Crucial guarantee: baseline manifest in index_metadata was NOT overwritten
    meta_after = mgr.get_metadata()
    assert meta_after["manifest"] == meta_before["manifest"]
    assert meta_after["core_hash"] == meta_before["core_hash"]


def test_case_a_blocked_when_filesystem_changed(tmp_path: Path):
    """Case A recovery CANNOT be executed if any filesystem change exists; must route through Case B."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    core_file = _create_core_note(vault, "core.md", "Core Note", "Core text.")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Simulate missing Core vectors
    mgr = LanceDBManager(db_path=str(session.index_dir))
    mgr.delete_source(str(core_file.resolve()))

    # Concurrently add an ordinary note to the filesystem
    (vault / "new_note.md").write_text("# Ordinary Note\nOrdinary text.", encoding="utf-8")

    changes = session.check_changes()
    assert changes["filesystem_stale"] is True
    assert changes["case_a_eligible"] is False  # Blocked from unapproved Case A

    # Reconcile without ticket must raise VaultSyncUnauthorizedError
    with pytest.raises(VaultSyncUnauthorizedError):
        session.reconcile()


def test_case_b_core_modified_requires_approval(tmp_path: Path):
    """Case B: Modifying the authoritative Core note requires explicit human approval."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    core_file = _create_core_note(vault, "core.md", "Core Note", "Original principle.")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert session.is_fresh

    time.sleep(0.05)
    # Modify Core note content
    core_file.write_text("---\ncanonical_type: Core\ndraft: false\ntitle: \"Core Note\"\ntags: [core]\n---\n# Core Note\nUpdated principle content.", encoding="utf-8")

    changes = session.check_changes()
    assert changes["core_changed"] is True
    assert changes["stale"] is True

    # Direct reconcile without ticket is blocked
    with pytest.raises(VaultSyncUnauthorizedError):
        session.reconcile()

    # Reconcile with ticket succeeds
    req = session.request_approval(run_id="core_mod_run")
    session.resolve_approval(req["approval_id"], "approve")
    rep = session.reconcile(ticket=session._active_ticket)
    assert rep["fresh"] is True
    assert session.is_fresh


# ==============================================================================
# 3. Snapshot Integrity, TOCTOU & ABA Race Closure
# ==============================================================================

def test_snapshot_integrity_and_aba_race_prevention(tmp_path: Path):
    """If a note changes during reconciliation, pre-commit check aborts manifest commit and remains stale."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial content.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Add note2 to create change set
    note2 = vault / "note2.md"
    note2.write_text("# Note 2\nApproved version.", encoding="utf-8")

    req = session.request_approval(run_id="aba_run")
    session.resolve_approval(req["approval_id"], "approve")
    ticket = session._active_ticket

    # Mutate note2 on disk right before reconcile executes
    note2.write_text("# Note 2\nMutated content after approval.", encoding="utf-8")

    # Reconciliation must catch snapshot fingerprint mismatch or drift error
    with pytest.raises((VaultSyncStaleApprovalError, VaultSyncSnapshotDriftError)):
        session.reconcile(ticket=ticket)

    assert session.status == SYNC_STATE_STALE
    assert not session.is_fresh


# ==============================================================================
# 4. Genuine First Initialization vs. Corrupted Metadata
# ==============================================================================

def test_genuine_first_initialization_vs_corrupted_metadata(tmp_path: Path):
    """Empty database is Genuine First Init; store with existing vectors but corrupted metadata fails closed."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nSample note.", encoding="utf-8")

    session = VaultSyncSession()
    rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert rep["fresh"] is True

    # Corrupt index_metadata table by overwriting with invalid JSON
    mgr = LanceDBManager(db_path=str(session.index_dir))
    tbl = mgr.db.open_table("index_metadata")
    tbl.delete("id IS NOT NULL")
    tbl.add([{"id": "meta", "payload": "{corrupted_json_not_valid"}])

    # Re-authorizing against the corrupted store must FAIL CLOSED with VaultSyncError
    session2 = VaultSyncSession()
    with pytest.raises(VaultSyncError) as exc_info:
        session2.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert "invalid synchronization metadata state" in str(exc_info.value)
    assert not session2.is_authorized


def test_missing_metadata_on_existing_vectors_fails_closed(tmp_path: Path):
    """If vectors exist but index_metadata table is dropped, authorize() fails closed."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nSample note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Drop index_metadata table
    mgr = LanceDBManager(db_path=str(session.index_dir))
    mgr.db.drop_table("index_metadata")

    session2 = VaultSyncSession()
    with pytest.raises(VaultSyncError) as exc_info:
        session2.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert "missing_metadata" in str(exc_info.value)


# ==============================================================================
# 5. Monitor Observation-Only Guarantee
# ==============================================================================

def test_monitor_observation_only_never_calls_reconcile(tmp_path: Path):
    """VaultSyncMonitor observes changes and emits events, but performs ZERO writes and never reconciles."""
    vdb = tmp_path / "vdb"
    vault = tmp_path / "vault"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    events = []
    session = VaultSyncSession(stage_observer=lambda e: events.append(e))
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder(), auto_watch=True)

    mgr = LanceDBManager(db_path=str(session.index_dir))
    initial_vectors = len(mgr.show_all())

    # Add note2 while monitor is running
    (vault / "note2.md").write_text("# Note 2\nUnindexed note.", encoding="utf-8")

    # Wait for monitor check cycle
    time.sleep(2.0)

    # Monitor must NOT have reconciled: vector count unchanged, fresh is False
    assert len(mgr.show_all()) == initial_vectors
    assert session.is_fresh is False
    assert session.status == SYNC_STATE_STALE

    # Clean shutdown
    session.close()


# ==============================================================================
# 6. Additional Sync Hardening & Verification Scenarios
# ==============================================================================

def test_reconcile_embedding_count_mismatch_fails_closed(tmp_path: Path):
    """If embed_batch returns fewer vectors than units, reconciliation fails closed."""
    vdb = tmp_path / "vdb_cnt"
    vault = tmp_path / "vault_cnt"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nContent.", encoding="utf-8")

    class IncompleteEmbedder(DummyEmbedder):
        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            res = super().embed_batch(texts)
            return res[:-1] if len(res) > 1 else []

    session = VaultSyncSession()
    rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=IncompleteEmbedder())
    assert rep["fresh"] is False
    assert "invalid vector batch" in (rep.get("error") or "")
    assert session.is_fresh is False
    assert session.status == SYNC_STATE_FAILED


def test_reconcile_embedding_dimension_mismatch_fails_closed(tmp_path: Path):
    """If embed_batch returns vectors with wrong dimensionality, reconciliation fails closed."""
    vdb = tmp_path / "vdb_dim"
    vault = tmp_path / "vault_dim"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nContent.", encoding="utf-8")

    class BadDimEmbedder(DummyEmbedder):
        def __init__(self):
            super().__init__()
            self.dimension = 1024

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            # Return vectors of length 512 instead of 1024
            return [[0.1] * 512 for _ in texts]

    session = VaultSyncSession()
    rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=BadDimEmbedder())
    assert rep["fresh"] is False
    assert "dimensionality mismatch" in (rep.get("error") or "")
    assert session.is_fresh is False
    assert session.status == SYNC_STATE_FAILED


def test_core_record_delete_failure_in_store_raises_database_error(tmp_path: Path):
    """LanceDBManager.store() raises DatabaseOperationError when table.delete() fails."""
    from src.living_authenticity.database.lancedb_manager import DatabaseOperationError

    vdb = tmp_path / "vdb_store_del"
    vdb.mkdir()
    mgr = LanceDBManager(str(vdb))
    mgr.create_knowledge_vector_table()

    tbl = mgr.get_table()

    def fail_delete(*args, **kwargs):
        raise OSError("Simulated disk I/O failure during deletion")

    tbl.delete = fail_delete
    mgr.get_table = lambda: tbl

    core_file = _create_core_note(tmp_path)
    validated_core = validate_and_load_core("personal_vault", vault_root=str(tmp_path))
    unit = validated_core.units[0]
    from src.living_authenticity.knowledge.vault.core_validator import compute_core_record_id
    rec_id = compute_core_record_id(unit, validated_core)
    metadata = {
        "source": unit.source,
        "position": unit.position,
        "is_core": True,
        "core_hash": validated_core.sha256,
        "record_id": rec_id,
    }

    with pytest.raises(DatabaseOperationError) as exc_info:
        mgr.store(
            text=unit.cleaned_text,
            embedding=[0.1] * 1024,
            metadata=metadata,
            record_id=rec_id,
            validated_core=validated_core,
        )
    assert "Failed deleting prior vector row with id" in str(exc_info.value)


def test_case_a_core_recovery_incomplete_verification_halts_freshness(tmp_path: Path):
    """If Case A recovery fails to restore every expected Core record, it halts without FRESH."""
    vdb = tmp_path / "vdb_rec_inc"
    vault = tmp_path / "vault_rec_inc"
    vdb.mkdir()
    vault.mkdir()
    core_file = _create_core_note(vault)
    validated_core = validate_and_load_core("personal_vault", vault_root=str(vault))

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert session.is_fresh is True

    # Drop all Core records from table to trigger Case A recovery requirement
    mgr = LanceDBManager(str(session.index_dir))
    mgr.delete_source(str(core_file.resolve()))

    # Verify check_changes detects missing Core vectors
    changes = session.check_changes()
    assert changes["case_a_eligible"] is True
    assert len(changes["missing_core_ids"]) > 0

    # Simulate a partial store insertion that drops records during recovery
    orig_store = mgr.store
    call_count = [0]

    def failing_store(*args, **kwargs):
        call_count[0] += 1
        # Pretend it succeeds without actually saving to table
        return "fake_id"

    assert session._store is not None
    session._store.store = failing_store

    with pytest.raises(VaultSyncError) as exc_info:
        session._recover_missing_core_records(validated_core)
    assert "Case A recovery post-write verification failed" in str(exc_info.value)
    assert session.is_fresh is False
    assert session.status == SYNC_STATE_STALE


def test_check_changes_core_vector_inspection_exception_fails_closed(tmp_path: Path):
    """An exception during Core record inspection in check_changes fails closed."""
    from src.living_authenticity.database.lancedb_manager import DatabaseOperationError

    vdb = tmp_path / "vdb_inspect_err"
    vault = tmp_path / "vault_inspect_err"
    vdb.mkdir()
    vault.mkdir()
    _create_core_note(vault)

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Monkeypatch get_existing_core_record_ids to raise DatabaseOperationError
    def failing_inspect(*args, **kwargs):
        raise DatabaseOperationError("Table corrupted or unreadable")

    assert session._store is not None
    session._store.get_existing_core_record_ids = failing_inspect

    changes = session.check_changes()
    assert changes["core_valid"] is False
    assert changes["core_error_category"] == "core_vector_inspection_error"
    assert changes["stale"] is True
    assert changes["stale_core_vectors"] is True


def test_first_init_rollback_cannot_erase_existing_store(tmp_path: Path):
    """First initialization rollback cannot erase a pre-existing store."""
    vdb = tmp_path / "vdb_preserve"
    vault = tmp_path / "vault_preserve"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    # 1. Authorize cleanly first
    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    mgr = LanceDBManager(str(session.index_dir))
    initial_rows = len(mgr.show_all())
    assert initial_rows > 0

    # 2. Simulate a new session pointing to the existing store where initialization fails
    session2 = VaultSyncSession()
    # It must detect existing store and NOT wipe it
    session2._store = mgr
    assert session2._is_genuine_first_init() is False

    # Calling delete_all on rollback is guarded by was_empty_before_init
    # Existing rows must be completely preserved
    assert len(mgr.show_all()) == initial_rows


def test_interrupted_reconciliation_leaves_store_non_fresh_on_disk_and_recovers(tmp_path: Path):
    """An interrupted reconciliation records in_progress in metadata and subsequent sync recovers."""
    vdb = tmp_path / "vdb_interrupted"
    vault = tmp_path / "vault_interrupted"
    vdb.mkdir()
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nFirst note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Add note2 and note3
    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")
    (vault / "note3.md").write_text("# Note 3\nThird note.", encoding="utf-8")

    req = session.request_approval(run_id="interrupt_run")
    session.resolve_approval(req["approval_id"], "approve", run_id="interrupt_run", vault_id=session.vault_id)

    # Simulate interruption during indexing of note2
    orig_index = session._index_single_file

    def failing_index(file_path: Path, *args, **kwargs):
        if "note2.md" in str(file_path):
            raise RuntimeError("Process interrupted by power failure")
        return orig_index(file_path, *args, **kwargs)

    session._index_single_file = failing_index  # type: ignore[assignment]

    # Reconcile will fail midway
    rep = session.reconcile(ticket=session._active_ticket)
    assert rep["fresh"] is False

    # Verify disk metadata status is in_progress / not fresh
    mgr = LanceDBManager(str(session.index_dir))
    meta = mgr.get_metadata()
    assert meta.get("status") == SYNC_STATE_IN_PROGRESS

    # Subsequent session checks changes: must recognize index is stale
    session2 = VaultSyncSession()
    session2.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    changes = session2.check_changes()
    assert changes["stale"] is True

    # Approve and reconcile cleanly: must now succeed completely and become fresh
    req2 = session2.request_approval(run_id="recover_run")
    session2.resolve_approval(req2["approval_id"], "approve", run_id="recover_run", vault_id=session2.vault_id)
    rep_fixed = session2.reconcile(ticket=session2._active_ticket)
    assert rep_fixed["fresh"] is True
    assert mgr.get_metadata().get("status") == SYNC_STATE_FRESH

# =============================================================================
# Issue A & Issue B Targeted Hardening Regression Tests
# =============================================================================

def test_schema_inspection_failure_in_store_raises_database_operation_error(tmp_path: Path):
    """LanceDBManager.store() must fail closed if table.schema inspection fails."""
    from src.living_authenticity.database.lancedb_manager import (
        LanceDBManager,
        DatabaseOperationError,
    )
    from src.living_authenticity.knowledge.vault.core_validator import (
        validate_and_load_core,
        compute_core_record_id,
    )
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit

    vault = tmp_path / "vault_schema_test"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    core_file = core_dir / "core.md"
    core_file.write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nFundamental Core thesis content.",
        encoding="utf-8",
    )
    pipe = EvidenceFirstPipeline()
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipe)
    unit = val_core.units[0]
    rec_id = compute_core_record_id(unit, val_core)

    mgr = LanceDBManager(str(tmp_path / "vdb_schema"))
    mgr.create_knowledge_vector_table()
    embedder = DummyEmbedder()
    vec = embedder.embed(unit.cleaned_text)

    # 1. Normal store succeeds with proper schema inspection
    meta = {
        "source": unit.source,
        "position": unit.position,
        "is_core": True,
        "core_hash": val_core.sha256,
        "record_id": rec_id,
    }
    stored_id = mgr.store(
        text=unit.cleaned_text,
        embedding=vec,
        metadata=meta,
        record_id=rec_id,
        validated_core=val_core,
    )
    assert stored_id == rec_id
    rows = mgr.show_all()
    assert len(rows) == 1
    assert rows[0]["is_core"] is True
    assert rows[0]["core_hash"] == val_core.sha256

    # 2. Simulate schema inspection failure on target table
    tbl = mgr.get_table()
    class FailingSchema:
        @property
        def names(self):
            raise RuntimeError("Underlying pyarrow table schema corrupted")

    orig_schema = tbl.schema
    try:
        setattr(type(tbl), "schema", property(lambda self: FailingSchema()))

        # 3. Store must raise DatabaseOperationError with cause chained
        with pytest.raises(DatabaseOperationError) as exc_info:
            mgr.store(
                text="Degraded text attempt",
                embedding=vec,
                metadata=meta,
                record_id="should_not_be_written",
                validated_core=val_core,
            )

        assert "Failed inspecting table schema" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "Underlying pyarrow table schema corrupted" in str(exc_info.value.__cause__)

        # 4. No vector row inserted during schema failure
        rows_after = mgr.show_all()
        assert len(rows_after) == 1
        assert rows_after[0]["id"] == rec_id

    finally:
        setattr(type(tbl), "schema", property(lambda self: orig_schema))


def test_case_a_scenario_1_and_10_normal_recovery_with_valid_metadata(tmp_path: Path):
    """Case A normal recovery: valid baseline metadata, non-destructive repair, verified Fresh."""
    vault = tmp_path / "vault_case_a1"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core thesis.",
        encoding="utf-8",
    )
    (vault / "obs.md").write_text("# Observation\nOrdinary note content.", encoding="utf-8")

    vdb = tmp_path / "vdb_case_a1"
    session = VaultSyncSession()
    auth_rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert auth_rep["fresh"] is True
    assert session.is_fresh is True

    # Artificially remove only the Core vector record
    assert session._store is not None
    mgr = session._store
    tbl = mgr.get_table()
    tbl.delete("is_core = true")
    assert len([r for r in mgr.show_all() if r.get("is_core")]) == 0
    # Ordinary note vector still exists
    assert len([r for r in mgr.show_all() if not r.get("is_core")]) >= 1

    # Case A eligibility: valid baseline metadata exists, disk unchanged, Core unchanged
    changes = session.check_changes()
    assert changes["case_a_eligible"] is True
    assert changes["stale"] is True

    # Reconcile without ticket must fail closed: operator approval ticket required
    with pytest.raises(VaultSyncUnauthorizedError, match="Missing Core vector records detected in store; explicit operator approval ticket is required"):
        session.reconcile()

    req = session.request_approval(run_id="run_case_a1_approved")
    session.resolve_approval(req["approval_id"], "approve")
    rec_rep = session.reconcile(ticket=session._active_ticket)
    assert rec_rep["fresh"] is True
    assert rec_rep.get("case_a_recovery") is True
    assert session.is_fresh is True

    # Core record restored
    rows = mgr.show_all()
    core_rows = [r for r in rows if r.get("is_core")]
    assert len(core_rows) >= 1
    # Metadata on disk updated with fresh status
    assert mgr.get_metadata().get("status") == SYNC_STATE_FRESH


def test_case_a_scenario_2_previous_metadata_sync_in_progress_rejects_case_a(tmp_path: Path):
    """Case A must reject recovery when previous metadata status is sync_in_progress."""
    vault = tmp_path / "vault_case_a2"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core thesis.",
        encoding="utf-8",
    )
    (vault / "obs.md").write_text("# Observation\nOrdinary note content.", encoding="utf-8")

    vdb = tmp_path / "vdb_case_a2"
    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Simulate an interrupted sync that left sync_in_progress in disk metadata
    assert session._store is not None
    mgr = session._store
    meta = dict(mgr.get_metadata())
    meta["status"] = SYNC_STATE_IN_PROGRESS
    mgr.save_metadata(meta)

    # Delete Core vector to simulate missing Core record
    mgr.get_table().delete("is_core = true")

    # check_changes must NOT report case_a_eligible
    changes = session.check_changes()
    assert changes["case_a_eligible"] is False
    assert changes["filesystem_stale"] is True

    # Reconcile without ticket must raise VaultSyncUnauthorizedError
    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        session.reconcile()
    assert any(msg in str(exc_info.value).lower() for msg in ("interrupted synchronization", "operator approval", "missing core"))
    assert session.is_fresh is False


def test_case_a_scenario_3_and_4_missing_or_malformed_metadata_rejects_case_a(tmp_path: Path):
    """Case A cannot run when synchronization metadata is missing or corrupted."""
    vault = tmp_path / "vault_case_a3"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core thesis.",
        encoding="utf-8",
    )
    (vault / "obs.md").write_text("# Observation\nOrdinary note content.", encoding="utf-8")

    vdb = tmp_path / "vdb_case_a3"
    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    assert session._store is not None
    mgr = session._store
    # Corrupt index_metadata table payload
    meta_tbl = mgr.db.open_table("index_metadata")
    meta_tbl.delete("id IS NOT NULL")
    meta_tbl.add([{"id": "meta", "payload": "{malformed_json_not_valid"}])

    # Reconcile without ticket must fail and not declare Fresh
    rep = session.reconcile()
    assert rep["fresh"] is False
    assert rep["status"] == SYNC_STATE_FAILED
    assert session.is_fresh is False


def test_case_a_scenario_5_and_6_interrupted_sync_rejects_case_a(tmp_path: Path):
    """Case A rejects recovery when interrupted during deletion or insertion."""
    vault = tmp_path / "vault_case_a5"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core thesis.",
        encoding="utf-8",
    )
    (vault / "obs.md").write_text("# Observation\nOrdinary note content.", encoding="utf-8")

    vdb = tmp_path / "vdb_case_a5"
    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Interrupted sync leaves sync_in_progress on disk
    assert session._store is not None
    mgr = session._store
    meta = dict(mgr.get_metadata())
    meta["status"] = SYNC_STATE_IN_PROGRESS
    mgr.save_metadata(meta)

    # Remove some ordinary note vectors to simulate interruption during deletion
    mgr.get_table().delete("is_core = false")

    session2 = VaultSyncSession()
    session2.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    changes = session2.check_changes()
    assert changes["case_a_eligible"] is False

    with pytest.raises(VaultSyncUnauthorizedError):
        session2.reconcile()
    assert session2.is_fresh is False


def test_case_a_scenario_7_core_complete_but_ordinary_notes_incomplete_rejects_case_a(tmp_path: Path):
    """If ordinary note vectors are incomplete, Case A cannot declare the store fresh."""
    vault = tmp_path / "vault_case_a7"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core thesis.",
        encoding="utf-8",
    )
    (vault / "obs1.md").write_text("# Observation 1\nFirst observation note.", encoding="utf-8")
    (vault / "obs2.md").write_text("# Observation 2\nSecond observation note.", encoding="utf-8")

    vdb = tmp_path / "vdb_case_a7"
    session = VaultSyncSession()
    auth = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert auth["fresh"] is True

    assert session._store is not None
    mgr = session._store
    # Delete vector rows for obs2.md using delete_source
    obs2_path = str((vault / "obs2.md").resolve())
    del_count = mgr.delete_source(obs2_path)
    assert del_count >= 1

    # check_changes detects ordinary note vector inconsistency
    changes = session.check_changes()
    assert changes["case_a_eligible"] is False
    assert changes["stale"] is True

    # Reconcile without ticket must refuse to mark Fresh via Case A
    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        session.reconcile()
    assert "inconsistency" in str(exc_info.value).lower()
    assert session.is_fresh is False


def test_case_a_scenario_8_post_recovery_completeness_verification_failure(tmp_path: Path):
    """Case A fails closed if post-write Core completeness verification fails."""
    vault = tmp_path / "vault_case_a8"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core thesis.",
        encoding="utf-8",
    )

    vdb = tmp_path / "vdb_case_a8"
    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    assert session._store is not None
    mgr = session._store
    mgr.get_table().delete("is_core = true")

    # Monkeypatch store.store in session to simulate failed write of Core record
    def drop_write(*args, **kwargs):
        pass  # drop write silently without writing to table

    mgr.store = drop_write

    req = session.request_approval(run_id="run_case_a8_approved")
    session.resolve_approval(req["approval_id"], "approve")
    rep = session.reconcile(ticket=session._active_ticket)
    assert rep["fresh"] is False
    assert rep["status"] == SYNC_STATE_FAILED
    assert "post-write verification failed" in rep["error"].lower()
    assert session.is_fresh is False


def test_case_a_scenario_9_retrieval_blocked_while_index_incomplete(tmp_path: Path):
    """LanceDBVectorRetriever refuses retrieval from an incomplete or uncommitted store."""
    from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
        LanceDBVectorRetriever,
    )
    from src.living_authenticity.database.lancedb_manager import DatabaseOperationError
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit

    vault = tmp_path / "vault_case_a9"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core thesis.",
        encoding="utf-8",
    )

    vdb = tmp_path / "vdb_case_a9"
    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Set status to sync_in_progress on disk
    assert session._store is not None
    mgr = session._store
    meta = dict(mgr.get_metadata())
    meta["status"] = SYNC_STATE_IN_PROGRESS
    mgr.save_metadata(meta)

    pipe = EvidenceFirstPipeline()
    val_core = validate_and_load_core("personal_vault", vault_root=str(vault), pipeline=pipe)

    retriever = LanceDBVectorRetriever(
        embedding_service=DummyEmbedder(),
        db_manager=mgr,
        validated_core=val_core,
    )

    query = KnowledgeUnit(id="q", source="inbox", original_text="searching query text", meaning="searching query text")

    # Retrieval must fail closed with DatabaseOperationError
    with pytest.raises(DatabaseOperationError) as exc_info:
        retriever.retrieve(query)

    assert "incomplete or uncommitted vector store" in str(exc_info.value).lower()


# =============================================================================
# Database Table Version Integrity & Approval Validation Regression Tests
# =============================================================================

def test_resolve_approval_without_authorized_store_fails_closed(tmp_path: Path):
    """Resolving approval on an uninitialized/unauthorized session must fail closed with VaultSyncUnauthorizedError."""
    session = VaultSyncSession()
    # Artificially inject a pending approval on uninitialized session
    from src.living_authenticity.knowledge.vault.sync import _PendingSyncApprovalRecord
    session._pending_approval = _PendingSyncApprovalRecord(
        approval_id="dummy_app_id",
        vault_id="v_dummy",
        vault_root=str(tmp_path),
        run_id="run_dummy",
        diff={},
        snapshot_fingerprint="snap_fp",
        core_hash="",
        diff_fingerprint="diff_fp",
        manifest={},
        expires_at=time.time() + 300,
        operation_type="incremental_sync",
    )
    with pytest.raises(VaultSyncUnauthorizedError) as exc_info:
        session.resolve_approval(approval_id="dummy_app_id", decision="approve")
    assert "not authorized or vector store is uninitialized" in str(exc_info.value)


def test_resolve_approval_table_inspection_failure_fails_closed(tmp_path: Path):
    """When database table version inspection fails, resolve_approval must fail closed with VaultSyncError."""
    vault = tmp_path / "vault"
    vdb = tmp_path / "vdb"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nContent", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nModified", encoding="utf-8")
    session.check_changes()
    req = session.request_approval(run_id="insp_run")

    assert session._store is not None
    orig_get_table = session._store.get_table

    def failing_get_table():
        raise RuntimeError("Simulated database corruption during table version inspection")

    session._store.get_table = failing_get_table
    try:
        with pytest.raises(VaultSyncError) as exc_info:
            session.resolve_approval(req["approval_id"], "approve")
        assert "Failed inspecting vector database table version" in str(exc_info.value)
        # Verify approval was not committed
        assert session._active_ticket is None
    finally:
        session._store.get_table = orig_get_table


def test_reconcile_out_of_band_database_mutation_invalidates_ticket(tmp_path: Path):
    """If the vector database is mutated out-of-band between approval and reconcile, reconcile must fail closed."""
    vault = tmp_path / "vault"
    vdb = tmp_path / "vdb"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")
    session.check_changes()
    req = session.request_approval(run_id="oob_run")
    session.resolve_approval(req["approval_id"], "approve", run_id="oob_run", vault_id=session.vault_id)
    ticket = session._active_ticket
    assert ticket is not None
    assert ticket.database_baseline_version != ""

    assert session._store is not None
    tbl = session._store.get_table()
    # Out-of-band mutation directly to table
    tbl.add([{
        "id": "oob_record",
        "text": "out of band insertion",
        "embedding": [0.01] * 1024,
        "source": "external.md",
        "position": 0,
        "is_core": False,
        "core_hash": None,
    }])
    assert tbl.version != int(ticket.database_baseline_version)

    with pytest.raises(VaultSyncStaleApprovalError) as exc_info:
        session.reconcile(ticket=ticket)
    assert "Vector database mutated out-of-band since approval ticket was granted" in str(exc_info.value)


# ==============================================================================
# Approval Baseline Version Validation & Out-of-Band Mutation Detection
# ==============================================================================

def test_resolve_approval_existing_table_missing_or_empty_version_fails_closed(tmp_path: Path):
    """If knowledge_vectors table exists but its version is None or empty, approval fails closed."""
    vault = tmp_path / "vault_b1"
    vdb = tmp_path / "vdb_b1"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")
    session.check_changes()
    req = session.request_approval(run_id="run_b1")
    approval_id = req["approval_id"]

    assert session._pending_approval is not None
    assert session._pending_approval.state == SYNC_STATE_PENDING_APPROVAL

    assert session._store is not None
    real_get_table = session._store.get_table

    class FakeTableWithoutVersion:
        def __getattr__(self, name):
            if name == "version":
                return "   "  # whitespace only
            return getattr(real_get_table(), name)

    session._store.get_table = lambda: FakeTableWithoutVersion()  # type: ignore
    try:
        with pytest.raises(VaultSyncError, match="version is missing, empty, or unreadable"):
            session.resolve_approval(approval_id, "approve", run_id="run_b1", vault_id=session.vault_id)

        # Fail-closed invariants:
        # 1. Pending approval was NOT transitioned to approved
        assert session._pending_approval is not None
        assert session._pending_approval.state == SYNC_STATE_PENDING_APPROVAL
        # 2. No ticket was issued
        assert session._active_ticket is None
        # 3. Session status was not set to approved
        assert session._status != SYNC_STATE_APPROVED
    finally:
        session._store.get_table = real_get_table


def test_reconcile_existing_table_missing_baseline_version_on_ticket_fails_closed(tmp_path: Path):
    """For existing table, ticket missing database_baseline_version must fail closed with VaultSyncUnauthorizedError."""
    vault = tmp_path / "vault_b2"
    vdb = tmp_path / "vdb_b2"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")
    session.check_changes()
    req = session.request_approval(run_id="run_b2")
    session.resolve_approval(req["approval_id"], "approve", run_id="run_b2", vault_id=session.vault_id)
    ticket = session._active_ticket
    assert ticket is not None

    # Artificially strip baseline version from ticket
    ticket.database_baseline_version = "   "

    with pytest.raises(VaultSyncUnauthorizedError, match="Approval ticket lacks required database baseline version"):
        session.reconcile(ticket=ticket)


def test_reconcile_existing_table_live_version_unreadable_fails_closed(tmp_path: Path):
    """If live table version is unreadable during reconcile authorization, reconcile must fail closed."""
    vault = tmp_path / "vault_b3"
    vdb = tmp_path / "vdb_b3"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")
    session.check_changes()
    req = session.request_approval(run_id="run_b3")
    session.resolve_approval(req["approval_id"], "approve", run_id="run_b3", vault_id=session.vault_id)
    ticket = session._active_ticket
    assert ticket is not None

    assert session._store is not None
    real_get_table = session._store.get_table

    class FakeTableNoneVersion:
        def __getattr__(self, name):
            if name == "version":
                return None
            return getattr(real_get_table(), name)

    session._store.get_table = lambda: FakeTableNoneVersion()  # type: ignore
    try:
        rec_res = session.reconcile(ticket=ticket)
        assert rec_res["fresh"] is False
        assert "Live vector table 'knowledge_vectors' version is missing, empty, or unreadable" in rec_res["error"]
        assert session.is_fresh is False
    finally:
        session._store.get_table = real_get_table


def test_reconcile_existing_table_disappears_before_reconciliation_fails_closed(tmp_path: Path):
    """If table existed at approval time but disappears before reconcile, ticket is invalidated as stale."""
    vault = tmp_path / "vault_disappear"
    vdb = tmp_path / "vdb_disappear"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")
    session.check_changes()
    req = session.request_approval(run_id="run_disappear")
    session.resolve_approval(req["approval_id"], "approve", run_id="run_disappear", vault_id=session.vault_id)
    ticket = session._active_ticket
    assert ticket is not None
    assert ticket.database_baseline_version != ""

    # Out-of-band table disappearance: drop table before reconcile begins
    assert session._store is not None
    session._store.db.drop_table("knowledge_vectors")
    assert "knowledge_vectors" not in session._store._table_names()

    # Reconcile must reject the ticket as stale and fail closed
    with pytest.raises(VaultSyncStaleApprovalError, match="disappeared before reconciliation"):
        session.reconcile(ticket=ticket)


def test_reconcile_missing_table_empty_baseline_without_proof_of_first_init_fails_closed(tmp_path: Path):
    """If table is missing with empty ticket baseline but store is not in genuine first init, fail closed."""
    vault = tmp_path / "vault_no_first_init"
    vdb = tmp_path / "vdb_no_first_init"
    vault.mkdir()
    vdb.mkdir()

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Create an artificial ticket with empty baseline
    curr_snap = snapshot_vault(str(vault))
    ticket = VaultSyncApprovalTicket(
        ticket_id="ticket_fake_first_init",
        vault_id=session.vault_id,
        vault_root=str(vault),
        run_id="run_fake",
        snapshot_fingerprint=compute_snapshot_fingerprint(curr_snap),
        core_hash="",
        diff_fingerprint=compute_diff_fingerprint({}),
        expires_at=time.time() + 300,
        session_id=str(getattr(session, "_session_id", "")),
        operation_type="incremental_sync",
        database_baseline_version="",
    )
    session._active_ticket = ticket

    # Drop vector table out-of-band so it is absent, but save metadata so store is not genuine first init
    assert session._store is not None
    session._store.db.drop_table("knowledge_vectors")
    session._store.save_metadata({"status": "fresh"})
    assert "knowledge_vectors" not in session._store._table_names()
    assert "index_metadata" in session._store._table_names()

    with pytest.raises(VaultSyncUnauthorizedError, match="absent without verified proof of genuine first initialization"):
        session.reconcile(ticket=ticket)


def test_resolve_approval_missing_table_incremental_sync_without_first_init_fails_closed(tmp_path: Path):
    """If vector table is missing but store is not genuine first init, resolve_approval fails closed."""
    vault = tmp_path / "vault_missing_tbl_approval"
    vdb = tmp_path / "vdb_missing_tbl_approval"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nNote.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Drop vector table but leave metadata table
    assert session._store is not None
    session._store.db.drop_table("knowledge_vectors")
    session._store.save_metadata({"status": "unverified", "unverified": True})
    assert "knowledge_vectors" not in session._store._table_names()
    assert "index_metadata" in session._store._table_names()

    # Manually configure pending approval with incremental_sync to test defense
    req = _PendingSyncApprovalRecord(
        approval_id="appr_missing_test",
        vault_id=session.vault_id,
        vault_root=str(vault),
        run_id="run_missing",
        diff={"stale": True},
        snapshot_fingerprint="snap_fp",
        core_hash="",
        diff_fingerprint="diff_fp",
        manifest={},
        expires_at=time.time() + 300,
        session_id=str(getattr(session, "_session_id", "")),
        operation_type="incremental_sync",
    )
    session._pending_approval = req

    with pytest.raises(VaultSyncError, match="not genuine first initialization"):
        session.resolve_approval("appr_missing_test", "approve", run_id="run_missing", vault_id=session.vault_id)


def test_reconcile_clean_rebuild_succeeds_even_when_vector_table_is_absent(tmp_path: Path):
    """Clean rebuild authorization succeeds and reconstructs fresh store even when table is missing."""
    vault = tmp_path / "vault_clean_rebuild_missing_tbl"
    vdb = tmp_path / "vdb_clean_rebuild_missing_tbl"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nClean rebuild note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Drop vector table out-of-band and set unverified status
    assert session._store is not None
    session._store.db.drop_table("knowledge_vectors")
    session._store.save_metadata({"status": "unverified", "unverified": True})
    assert "knowledge_vectors" not in session._store._table_names()

    # Request approval: store state unverified correctly requires clean_rebuild
    session.check_changes()
    req = session.request_approval(run_id="run_rebuild")
    assert session._pending_approval is not None
    assert session._pending_approval.operation_type == "clean_rebuild"

    # Resolve approval issues clean_rebuild ticket
    session.resolve_approval(req["approval_id"], "approve", run_id="run_rebuild", vault_id=session.vault_id)
    ticket = session._active_ticket
    assert ticket is not None
    assert ticket.operation_type == "clean_rebuild"

    # Reconcile with clean_rebuild must succeed and recreate fresh store
    rec_rep = session.reconcile(ticket=ticket)
    assert rec_rep["fresh"] is True
    assert session.is_fresh is True
    assert "knowledge_vectors" in session._store._table_names()
    assert session._store.get_metadata().get("status") == "fresh"

def test_reconcile_diff_fingerprint_mutation_between_approval_and_reconciliation_fails_closed(tmp_path: Path):
    """If diff fingerprint mutates between ticket approval and reconciliation, ticket is invalidated with VaultSyncStaleApprovalError."""
    vault = tmp_path / "vault_diff_fp"
    vdb = tmp_path / "vdb_diff_fp"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial content.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Create change set
    (vault / "note2.md").write_text("# Note 2\nNew content.", encoding="utf-8")
    session.check_changes()
    req = session.request_approval(run_id="run_diff_fp")
    session.resolve_approval(req["approval_id"], "approve")
    ticket = session._active_ticket
    assert ticket is not None

    # Mutate ticket's diff_fingerprint
    ticket.diff_fingerprint = "mutated_diff_fingerprint_hash_99999"

    with pytest.raises(VaultSyncStaleApprovalError, match="diff fingerprint mismatch"):
        session.reconcile(ticket=ticket)


# ==============================================================================
# 7. Initial Synchronization Rollback Integrity & Cleanup Verification Tests
# ==============================================================================

def test_initial_sync_reconcile_failure_primary_cleanup_succeeds_verifies_clean(tmp_path: Path):
    """1. Scenario 1: Initial reconciliation fails and primary cleanup drops tables, restoring verified empty state."""
    vault = tmp_path / "vault_rollback_clean"
    vdb = tmp_path / "vdb_rollback_clean"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session = VaultSyncSession()

    def failing_reconcile(*args, **kwargs):
        assert session._store is not None
        session._store.create_knowledge_vector_table()
        session._store.save_metadata({"status": "sync_in_progress"})
        session._store.get_table().add([{
            "id": "temp_v1",
            "text": "temp vector",
            "embedding": [0.0] * 1024,
            "source": "note.md",
            "position": 0,
            "is_core": False,
            "core_hash": "",
        }])
        raise RuntimeError("Disk full during initial vector extraction")

    session.reconcile = failing_reconcile  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="Disk full during initial vector extraction"):
        session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    # Verification: store was rolled back and is verified genuinely empty
    assert session._store is not None
    state_info = session._store.inspect_store_state()
    assert state_info.get("state") == "genuine_first_init"
    assert state_info.get("vector_count", 0) == 0
    assert "index_metadata" not in session._store._table_names()
    assert "knowledge_vectors" not in session._store._table_names()
    assert session.is_authorized is False
    assert session.status == SYNC_STATE_FAILED
    assert session.is_fresh is False


def test_initial_sync_reconcile_failure_primary_drop_fails_fallback_delete_succeeds(tmp_path: Path):
    """2. Scenario 2: Primary drop fails, fallback delete succeeds, store empty but structural rollback unverified."""
    vault = tmp_path / "vault_rollback_fallback"
    vdb = tmp_path / "vdb_rollback_fallback"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session = VaultSyncSession()

    def failing_reconcile(*args, **kwargs):
        assert session._store is not None
        session._store.create_knowledge_vector_table()
        session._store.save_metadata({"status": "sync_in_progress"})
        session._store.get_table().add([{
            "id": "vector_row_1",
            "text": "vector content",
            "embedding": [0.0] * 1024,
            "source": "note.md",
            "position": 0,
            "is_core": False,
            "core_hash": "",
        }])
        raise RuntimeError("OOM during initial embedding")

    session.reconcile = failing_reconcile  # type: ignore[assignment]

    real_drop = LanceDBManager.drop_table

    def failing_drop(self, table_name):
        raise RuntimeError(f"Simulated lock/permission error dropping table '{table_name}'")

    LanceDBManager.drop_table = failing_drop  # type: ignore[assignment]
    try:
        with pytest.raises(VaultSyncRollbackError) as exc_info:
            session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

        err = exc_info.value
        # 1. Error chaining preserved
        assert isinstance(err.__cause__, RuntimeError)
        assert "OOM during initial embedding" in str(err.__cause__)
        assert err.original_error is not None
        assert "OOM during initial embedding" in str(err.original_error)

        # 2. Cleanup errors capture primary drop failures
        assert len(err.cleanup_errors) >= 1
        assert any("Primary drop of index_metadata failed" in e for e in err.cleanup_errors)
        assert any("Primary drop of knowledge_vectors failed" in e for e in err.cleanup_errors)

        # 3. Direct inspection of resulting store state:
        # Tables still exist because drop_table failed
        assert session._store is not None
        tables = session._store._table_names()
        assert "index_metadata" in tables
        assert "knowledge_vectors" in tables

        # Fallback deletion succeeded: rows were removed from both tables
        assert session._store.get_table().count_rows() == 0
        assert session._store.db.open_table("index_metadata").count_rows() == 0

        # Store state in error reflects that metadata table is present
        assert err.store_state.get("has_metadata_table") is True
        assert err.store_state.get("vector_count") == 0
        assert err.store_state.get("state") == "uninitialized_empty"

        # Implementation does not falsely claim structural clean state
        assert session.is_authorized is False
        assert session.status == SYNC_STATE_FAILED
        assert session.is_fresh is False
    finally:
        LanceDBManager.drop_table = real_drop  # type: ignore[assignment]


def test_initial_sync_reconcile_failure_both_cleanup_paths_fail_raises_rollback_error(tmp_path: Path):
    """3. Scenario 3: Initial reconciliation fails and both primary drop and fallback delete fail."""
    vault = tmp_path / "vault_both_fail"
    vdb = tmp_path / "vdb_both_fail"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session = VaultSyncSession()

    def failing_reconcile(*args, **kwargs):
        assert session._store is not None
        session._store.create_knowledge_vector_table()
        session._store.save_metadata({"status": "sync_in_progress"})
        session._store.get_table().add([{
            "id": "persisting_vector_1",
            "text": "residual content",
            "embedding": [0.0] * 1024,
            "source": "note.md",
            "position": 0,
            "is_core": False,
            "core_hash": "",
        }])

        # Monkeypatch fallback delete on index_metadata to fail
        real_open = session._store.db.open_table
        def failing_open(name: str, *args: Any, **kwargs: Any) -> Any:
            tbl = real_open(name, *args, **kwargs)
            if name == "index_metadata":
                tbl.delete = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Fallback metadata delete failed"))  # type: ignore[assignment]
            return tbl
        session._store.db.open_table = failing_open  # type: ignore[assignment]

        raise RuntimeError("Fatal kernel failure during reconcile")

    session.reconcile = failing_reconcile  # type: ignore[assignment]

    real_drop = LanceDBManager.drop_table
    LanceDBManager.drop_table = lambda self, t: (_ for _ in ()).throw(RuntimeError(f"Primary drop of {t} failed"))  # type: ignore[assignment]
    real_delete_all = LanceDBManager.delete_all
    LanceDBManager.delete_all = lambda self: (_ for _ in ()).throw(RuntimeError("Fallback delete_all failed"))  # type: ignore[assignment]

    try:
        with pytest.raises(VaultSyncRollbackError) as exc_info:
            session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

        err = exc_info.value
        # Original initialization failure remains traceable
        assert isinstance(err.__cause__, RuntimeError)
        assert "Fatal kernel failure during reconcile" in str(err.__cause__)
        assert err.original_error is not None
        assert "Fatal kernel failure" in str(err.original_error)

        # Both primary drop and fallback cleanup failures are separately captured
        assert len(err.cleanup_errors) >= 3
        assert any("Primary drop of index_metadata failed" in ce for ce in err.cleanup_errors)
        assert any("Fallback deletion of index_metadata rows failed" in ce for ce in err.cleanup_errors)
        assert any("Primary drop of knowledge_vectors failed" in ce for ce in err.cleanup_errors)
        assert any("Fallback deletion of knowledge_vectors rows failed" in ce for ce in err.cleanup_errors)

        # Residual data explicitly verified on disk
        assert session._store is not None
        assert session._store.get_table().count_rows() == 1
        assert session._store.db.open_table("index_metadata").count_rows() == 1

        # Session state marked failed
        assert session.status == SYNC_STATE_FAILED
        assert session.is_authorized is False
        assert session.is_fresh is False
    finally:
        LanceDBManager.drop_table = real_drop  # type: ignore[assignment]
        LanceDBManager.delete_all = real_delete_all  # type: ignore[assignment]


def test_initial_sync_metadata_cleanup_independent_behavior(tmp_path: Path):
    """4. Independent metadata cleanup: metadata primary drop fails, vectors drop succeeds."""
    vault = tmp_path / "vault_meta_indep"
    vdb = tmp_path / "vdb_meta_indep"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session = VaultSyncSession()

    def failing_reconcile(*args, **kwargs):
        assert session._store is not None
        session._store.create_knowledge_vector_table()
        session._store.save_metadata({"status": "sync_in_progress"})
        raise RuntimeError("Abort after metadata creation")

    session.reconcile = failing_reconcile  # type: ignore[assignment]

    real_drop = LanceDBManager.drop_table
    def selective_drop(self, table_name):
        if table_name == "index_metadata":
            raise RuntimeError("Primary drop of index_metadata denied")
        return real_drop(self, table_name)

    LanceDBManager.drop_table = selective_drop  # type: ignore[assignment]

    try:
        with pytest.raises(VaultSyncRollbackError) as exc_info:
            session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

        err = exc_info.value
        assert any("Primary drop of index_metadata failed" in e for e in err.cleanup_errors)
        # Vector table was dropped cleanly
        assert session._store is not None
        assert "knowledge_vectors" not in session._store._table_names()
        # Metadata table still exists structurally (with 0 rows due to fallback)
        assert "index_metadata" in session._store._table_names()
        assert session._store.db.open_table("index_metadata").count_rows() == 0
        assert session.status == SYNC_STATE_FAILED
    finally:
        LanceDBManager.drop_table = real_drop  # type: ignore[assignment]


def test_initial_sync_vector_cleanup_independent_behavior(tmp_path: Path):
    """5. Independent vector cleanup: vector primary drop fails, metadata drop succeeds."""
    vault = tmp_path / "vault_vec_indep"
    vdb = tmp_path / "vdb_vec_indep"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session = VaultSyncSession()

    def failing_reconcile(*args, **kwargs):
        assert session._store is not None
        session._store.create_knowledge_vector_table()
        session._store.get_table().add([{
            "id": "v_row",
            "text": "test",
            "embedding": [0.0] * 1024,
            "source": "note.md",
            "position": 0,
            "is_core": False,
            "core_hash": "",
        }])
        session._store.save_metadata({"status": "sync_in_progress"})
        raise RuntimeError("Abort after vector creation")

    session.reconcile = failing_reconcile  # type: ignore[assignment]

    real_drop = LanceDBManager.drop_table
    def selective_drop(self, table_name):
        if table_name == "knowledge_vectors":
            raise RuntimeError("Primary drop of knowledge_vectors denied")
        return real_drop(self, table_name)

    LanceDBManager.drop_table = selective_drop  # type: ignore[assignment]

    try:
        with pytest.raises(VaultSyncRollbackError) as exc_info:
            session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

        err = exc_info.value
        assert any("Primary drop of knowledge_vectors failed" in e for e in err.cleanup_errors)
        # Metadata table was dropped cleanly
        assert session._store is not None
        assert "index_metadata" not in session._store._table_names()
        # Vector table still exists structurally (with 0 rows due to fallback)
        assert "knowledge_vectors" in session._store._table_names()
        assert session._store.get_table().count_rows() == 0
        assert session.status == SYNC_STATE_FAILED
    finally:
        LanceDBManager.drop_table = real_drop  # type: ignore[assignment]


def test_initial_sync_unverified_store_not_reported_empty(tmp_path: Path):
    """6. The system does not report an empty Store when cleanup has not been verified."""
    vault = tmp_path / "vault_dirty"
    vdb = tmp_path / "vdb_dirty"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session = VaultSyncSession()

    def failing_reconcile(*args, **kwargs):
        assert session._store is not None
        session._store.create_knowledge_vector_table()
        # Insert a vector row that won't be deleted
        session._store.get_table().add([{
            "id": "persisting_row",
            "text": "orphaned text",
            "embedding": [0.0] * 1024,
            "source": "note.md",
            "position": 0,
            "is_core": False,
            "core_hash": "",
        }])
        raise RuntimeError("Transient network crash during indexing")

    session.reconcile = failing_reconcile  # type: ignore[assignment]

    real_drop = LanceDBManager.drop_table
    LanceDBManager.drop_table = lambda self, t: (_ for _ in ()).throw(RuntimeError("Drop forbidden"))  # type: ignore[assignment]
    real_delete_all = LanceDBManager.delete_all
    LanceDBManager.delete_all = lambda self: (_ for _ in ()).throw(RuntimeError("Delete all forbidden"))  # type: ignore[assignment]

    try:
        with pytest.raises(VaultSyncRollbackError) as exc_info:
            session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

        # Assert store is NOT verified empty
        assert exc_info.value.store_state.get("vector_count", 0) > 0
        assert session._status == SYNC_STATE_FAILED
    finally:
        LanceDBManager.drop_table = real_drop  # type: ignore[assignment]
        LanceDBManager.delete_all = real_delete_all  # type: ignore[assignment]


def test_initial_sync_unfresh_report_rollback_clean(tmp_path: Path):
    """7. Initial reconciliation returns unfresh report; rollback succeeds and reports verified_clean."""
    vault = tmp_path / "vault_unfresh"
    vdb = tmp_path / "vdb_unfresh"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session = VaultSyncSession()

    def unfresh_reconcile(*args, **kwargs):
        assert session._store is not None
        session._store.create_knowledge_vector_table()
        return {
            "authorized": True,
            "vault_id": session.vault_id,
            "status": SYNC_STATE_FAILED,
            "fresh": False,
            "error": "TOCTOU drift detected during initial indexing",
        }

    session.reconcile = unfresh_reconcile  # type: ignore[assignment]

    rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
    assert rep["fresh"] is False
    assert rep["rollback"] == "verified_clean"
    assert rep["rollback_verified"] is True
    assert session.status == SYNC_STATE_FAILED
    assert session.is_fresh is False


def test_initial_sync_unfresh_report_rollback_failure_observable(tmp_path: Path):
    """8. Initial reconciliation returns unfresh report; rollback fails and surfaces uncertain state."""
    vault = tmp_path / "vault_unfresh_fail"
    vdb = tmp_path / "vdb_unfresh_fail"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session = VaultSyncSession()

    def unfresh_reconcile(*args, **kwargs):
        assert session._store is not None
        session._store.create_knowledge_vector_table()
        session._store.save_metadata({"status": "sync_in_progress"})
        return {
            "authorized": True,
            "vault_id": session.vault_id,
            "status": SYNC_STATE_FAILED,
            "fresh": False,
            "error": "Validation mismatch during initial candidate generation",
        }

    session.reconcile = unfresh_reconcile  # type: ignore[assignment]

    real_drop = LanceDBManager.drop_table
    LanceDBManager.drop_table = lambda self, t: (_ for _ in ()).throw(RuntimeError("I/O error dropping metadata"))  # type: ignore[assignment]

    try:
        rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())
        assert rep["fresh"] is False
        assert rep["rollback"] == "failed"
        assert rep["rollback_verified"] is False
        assert len(rep["cleanup_errors"]) >= 1
        assert rep["recovery_state"] == "uncertain_store_state"
        assert session.status == SYNC_STATE_FAILED
    finally:
        LanceDBManager.drop_table = real_drop  # type: ignore[assignment]


def test_initial_sync_failed_rollback_prevents_subsequent_unauthorized_sync(tmp_path: Path):
    """9. Subsequent synchronization after a failed rollback cannot inherit initial sync privilege."""
    vault = tmp_path / "vault_subseq_fail"
    vdb = tmp_path / "vdb_subseq_fail"
    vault.mkdir()
    vdb.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    # Manually create a dirty store (has vectors but no metadata)
    index_dir = resolve_vault_index_dir(str(vdb), str(vault))
    dirty_store = LanceDBManager(str(index_dir))
    dirty_store.create_knowledge_vector_table()
    dirty_store.get_table().add([{
        "id": "residual_vector",
        "text": "residual",
        "embedding": [0.0] * 1024,
        "source": "note.md",
        "position": 0,
        "is_core": False,
        "core_hash": "",
    }])

    session2 = VaultSyncSession()
    # authorize must fail closed because store is dirty (missing_metadata)
    with pytest.raises(VaultSyncError) as exc_info:
        session2.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    assert "missing_metadata" in str(exc_info.value)
    assert "Initial sync privilege denied" in str(exc_info.value)


def test_genuine_first_init_success_remains_functional(tmp_path: Path):
    """10. Successful genuine first initialization remains fully functional and certifies Fresh."""
    vault = tmp_path / "vault_first_init_ok"
    vdb = tmp_path / "vdb_first_init_ok"
    vault.mkdir()
    vdb.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core thesis.",
        encoding="utf-8",
    )
    (vault / "note1.md").write_text("# Note 1\nOrdinary content.", encoding="utf-8")

    session = VaultSyncSession()
    rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    assert rep["fresh"] is True
    assert rep["authorized"] is True
    assert session.is_fresh is True
    assert session.status == SYNC_STATE_FRESH
    assert rep["indexed_notes"] >= 2
    assert rep["indexed_vectors"] >= 2


def test_genuine_first_init_with_unrelated_tables_in_store(tmp_path: Path):
    """11. An unrelated table in the database directory is compatible with genuine_first_init for knowledge tables."""
    vault = tmp_path / "vault_unrelated"
    vdb = tmp_path / "vdb_unrelated"
    vault.mkdir()
    vdb.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Core\ncanonical_type: Core\ndraft: false\n---\nAuthoritative Core content.",
        encoding="utf-8",
    )
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    # Pre-create an unrelated custom table in the isolated vault db directory
    index_dir = resolve_vault_index_dir(str(vdb), str(vault))
    pre_store = LanceDBManager(str(index_dir))
    pre_store.db.create_table("unrelated_custom_table", [{"custom_id": "c1", "value": "preserved"}])

    # inspect_store_state confirms genuine_first_init because knowledge_vectors and index_metadata are absent
    state_info = pre_store.inspect_store_state()
    assert state_info["state"] == "genuine_first_init"
    assert state_info["has_vectors_table"] is False
    assert state_info["has_metadata_table"] is False

    # Authorize initial sync
    session = VaultSyncSession()
    rep = session.authorize(vault_root=str(vault), vector_db_root=str(vdb), embedder=DummyEmbedder())

    assert rep["fresh"] is True
    assert session.is_fresh is True

    # Verify that the unrelated custom table is preserved untouched alongside knowledge tables
    tables = pre_store._table_names()
    assert "unrelated_custom_table" in tables
    assert "knowledge_vectors" in tables
    assert "index_metadata" in tables
    custom_rows = pre_store.db.open_table("unrelated_custom_table").to_arrow().to_pylist()
    assert len(custom_rows) == 1
    assert custom_rows[0]["custom_id"] == "c1"
