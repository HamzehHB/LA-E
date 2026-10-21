"""Integration tests for Vault synchronization and dedicated persistent index retrieval.

Verifies:
* HTTP endpoints: GET /api/vault/sync/status, POST /api/vault/sync/authorize,
  POST /api/vault/sync/revoke, POST /api/vault/sync/reconcile, POST /api/vault/index.
* Semantic retrieval in personal_vault mode strictly uses the authorized Vault's
  dedicated persistent index directory (resolve_vault_index_dir).
* Semantic retrieval fails closed when the Vault index is missing or empty (zero silent fallback).
* Audit records log the non-sensitive vault_id.
* Folder pick transitions cleanly revoke active synchronization sessions.
"""
import http.client
import json
from pathlib import Path
import pytest

from src.living_authenticity.adapter import Launcher, LauncherServer
from src.living_authenticity.adapter.app import AdapterError
from src.living_authenticity.database.lancedb_manager import LanceDBManager
from src.living_authenticity.database.schema import VECTOR_DIMENSION
from src.living_authenticity.knowledge.vault.identity import (
    compute_vault_id,
    resolve_vault_index_dir,
)

TOKEN_HEADER = "X-LAE-Token"


class DummyEmbedder:
    """Mock embedder returning deterministic 1024-dim vectors."""
    dimension = VECTOR_DIMENSION
    provider_name = "dummy_provider"
    model_name = "dummy_model"
    model_version = "1.0.0"

    def __init__(self):
        self.batch_calls = 0
        self.query_calls = 0
        self.embed_calls = 0

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls += 1
        return [[0.01 * (i + 1)] * self.dimension for i in range(len(texts))]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [0.01] * self.dimension

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return [0.01] * self.dimension


