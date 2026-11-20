"""Authoritative Core validation and lifecycle enforcement.

The Core class represents the central research anchor of the repository.
Under the Cold Start Governance policy:
- The system must never interpret a missing Core as an empty Core, silently
  initialize a replacement Core, or continue processing under an assumed
  Core state.
- In Vault Mode: The Core class is represented by the dedicated '01 Core'
  folder, which must hold exactly one active Markdown note (max_active_notes: 1).
  No subdirectories or non-markdown files are permitted.
- In Test Mode: The Core is represented by one dedicated, explicitly
  designated example file (e.g. '01-core-calibration.md').
- Validation is strictly read-only: it validates and loads approved Core
  knowledge; it never creates, initializes, normalizes, or mutates notes.
"""
from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
import threading
import time
from typing import Any, Final, Sequence, TypeGuard

import yaml

from Config.settings import ROOT as REPOSITORY_ROOT
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.security.path_boundary import PathBoundary

_SCHEMA_PATH = Path(__file__).resolve().parents[4] / "Knowledge-Schema.yaml"
_ALLOWED_SUFFIXES = frozenset({".md"})
VAULT_CORE_FOLDER: Final[str] = "01 Core"

__all__ = [
    "CoreFailureCategory",
    "CoreResolutionError",
    "CoreValidationContext",
    "ValidatedCore",
    "is_trusted_validation_context",
    "SyncAuthorizationContext",
    "is_trusted_sync_authorization",
    "consume_sync_authorization",
    "claim_and_consume_sync_authorization",
    "create_sync_authorization",
    "compute_core_record_id",
    "normalize_source_identity",
    "resolve_schema_core_folder",
    "is_core_relative_path",
    "validate_and_load_core",
    "is_validated_core_unit",
    "verify_core_units_provenance",
    "compute_core_vector_digest",
    "VAULT_CORE_FOLDER",
    "is_trusted_vault_sync_session",
    "is_trusted_approval_ticket",
]


class CoreFailureCategory(str, Enum):
    """Categorized Core failure reasons distinguishing operational states."""
    CONFIG_UNAVAILABLE = "core_config_unavailable"
    DIRECTORY_MISSING = "core_directory_missing"
    DIRECTORY_EMPTY = "core_directory_empty"
    NOTE_MISSING = "core_note_missing"
    NOTE_EMPTY = "core_note_empty"
    FRONTMATTER_MALFORMED = "core_frontmatter_malformed"
    CANONICAL_TYPE_INVALID = "core_canonical_type_invalid"
    DRAFT_NOTE_REJECTED = "core_draft_note_rejected"
    SCHEMA_UNAVAILABLE = "core_schema_unavailable"
    MULTIPLE_ACTIVE_NOTES = "core_multiple_active_notes"
    UNEXPECTED_ENTRIES = "core_unexpected_entries"
    INACCESSIBLE = "core_inaccessible"
    SECURITY_VIOLATION = "core_security_violation"
    IDENTITY_AMBIGUOUS = "core_identity_ambiguous"
    CHANGED_DURING_PAUSE = "core_changed_during_pause"


