"""Tests for Authorized Vault Synchronization (Process A).

Verifies:
- Identity computation and directory isolation per Vault.
- Strict authorization boundary: no sync before authorization.
- Disjoint boundary enforcement between Vault and Vector DB.
- Incremental change detection: additions, modifications, deletions, renames.
- Eligibility rules: non-markdown, symlinks, staging/audit/vdb exclusions.
- Clean revocation and monitor shutdown.
- Physical storage isolation between distinct Vaults.
"""
import time
from pathlib import Path
import pytest

from src.living_authenticity.database.lancedb_manager import (
    DatabaseOperationError,
    LanceDBManager,
)
from src.living_authenticity.knowledge.vault.identity import (
    compute_vault_id,
    resolve_vault_index_dir,
)
from src.living_authenticity.knowledge.vault.sync import (
    VaultSyncSession,
    VaultSyncError,
    SYNC_STATE_UNAUTHORIZED,
    SYNC_STATE_AUTHORIZING,
    SYNC_STATE_FRESH,
    SYNC_STATE_STALE,
    SYNC_STATE_FAILED,
    SYNC_STATE_REVOKED,
)
from src.living_authenticity.security.path_boundary import PathOutsideBoundaryError


class DummyEmbedder:
    """Mock embedder returning deterministic 1024-dimension float vectors."""
    dimension = 1024

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.01 * (i + 1)] * 1024 for i in range(len(texts))]

    def embed_query(self, text: str) -> list[float]:
        return [0.01] * 1024


def test_vault_id_and_resolution_boundary(tmp_path: Path):
    vdb_root = tmp_path / "vector_db"
    vdb_root.mkdir()
    vault_root = tmp_path / "vault_one"
    vault_root.mkdir()

    vid = compute_vault_id(str(vault_root))
    assert vid.startswith("vault_")
    assert len(vid) == 18  # "vault_" (6) + 12 hex chars

    # Stable id across calls
    assert compute_vault_id(str(vault_root)) == vid

    # Dedicated index directory inside vdb_root
    index_dir = resolve_vault_index_dir(str(vdb_root), str(vault_root))
    assert index_dir == vdb_root / vid
    assert str(index_dir).startswith(str(vdb_root))


def test_no_sync_before_authorization():
    session = VaultSyncSession()
    assert not session.is_authorized
    assert session.status == SYNC_STATE_UNAUTHORIZED
    assert not session.is_fresh

    # Reconcile fails cleanly when unauthorized
    res = session.reconcile()
    assert res["authorized"] is False
    assert res["status"] == SYNC_STATE_UNAUTHORIZED
    assert res["fresh"] is False


def test_disjoint_boundary_enforcement(tmp_path: Path):
    session = VaultSyncSession()
    parent = tmp_path / "parent_vault"
    parent.mkdir()
    nested_vdb = parent / "nested_vdb"
    nested_vdb.mkdir()

    # Nested vector DB inside vault must be rejected
    with pytest.raises(VaultSyncError, match="must be mutually disjoint"):
        session.authorize(
            vault_root=str(parent),
            vector_db_root=str(nested_vdb),
            embedder=DummyEmbedder(),
            auto_watch=False,
        )


def test_distinct_vaults_have_physical_storage_isolation(tmp_path: Path):
    vdb_root = tmp_path / "vector_db"
    vdb_root.mkdir()

    vault_a = tmp_path / "vault_a"
    vault_a.mkdir()
    (vault_a / "note_a.md").write_text("# Note in Vault A\nAuthentic content A", encoding="utf-8")

    vault_b = tmp_path / "vault_b"
    vault_b.mkdir()
    (vault_b / "note_b.md").write_text("# Note in Vault B\nAuthentic content B", encoding="utf-8")

    # Authorize Vault A
    session_a = VaultSyncSession()
    session_a.authorize(
        vault_root=str(vault_a),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
        auto_watch=False,
    )
    assert session_a.is_fresh
    index_dir_a = session_a.index_dir
    assert index_dir_a is not None
    assert index_dir_a.exists()

    # Authorize Vault B
    session_b = VaultSyncSession()
    session_b.authorize(
        vault_root=str(vault_b),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
        auto_watch=False,
    )
    assert session_b.is_fresh
    index_dir_b = session_b.index_dir
    assert index_dir_b is not None
    assert index_dir_b.exists()

    # Completely different directories and IDs
    assert session_a.vault_id != session_b.vault_id
    assert index_dir_a != index_dir_b

    # Verify tables in Vault A contain only Vault A data
    mgr_a = LanceDBManager(db_path=str(index_dir_a))
    rows_a = mgr_a.show_all()
    assert len(rows_a) > 0
    assert all("vault_a" in r["source"].lower() for r in rows_a)
    assert not any("vault_b" in r["source"].lower() for r in rows_a)

    # Verify tables in Vault B contain only Vault B data
    mgr_b = LanceDBManager(db_path=str(index_dir_b))
    rows_b = mgr_b.show_all()
    assert len(rows_b) > 0
    assert all("vault_b" in r["source"].lower() for r in rows_b)
    assert not any("vault_a" in r["source"].lower() for r in rows_b)


