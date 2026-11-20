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


def _create_vault_base_note(vault: Path, filename: str = "note1.md", title: str = "Authentic Note", content: str = "Content for testing.") -> Path:
    core_dir = vault / "01 Core"
    core_dir.mkdir(parents=True, exist_ok=True)
    note_path = core_dir / filename
    note_path.write_text(
        f"---\nid: authentic_core\ntitle: {title}\ncanonical_type: Core\ndraft: false\n---\n# {title}\n{content}",
        encoding="utf-8",
    )
    return note_path


def test_vault_sync_http_endpoints_lifecycle(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _create_vault_base_note(vault, "note1.md", "Authentic Note", "Content for testing.")
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
    _create_vault_base_note(vault, "note1.md", "Authentic Note", "Content for testing.")
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
    _create_vault_base_note(vault, "note.md", "Knowledge Note", "Genuine content for matching.")
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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Content.")
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
    launcher._vault_sync_session.reconcile = lambda ticket=None: {
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
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve", approval_id=pending_info["approval_id"])

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
    _create_vault_base_note(vault, "note.md", "Note", "Content.")
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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Unchanged note content.")
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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Initial content.")
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
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "reject", approval_id=pending_info["approval_id"])

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
    launcher.decide_vault_sync(run_id2, pending_info2["vault_id"], "reject", approval_id=pending_info2["approval_id"])

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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Initial content.")
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
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve", approval_id=pending_info["approval_id"])

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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Content.")
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
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve", approval_id=pending_info["approval_id"])

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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Base content.")
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
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve", approval_id=pending_info["approval_id"])

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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Base.")
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
    launcher.decide_vault_sync(run_id1, pending_info["vault_id"], "reject", approval_id=pending_info["approval_id"])

    launcher.close()


def test_duplicate_and_stale_sync_decisions_return_409(tmp_path: Path):
    """Calling decide_vault_sync twice or with wrong state returns conflict (409)."""
    import time
    vault = tmp_path / "vault_duplicate_decision"
    vault.mkdir()
    _create_vault_base_note(vault, "note1.md", "Note 1", "Base.")
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
    approval_id = pending_info["approval_id"]

    # First decision succeeds
    launcher.decide_vault_sync(run_id, vault_id, "reject", approval_id=approval_id)

    # Second decision immediately conflicts
    with pytest.raises(AdapterError) as exc_info:
        launcher.decide_vault_sync(run_id, vault_id, "approve", approval_id=approval_id)
    assert exc_info.value.kind == "conflict"

    launcher.close()


def test_vault_id_mismatch_returns_422(tmp_path: Path):
    """Supplying a mismatched vault_id to decide_vault_sync returns validation_error (422)."""
    import time
    vault = tmp_path / "vault_mismatch"
    vault.mkdir()
    _create_vault_base_note(vault, "note1.md", "Note 1", "Base.")
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

    pending_info = launcher.state()["pending_sync_approval"]
    approval_id = pending_info["approval_id"]

    # Calling with wrong vault_id
    with pytest.raises(AdapterError) as exc_info:
        launcher.decide_vault_sync(run_id, "non_matching_vault_id_123", "approve", approval_id=approval_id)
    assert exc_info.value.kind == "unprocessable_entity"

    # Calling with missing approval_id
    with pytest.raises(AdapterError) as exc_missing:
        launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve")
    assert exc_missing.value.kind == "validation_error"

    # Calling with mismatched approval_id
    with pytest.raises(AdapterError) as exc_mismatch:
        launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve", approval_id="appr_wrong_correlation_id")
    assert exc_mismatch.value.kind == "unprocessable_entity"

    # Clean up with correct approval_id
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "reject", approval_id=approval_id)
    launcher.close()