class CoreResolutionError(Exception):
    """Structured exception raised when authoritative Core validation fails."""

    def __init__(
        self,
        category: CoreFailureCategory,
        message: str,
        path: str = "",
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.path = path
        self.cause = cause

    @property
    def safe_diagnostic(self) -> str:
        """Sanitized diagnostic message suitable for audit and events."""
        from src.living_authenticity.security.diagnostic import (
            sanitize_diagnostic_text,
            sanitize_path,
        )
        safe_msg = sanitize_diagnostic_text(self.message)
        if self.path:
            safe_p = sanitize_path(self.path)
            return f"[{self.category.value}] {safe_msg} (target: {safe_p})"
        return f"[{self.category.value}] {safe_msg}"


# Process-local identity sentinel distinguishing authoritative validation contexts.
# This sentinel establishes a capability boundary within the local runtime process:
# only internal validation routines within this module can bind this object.
# It is a runtime memory-identity sentinel, not a cryptographic proof.
_VALIDATOR_PROOF = object()


@dataclass(frozen=True)
class CoreValidationContext:
    """Immutable authoritative validation context for Core identity and provenance."""
    core_hash: str
    source_path: str
    canonical_type: str = "Core"
    source_mode: str = ""
    unit_fingerprints: frozenset[tuple[Any, ...]] = frozenset()
    unit_instance_ids: frozenset[int] = frozenset()
    _validator_proof: Any = None


def is_trusted_validation_context(ctx: Any) -> TypeGuard[CoreValidationContext]:
    """Check if context originates from authoritative Core validation.

    Zero-heuristic, fail-closed contract:
    - Context must be an actual CoreValidationContext instance.
    - Context must carry the private module validator proof from authoritative validation.
    - Context must have non-empty core_hash, source_path, and unit_fingerprints.
    - Any duck-typed object, raw context, or empty context fails closed.
    """
    if not isinstance(ctx, CoreValidationContext):
        return False
    if ctx._validator_proof is not _VALIDATOR_PROOF:
        return False
    if not ctx.unit_fingerprints or not ctx.core_hash or not ctx.source_path:
        return False
    return True


# Private capability sentinels for authentic session and approval ticket issuance
_SESSION_PROOF = object()
_TICKET_PROOF = object()


def is_trusted_approval_ticket(ticket: Any, session: Any = None) -> bool:
    """Verify that an approval ticket was authentically issued by VaultSyncSession."""
    if ticket is None:
        return False
    if type(ticket).__name__ != "VaultSyncApprovalTicket":
        return False
    if getattr(ticket, "_ticket_proof", None) is not _TICKET_PROOF:
        return False
    if getattr(ticket, "decision", None) != "approve":
        return False
    if not getattr(ticket, "ticket_id", None) or not getattr(ticket, "vault_id", None):
        return False
    if session is not None:
        if not is_trusted_vault_sync_session(session):
            return False
        check_fn = getattr(session, "is_ticket_issued_by_session", None)
        if not callable(check_fn) or not check_fn(ticket):
            return False
        if str(getattr(ticket, "session_id", "")) != str(getattr(session, "session_id", "")):
            return False
        v_id_sess = str(getattr(session, "vault_id", "") or getattr(session, "_vault_id", "") or "")
        if str(getattr(ticket, "vault_id", "")) != v_id_sess:
            return False
    return True


def is_trusted_vault_sync_session(session: Any) -> bool:
    """Verify that a session is an authentic, genuine VaultSyncSession instance."""
    if session is None:
        return False
    if getattr(session, "_session_proof", None) is not _SESSION_PROOF:
        return False
    if not getattr(session, "session_id", None):
        return False
    return True


def normalize_source_identity(path_str: str) -> str:
    """Return a canonical, resolved string identity for a path.

    Normalizes slashes, resolves existing paths, strips trailing slashes,
    and normalizes Windows drive letters to lowercase.
    Never uses substrings, folder heuristics, or basenames.
    """
    if not path_str or not isinstance(path_str, str):
        return ""
    p_str = path_str.strip()
    if not p_str:
        return ""
    p = Path(p_str)
    try:
        if p.exists():
            resolved = str(p.resolve()).replace("\\", "/").rstrip("/")
            if len(resolved) >= 2 and resolved[1] == ":":
                resolved = resolved[0].lower() + resolved[1:]
            return resolved
    except Exception:
        pass
    norm = str(p).replace("\\", "/").rstrip("/")
    if len(norm) >= 2 and norm[1] == ":":
        norm = norm[0].lower() + norm[1:]
    return norm


def _serialize_unit_text(u: Any) -> str:
    """Unambiguous canonical length-prefixed serialization of unit text content.

    Using length-prefixed framing (len:payload) ensures that delimiter collision
    or boundary ambiguity across fields is mathematically impossible, even when
    delimiter characters occur inside original_text, cleaned_text, normalized_text, or meaning.
    """
    orig = str(getattr(u, "original_text", "") or "")
    clean = str(getattr(u, "cleaned_text", "") or "")
    norm = str(getattr(u, "normalized_text", "") or "")
    mean = str(getattr(u, "meaning", "") or "")
    orig_b = orig.encode("utf-8")
    clean_b = clean.encode("utf-8")
    norm_b = norm.encode("utf-8")
    mean_b = mean.encode("utf-8")
    return f"{len(orig_b)}:{orig}:{len(clean_b)}:{clean}:{len(norm_b)}:{norm}:{len(mean_b)}:{mean}"


def _compute_unit_fingerprint(u: Any) -> tuple[str, str, int, str, str, str, bool]:
    """Compute comprehensive provenance fingerprint binding content and provenance fields."""
    serialized = _serialize_unit_text(u)
    t_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return (
        str(getattr(u, "id", "")),
        t_hash,
        int(getattr(u, "position", 0)),
        normalize_source_identity(str(getattr(u, "source", ""))),
        normalize_source_identity(str(getattr(u, "core_source_path", ""))),
        str(getattr(u, "core_hash", "")),
        bool(getattr(u, "is_core", False)),
    )


def compute_core_record_id(unit: Any, val_ctx_or_core: Any) -> str:
    """Compute deterministic persistent vector record identity bound to a validated Core KnowledgeUnit.

    Enforces fail-closed validation: the unit and context must be authoritatively
    validated. Untrusted callers or unverified units raise ValueError.

    The persistent record identity is distinct from the KnowledgeUnit id:
    - unit.id: identifies the in-memory or extracted analytical knowledge unit.
    - record_id: uniquely identifies the persisted vector record in LanceDB,
      deterministically bound to the unit's verified provenance fingerprint,
      position, unit id, and authoritative Core hash.
    """
    if not is_validated_core_unit(unit, context=val_ctx_or_core):
        raise ValueError("Cannot compute Core record ID for unvalidated unit or untrusted context")
    fp = _compute_unit_fingerprint(unit)
    val_ctx = getattr(val_ctx_or_core, "validation_context", val_ctx_or_core)
    core_hash = str(getattr(val_ctx, "core_hash", "") or getattr(val_ctx_or_core, "sha256", "") or "")
    u_id = str(getattr(unit, "id", "") or "")
    t_hash = fp[1]
    pos = int(getattr(unit, "position", 0))
    serialized = f"{len(core_hash)}:{core_hash}:{len(u_id)}:{u_id}:{len(t_hash)}:{t_hash}:{pos}"
    rec_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"core_vec_{core_hash[:8]}_{u_id}_{rec_hash[:16]}"


# Process-local identity sentinel distinguishing authorized sync execution contexts.
# This sentinel establishes a capability boundary within the local runtime process:
# only authorized VaultSyncSession execution can mint this token.
_AUTH_FACTORY_SECRET = object()
_SYNC_AUTH_PROOF = object()
_INTERNAL_INIT_PROOF = object()
_ACTIVE_SYNC_AUTHORIZATIONS: dict[int, dict[str, Any]] = {}
_AUTH_REGISTRY_LOCK = threading.RLock()
_ACTIVE_FACTORY_INVOCATION: dict[str, Any] | None = None


@dataclass(frozen=True)
class SyncAuthorizationContext:
    """Immutable capability token certifying authorized synchronization execution."""
    vault_id: str
    core_hash: str
    ticket_id: str | None = None
    session_id: str = ""
    is_initial_sync: bool = False
    _sync_proof: Any = None

    def __init__(
        self,
        vault_id: str,
        core_hash: str,
        ticket_id: str | None = None,
        session_id: str = "",
        is_initial_sync: bool = False,
        _sync_proof: Any = None,
        _factory_secret: Any = None,
    ) -> None:
        # Public direct constructor calls fail closed: _sync_proof is None and never trusted
        if _factory_secret is not _INTERNAL_INIT_PROOF:
            object.__setattr__(self, "_sync_proof", None)
        else:
            object.__setattr__(self, "_sync_proof", _SYNC_AUTH_PROOF)
        object.__setattr__(self, "vault_id", str(vault_id or ""))
        object.__setattr__(self, "core_hash", str(core_hash or ""))
        object.__setattr__(self, "ticket_id", ticket_id)
        object.__setattr__(self, "session_id", str(session_id or ""))
        object.__setattr__(self, "is_initial_sync", bool(is_initial_sync))

    @classmethod
    def _mint_authorized_context(
        cls,
        *,
        vault_id: str,
        core_hash: str,
        ticket_id: str | None = None,
        session_id: str = "",
        is_initial_sync: bool = False,
        factory_secret: Any = None,
    ) -> "SyncAuthorizationContext":
        import sys
        caller_frame = sys._getframe(1)
        if caller_frame.f_code.co_name != "create_sync_authorization":
            raise ValueError("Direct minting of sync authorization outside create_sync_authorization is strictly prohibited")
        if factory_secret is _AUTH_FACTORY_SECRET:
            raise ValueError("Static factory secrets are prohibited; dynamic execution proof required")

        with _AUTH_REGISTRY_LOCK:
            global _ACTIVE_FACTORY_INVOCATION
            if _ACTIVE_FACTORY_INVOCATION is None:
                raise ValueError("Cannot mint sync authorization outside active create_sync_authorization invocation")
            expected_token = _ACTIVE_FACTORY_INVOCATION.get("token")
            if factory_secret is not expected_token or expected_token is None:
                raise ValueError("Invalid factory secret: does not match active invocation proof")
            if _ACTIVE_FACTORY_INVOCATION.get("thread_id") != threading.get_ident():
                raise ValueError("Cross-thread factory invocation is prohibited")
            if (
                _ACTIVE_FACTORY_INVOCATION.get("vault_id") != vault_id
                or _ACTIVE_FACTORY_INVOCATION.get("core_hash") != core_hash
                or _ACTIVE_FACTORY_INVOCATION.get("session_id") != session_id
                or _ACTIVE_FACTORY_INVOCATION.get("ticket_id") != ticket_id
            ):
                raise ValueError("Factory invocation parameters do not match active invocation contract")
            # Consume the active invocation token immediately
            _ACTIVE_FACTORY_INVOCATION = None

        ctx = cls(
            vault_id=vault_id,
            core_hash=core_hash,
            ticket_id=ticket_id,
            session_id=session_id,
            is_initial_sync=is_initial_sync,
            _factory_secret=_INTERNAL_INIT_PROOF,
        )
        with _AUTH_REGISTRY_LOCK:
            _ACTIVE_SYNC_AUTHORIZATIONS[id(ctx)] = {
                "vault_id": vault_id,
                "core_hash": core_hash,
                "ticket_id": ticket_id,
                "session_id": session_id,
                "consumed": False,
                "issued_at": time.time(),
            }
        return ctx


def is_trusted_sync_authorization(
    auth: Any,
    expected_core_hash: str | None = None,
    expected_vault_id: str | None = None,
    expected_session_id: str | None = None,
    expected_ticket_id: str | None = None,
) -> TypeGuard[SyncAuthorizationContext]:
    """Check if capability originates from authorized synchronization lifecycle.

    Zero-heuristic, fail-closed contract:
    - auth must be an actual SyncAuthorizationContext instance.
    - auth._sync_proof must match the process-local _SYNC_AUTH_PROOF sentinel.
    - auth must be actively registered in the module-level authorization registry.
    - auth must NOT have been consumed by a prior baseline certification.
    - auth.vault_id and auth.core_hash must be non-empty strings matching registry values.
    - If expected_core_hash is provided, auth.core_hash must match it exactly.
    - If expected_vault_id is provided, auth.vault_id must match it exactly.
    - If expected_session_id is provided, auth.session_id must match it exactly.
    - If expected_ticket_id is provided, auth.ticket_id must match it exactly.
    - Any duck-typed object, boolean, forged token, or consumed token fails closed.
    """
    if not isinstance(auth, SyncAuthorizationContext):
        return False
    if getattr(auth, "_sync_proof", None) is not _SYNC_AUTH_PROOF:
        return False
    if not auth.vault_id or not auth.core_hash:
        return False
    if expected_core_hash is not None and auth.core_hash != expected_core_hash:
        return False
    if expected_vault_id is not None and auth.vault_id != expected_vault_id:
        return False
    if expected_session_id is not None and auth.session_id != expected_session_id:
        return False
    if expected_ticket_id is not None and auth.ticket_id != expected_ticket_id:
        return False
    with _AUTH_REGISTRY_LOCK:
        rec = _ACTIVE_SYNC_AUTHORIZATIONS.get(id(auth))
        if rec is None:
            return False
        if rec.get("consumed", False):
            return False
        if rec.get("vault_id") != auth.vault_id:
            return False
        if rec.get("core_hash") != auth.core_hash:
            return False
    return True


def claim_and_consume_sync_authorization(
    auth: Any,
    expected_core_hash: str | None = None,
    expected_vault_id: str | None = None,
    expected_session_id: str | None = None,
    expected_ticket_id: str | None = None,
) -> bool:
    """Atomically verify identity bindings and mark a SyncAuthorizationContext as consumed
    in a single thread-safe operation under _AUTH_REGISTRY_LOCK.

    Fail-closed rules:
    - auth must be an actual SyncAuthorizationContext instance.
    - auth._sync_proof must match _SYNC_AUTH_PROOF.
    - auth must have non-empty vault_id and core_hash.
    - auth must be actively registered in _ACTIVE_SYNC_AUTHORIZATIONS.
    - auth must NOT have been consumed already (consumed is False).
    - If expected_core_hash is given, auth.core_hash and registry core_hash must match it.
    - If expected_vault_id is given, auth.vault_id and registry vault_id must match it.
    - If expected_session_id is given, auth.session_id and registry session_id must match it.
    - If expected_ticket_id is given, auth.ticket_id and registry ticket_id must match it.
    - If all checks pass, rec['consumed'] is atomically set to True.
    - Any mismatch, already consumed token, or untrusted context returns False.
    """
    if not isinstance(auth, SyncAuthorizationContext):
        return False
    if getattr(auth, "_sync_proof", None) is not _SYNC_AUTH_PROOF:
        return False
    if not auth.vault_id or not auth.core_hash:
        return False
    with _AUTH_REGISTRY_LOCK:
        rec = _ACTIVE_SYNC_AUTHORIZATIONS.get(id(auth))
        if rec is None:
            return False
        if rec.get("consumed", False):
            return False
        if rec.get("vault_id") != auth.vault_id:
            return False
        if rec.get("core_hash") != auth.core_hash:
            return False
        if expected_core_hash is not None:
            if auth.core_hash != expected_core_hash or rec.get("core_hash") != expected_core_hash:
                return False
        if expected_vault_id is not None:
            if auth.vault_id != expected_vault_id or rec.get("vault_id") != expected_vault_id:
                return False
        if expected_session_id is not None:
            if auth.session_id != expected_session_id or rec.get("session_id") != expected_session_id:
                return False
        if expected_ticket_id is not None:
            if auth.ticket_id != expected_ticket_id or rec.get("ticket_id") != expected_ticket_id:
                return False
        rec["consumed"] = True
        rec["consumed_at"] = time.time()
        return True


def consume_sync_authorization(auth: Any) -> bool:
    """Atomically mark a SyncAuthorizationContext as consumed upon successful baseline certification."""
    return claim_and_consume_sync_authorization(auth)


# Lifecycle state constants for sync authorization validation
SYNC_STATE_AUTHORIZING = "authorizing"
SYNC_STATE_FRESH = "fresh"
SYNC_STATE_IN_PROGRESS = "sync_in_progress"


def create_sync_authorization(
    session: Any,
    validated_core: Any,
    ticket: Any = None,
) -> SyncAuthorizationContext:
    """Mint an authorized SyncAuthorizationContext strictly bound to an authentic VaultSyncSession
    executing an authorized synchronization operation.

    Fail-closed invariants:
    - validated_core must carry a trusted CoreValidationContext with valid Core hash.
    - session must be an authentic VaultSyncSession instance with valid session proof.
    - session must be authorized (session.is_authorized is True).
    - If ticketed reconciliation:
      - ticket must be provided and must be an authentic VaultSyncApprovalTicket carrying valid ticket proof.
      - ticket must have decision == "approve".
      - ticket must not be expired (time.time() < ticket.expires_at).
      - ticket must not be already consumed (ticket.is_consumed is False).
      - ticket.core_hash must match validated Core hash.
      - ticket.vault_id must match session vault_id.
      - ticket.session_id must match session session_id.
      - session must be in active reconciliation state (session._status == SYNC_STATE_IN_PROGRESS).
      - session._current_reconciliation_ticket must match ticket.
    - If initial cold-start sync:
      - ticket must not be provided (ticket is None).
      - session._initial_sync_active must be True.
      - session._initial_sync_privilege must match session._session_proof.
      - session._status must be SYNC_STATE_AUTHORIZING or SYNC_STATE_IN_PROGRESS.
    - Any fabricated session, copied attributes, fabricated ticket, cross-session ticket,
      expired ticket, consumed ticket, or out-of-lifecycle call fails closed with ValueError.
    """
    val_ctx = getattr(validated_core, "validation_context", None)
    if not is_trusted_validation_context(val_ctx):
        raise ValueError("Cannot mint sync authorization for unvalidated Core")

    expected_hash = getattr(val_ctx, "core_hash", None) or getattr(validated_core, "sha256", None)
    if not expected_hash:
        raise ValueError("Validated Core carries no valid Core hash")

    if not is_trusted_vault_sync_session(session):
        raise ValueError("Session is not an authentic, authorized VaultSyncSession instance")

    if not getattr(session, "is_authorized", False):
        raise ValueError("Session is not in an authorized state")

    vault_id = str(getattr(session, "vault_id", "") or getattr(session, "_vault_id", "") or "")
    session_id = str(getattr(session, "session_id", "") or "")
    if not vault_id or not session_id:
        raise ValueError("Session missing required vault_id or session_id")

    status = getattr(session, "_status", None)
    is_initial = bool(getattr(session, "_initial_sync_active", False))
    session_proof = getattr(session, "_session_proof", None)
    init_privilege = getattr(session, "_initial_sync_privilege", None)

    if ticket is not None:
        t_lock = getattr(ticket, "_lock", None)
        import contextlib
        lock_ctx = t_lock if t_lock is not None else contextlib.nullcontext()
        with lock_ctx:
            if getattr(ticket, "_issuance_in_progress", False):
                raise ValueError("Re-entrant authorization issuance is prohibited")
            ticket._issuance_in_progress = True
            try:
                if not is_trusted_approval_ticket(ticket):
                    raise ValueError("Approval ticket is forged, untrusted, or was not issued through authorized approval")

                t_decision = getattr(ticket, "decision", None)
                if t_decision != "approve":
                    raise ValueError(f"Approval ticket decision is '{t_decision}'; must be 'approve'")

                exp_at = getattr(ticket, "expires_at", None)
                if exp_at is not None and time.time() >= exp_at:
                    raise ValueError("Approval ticket has expired")

                t_core_hash = getattr(ticket, "core_hash", None)
                if t_core_hash != expected_hash:
                    raise ValueError(
                        f"Ticket Core hash ('{t_core_hash}') does not match "
                        f"validated Core hash ('{expected_hash}')"
                    )

                t_vault_id = getattr(ticket, "vault_id", None)
                if t_vault_id != vault_id:
                    raise ValueError(
                        f"Ticket vault_id ('{t_vault_id}') does not match session vault_id ('{vault_id}')"
                    )

                t_session_id = getattr(ticket, "session_id", None)
                if t_session_id != session_id:
                    raise ValueError(
                        f"Ticket session_id ('{t_session_id}') does not match active session_id ('{session_id}')"
                    )

                if not getattr(session, "is_ticket_issued_by_session", lambda t: False)(ticket):
                    raise ValueError("Approval ticket was not issued by the active session")

                if getattr(ticket, "is_consumed", False):
                    raise ValueError("Approval ticket has already been consumed by a completed reconciliation")

                if getattr(ticket, "_authorization_issued", False):
                    raise ValueError("Approval ticket has already authorized baseline certification; multiple authorization issuance is prohibited")

                if status != SYNC_STATE_IN_PROGRESS:
                    raise ValueError(
                        f"Cannot mint sync authorization outside active reconciliation (status='{status}')"
                    )

                active_rec_ticket = getattr(session, "_current_reconciliation_ticket", None)
                if active_rec_ticket is not ticket:
                    raise ValueError("Ticket does not match active reconciliation ticket on session")

                ticket._authorization_issued = True
                ticket_id = getattr(ticket, "ticket_id", None)
            finally:
                ticket._issuance_in_progress = False
    elif is_initial:
        with session._lock:
            if getattr(session, "_initial_issuance_in_progress", False):
                raise ValueError("Re-entrant initial sync authorization issuance is prohibited")
            session._initial_issuance_in_progress = True
            try:
                if init_privilege is not session_proof or session_proof is None:
                    raise ValueError("Initial sync privilege not legitimately granted by cold-start flow")
                if status not in (SYNC_STATE_AUTHORIZING, SYNC_STATE_IN_PROGRESS):
                    raise ValueError(
                        f"Initial sync session not in active authorization state (status='{status}')"
                    )
                if getattr(session, "_initial_sync_authorization_issued", False):
                    raise ValueError("Initial sync privilege has already minted an authorization context; multiple issuance is prohibited")
                session._initial_sync_authorization_issued = True
                ticket_id = "initial_sync"
            finally:
                session._initial_issuance_in_progress = False
    else:
        raise ValueError(
            "Cannot mint sync authorization without valid approval ticket or initial sync privilege"
        )

    with _AUTH_REGISTRY_LOCK:
        global _ACTIVE_FACTORY_INVOCATION
        if _ACTIVE_FACTORY_INVOCATION is not None:
            raise ValueError("Concurrent or re-entrant sync authorization minting is prohibited")
        invocation_token = object()
        _ACTIVE_FACTORY_INVOCATION = {
            "token": invocation_token,
            "vault_id": vault_id,
            "core_hash": expected_hash,
            "session_id": session_id,
            "ticket_id": ticket_id,
            "thread_id": threading.get_ident(),
        }

    try:
        return SyncAuthorizationContext._mint_authorized_context(
            vault_id=vault_id,
            core_hash=expected_hash,
            ticket_id=ticket_id,
            session_id=session_id,
            is_initial_sync=is_initial,
            factory_secret=invocation_token,
        )
    finally:
        with _AUTH_REGISTRY_LOCK:
            _ACTIVE_FACTORY_INVOCATION = None


@dataclass(frozen=True)
class ValidatedCore:
    """Immutable representation of a validated authoritative Core."""
    path: Path
    source_path: str
    content: str
    title: str
    canonical_type: str
    source_mode: str
    sha256: str
    mtime_ns: int
    size: int
    units: tuple[KnowledgeUnit, ...]
    validation_context: CoreValidationContext | None = None

    @property
    def core_id(self) -> str:
        return f"core_{self.sha256[:12]}"

    @property
    def content_hash(self) -> str:
        return self.sha256


def resolve_schema_core_folder(schema_path: Path | None = None) -> str:
    """Read and validate the canonical Core folder name from Knowledge-Schema.yaml.

    Enforces that:
    1. Knowledge-Schema.yaml exists, is valid YAML, and contains a valid 'folder_structure' mapping.
    2. Exactly one folder in 'folder_structure' declares canonical_type == 'Core'.
    3. That folder matches the required canonical folder name VAULT_CORE_FOLDER ('01 Core').

    Raises CoreResolutionError(CoreFailureCategory.SCHEMA_UNAVAILABLE) if the schema
    cannot be read, declares zero or multiple Core folders, or declares a folder
    that diverges from the established architectural decision '01 Core'.
    """
    target_path = schema_path if schema_path is not None else _SCHEMA_PATH
    try:
        data = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CoreResolutionError(
            CoreFailureCategory.SCHEMA_UNAVAILABLE,
            f"Knowledge-Schema.yaml cannot be read or parsed: {exc}",
            path=str(target_path),
            cause=exc,
        ) from exc

    if not isinstance(data, dict):
        raise CoreResolutionError(
            CoreFailureCategory.SCHEMA_UNAVAILABLE,
            "Knowledge-Schema.yaml root is not a mapping",
            path=str(target_path),
        )

    structure = data.get("folder_structure")
    if not isinstance(structure, dict):
        raise CoreResolutionError(
            CoreFailureCategory.SCHEMA_UNAVAILABLE,
            "Knowledge-Schema.yaml lacks a valid 'folder_structure' mapping",
            path=str(target_path),
        )

    core_folders: list[str] = [
        str(folder_name)
        for folder_name, spec in structure.items()
        if isinstance(spec, dict) and spec.get("canonical_type") == "Core"
    ]

    if not core_folders:
        raise CoreResolutionError(
            CoreFailureCategory.SCHEMA_UNAVAILABLE,
            "Knowledge-Schema.yaml folder_structure declares no folder with canonical_type='Core'",
            path=str(target_path),
        )

    if len(core_folders) > 1:
        raise CoreResolutionError(
            CoreFailureCategory.SCHEMA_UNAVAILABLE,
            f"Knowledge-Schema.yaml declares multiple Core folders {core_folders}; exactly one is permitted",
            path=str(target_path),
        )

    declared_folder = core_folders[0]
    if declared_folder != VAULT_CORE_FOLDER:
        raise CoreResolutionError(
            CoreFailureCategory.SCHEMA_UNAVAILABLE,
            f"Knowledge-Schema.yaml declares Core folder '{declared_folder}', which does not match canonical architectural requirement '{VAULT_CORE_FOLDER}'",
            path=str(target_path),
        )

    return declared_folder


def is_core_relative_path(rel_path: str, schema_path: Path | None = None) -> bool:
    """Return True if rel_path resolves to or resides within the authoritative Core folder.

    Normalizes slashes and checks whether the path is equal to or resides
    directly under the authoritative Core folder resolved from schema.
    """
    if not rel_path:
        return False
    core_folder = resolve_schema_core_folder(schema_path=schema_path)
    norm = rel_path.replace("\\", "/").lstrip("/")
    return norm == core_folder or norm.startswith(f"{core_folder}/")


def _parse_frontmatter(text: str, file_path: Path) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and return (metadata, body_text).

    Enforces that frontmatter must start at the very first line with '---'.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise CoreResolutionError(
            CoreFailureCategory.FRONTMATTER_MALFORMED,
            "Core note is missing required YAML frontmatter (must start with '---')",
            path=str(file_path),
        )

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx == -1:
        raise CoreResolutionError(
            CoreFailureCategory.FRONTMATTER_MALFORMED,
            "Core note YAML frontmatter is unclosed (missing closing '---')",
            path=str(file_path),
        )

    fm_raw = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1:]).strip()

    try:
        meta = yaml.safe_load(fm_raw)
    except Exception as exc:
        raise CoreResolutionError(
            CoreFailureCategory.FRONTMATTER_MALFORMED,
            f"Core note YAML frontmatter failed to parse: {exc}",
            path=str(file_path),
            cause=exc,
        ) from exc

    if not isinstance(meta, dict):
        raise CoreResolutionError(
            CoreFailureCategory.FRONTMATTER_MALFORMED,
            "Core note YAML frontmatter must evaluate to a dictionary mapping",
            path=str(file_path),
        )

    return meta, body