def test_incremental_change_detection_lifecycle(tmp_path: Path):
    vdb_root = tmp_path / "vector_db"
    vdb_root.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()

    # 1. Initial note
    note1 = vault / "note1.md"
    note1.write_text("# Note 1\nInitial content for note one.", encoding="utf-8")

    session = VaultSyncSession()
    rep1 = session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
        auto_watch=False,
    )
    assert rep1["indexed_notes"] == 1
    assert rep1["indexed_vectors"] > 0
    initial_vectors = rep1["indexed_vectors"]

    # 2. Add second note
    note2 = vault / "note2.md"
    note2.write_text("# Note 2\nSecond note with genuine content.", encoding="utf-8")

    rep2 = session.reconcile()
    assert rep2["added"] == 1
    assert rep2["indexed_notes"] == 2
    assert rep2["indexed_vectors"] > initial_vectors

    # 3. Modify first note
    # Ensure mtime changes
    time.sleep(0.05)
    note1.write_text("# Note 1 Modified\nUpdated content with more detail.", encoding="utf-8")

    rep3 = session.reconcile()
    assert rep3["modified"] == 1
    assert rep3["indexed_notes"] == 2

    # 4. Delete second note
    note2.unlink()
    rep4 = session.reconcile()
    assert rep4["removed"] == 1
    assert rep4["indexed_notes"] == 1

    # LanceDB table must no longer have rows for note2
    assert session.index_dir is not None
    mgr = LanceDBManager(db_path=str(session.index_dir))
    rows = mgr.show_all()
    assert not any("note2.md" in r["source"] for r in rows)


def test_eligibility_exclusions(tmp_path: Path):
    vdb_root = tmp_path / "vector_db"
    vdb_root.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()

    # Markdown note (valid)
    (vault / "valid.md").write_text("# Valid Note\nAuthentic text.", encoding="utf-8")
    # Non-markdown file (ignored)
    (vault / "data.csv").write_text("a,b,c\n1,2,3", encoding="utf-8")
    (vault / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    session = VaultSyncSession()
    rep = session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        staging_root=str(staging),
        audit_root=str(audit),
        embedder=DummyEmbedder(),
        auto_watch=False,
    )
    assert rep["indexed_notes"] == 1

    # Check eligibility helper
    assert session._is_file_eligible(vault / "valid.md")
    assert not session._is_file_eligible(vault / "data.csv")
    assert not session._is_file_eligible(vault / "image.png")

    # Staging note inside staging folder is rejected
    staging_note = staging / "staged.md"
    staging_note.write_text("# Staged\nDo not index", encoding="utf-8")
    assert not session._is_file_eligible(staging_note)

    # Audit file inside audit folder is rejected
    audit_file = audit / "trail.md"
    audit_file.write_text("# Audit log", encoding="utf-8")
    assert not session._is_file_eligible(audit_file)


def test_revocation_stops_monitor_and_updates_status(tmp_path: Path):
    vdb_root = tmp_path / "vector_db"
    vdb_root.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nSample note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
        auto_watch=True,  # Start background monitor
    )

    assert session.is_authorized
    status_before = session.get_status()
    assert status_before["sync_active"] is True

    # Revoke authorization
    rev_res = session.revoke()
    assert rev_res["authorized"] is False
    assert rev_res["status"] == SYNC_STATE_REVOKED
    assert not session.is_authorized

    status_after = session.get_status()
    assert status_after["authorized"] is False
    assert status_after["sync_active"] is False
    assert status_after["status"] == SYNC_STATE_REVOKED

    session.close()