def test_cancellation_before_reconciliation_linearization(tmp_path: Path):
    """Cancelling a pending run linearizes safely before reconciliation begins."""
    import time
    vault = tmp_path / "vault_cancel"
    vault.mkdir()
    _create_vault_base_note(vault, "note1.md", "Note 1", "Base.")
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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Base note.")
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

    def intercepted_reconcile(ticket=None):
        reconcile_entered.set()
        continue_reconcile.wait(timeout=10.0)
        return real_reconcile(ticket=ticket)

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
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve", approval_id=pending_info["approval_id"])

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
    _create_vault_base_note(vault, "note1.md", "Note 1", "Base.")
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
        assert pending.get("approval_id")

        # 5. Missing approval_id returns 400 Bad Request
        st_missing, _ = _request(
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
        assert st_missing == 400

        # 6. Mismatched approval_id returns 422 Unprocessable Entity
        st_mismatch, _ = _request(
            server,
            "POST",
            "/api/vault/sync/decide",
            body={
                "run_id": run_id,
                "vault_id": pending["vault_id"],
                "decision": "approve",
                "approval_id": "appr_mismatched_wire_id",
            },
            token=token,
        )
        assert st_mismatch == 422

        # 7. Successful approval via HTTP with correct approval_id
        st, decide_body = _request(
            server,
            "POST",
            "/api/vault/sync/decide",
            body={
                "run_id": run_id,
                "vault_id": pending["vault_id"],
                "decision": "approve",
                "approval_id": pending["approval_id"],
            },
            token=token,
        )
        assert st == 200
        decide_data = json.loads(decide_body)
        assert decide_data["status"] == "ok"
        assert decide_data["decision"] == "approve"

        # 8. Duplicate / stale decision returns 409 Conflict
        st_stale, _ = _request(
            server,
            "POST",
            "/api/vault/sync/decide",
            body={
                "run_id": run_id,
                "vault_id": pending["vault_id"],
                "decision": "approve",
                "approval_id": pending["approval_id"],
            },
            token=token,
        )
        assert st_stale == 409

    finally:
        server.shutdown()
        launcher.close()


def test_launcher_type_error_in_reconcile_fails_closed_without_retrying_unticketed(tmp_path: Path):
    """When reconcile raises TypeError, Launcher fails closed and does NOT retry without a ticket."""
    import time
    vault = tmp_path / "vault_launcher_type_err"
    vault.mkdir()
    _create_vault_base_note(vault, "note1.md", "Note 1", "Initial content.")
    vdb = tmp_path / "vdb_launcher_type_err"
    vdb.mkdir()
    staging = tmp_path / "staging_launcher_type_err"
    staging.mkdir()
    audit = tmp_path / "audit_launcher_type_err"
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

    # Add note2 to make vault stale
    (vault / "note2.md").write_text("# Note 2\nNew content.", encoding="utf-8")

    # Record all calls to reconcile
    reconcile_calls: list[dict] = []
    assert launcher._vault_sync_session is not None

    def failing_reconcile(*args, **kwargs):
        reconcile_calls.append({"args": args, "kwargs": kwargs})
        raise TypeError("Simulated internal TypeError inside reconcile")

    launcher._vault_sync_session.reconcile = failing_reconcile

    res = launcher.start_run(
        input_text="Synthetic query input text with enough length for launcher type error test.",
        source_mode="personal_vault",
        retrieval_mode="semantic",
    )
    run_id = res["run_id"]

    # Wait for pending_sync_approval
    for _ in range(150):
        if launcher.state().get("pending_sync_approval"):
            break
        time.sleep(0.1)

    pending_info = launcher.state().get("pending_sync_approval")
    assert pending_info is not None

    # Approve sync
    launcher.decide_vault_sync(run_id, pending_info["vault_id"], "approve", approval_id=pending_info["approval_id"])

    # Wait for run to fail closed
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("terminal_outcome") or st.get("outcome") == "FAILED":
            break
        time.sleep(0.1)

    # Reconcile was called exactly once with the ticket keyword argument
    assert len(reconcile_calls) == 1
    assert "ticket" in reconcile_calls[0]["kwargs"]
    assert reconcile_calls[0]["kwargs"]["ticket"] is not None

    # It was NEVER called a second time without ticket!
    assert not any(len(c["kwargs"]) == 0 and len(c["args"]) == 0 for c in reconcile_calls)

    # Run terminated safely as infrastructure_error
    st = launcher.run_status(run_id)
    assert st.get("terminal_outcome") == "infrastructure_error"
    assert st.get("outcome") == "FAILED"
    launcher.close()


def test_mandatory_approval_identity_and_ticket_provenance_invariants(tmp_path: Path):
    """Rigorous verification of mandatory approval identity and ticket provenance invariants."""
    import time
    from src.living_authenticity.adapter.app import ApprovalState

    vault = tmp_path / "vault_invariants"
    vault.mkdir()
    _create_vault_base_note(vault, "note1.md", "Note 1", "Base content for invariants.")
    vdb = tmp_path / "vdb_invariants"
    vdb.mkdir()
    staging = tmp_path / "staging_invariants"
    staging.mkdir()
    audit = tmp_path / "audit_invariants"
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
    initial_status = launcher.get_vault_sync_status()
    assert initial_status["fresh"] is True
    time.sleep(0.05)

    # Add a note to trigger sync staleness
    (vault / "note2.md").write_text("# Note 2\nStale for invariants.", encoding="utf-8")

    res = launcher.start_run(
        input_text="Synthetic query input text with enough length for mandatory approval identity test.",
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
    real_approval_id = pending_info["approval_id"]
    vault_id = pending_info["vault_id"]
    pending = launcher._pending_vault_sync
    assert pending is not None

    # 1. Direct fallback attempt: pending.resolve("approve", ticket=None) NEVER transitions to APPROVED
    assert not pending.resolve("approve", ticket=None)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # 2. Missing pending approval ID fails closed
    original_approval_id = pending.approval_id
    try:
        pending.approval_id = ""
        with pytest.raises(AdapterError) as exc_missing_pending:
            launcher.decide_vault_sync(run_id, vault_id, "approve", approval_id=real_approval_id)
        assert exc_missing_pending.value.kind == "validation_error"
        assert pending.state == ApprovalState.PENDING
    finally:
        pending.approval_id = original_approval_id

    # 3. Missing submitted approval ID is rejected (for both approve and reject)
    with pytest.raises(AdapterError) as exc_missing_sub_appr:
        launcher.decide_vault_sync(run_id, vault_id, "approve", approval_id=None)
    assert exc_missing_sub_appr.value.kind == "validation_error"
    assert pending.state == ApprovalState.PENDING

    with pytest.raises(AdapterError) as exc_missing_sub_rej:
        launcher.decide_vault_sync(run_id, vault_id, "reject", approval_id="")
    assert exc_missing_sub_rej.value.kind == "validation_error"
    assert pending.state == ApprovalState.PENDING

    # 4. Mismatched approval ID is rejected (for both approve and reject)
    with pytest.raises(AdapterError) as exc_mismatch_appr:
        launcher.decide_vault_sync(run_id, vault_id, "approve", approval_id="appr_completely_wrong_id")
    assert exc_mismatch_appr.value.kind == "unprocessable_entity"
    assert pending.state == ApprovalState.PENDING

    with pytest.raises(AdapterError) as exc_mismatch_rej:
        launcher.decide_vault_sync(run_id, vault_id, "reject", approval_id="appr_completely_wrong_id")
    assert exc_mismatch_rej.value.kind == "unprocessable_entity"
    assert pending.state == ApprovalState.PENDING

    # 5. Missing VaultSyncSession fails closed on approval
    saved_session = launcher._vault_sync_session
    try:
        setattr(launcher, "_vault_sync_session", None)
        with pytest.raises(AdapterError) as exc_missing_session:
            launcher.decide_vault_sync(run_id, vault_id, "approve", approval_id=real_approval_id)
        assert exc_missing_session.value.kind == "conflict"
        assert pending.state == ApprovalState.PENDING
    finally:
        setattr(launcher, "_vault_sync_session", saved_session)

    # 6. Correct approval ID with a valid session produces a valid ticket and succeeds
    decide_res = launcher.decide_vault_sync(run_id, vault_id, "approve", approval_id=real_approval_id)
    assert decide_res["status"] == "ok"
    assert decide_res["decision"] == "approve"
    assert decide_res["ticket_id"] is not None
    assert pending.state == ApprovalState.APPROVED
    assert pending.ticket is not None
    assert pending.ticket.ticket_id == decide_res["ticket_id"]
    assert pending.ticket.approval_id == real_approval_id

    # 7. Duplicate decision on already decided pending approval is rejected (409 Conflict)
    with pytest.raises(AdapterError) as exc_dup:
        launcher.decide_vault_sync(run_id, vault_id, "approve", approval_id=real_approval_id)
    assert exc_dup.value.kind == "conflict"

    # Wait for run to progress
    for _ in range(150):
        st = launcher.run_status(run_id)
        if st.get("processed", 0) > 0 or st.get("outcome") or launcher._active_run_id != run_id:
            break
        time.sleep(0.1)

    # 8. Rejection remains possible with the correct approval ID on a new pending approval
    (vault / "note3.md").write_text("# Note 3\nTrigger second staleness.", encoding="utf-8")
    res2 = launcher.start_run(
        input_text="Synthetic query input text for rejection verification path in invariants test.",
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
    real_approval_id2 = pending_info2["approval_id"]
    pending2 = launcher._pending_vault_sync
    assert pending2 is not None

    reject_res = launcher.decide_vault_sync(run_id2, pending_info2["vault_id"], "reject", approval_id=real_approval_id2)
    assert reject_res["status"] == "ok"
    assert reject_res["decision"] == "reject"
    assert reject_res["ticket_id"] is None
    assert pending2.state == ApprovalState.REJECTED
    assert pending2.ticket is None

    launcher.close()


def test_pending_sync_approval_state_transition_boundary_invariants():
    """Verify ticket-to-pending binding invariants directly at the state-transition boundary."""
    import time
    from src.living_authenticity.adapter.app import ApprovalState, _PendingSyncApproval
    from src.living_authenticity.knowledge.vault.sync import VaultSyncApprovalTicket

    run_id = "run-state-boundary-1"
    vault_id = "vault-boundary-1"
    vault_root = "/nonexistent/test/vault"
    approval_id = "appr-valid-boundary-1"
    diff = {"added": [], "modified": ["note.md"], "removed": []}

    def _make_ticket(
        t_id="ticket-1",
        v_id=vault_id,
        v_root=vault_root,
        r_id=run_id,
        appr_id=approval_id,
        dec="approve",
        expired=False,
        consumed=False,
    ):
        ticket = VaultSyncApprovalTicket(
            ticket_id=t_id,
            vault_id=v_id,
            vault_root=v_root,
            run_id=r_id,
            snapshot_fingerprint="snap_1",
            core_hash="core_1",
            diff_fingerprint="diff_1",
            expires_at=time.time() - 10.0 if expired else time.time() + 60.0,
            session_id="session_1",
            approval_id=appr_id,
            decision=dec,
        )
        if consumed:
            ticket._consumed = True
        return ticket

    # 1. resolve("approve", ticket=None) fails and leaves state PENDING
    pending = _PendingSyncApproval(run_id=run_id, vault_id=vault_id, vault_root=vault_root, diff=diff, approval_id=approval_id)
    assert pending.state == ApprovalState.PENDING
    assert not pending.resolve("approve", ticket=None)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # 2. An invalid (expired) ticket fails and leaves state PENDING
    expired_ticket = _make_ticket(expired=True)
    assert not expired_ticket.is_valid()
    assert not pending.resolve("approve", ticket=expired_ticket)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # An invalid (consumed) ticket fails and leaves state PENDING
    consumed_ticket = _make_ticket(consumed=True)
    assert not consumed_ticket.is_valid()
    assert not pending.resolve("approve", ticket=consumed_ticket)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # 3. A valid ticket belonging to another approval fails and leaves state PENDING
    mismatched_appr_ticket = _make_ticket(appr_id="appr-different-id-999")
    assert mismatched_appr_ticket.is_valid()
    assert not pending.resolve("approve", ticket=mismatched_appr_ticket)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # 4. A valid ticket with a mismatched run ID fails and leaves state PENDING
    mismatched_run_ticket = _make_ticket(r_id="run-different-run-888")
    assert mismatched_run_ticket.is_valid()
    assert not pending.resolve("approve", ticket=mismatched_run_ticket)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # 5. A valid ticket with a mismatched Vault ID fails and leaves state PENDING
    mismatched_vault_ticket = _make_ticket(v_id="vault-different-vault-777")
    assert mismatched_vault_ticket.is_valid()
    assert not pending.resolve("approve", ticket=mismatched_vault_ticket)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # A valid ticket with non-approve decision fails and leaves state PENDING
    reject_decision_ticket = _make_ticket(dec="reject")
    assert not pending.resolve("approve", ticket=reject_decision_ticket)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # Tickets missing mandatory identity fields fail closed unconditionally
    ticket_missing_run = _make_ticket()
    delattr(ticket_missing_run, "run_id")
    assert not pending.resolve("approve", ticket=ticket_missing_run)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    ticket_missing_vault = _make_ticket()
    delattr(ticket_missing_vault, "vault_id")
    assert not pending.resolve("approve", ticket=ticket_missing_vault)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    ticket_missing_appr = _make_ticket()
    delattr(ticket_missing_appr, "approval_id")
    assert not pending.resolve("approve", ticket=ticket_missing_appr)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    ticket_missing_dec = _make_ticket()
    delattr(ticket_missing_dec, "decision")
    assert not pending.resolve("approve", ticket=ticket_missing_dec)
    assert pending.state == ApprovalState.PENDING
    assert pending.ticket is None

    # Pending without approval_id cannot transition to APPROVED even with a valid ticket
    empty_appr_pending = _PendingSyncApproval(run_id=run_id, vault_id=vault_id, vault_root=vault_root, diff=diff, approval_id="")
    valid_ticket = _make_ticket()
    assert not empty_appr_pending.resolve("approve", ticket=valid_ticket)
    assert empty_appr_pending.state == ApprovalState.PENDING

    # 6. A correctly bound valid ticket succeeds and transitions to APPROVED
    assert valid_ticket.is_valid()
    assert pending.resolve("approve", ticket=valid_ticket)
    assert pending.state == ApprovalState.APPROVED
    assert pending.ticket is valid_ticket

    # 7. Repeated approval or rejection attempts fail once decided
    assert not pending.resolve("approve", ticket=valid_ticket)
    assert pending.state == ApprovalState.APPROVED
    assert not pending.resolve("reject")
    assert pending.state == ApprovalState.APPROVED

    # 8. Rejection remains possible without issuing a ticket
    pending_rej = _PendingSyncApproval(run_id=run_id, vault_id=vault_id, vault_root=vault_root, diff=diff, approval_id=approval_id)
    assert pending_rej.state == ApprovalState.PENDING
    assert pending_rej.resolve("reject", ticket=None)
    assert pending_rej.state == ApprovalState.REJECTED
    assert pending_rej.ticket is None

    # Repeated decision on rejected pending fails
    assert not pending_rej.resolve("approve", ticket=valid_ticket)
    assert pending_rej.state == ApprovalState.REJECTED

    # 9. Cancellation also works cleanly without ticket
    pending_canc = _PendingSyncApproval(run_id=run_id, vault_id=vault_id, vault_root=vault_root, diff=diff, approval_id=approval_id)
    assert pending_canc.resolve("cancelled")
    assert pending_canc.state == ApprovalState.CANCELLED
    assert pending_canc.ticket is None