def _validate_vault_mode_core(vault_root: str, pipeline: Any = None) -> ValidatedCore:
    """Validate and load the singleton active Core from the configured Obsidian Vault."""
    if not isinstance(vault_root, str) or not vault_root.strip():
        raise CoreResolutionError(
            CoreFailureCategory.CONFIG_UNAVAILABLE,
            "Vault root path is not configured or is empty",
        )

    root = Path(vault_root)
    if root.is_symlink() or not root.is_dir():
        raise CoreResolutionError(
            CoreFailureCategory.CONFIG_UNAVAILABLE,
            f"Vault root is not an existing directory or is a symlink: {vault_root}",
            path=str(vault_root),
        )

    try:
        boundary = PathBoundary(str(root))
    except Exception as exc:
        raise CoreResolutionError(
            CoreFailureCategory.SECURITY_VIOLATION,
            f"Vault root boundary initialization failed: {exc}",
            path=str(vault_root),
            cause=exc,
        ) from exc

    core_folder_name = resolve_schema_core_folder()
    core_dir = root / core_folder_name

    try:
        boundary.validate(str(core_dir))
    except Exception as exc:
        raise CoreResolutionError(
            CoreFailureCategory.SECURITY_VIOLATION,
            f"Core directory escapes the authorized Vault boundary: {exc}",
            path=str(core_dir),
            cause=exc,
        ) from exc

    if core_dir.is_symlink():
        raise CoreResolutionError(
            CoreFailureCategory.SECURITY_VIOLATION,
            f"Core directory is a symlink, which is prohibited: {core_dir}",
            path=str(core_dir),
        )

    if not core_dir.exists():
        raise CoreResolutionError(
            CoreFailureCategory.DIRECTORY_MISSING,
            f"Core directory '{core_folder_name}' does not exist in Vault",
            path=str(core_dir),
        )

    if not core_dir.is_dir():
        raise CoreResolutionError(
            CoreFailureCategory.DIRECTORY_MISSING,
            f"Core path '{core_folder_name}' exists but is not a directory",
            path=str(core_dir),
        )

    try:
        entries = list(core_dir.iterdir())
    except OSError as exc:
        raise CoreResolutionError(
            CoreFailureCategory.INACCESSIBLE,
            f"Core directory cannot be read: {exc}",
            path=str(core_dir),
            cause=exc,
        ) from exc

    md_files: list[Path] = []
    unexpected_entries: list[Path] = []

    for entry in entries:
        try:
            boundary.validate(str(entry))
        except Exception as exc:
            raise CoreResolutionError(
                CoreFailureCategory.SECURITY_VIOLATION,
                f"Entry inside Core directory escapes boundary: {entry.name}",
                path=str(entry),
                cause=exc,
            ) from exc

        if entry.is_symlink():
            raise CoreResolutionError(
                CoreFailureCategory.SECURITY_VIOLATION,
                f"Entry inside Core directory is a symlink: {entry.name}",
                path=str(entry),
            )

        if entry.is_dir():
            unexpected_entries.append(entry)
        elif entry.is_file():
            if entry.suffix.lower() in _ALLOWED_SUFFIXES:
                md_files.append(entry)
            else:
                unexpected_entries.append(entry)
        else:
            unexpected_entries.append(entry)

    if unexpected_entries:
        names = ", ".join(e.name for e in unexpected_entries)
        raise CoreResolutionError(
            CoreFailureCategory.UNEXPECTED_ENTRIES,
            f"Core directory contains unexpected subdirectories or non-markdown files: {names}",
            path=str(core_dir),
        )

    if not md_files:
        raise CoreResolutionError(
            CoreFailureCategory.DIRECTORY_EMPTY,
            f"Core directory '{core_folder_name}' contains no active markdown notes",
            path=str(core_dir),
        )

    if len(md_files) > 1:
        names = ", ".join(f.name for f in md_files)
        raise CoreResolutionError(
            CoreFailureCategory.MULTIPLE_ACTIVE_NOTES,
            f"Core directory contains {len(md_files)} active notes ({names}); singleton policy requires exactly 1 active note",
            path=str(core_dir),
        )

    active_note = md_files[0]
    return _validate_core_note_file(active_note, source_mode="personal_vault", pipeline=pipeline)