def test_authorization_does_not_start_background_watcher(tmp_path: Path):
    """Authorization alone must NEVER spawn a background watcher in operational path.

    Adheres strictly to SECURITY.md (operator action only — no watcher and no background reindex).
    Explicit reconciliation remains required and authoritative.
    """
    import time
    vdb_root = tmp_path / "vector_db"
    vdb_root.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nFirst note.", encoding="utf-8")

    session = VaultSyncSession()
    # Default authorization without passing auto_watch
    session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
    )

    assert session.is_authorized
    assert session._monitor is None
    status = session.get_status()
    assert status["sync_active"] is False
    assert status["authorized"] is True
    assert status["fresh"] is True
    assert status["indexed_notes"] == 1

    # Add a new note to the vault
    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")
    time.sleep(0.1)

    # Verify no background watcher reconciled the new note automatically
    assert session._monitor is None
    status_unreconciled = session.get_status()
    assert status_unreconciled["sync_active"] is False
    assert status_unreconciled["indexed_notes"] == 1

    # Explicit operator reconciliation indexes the new note
    reconcile_res = session.reconcile()
    assert reconcile_res["fresh"] is True
    assert reconcile_res["indexed_notes"] == 2

    status_reconciled = session.get_status()
    assert status_reconciled["indexed_notes"] == 2
    assert status_reconciled["sync_active"] is False
    assert status_reconciled["fresh"] is True

    session.close()


def test_metadata_persistence(tmp_path: Path):
    vdb_root = tmp_path / "vector_db"
    vdb_root.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nSample metadata test.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
        auto_watch=False,
    )

    assert session.index_dir is not None
    mgr = LanceDBManager(db_path=str(session.index_dir))
    meta = mgr.get_metadata()
    assert meta["vault_id"] == session.vault_id
    assert meta["status"] == SYNC_STATE_FRESH
    assert meta["indexed_notes"] == 1
    assert meta["indexed_vectors"] > 0
    assert "manifest" in meta


def test_database_operation_error_propagation(tmp_path: Path):
    """Database failures must raise DatabaseOperationError and not fail silently."""
    db_path = tmp_path / "vdb_err"
    db_path.mkdir()
    mgr = LanceDBManager(db_path=str(db_path))
    mgr.create_knowledge_vector_table()

    # delete_source on invalid table
    class FailingTable:
        def delete(self, *args, **kwargs):
            raise RuntimeError("Simulated disk error")

    setattr(mgr, "get_table", lambda: FailingTable())

    with pytest.raises(DatabaseOperationError, match="Failed deleting vectors"):
        mgr.delete_source("some_source.md")

    with pytest.raises(DatabaseOperationError, match="Failed clearing knowledge vector table"):
        mgr.delete_all()


def test_partial_indexing_failure_recovery_and_idempotence(tmp_path: Path):
    """If one note fails during reconciliation, freshness is halted and retrying does not create duplicate vectors."""
    vdb_root = tmp_path / "vdb_partial"
    vdb_root.mkdir()
    vault = tmp_path / "vault_partial"
    vault.mkdir()

    note1 = vault / "note1.md"
    note1.write_text("# Note 1\nReliable content that will succeed.", encoding="utf-8")
    note2 = vault / "note2.md"
    note2.write_text("# Note 2\nContent that will fail on first attempt.", encoding="utf-8")

    class FlakyEmbedder(DummyEmbedder):
        def __init__(self):
            self.fail_note2 = True

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            if self.fail_note2 and any("fail on first attempt" in t for t in texts):
                raise RuntimeError("Embedding network timeout")
            return super().embed_batch(texts)

    embedder = FlakyEmbedder()
    session = VaultSyncSession()

    # 1. Authorize with failure on note2: must fail authorization cleanly
    rep = session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=embedder,
        auto_watch=False,
    )
    assert rep["fresh"] is False
    assert not session.is_authorized
    assert session.status == SYNC_STATE_FAILED

    # Manifest must not be marked fresh or fully completed
    mgr = LanceDBManager(db_path=str(session.index_dir))
    meta = mgr.get_metadata()
    assert meta.get("status") != SYNC_STATE_FRESH

    # 2. Fix the flaky condition
    embedder.fail_note2 = False

    # 3. Re-authorize / reconcile: must now succeed completely
    rep_fixed = session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=embedder,
        auto_watch=False,
    )
    assert rep_fixed["fresh"] is True
    assert session.is_authorized
    assert session.status == SYNC_STATE_FRESH
    assert rep_fixed["indexed_notes"] == 2

    # 4. Check vector rows: note 1 must have exactly its own vectors (no duplicate copies from retry)
    # note1 produces 2 units (# Note 1 header + body), note2 produces 2 units
    rows = mgr.show_all()
    note1_rows = [r for r in rows if "note1.md" in r["source"]]
    note2_rows = [r for r in rows if "note2.md" in r["source"]]
    assert len(note1_rows) == 2
    assert len(note2_rows) == 2