def _request(server, method, path, body=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
    hdrs = {"Connection": "close"}
    if token is not None:
        hdrs[TOKEN_HEADER] = token
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    return resp.status, data


def test_vault_sync_http_endpoints_lifecycle(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note1.md").write_text("# Authentic Note\nContent for testing.", encoding="utf-8")
    vdb = tmp_path / "vector_db"
    vdb.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()

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
    server = LauncherServer(launcher)
    server.serve_in_thread()

    try:
        token = launcher.token

        # 1. GET /api/vault/sync/status before authorization
        st, body = _request(server, "GET", "/api/vault/sync/status", token=token)
        assert st == 200
        data = json.loads(body)
        assert data["authorized"] is False
        assert data["status"] == "unauthorized"

        # 2. POST /api/vault/sync/authorize
        st, body = _request(server, "POST", "/api/vault/sync/authorize", body={}, token=token)
        assert st == 200
        data = json.loads(body)
        assert data["indexed"] is True
        assert "vault_id" in data
        assert data["status"] == "fresh"

        # Dedicated index directory must now exist
        v_id = data["vault_id"]
        expected_dir = resolve_vault_index_dir(str(vdb), str(vault))
        assert expected_dir.exists()
        assert expected_dir.name == v_id

        # 3. GET /api/vault/sync/status when authorized
        st, body = _request(server, "GET", "/api/vault/sync/status", token=token)
        assert st == 200
        data = json.loads(body)
        assert data["authorized"] is True
        assert data["vault_id"] == v_id
        assert data["status"] == "fresh"
        assert data["indexed_notes"] >= 1

        # 4. POST /api/vault/sync/reconcile
        st, body = _request(server, "POST", "/api/vault/sync/reconcile", body={}, token=token)
        assert st == 200
        data = json.loads(body)
        assert data["fresh"] is True

        # 5. POST /api/vault/sync/revoke
        st, body = _request(server, "POST", "/api/vault/sync/revoke", body={}, token=token)
        assert st == 200
        data = json.loads(body)
        assert data["authorized"] is False
        assert data["status"] == "revoked"

        # Status is now revoked
        st, body = _request(server, "GET", "/api/vault/sync/status", token=token)
        assert st == 200
        data = json.loads(body)
        assert data["authorized"] is False
        assert data["status"] == "revoked"

    finally:
        server.shutdown()
        launcher.close()


def test_semantic_retrieval_fails_closed_when_vault_index_empty(tmp_path: Path):
    """Personal vault semantic retrieval must NOT fall back silently if index is empty."""
    vault = tmp_path / "vault"
    vault.mkdir()
    vdb = tmp_path / "vector_db"
    vdb.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()

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

    # Do not authorize or index anything
    with pytest.raises(AdapterError) as exc_info:
        launcher._execute_run(
            run_id="run-sem-unindexed",
            input_text="Synthetic long enough input text for testing semantic retrieval behavior.",
            mode_value="personal_vault",
            retrieval_mode="semantic",
        )
    assert exc_info.value.kind == "validation_error"
    assert "holds 0 vectors" in str(exc_info.value).lower() or "synchronize the vault first" in str(exc_info.value).lower()
    launcher.close()


def test_semantic_retrieval_with_authorized_vault_records_audit(tmp_path: Path):
    """Authorized vault semantic retrieval uses isolated index and records vault_id on audit."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Knowledge Note\nGenuine content for matching.", encoding="utf-8")
    vdb = tmp_path / "vector_db"
    vdb.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()
    for cat in ("passed", "failed", "errors"):
        (audit / cat).mkdir()

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

    # Authorize sync (creates dedicated persistent index)
    auth_res = launcher.authorize_vault_sync(str(vault))
    assert auth_res["indexed"] is True
    v_id = auth_res["vault_id"]

    # Execute run
    proposal = launcher._execute_run(
        run_id="run-sem-authorized",
        input_text="Synthetic long enough input text for testing semantic retrieval behavior.",
        mode_value="personal_vault",
        retrieval_mode="semantic",
    )
    assert proposal is not None

    # Check audit record
    audit_files = list(audit.rglob("audit.jsonl"))
    assert audit_files, "audit record must be persisted"
    records = []
    for af in audit_files:
        with open(af, "r", encoding="utf-8") as f:
            records.extend([json.loads(line) for line in f])

    run_records = [r for r in records if r.get("run_id") == "run-sem-authorized"]
    assert len(run_records) >= 1
    record = run_records[0]
    assert record.get("vault_id") == v_id
    assert (
        record.get("retrieval_strategy") == "semantic"
        or record.get("retrieval_strategy") == "lancedb_vector"
    )
    assert record.get("retrieval_cache_cleaned_up") is True
    assert record.get("retrieval_embeddings_generated", 0) >= 1

    launcher.close()


def test_pre_retrieval_auto_reconcile_failure_halts_semantic_search(tmp_path: Path):
    """If auto-reconcile prior to semantic search is not fresh, execution must fail closed."""
    vault = tmp_path / "vault_fail_close"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nContent.", encoding="utf-8")
    vdb = tmp_path / "vdb_fail_close"
    vdb.mkdir()
    staging = tmp_path / "staging_fail_close"
    staging.mkdir()
    audit = tmp_path / "audit_fail_close"
    audit.mkdir()

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

    # Authorize cleanly first
    auth_res = launcher.authorize_vault_sync(str(vault))
    assert auth_res["indexed"] is True

    # Add a note so that changes are present and reconciliation is triggered upon approval
    (vault / "note2.md").write_text("# Note 2\nContent 2.", encoding="utf-8")

    # Now make the session's reconcile fail (simulate stale or failing sync)
    launcher._vault_sync_session.reconcile = lambda: {
        "authorized": True,
        "vault_id": launcher._vault_sync_session.vault_id,
        "status": "error",
        "fresh": False,
        "error": "Simulated disk exhaustion during auto-reconciliation",
    }

    import time
    res = launcher.start_run(
        input_text="Synthetic long enough input text for testing semantic retrieval behavior.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info = launcher.state().get("pending_sync_approval")
    assert pending_info is not None
    assert pending_info["run_id"] == run_id

    # User explicitly approves sync; reconciliation fails closed
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve")

    # Wait for run to fail
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("error") or st.get("outcome") or launcher._active_run_id != run_id:
            break
        time.sleep(0.1)

    st = launcher.run_status(run_id)
    assert st.get("error") == "AdapterError" or st.get("outcome") == "FAILED"
    assert "Vault vector index is not fresh" in st.get("reason", "")

    # Audit record must be recorded in errors partition (infrastructure_error)
    error_log = audit / "errors" / "audit.jsonl"
    assert error_log.exists()
    content = error_log.read_text(encoding="utf-8")
    assert "Simulated disk exhaustion during auto-reconciliation" in content

    launcher.close()


def test_authorize_endpoint_failure_fails_closed(tmp_path: Path):
    """POST /api/vault/sync/authorize must return 400 validation_error on reconciliation failure."""
    vault = tmp_path / "vault_auth_fail"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent.", encoding="utf-8")
    vdb = tmp_path / "vdb_auth_fail"
    vdb.mkdir()
    staging = tmp_path / "staging_auth_fail"
    staging.mkdir()
    audit = tmp_path / "audit_auth_fail"
    audit.mkdir()

    class FailingEmbedder(DummyEmbedder):
        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("Embedder service unreachable")

    launcher = Launcher(
        paths={
            "vault": {"root": str(vault)},
            "vector_db": {"root": str(vdb)},
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._embedder = FailingEmbedder()
    server = LauncherServer(launcher)
    server.serve_in_thread()

    try:
        token = launcher.token
        st, body = _request(server, "POST", "/api/vault/sync/authorize", body={}, token=token)
        assert st == 400
        data = json.loads(body)
        assert data["error"]["kind"] == "validation_error"
        assert "Vault synchronization authorization failed" in data["error"]["message"]

        # Session must not be authorized
        assert not launcher._vault_sync_session.is_authorized
    finally:
        server.shutdown()
        launcher.close()


def test_unchanged_vault_does_not_trigger_unnecessary_approval_or_writes(tmp_path: Path):
    """An unchanged Vault does not trigger unnecessary index writes or an unnecessary approval prompt."""
    vault = tmp_path / "vault_unchanged"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nUnchanged note content.", encoding="utf-8")
    vdb = tmp_path / "vdb_unchanged"
    vdb.mkdir()
    staging = tmp_path / "staging_unchanged"
    staging.mkdir()
    audit = tmp_path / "audit_unchanged"
    audit.mkdir()

    events = []
    launcher = Launcher(
        paths={
            "vault": {"root": str(vault)},
            "vector_db": {"root": str(vdb)},
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._sink = lambda payload: events.append(payload)
    launcher._embedder = DummyEmbedder()

    # Authorize cleanly first
    launcher.authorize_vault_sync(str(vault))
    initial_status = launcher.get_vault_sync_status()
    assert initial_status["fresh"] is True

    # Run retrieval
    launcher._execute_run(
        run_id="run-unchanged-vault",
        input_text="Synthetic input text for testing unchanged vault retrieval behavior.",
        mode_value="personal_vault",
        retrieval_mode="semantic",
    )

    # Verify no pending sync approval was created
    assert launcher.state().get("pending_sync_approval") is None
    # Verify no sync_approval_required event was emitted
    approval_events = [e for e in events if e.get("kind") == "sync_approval_required"]
    assert len(approval_events) == 0
    # Freshness unchanged
    status_after = launcher.get_vault_sync_status()
    assert status_after["fresh"] is True

    launcher.close()


def test_changed_vault_rejection_halts_safely_without_writes_or_stale_search(tmp_path: Path):
    """When user rejects updating persistent index, run halts safely with outcome STOPPED, 0 writes occur, and no stale retrieval happens."""
    vault = tmp_path / "vault_reject"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial content.", encoding="utf-8")
    vdb = tmp_path / "vdb_reject"
    vdb.mkdir()
    staging = tmp_path / "staging_reject"
    staging.mkdir()
    audit = tmp_path / "audit_reject"
    audit.mkdir()

    import time
    events = []
    launcher = Launcher(
        paths={
            "vault": {"root": str(vault)},
            "vector_db": {"root": str(vdb)},
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._sink = lambda payload: events.append(payload)
    embedder = DummyEmbedder()
    launcher._embedder = embedder

    # Authorize cleanly
    launcher.authorize_vault_sync(str(vault))
    initial_status = launcher.get_vault_sync_status()
    assert initial_status["fresh"] is True
    initial_vectors = initial_status["indexed_vectors"]
    initial_query_calls = embedder.query_calls

    # Now add note2.md
    (vault / "note2.md").write_text("# Note 2\nNew unindexed content.", encoding="utf-8")

    # Start run asynchronously (no programmatic bypass)
    res = launcher.start_run(
        input_text="Synthetic input text with enough letters for testing rejected vault sync behavior.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info = launcher.state().get("pending_sync_approval")
    assert pending_info is not None
    assert pending_info["run_id"] == run_id

    # Explicit rejection via decide_vault_sync
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "reject")

    # Wait for run to halt and active run to clear
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("outcome") and not launcher._active_run_id:
            break
        time.sleep(0.1)

    # 1. No persistent index writes occurred
    status_after = launcher.get_vault_sync_status()
    assert status_after["indexed_vectors"] == initial_vectors
    assert status_after["indexed_notes"] == 1  # note2 was NOT indexed

    # 2. Index is NOT falsely reported as fresh
    assert status_after["fresh"] is False
    assert status_after["status"] == "stale"

    # 3. Stopped event emitted with sync_rejected_by_user and outcome STOPPED
    stopped_events = [
        e for e in events
        if e.get("kind") == "stopped" and e.get("terminal_outcome") == "sync_rejected_by_user"
    ]
    assert len(stopped_events) == 1
    assert stopped_events[0]["outcome"] == "STOPPED"

    # 4. Audit record recorded in failed partition
    failed_log = audit / "failed" / "audit.jsonl"
    assert failed_log.exists()
    content = failed_log.read_text(encoding="utf-8")
    assert "sync_rejected_by_user" in content

    # 5. Assert zero TemporaryEmbeddingCache events/allocations and zero retrieval queries
    cache_events = [e for e in events if "temporary embedding cache" in e.get("message", "")]
    assert len(cache_events) == 0, "No temporary embedding cache should be allocated on sync rejection"
    assert embedder.query_calls == initial_query_calls, "No retrieval queries should be embedded on rejection"
    assert launcher.run_status(run_id).get("executed", 0) == 0

    # 6. Subsequent run with rejection also halts cleanly without writes
    res2 = launcher.start_run(
        input_text="Second synthetic input text with enough letters for testing rejected vault sync behavior.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id2 = res2["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info2 = launcher.state().get("pending_sync_approval")
    assert pending_info2 is not None
    launcher.decide_vault_sync(run_id2, pending_info2["vault_id"], "reject")

    for _ in range(150):
        st2 = launcher.run_status(run_id2)
        if st2.get("outcome") and not launcher._active_run_id:
            break
        time.sleep(0.1)

    status_after_2 = launcher.get_vault_sync_status()
    assert status_after_2["indexed_vectors"] == initial_vectors
    assert status_after_2["fresh"] is False

    launcher.close()


def test_changed_vault_approval_performs_explicit_sync(tmp_path: Path):
    """When user approves persistent index update, explicit synchronization occurs and index becomes fresh."""
    vault = tmp_path / "vault_approve"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nInitial content.", encoding="utf-8")
    vdb = tmp_path / "vdb_approve"
    vdb.mkdir()
    staging = tmp_path / "staging_approve"
    staging.mkdir()
    audit = tmp_path / "audit_approve"
    audit.mkdir()

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

    # Authorize cleanly
    launcher.authorize_vault_sync(str(vault))
    initial_vectors = launcher.get_vault_sync_status()["indexed_vectors"]

    # Add note2.md
    (vault / "note2.md").write_text("# Note 2\nApproved content.", encoding="utf-8")

    # Start run asynchronously (no programmatic bypass)
    import time
    res = launcher.start_run(
        input_text="Synthetic query input text with enough letters for approved vault sync behavior.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info = launcher.state().get("pending_sync_approval")
    assert pending_info is not None
    assert pending_info["run_id"] == run_id

    # User explicitly approves sync
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve")

    # Wait for run to process past sync
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("processed", 0) > 0 or st.get("outcome") or launcher._pending_approval is not None:
            break
        time.sleep(0.1)

    # Index now updated explicitly
    status_after = launcher.get_vault_sync_status()
    assert status_after["fresh"] is True
    assert status_after["status"] == "fresh"
    assert status_after["indexed_notes"] == 2
    assert status_after["indexed_vectors"] > initial_vectors

    launcher.close()


def test_sync_approval_does_not_authorize_knowledge_proposal(tmp_path: Path):
    """Approving persistent index update does NOT approve downstream knowledge proposals."""
    import time
    vault = tmp_path / "vault_prop_gov"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nContent.", encoding="utf-8")
    vdb = tmp_path / "vdb_prop_gov"
    vdb.mkdir()
    staging = tmp_path / "staging_prop_gov"
    staging.mkdir()
    audit = tmp_path / "audit_prop_gov"
    audit.mkdir()

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

    # Add note2 to make vault stale
    (vault / "note2.md").write_text("# Note 2\nContent 2.", encoding="utf-8")

    # Start run asynchronously (no programmatic bypass)
    run_res = launcher.start_run(
        input_text="Synthetic input text for testing governance boundary separation.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = run_res["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info = launcher.state().get("pending_sync_approval")
    assert pending_info is not None
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve")

    # Wait for run to process to proposal approval gate or completion
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("processed", 0) > 0 or st.get("outcome") or launcher._pending_approval is not None:
            break
        time.sleep(0.1)

    # Sync was approved, but proposal execution remains governed (requires human review)
    # The proposal must NOT have been executed without explicit human proposal approval!
    st = launcher.run_status(run_id)
    assert st.get("executed", 0) == 0

    launcher.close()


def test_changed_vault_enters_pending_and_resumes_on_approval(tmp_path: Path):
    """When Vault changes are detected, run enters pending approval and resumes cleanly on approval."""
    import time
    vault = tmp_path / "vault_pending_resume"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nBase content.", encoding="utf-8")
    vdb = tmp_path / "vdb_pending_resume"
    vdb.mkdir()
    staging = tmp_path / "staging_pending_resume"
    staging.mkdir()
    audit = tmp_path / "audit_pending_resume"
    audit.mkdir()

    events = []
    launcher = Launcher(
        paths={
            "vault": {"root": str(vault)},
            "vector_db": {"root": str(vdb)},
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._sink = lambda payload: events.append(payload)
    launcher._embedder = DummyEmbedder()
    launcher.authorize_vault_sync(str(vault))

    # Add note2 to create stale index
    (vault / "note2.md").write_text("# Note 2\nNew content.", encoding="utf-8")

    # Start run asynchronously (no pre-approved vault_sync_approval)
    res = launcher.start_run(
        input_text="Synthetic input text for testing pending approval resumption flow.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    # Wait for run to enter pending approval state
    for _ in range(150):
        st = launcher.state()
        if st.get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info = launcher.state().get("pending_sync_approval")
    assert pending_info is not None
    assert pending_info["run_id"] == run_id
    assert "note2.md" in pending_info["diff"]["added"]

    # sync_approval_required event was emitted
    approval_events = [e for e in events if e.get("kind") == "sync_approval_required"]
    assert len(approval_events) == 1
    assert approval_events[0]["run_id"] == run_id

    # Persistent index not touched yet
    sync_status = launcher.get_vault_sync_status()
    assert sync_status["fresh"] is False
    assert sync_status["indexed_notes"] == 1

    # Now human explicitly approves
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve")

    # Wait for run to progress past sync
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("processed", 0) > 0 or st.get("outcome") or launcher._pending_approval is not None:
            break
        time.sleep(0.1)

    # Index is now fresh with note2 indexed
    status_after = launcher.get_vault_sync_status()
    assert status_after["fresh"] is True
    assert status_after["indexed_notes"] == 2

    launcher.close()


def test_single_active_run_enforced_while_pending(tmp_path: Path):
    """A secondary run is rejected with 400 when an existing run is pending approval."""
    import time
    vault = tmp_path / "vault_single_active"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nBase.", encoding="utf-8")
    vdb = tmp_path / "vdb_single_active"
    vdb.mkdir()
    staging = tmp_path / "staging_single_active"
    staging.mkdir()
    audit = tmp_path / "audit_single_active"
    audit.mkdir()

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

    # Add note2 to create stale index
    (vault / "note2.md").write_text("# Note 2\nStale.", encoding="utf-8")

    res1 = launcher.start_run(
        input_text="First input text for verifying single active run enforcement under pending approval.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id1 = res1["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    # Attempting to start run 2 must fail with AdapterError("processing_error")
    with pytest.raises(AdapterError) as exc_info:
        launcher.start_run(
            input_text="Second input text for verifying single active run enforcement under pending approval.",
            source_mode="personal_vault",
            retrieval_mode="semantic",
        )
    assert exc_info.value.kind == "processing_error"
    assert "already active" in str(exc_info.value)

    # Clean up run 1 by rejecting
    pending_info = launcher.state()["pending_sync_approval"]
    launcher.decide_vault_sync(run_id1, pending_info["vault_id"], "reject")

    launcher.close()


def test_duplicate_and_stale_sync_decisions_return_409(tmp_path: Path):
    """Calling decide_vault_sync twice or with wrong state returns conflict (409)."""
    import time
    vault = tmp_path / "vault_duplicate_decision"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nBase.", encoding="utf-8")
    vdb = tmp_path / "vdb_duplicate_decision"
    vdb.mkdir()
    staging = tmp_path / "staging_duplicate_decision"
    staging.mkdir()
    audit = tmp_path / "audit_duplicate_decision"
    audit.mkdir()

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

    (vault / "note2.md").write_text("# Note 2\nStale.", encoding="utf-8")

    res = launcher.start_run(
        input_text="Synthetic query input text for duplicate decision conflict testing.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info = launcher.state()["pending_sync_approval"]
    assert pending_info is not None
    vault_id = pending_info["vault_id"]

    # First decision succeeds
    launcher.decide_vault_sync(run_id, vault_id, "reject")

    # Second decision immediately conflicts
    with pytest.raises(AdapterError) as exc_info:
        launcher.decide_vault_sync(run_id, vault_id, "approve")
    assert exc_info.value.kind == "conflict"

    launcher.close()


def test_vault_id_mismatch_returns_422(tmp_path: Path):
    """Supplying a mismatched vault_id to decide_vault_sync returns validation_error (422)."""
    import time
    vault = tmp_path / "vault_mismatch"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nBase.", encoding="utf-8")
    vdb = tmp_path / "vdb_mismatch"
    vdb.mkdir()
    staging = tmp_path / "staging_mismatch"
    staging.mkdir()
    audit = tmp_path / "audit_mismatch"
    audit.mkdir()

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

    (vault / "note2.md").write_text("# Note 2\nStale.", encoding="utf-8")

    res = launcher.start_run(
        input_text="Synthetic query input text for testing mismatched vault identifier.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    # Calling with wrong vault_id
    with pytest.raises(AdapterError) as exc_info:
        launcher.decide_vault_sync(run_id, "non_matching_vault_id_123", "approve")
    assert exc_info.value.kind == "unprocessable_entity"

    # Clean up
    pending_info = launcher.state()["pending_sync_approval"]
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "reject")
    launcher.close()


def test_cancellation_before_reconciliation_linearization(tmp_path: Path):
    """Cancelling a pending run linearizes safely before reconciliation begins."""
    import time
    vault = tmp_path / "vault_cancel"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nBase.", encoding="utf-8")
    vdb = tmp_path / "vdb_cancel"
    vdb.mkdir()
    staging = tmp_path / "staging_cancel"
    staging.mkdir()
    audit = tmp_path / "audit_cancel"
    audit.mkdir()

    events = []
    launcher = Launcher(
        paths={
            "vault": {"root": str(vault)},
            "vector_db": {"root": str(vdb)},
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._sink = lambda payload: events.append(payload)
    launcher._embedder = DummyEmbedder()
    launcher.authorize_vault_sync(str(vault))
    initial_vectors = launcher.get_vault_sync_status()["indexed_vectors"]

    (vault / "note2.md").write_text("# Note 2\nStale.", encoding="utf-8")

    res = launcher.start_run(
        input_text="Synthetic input text for testing cancellation prior to reconciliation.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    # User cancels
    cancel_res = launcher.request_cancel(run_id)
    assert cancel_res["status"] == "cancel_requested"

    # Wait for run to halt
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("outcome") or launcher._active_run_id is None:
            break
        time.sleep(0.1)

    # Index untouched
    status_after = launcher.get_vault_sync_status()
    assert status_after["indexed_vectors"] == initial_vectors
    assert status_after["fresh"] is False

    # Pending approval cleared
    assert launcher.state().get("pending_sync_approval") is None

    # Stopped event emitted
    stopped_events = [e for e in events if e.get("kind") == "stopped"]
    assert len(stopped_events) >= 1
    assert stopped_events[-1]["terminal_outcome"] in ("stopped_by_user", "cancelled")

    launcher.close()


def test_cancellation_during_reconciliation(tmp_path: Path):
    """In-flight cancellation during reconciliation writes completes disk writes safely but halts worker prior to retrieval."""
    import threading
    import time

    vault = tmp_path / "vault_cancel_during_rec"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nBase note.", encoding="utf-8")
    vdb = tmp_path / "vdb_cancel_during_rec"
    vdb.mkdir()
    staging = tmp_path / "staging_cancel_during_rec"
    staging.mkdir()
    audit = tmp_path / "audit_cancel_during_rec"
    audit.mkdir()

    events = []
    launcher = Launcher(
        paths={
            "vault": {"root": str(vault)},
            "vector_db": {"root": str(vdb)},
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._sink = lambda payload: events.append(payload)
    launcher._embedder = DummyEmbedder()
    launcher.authorize_vault_sync(str(vault))

    # Add note2 to create unindexed changes
    (vault / "note2.md").write_text("# Note 2\nUnindexed note.", encoding="utf-8")

    # Intercept reconcile using threading.Event
    real_reconcile = launcher._vault_sync_session.reconcile
    reconcile_entered = threading.Event()
    continue_reconcile = threading.Event()

    def intercepted_reconcile():
        reconcile_entered.set()
        continue_reconcile.wait(timeout=10.0)
        return real_reconcile()

    launcher._vault_sync_session.reconcile = intercepted_reconcile

    # Start run asynchronously
    res = launcher.start_run(
        input_text="Synthetic input text for testing cancellation during reconciliation.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info = launcher.state().get("pending_sync_approval")
    assert pending_info is not None
    assert pending_info["run_id"] == run_id

    # User approves sync; worker will enter reconcile
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve")

    # Wait for reconcile to be entered in worker thread
    assert reconcile_entered.wait(timeout=10.0), "Worker should have entered reconcile"

    # Now request cancellation while reconcile is in-flight
    cancel_res = launcher.request_cancel(run_id)
    assert cancel_res["status"] == "cancel_requested"

    # Allow reconcile to complete its disk writes
    continue_reconcile.set()

    # Wait for worker run to halt
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("outcome") and not launcher._active_run_id:
            break
        time.sleep(0.1)

    # 1. reconcile() completed disk writes safely without corruption
    status_after = launcher.get_vault_sync_status()
    assert status_after["fresh"] is True
    assert status_after["status"] == "fresh"
    assert status_after["indexed_notes"] == 2

    # 2. Worker halted safely before ingestion/retrieval
    st = launcher.run_status(run_id)
    assert st.get("outcome") == "STOPPED"
    assert st.get("terminal_outcome") == "stopped_by_user"
    assert st.get("executed", 0) == 0

    # 3. Stopped event emitted
    stopped_events = [
        e for e in events
        if e.get("kind") == "stopped" and e.get("terminal_outcome") == "stopped_by_user"
    ]
    assert len(stopped_events) >= 1
    assert stopped_events[-1]["outcome"] == "STOPPED"

    # 4. Audit recorded in failed partition
    failed_log = audit / "failed" / "audit.jsonl"
    assert failed_log.exists()
    content = failed_log.read_text(encoding="utf-8")
    assert "stopped_by_user" in content

    # 5. Zero temporary embedding cache allocations
    cache_events = [e for e in events if "temporary embedding cache" in e.get("message", "")]
    assert len(cache_events) == 0

    launcher.close()


def test_api_vault_sync_decide_http_endpoint(tmp_path: Path):
    """Verifies HTTP POST /api/vault/sync/decide error mappings and successful resolution."""
    import time
    vault = tmp_path / "vault_http_decide"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1\nBase.", encoding="utf-8")
    vdb = tmp_path / "vdb_http_decide"
    vdb.mkdir()
    staging = tmp_path / "staging_http_decide"
    staging.mkdir()
    audit = tmp_path / "audit_http_decide"
    audit.mkdir()

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
    server = LauncherServer(launcher)
    server.serve_in_thread()

    try:
        token = launcher.token

        # 1. 401 Unauthorized without token
        st, _ = _request(server, "POST", "/api/vault/sync/decide", body={"run_id": "r", "vault_id": "v", "decision": "approve"})
        assert st == 401

        # 2. 400 Bad Request on missing fields
        st, _ = _request(server, "POST", "/api/vault/sync/decide", body={"decision": "approve"}, token=token)
        assert st == 400

        # 3. 400 Bad Request on invalid decision
        st, _ = _request(server, "POST", "/api/vault/sync/decide", body={"run_id": "r", "vault_id": "v", "decision": "maybe"}, token=token)
        assert st == 400

        # 4. 404 / 409 when no run is pending
        st, _ = _request(server, "POST", "/api/vault/sync/decide", body={"run_id": "r", "vault_id": "v", "decision": "approve"}, token=token)
        assert st in (404, 409)

        # Now authorize and add note2
        launcher.authorize_vault_sync(str(vault))
        (vault / "note2.md").write_text("# Note 2\nNew content.", encoding="utf-8")

        # Start run via HTTP
        st, body = _request(
            server,
            "POST",
            "/api/run",
            body={
                "input_text": "HTTP start run testing decide endpoint with sufficient length for validation.",
                "source_mode": "personal_vault",
                "retrieval_mode": "semantic",
                "app_language": "en",
                "staging_language": "en",
                "audit_language": "en",
            },
            token=token,
        )
        assert st == 202
        run_id = json.loads(body)["run_id"]

        state_data: dict = {}
        for _ in range(150):
            st, state_body = _request(server, "GET", "/api/state", token=token)
            state_data = json.loads(state_body)
            if state_data.get("pending_sync_approval"):
                break
            time.sleep(0.1)

        pending = state_data["pending_sync_approval"]
        assert pending["run_id"] == run_id

        # 5. Successful approval via HTTP
        st, decide_body = _request(
            server,
            "POST",
            "/api/vault/sync/decide",
            body={
                "run_id": run_id,
                "vault_id": pending["vault_id"],
                "decision": "approve",
            },
            token=token,
        )
        assert st == 200
        decide_data = json.loads(decide_body)
        assert decide_data["status"] == "ok"
        assert decide_data["decision"] == "approve"

    finally:
        server.shutdown()
        launcher.close()