def _validate_examples_mode_core(
    examples_root: str = "",
    pipeline: Any = None,
    designated_filename: str = "01-core-calibration.md",
) -> ValidatedCore:
    """Validate and load the explicitly designated Core note in Test/Examples Mode.

    In Test Mode, exactly one specifically designated Core example note is permitted.
    Arbitrary markdown notes, unexpected entries, and symlinks are strictly rejected.
    """
    repo_root = Path(REPOSITORY_ROOT)
    root = (
        Path(examples_root)
        if examples_root
        else repo_root / "examples" / "bounded_local_integration"
    )

    try:
        boundary = PathBoundary(str(root))
    except Exception as exc:
        raise CoreResolutionError(
            CoreFailureCategory.SECURITY_VIOLATION,
            f"Test Mode root boundary initialization failed: {exc}",
            path=str(root),
            cause=exc,
        ) from exc

    core_dir = root / "core"

    try:
        boundary.validate(str(core_dir))
    except Exception as exc:
        raise CoreResolutionError(
            CoreFailureCategory.SECURITY_VIOLATION,
            f"Test Mode Core directory escapes authorized boundary: {exc}",
            path=str(core_dir),
            cause=exc,
        ) from exc

    if core_dir.is_symlink():
        raise CoreResolutionError(
            CoreFailureCategory.SECURITY_VIOLATION,
            f"Test Mode Core directory is a symlink: {core_dir}",
            path=str(core_dir),
        )

    if not core_dir.exists():
        raise CoreResolutionError(
            CoreFailureCategory.DIRECTORY_MISSING,
            f"Test Mode Core directory does not exist: {core_dir}",
            path=str(core_dir),
        )

    if not core_dir.is_dir():
        raise CoreResolutionError(
            CoreFailureCategory.DIRECTORY_MISSING,
            f"Test Mode Core path exists but is not a directory: {core_dir}",
            path=str(core_dir),
        )

    try:
        entries = list(core_dir.iterdir())
    except OSError as exc:
        raise CoreResolutionError(
            CoreFailureCategory.INACCESSIBLE,
            f"Test Mode Core directory cannot be read: {exc}",
            path=str(core_dir),
            cause=exc,
        ) from exc

    if not entries:
        raise CoreResolutionError(
            CoreFailureCategory.DIRECTORY_EMPTY,
            f"Test Mode Core directory '{core_dir}' contains no files",
            path=str(core_dir),
        )

    unexpected: list[str] = []
    md_files: list[Path] = []
    for entry in entries:
        try:
            boundary.validate(str(entry))
        except Exception as exc:
            raise CoreResolutionError(
                CoreFailureCategory.SECURITY_VIOLATION,
                f"Entry inside Test Mode Core directory escapes boundary: {entry.name}",
                path=str(entry),
                cause=exc,
            ) from exc

        if entry.is_symlink():
            raise CoreResolutionError(
                CoreFailureCategory.SECURITY_VIOLATION,
                f"Entry inside Test Mode Core directory is a symlink: {entry.name}",
                path=str(entry),
            )

        if entry.is_dir():
            unexpected.append(f"{entry.name}/")
        elif entry.is_file():
            if entry.suffix.lower() == ".md":
                md_files.append(entry)
            else:
                unexpected.append(entry.name)
        else:
            unexpected.append(entry.name)

    if unexpected:
        names = ", ".join(unexpected)
        raise CoreResolutionError(
            CoreFailureCategory.UNEXPECTED_ENTRIES,
            f"Test Mode Core directory contains unexpected subdirectories or non-markdown files: {names}",
            path=str(core_dir),
        )

    # Check for the explicitly designated Core note
    designated_path = core_dir / designated_filename
    if not designated_path.is_file():
        raise CoreResolutionError(
            CoreFailureCategory.NOTE_MISSING,
            f"Designated Test Mode Core note '{designated_filename}' does not exist in '{core_dir}'",
            path=str(designated_path),
        )

    if len(md_files) > 1:
        names = ", ".join(p.name for p in md_files)
        raise CoreResolutionError(
            CoreFailureCategory.MULTIPLE_ACTIVE_NOTES,
            f"Test Mode Core directory contains {len(md_files)} notes ({names}); singleton contract requires exactly 1 designated note ('{designated_filename}')",
            path=str(core_dir),
        )

    return _validate_core_note_file(designated_path, source_mode="examples", pipeline=pipeline)