def test_metadata_save_failure_halts_freshness(tmp_path: Path):
    """If metadata persistence fails, reconciliation must report failed and not fresh."""
    vdb_root = tmp_path / "vdb_meta_fail"
    vdb_root.mkdir()
    vault = tmp_path / "vault_meta_fail"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")

    session: VaultSyncSession = VaultSyncSession()
    # First authorize cleanly
    session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
        auto_watch=False,
    )
    assert session.is_fresh

    # Now make save_metadata fail
    assert session._store is not None
    setattr(
        session._store,
        "save_metadata",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            DatabaseOperationError("Simulated metadata write failure")
        ),
    )

    # Add another note and reconcile
    (vault / "note2.md").write_text("# Note 2\nContent 2.", encoding="utf-8")
    rep = session.reconcile()

    assert rep["fresh"] is False
    assert session.status == SYNC_STATE_FAILED
    assert not session.is_fresh


def test_database_delete_failure_on_reconcile_halts_freshness(tmp_path: Path):
    """If database delete_source fails during reconciliation, freshness is halted and failure reported."""
    vdb_root = tmp_path / "vdb_del_fail"
    vdb_root.mkdir()
    vault = tmp_path / "vault_del_fail"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nContent 1.", encoding="utf-8")

    session: VaultSyncSession = VaultSyncSession()
    session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
        auto_watch=False,
    )
    assert session.is_fresh

    # Now simulate delete_source failure on the database store
    assert session._store is not None
    setattr(
        session._store,
        "delete_source",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            DatabaseOperationError("Simulated disk error during delete_source")
        ),
    )

    # Add a modified or new note to trigger delete_source during reconcile
    (vault / "note1.md").write_text("# Note 1 Updated\nUpdated content.", encoding="utf-8")
    rep = session.reconcile()

    assert rep["fresh"] is False
    assert session.status == SYNC_STATE_FAILED
    assert not session.is_fresh
    assert "Simulated disk error during delete_source" in str(rep.get("error", ""))


def test_check_changes_inspects_freshness_without_modifying_database(tmp_path: Path):
    """check_changes detects added, modified, and removed notes without mutating persistent vectors."""
    vdb_root = tmp_path / "vdb_check_changes"
    vdb_root.mkdir()
    vault = tmp_path / "vault_check_changes"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial note.", encoding="utf-8")

    session = VaultSyncSession()
    session.authorize(
        vault_root=str(vault),
        vector_db_root=str(vdb_root),
        embedder=DummyEmbedder(),
        auto_watch=False,
    )
    assert session.is_fresh
    assert session.status == SYNC_STATE_FRESH

    # 1. Unchanged vault check: reports stale=False, leaves fresh
    diff_unchanged = session.check_changes()
    assert diff_unchanged["authorized"] is True
    assert diff_unchanged["stale"] is False
    assert diff_unchanged["added"] == []
    assert session.is_fresh
    assert session.status == SYNC_STATE_FRESH

    # 2. Add a note: check_changes detects stale=True, added note, but does NOT write to store
    (vault / "note2.md").write_text("# Note 2\nSecond note.", encoding="utf-8")
    diff_added = session.check_changes()
    assert diff_added["stale"] is True
    assert "note2.md" in diff_added["added"]
    assert session.status == SYNC_STATE_STALE
    assert not session.is_fresh

    # Verify store was NOT modified: still only note1 rows
    assert session._store is not None
    rows = session._store.show_all()
    note2_rows = [r for r in rows if "note2.md" in r["source"]]
    assert len(note2_rows) == 0

    # 3. Modify note1: check_changes detects modified
    (vault / "note1.md").write_text("# Note 1 Modified\nModified content.", encoding="utf-8")
    diff_mod = session.check_changes()
    assert diff_mod["stale"] is True
    assert "note1.md" in diff_mod["modified"]

    # 4. Remove note2: check_changes detects removed
    (vault / "note2.md").unlink()
    diff_del = session.check_changes()
    assert diff_del["stale"] is True
    assert "note2.md" not in diff_del["added"]

    session.close()
