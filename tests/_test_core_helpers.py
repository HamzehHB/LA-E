"""Test-only Core validation fixture helpers.

This module is strictly restricted to authorized test workspace usage.
It must never be imported by, referenced by, or exposed to production runtime paths.
"""
from pathlib import Path
from typing import Any, Sequence

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.vault.core_validator import (
    _SYNC_AUTH_PROOF,
    _VALIDATOR_PROOF,
    _compute_unit_fingerprint,
    normalize_source_identity,
    CoreValidationContext,
    SyncAuthorizationContext,
    ValidatedCore,
)


def build_test_validated_core(
    units: Sequence[KnowledgeUnit] | None = None,
    source_path: str = "core.md",
    core_hash: str = "mock_test_core_hash",
    source_mode: str = "examples",
) -> ValidatedCore:
    """Build a ValidatedCore instance strictly for authorized test doubles and test fixtures."""
    norm_source = normalize_source_identity(source_path)
    if units is None:
        u_list = [
            KnowledgeUnit(
                id="default_test_core_unit_1",
                source=norm_source,
                original_text="Authoritative Core test baseline unit",
                meaning="Calibration identity unit for test suite",
                cleaned_text="Authoritative Core test baseline unit",
                normalized_text="authoritative core test baseline unit",
                position=0,
                is_core=True,
                core_hash=core_hash,
                core_source_path=norm_source,
            )
        ]
    else:
        u_list = list(units)
    fps: set[tuple[Any, ...]] = set()
    for u in u_list:
        u.is_core = True
        u.core_hash = core_hash
        u.core_source_path = str(source_path)
        for priv_attr in ("_validated_core_hash", "_validated_core_source"):
            if hasattr(u, priv_attr):
                try:
                    delattr(u, priv_attr)
                except AttributeError:
                    pass
        fps.add(_compute_unit_fingerprint(u))

    val_ctx = CoreValidationContext(
        core_hash=core_hash,
        source_path=norm_source,
        canonical_type="Core",
        source_mode=source_mode,
        unit_fingerprints=frozenset(fps),
        unit_instance_ids=frozenset(id(u) for u in u_list),
        _validator_proof=_VALIDATOR_PROOF,
    )
    return ValidatedCore(
        path=Path(source_path),
        source_path=norm_source,
        content="Test core content",
        title="Test Core",
        canonical_type="Core",
        source_mode=source_mode,
        sha256=core_hash,
        mtime_ns=0,
        size=100,
        units=tuple(u_list),
        validation_context=val_ctx,
    )


def build_test_sync_authorization(
    validated_core: ValidatedCore,
    vault_id: str = "test_vault",
    ticket_id: str | None = "test_ticket_fixture",
    session_id: str = "test_session_fixture",
    is_initial_sync: bool = False,
) -> SyncAuthorizationContext:
    """Build an authentic SyncAuthorizationContext strictly for authorized test fixtures."""
    import threading
    import time
    from src.living_authenticity.knowledge.vault.core_validator import (
        _SESSION_PROOF,
        _TICKET_PROOF,
        create_sync_authorization,
        SYNC_STATE_AUTHORIZING,
        SYNC_STATE_IN_PROGRESS,
    )
    from src.living_authenticity.knowledge.vault.sync import (
        VaultSyncApprovalTicket,
        VaultSyncSession,
    )

    session = VaultSyncSession.__new__(VaultSyncSession)
    session._session_proof = _SESSION_PROOF
    session._session_id = session_id
    session._vault_id = vault_id
    session._authorized = True
    session._lock = threading.RLock()
    session._issued_tickets = set()
    session._approved_tickets_by_id = {}
    session._approval_history = {}

    if is_initial_sync or ticket_id is None or ticket_id == "initial_sync":
        session._status = SYNC_STATE_AUTHORIZING
        session._initial_sync_active = True
        session._initial_sync_privilege = _SESSION_PROOF
        session._initial_sync_authorization_issued = False
        return create_sync_authorization(session=session, validated_core=validated_core, ticket=None)
    else:
        session._status = SYNC_STATE_IN_PROGRESS
        t_id = ticket_id or "test_ticket"
        appr_id = "test_approval_fixture"
        ticket = VaultSyncApprovalTicket(
            ticket_id=t_id,
            vault_id=vault_id,
            vault_root="test_vault_root",
            run_id="test_run_fixture",
            snapshot_fingerprint="test_snap_fixture",
            core_hash=validated_core.sha256,
            diff_fingerprint="test_diff_fixture",
            expires_at=time.time() + 3600,
            session_id=session_id,
            approval_id=appr_id,
            decision="approve",
        )
        ticket._ticket_proof = _TICKET_PROOF
        session._current_reconciliation_ticket = ticket
        session._issued_tickets.add(id(ticket))
        session._approved_tickets_by_id[t_id] = ticket
        session._approval_history[appr_id] = {
            "ticket_id": t_id,
            "decision": "approve",
        }
        return create_sync_authorization(session=session, validated_core=validated_core, ticket=ticket)