def _validate_core_note_file(
    file_path: Path,
    source_mode: str,
    pipeline: Any = None,
) -> ValidatedCore:
    """Validate frontmatter, content, and units for a single active Core note."""
    try:
        stat_before = file_path.stat()
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        raise CoreResolutionError(
            CoreFailureCategory.INACCESSIBLE,
            f"Core note cannot be read: {exc}",
            path=str(file_path),
            cause=exc,
        ) from exc

    if not raw_bytes or not raw_bytes.strip():
        raise CoreResolutionError(
            CoreFailureCategory.NOTE_EMPTY,
            "Core note file is 0 bytes or empty",
            path=str(file_path),
        )

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoreResolutionError(
            CoreFailureCategory.INACCESSIBLE,
            f"Core note is not valid UTF-8 text: {exc}",
            path=str(file_path),
            cause=exc,
        ) from exc

    if not text.strip():
        raise CoreResolutionError(
            CoreFailureCategory.NOTE_EMPTY,
            "Core note content contains only whitespace",
            path=str(file_path),
        )

    meta, body = _parse_frontmatter(text, file_path)

    if not body.strip():
        raise CoreResolutionError(
            CoreFailureCategory.NOTE_EMPTY,
            "Core note body contains no text outside frontmatter",
            path=str(file_path),
        )

    # In both Vault Mode and Test Mode, validate canonical_type strictly against Knowledge-Schema.yaml
    raw_type = str(meta.get("canonical_type", "")).strip()
    if raw_type.lower() != "core":
        # Rule: tags: [core] cannot substitute for missing or incompatible canonical_type
        raise CoreResolutionError(
            CoreFailureCategory.CANONICAL_TYPE_INVALID,
            f"Core note declares invalid canonical_type='{raw_type}'; must be 'Core'",
            path=str(file_path),
        )
    canon_type = "Core"

    # Validate draft status: Core note must explicitly declare 'draft: false' (boolean)
    if "draft" not in meta:
        raise CoreResolutionError(
            CoreFailureCategory.DRAFT_NOTE_REJECTED,
            "Core note is missing required 'draft' frontmatter field (must be explicitly 'draft: false')",
            path=str(file_path),
        )
    draft_val = meta.get("draft")
    if not isinstance(draft_val, bool) or draft_val is not False:
        raise CoreResolutionError(
            CoreFailureCategory.DRAFT_NOTE_REJECTED,
            f"Core note declares invalid draft={repr(draft_val)}; must explicitly be boolean 'draft: false'",
            path=str(file_path),
        )
    status_val = str(meta.get("status", "")).strip().lower()
    if status_val == "draft":
        raise CoreResolutionError(
            CoreFailureCategory.DRAFT_NOTE_REJECTED,
            "Core note status is 'draft'; draft notes cannot serve as active authoritative Core",
            path=str(file_path),
        )

    title = str(meta.get("title", "")).strip() or file_path.stem
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    # Extract knowledge units
    norm_source = normalize_source_identity(str(file_path))
    units_list: list[KnowledgeUnit] = []
    if pipeline is not None and hasattr(pipeline, "ingestion"):
        try:
            ingestion_res = pipeline.ingestion.ingest(str(file_path))
            for u in ingestion_res.knowledge_units:
                # Mark units explicitly with authoritative Core provenance
                u.proposed_type = "Core"
                u.is_core = True
                u.core_hash = sha256
                u.core_source_path = str(file_path)
                units_list.append(u)
        except Exception as exc:
            raise CoreResolutionError(
                CoreFailureCategory.INACCESSIBLE,
                f"Core note failed knowledge unit ingestion: {exc}",
                path=str(file_path),
                cause=exc,
            ) from exc
    else:
        # Fallback unit extraction when pipeline is not yet initialized
        import uuid
        ku_id = f"ku-core-{uuid.uuid4().hex[:8]}"
        u = KnowledgeUnit(
            id=ku_id,
            source=str(file_path),
            original_text=body,
            cleaned_text=body,
            normalized_text=body.lower(),
            meaning=body,
            proposed_type="Core",
            position=0,
            boundary_basis="authoritative_core_note",
            is_core=True,
            core_hash=sha256,
            core_source_path=str(file_path),
        )
        units_list.append(u)

    if not units_list:
        raise CoreResolutionError(
            CoreFailureCategory.NOTE_EMPTY,
            "Core note extraction produced 0 knowledge units",
            path=str(file_path),
        )

    # Compute immutable unit fingerprints for post-validation tamper detection
    unit_fps: set[tuple[Any, ...]] = set()
    for u in units_list:
        unit_fps.add(_compute_unit_fingerprint(u))

    val_context = CoreValidationContext(
        core_hash=sha256,
        source_path=norm_source,
        canonical_type=canon_type,
        source_mode=source_mode,
        unit_fingerprints=frozenset(unit_fps),
        unit_instance_ids=frozenset(id(u) for u in units_list),
        _validator_proof=_VALIDATOR_PROOF,
    )

    try:
        raw_bytes_after = file_path.read_bytes()
        stat_after = file_path.stat()
        if (
            raw_bytes_after != raw_bytes
            or stat_after.st_mtime_ns != stat_before.st_mtime_ns
            or stat_after.st_size != stat_before.st_size
        ):
            raise CoreResolutionError(
                CoreFailureCategory.CHANGED_DURING_PAUSE,
                "Core note content mutated on disk during validation pass",
                path=str(file_path),
            )
    except OSError as exc:
        raise CoreResolutionError(
            CoreFailureCategory.INACCESSIBLE,
            f"Core note became inaccessible during validation pass: {exc}",
            path=str(file_path),
            cause=exc,
        ) from exc
    stat = stat_after
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    return ValidatedCore(
        path=file_path,
        source_path=str(file_path),
        content=text,
        title=title,
        canonical_type=canon_type,
        source_mode=source_mode,
        sha256=sha256,
        mtime_ns=int(stat.st_mtime_ns),
        size=int(stat.st_size),
        units=tuple(units_list),
        validation_context=val_context,
    )


def validate_and_load_core(
    mode_value: str,
    paths: dict[str, Any] | None = None,
    pipeline: Any = None,
    vault_root: str = "",
    examples_root: str = "",
) -> ValidatedCore:
    """Single authoritative Core validation and loading entry point.

    Enforces the non-negotiable Cold Start governance invariant across all modes:
    - In Vault Mode ('personal_vault'): validates the singleton Core note in '01 Core'.
    - In Test Mode ('examples'): validates the dedicated Core note fixture in 'core/'.

    Raises CoreResolutionError on any failure. Never returns an empty Core.
    """
    if mode_value == "personal_vault":
        effective_vault = vault_root
        if not effective_vault and isinstance(paths, dict):
            effective_vault = paths.get("obsidian", {}).get("vault", "")
        return _validate_vault_mode_core(effective_vault, pipeline=pipeline)
    elif mode_value == "examples":
        effective_examples = examples_root
        if not effective_examples and isinstance(paths, dict):
            effective_examples = paths.get("examples", {}).get("root", "")
        return _validate_examples_mode_core(effective_examples, pipeline=pipeline)
    else:
        raise CoreResolutionError(
            CoreFailureCategory.CONFIG_UNAVAILABLE,
            f"Unsupported operating mode for Core validation: '{mode_value}'",
        )


def is_validated_core_unit(unit: Any, context: Any = None, expected_core_hash: str | None = None) -> bool:
    """Verify that a KnowledgeUnit has authoritative validated Core provenance against context.

    Zero-heuristic, fail-closed contract:
    - If context is None, incomplete, or untrusted: returns False. Units cannot self-authenticate.
    - Context must be an authenticated CoreValidationContext originating from authoritative Core validation.
    - Duck-typed objects, raw dicts, or unverified contexts fail closed immediately.
    - Unit must be a KnowledgeUnit with is_core=True.
    - Unit core_hash must match context core_hash exactly (non-empty).
    - Unit source/core_source_path must match context source_path exactly via normalize_source_identity.
    - Unit object identity must be bound to context unit_instance_ids (clones or separate units fail).
    - Unit comprehensive fingerprint (id, content sha256, position, normalized sources, core_hash, is_core)
      must match unit_fingerprints in context.
    - Any post-validation mutation of any provenance-bearing or content field invalidates its status.
    """
    if context is None:
        return False
    if not isinstance(unit, KnowledgeUnit):
        return False
    if not getattr(unit, "is_core", False):
        return False

    val_ctx: CoreValidationContext | None = None
    if isinstance(context, ValidatedCore):
        val_ctx = context.validation_context
    elif isinstance(context, CoreValidationContext):
        val_ctx = context
    elif hasattr(context, "validation_context") and isinstance(context.validation_context, CoreValidationContext):
        val_ctx = context.validation_context

    if val_ctx is None or not is_trusted_validation_context(val_ctx):
        return False

    unit_hash = getattr(unit, "core_hash", "")
    if not unit_hash or unit_hash != val_ctx.core_hash:
        return False

    if expected_core_hash and unit_hash != expected_core_hash:
        return False

    unit_src = getattr(unit, "core_source_path", "") or getattr(unit, "source", "")
    if not unit_src:
        return False

    if normalize_source_identity(unit_src) != normalize_source_identity(val_ctx.source_path):
        return False

    if val_ctx.unit_instance_ids and id(unit) not in val_ctx.unit_instance_ids:
        return False

    fp = _compute_unit_fingerprint(unit)
    if fp not in val_ctx.unit_fingerprints:
        return False

    return True


def verify_core_units_provenance(core_units: Sequence[Any], context: Any = None, expected_core_hash: str | None = None) -> None:
    """Raise ValueError if core_units is empty or contains any unit lacking validated Core provenance against context."""
    if not core_units:
        raise ValueError("core_units must contain at least one validated KnowledgeUnit; Core is mandatory")
    if context is None:
        raise ValueError(
            "core_units cannot be verified: missing authoritative validation context; "
            "unit lacks authoritative Core provenance"
        )
    for u in core_units:
        if not is_validated_core_unit(u, context=context, expected_core_hash=expected_core_hash):
            raise ValueError(
                f"KnowledgeUnit '{getattr(u, 'id', str(u))}' lacks authoritative Core provenance; "
                f"unverified notes cannot satisfy the Core invariant"
            )


def compute_core_vector_digest(core_records: Sequence[dict]) -> str:
    """Compute deterministic cryptographic SHA-256 digest of all authoritative Core vector records.

    Covers all identity-bearing and semantic fields of Core vector records in LanceDB:
    - id (deterministic record ID)
    - source (normalized source path)
    - position (integer position within Core note)
    - text (exact stored content text, preserving leading/trailing/internal whitespace)
    - is_core (boolean identity)
    - core_hash (SHA-256 hash of authoritative Core note)
    - embedding (explicit little-endian IEEE 754 float32 byte array hex representation)

    Fail-closed invariants:
    - Non-finite embedding values (NaN, +Inf, -Inf) are strictly rejected with ValueError.
    - Floating point byte order is explicitly little-endian (<f4) regardless of host architecture.
    - Text whitespace is preserved exactly without trimming.
    """
    if not core_records:
        return ""

    import hashlib
    import json
    import numpy as np

    sorted_recs = sorted(core_records, key=lambda r: str(r.get("id", "")))
    canonical_items: list[dict[str, Any]] = []
    for r in sorted_recs:
        r_id = str(r.get("id", ""))
        r_source = normalize_source_identity(str(r.get("source", "")))
        r_pos = int(r.get("position", 0))

        raw_text = r.get("text")
        if raw_text is None:
            r_text = ""
        elif isinstance(raw_text, str):
            r_text = raw_text
        else:
            r_text = str(raw_text)

        r_is_core = bool(r.get("is_core", False))
        r_core_hash = str(r.get("core_hash", ""))

        emb = r.get("vector")
        if emb is None:
            emb = r.get("embedding")
        if emb is None:
            emb_bytes_hex = ""
        else:
            arr = np.asarray(emb, dtype=np.dtype("<f4"))
            if not np.all(np.isfinite(arr)):
                raise ValueError("Non-finite float value detected in Core embedding vector (NaN or Inf)")
            emb_bytes_hex = arr.tobytes().hex()

        canonical_items.append({
            "id": r_id,
            "source": r_source,
            "position": r_pos,
            "text": r_text,
            "is_core": r_is_core,
            "core_hash": r_core_hash,
            "embedding_hex": emb_bytes_hex,
        })

    canonical_payload = json.dumps(canonical_items, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
