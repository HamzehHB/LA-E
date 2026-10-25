"""Application core for the loopback launcher (no HTTP layer here).

Owns: run orchestration over the existing ``run_bounded_integration``
contract, the machine-readable approval seam, the per-call cloud privacy
consent gate, journal correlation, and the in-memory event log.

Performs no direct filesystem writes: staging artifacts are produced only
by the existing ``ControlledExecutor`` and audit records only by the
existing ``AuditWriter``, inside the runner.
"""
import hashlib
import json
import logging
import secrets
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

APP_NAME = "living-authenticity"
VERSION = "0.1.0"

TOKEN_HEADER = "X-LAE-Token"

# Bounded request/decision limits.
MAX_TEXT_BYTES = 1_000_000
MAX_BODY_BYTES = 1_000_000
APPROVAL_TIMEOUT_SECONDS = 900
CONSENT_TIMEOUT_SECONDS = 300
MAX_JOURNAL_ENTRIES = 10

SOURCE_MODES = ("personal_vault", "examples")
APPROVE_DECISIONS = ("approve", "reject")
# Allowed UI/content language values (app/staging/audit). Validated at the
# run boundary; anything else is rejected rather than silently ignored.
LANGUAGE_MODES = ("en", "fa")

# Event kinds that describe exactly one processing run. The sink stamps
# these with the active run_id when the emitter did not set one, so the
# frontend can attribute every event to the run the user submitted.
# Events that already carry a run_id are never rewritten.
_RUN_SCOPED_EVENT_KINDS = frozenset({
    "run_started", "run_completed", "run_rejected", "stage_started",
    "stage_completed", "status", "warning", "error", "stopped",
    "human_review_required", "approval_decided", "consent_required",
    "revalidation_result", "execution_result", "completed",
    "sync_approval_required",
})


class AdapterError(Exception):
    """Typed, bounded launcher error (never a raw stack trace)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RECONCILING = "reconciling"
    TERMINAL = "terminal"


class _Pending:
    """One pending human decision (approval or consent), run-bound."""

    def __init__(self, run_id: str, key: str, payload: dict,
                 proposal=None, note=None, confidence=None) -> None:
        self.run_id = run_id
        self.key = key
        self.payload = dict(payload)
        # Authoritative objects backing this decision, stashed so a
        # server-side edit can rebuild the exact proposal/note without
        # trusting any client-supplied content or hash.
        self.proposal = proposal
        self.note = note
        self.confidence = confidence
        self.state = ApprovalState.PENDING
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._result = None

    def resolve(self, value) -> bool:
        with self._lock:
            if self.state != ApprovalState.PENDING:
                return False
            val_str = str(value).lower()
            if val_str == "approve":
                self.state = ApprovalState.APPROVED
                self._result = "approve"
            elif val_str == "reject":
                self.state = ApprovalState.REJECTED
                self._result = "reject"
            elif val_str == "cancelled":
                self.state = ApprovalState.CANCELLED
                self._result = "cancelled"
            else:
                self._result = value
            self._event.set()
            return True

    def wait(self, timeout: float):
        if not self._event.wait(timeout):
            with self._lock:
                if self.state == ApprovalState.PENDING:
                    self.state = ApprovalState.TIMED_OUT
                    self._result = "timeout"
                    self._event.set()
        return self._result


class _PendingSyncApproval:
    """One pending human Vault sync approval decision, run-bound."""

    def __init__(self, run_id: str, vault_id: str, vault_root: str,
                 diff: dict, manifest: dict | None = None) -> None:
        self.run_id = run_id
        self.vault_id = vault_id
        self.vault_root = vault_root
        self.diff = diff
        self.manifest = manifest or {}
        self.created_at = _utcnow()
        self.state = ApprovalState.PENDING
        self._lock = threading.Lock()
        self._event = threading.Event()

    def resolve(self, decision: str) -> bool:
        """Atomically transition state from PENDING. Returns True if first transition."""
        with self._lock:
            if self.state != ApprovalState.PENDING:
                return False
            if decision == "approve":
                self.state = ApprovalState.APPROVED
            elif decision == "reject":
                self.state = ApprovalState.REJECTED
            elif decision == "cancelled":
                self.state = ApprovalState.CANCELLED
            else:
                return False
            self._event.set()
            return True

    def transition_to_reconciling(self) -> bool:
        """Transition from APPROVED to RECONCILING before executing reconcile."""
        with self._lock:
            if self.state != ApprovalState.APPROVED:
                return False
            self.state = ApprovalState.RECONCILING
            return True

    def retire(self) -> None:
        """Mark as terminal and unblock any waiters."""
        with self._lock:
            self.state = ApprovalState.TERMINAL
            self._event.set()

    def wait(self, timeout: float = 0.5) -> bool:
        return self._event.wait(timeout)


class _PublishingGate:
    """Approval gate wrapper that publishes the exact pending request.

    Wraps the existing ``ExplicitApprovalGate`` semantics unchanged: one
    build, one display, one read (the injected reader), one decision.
    The only addition is registering the request with the launcher so the
    UI can answer the exact ``proposal_hash``/``query_unit_id`` pair.
    """

    def __init__(self, launcher, strategy: str = "explicit_ui") -> None:
        from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
            ExplicitApprovalGate,
        )

        self._inner = ExplicitApprovalGate(strategy)
        self._launcher = launcher
        self._strategy = strategy
        self.last_active_proposal = None
        self.last_active_note = None

    @property
    def name(self) -> str:
        return self._strategy

    def build_request(self, query, proposal=None, confidence=None, note=None):
        self.last_active_proposal = proposal
        self.last_active_note = note
        return self._inner.build_request(query, proposal, confidence, note)

    def display(self, request):
        return self._inner.display(request)

    def decide(self, request, raw_input, timestamp: str = ""):
        return self._inner.decide(request, raw_input, timestamp)

    def request_approval(self, query, proposal=None, confidence=None,
                         reader=None, note=None):
        # Cooperative edit loop: the human may edit the proposal while the
        # decision is pending. Each edit rebuilds the authoritative request
        # from the server-side edited proposal (new content hash) and
        # re-displays it, so any prior approval of the old content can never
        # authorize the edited version. The loop ends on a real decision.
        while True:
            request = self._inner.build_request(
                query, proposal, confidence, note)
            self._launcher.register_approval(
                request, note, proposal=proposal, confidence=confidence)
            text = self._inner.display(request)
            read = reader if reader is not None else input
            if not callable(read):
                raise TypeError("reader must be callable")
            try:
                raw = read()
            except Exception:
                raw = ""
            if raw == "cancel" or (bool(self._launcher._active_run_id) and self._launcher._active_run_id in self._launcher._cancel_flags):
                outcome = self._inner.decide(request, "n")
                self.last_active_proposal = proposal
                self.last_active_note = note
                return (request, text, outcome)
            # A pending edit supersedes this request: adopt the edited
            # proposal/note and loop to re-request approval of the new hash.
            edited = self._launcher.consume_pending_edit(
                request.proposal_hash)
            if edited is not None:
                proposal = edited.proposal
                note = edited.note
                continue
            outcome = self._inner.decide(request, raw)
            self.last_active_proposal = proposal
            self.last_active_note = note
            return (request, text, outcome)



class Launcher:
    """Backend application core behind the loopback HTTP transport.

    Injected seams (all optional, for tests): ``synthesis_factory``,
    ``pipeline_factory``, ``picker``, ``paths``/``llm_config``.
    """

    def __init__(self, paths=None, llm_config=None, web_root=None,
                 synthesis_factory=None, pipeline_factory=None,
                 picker=None, open_browser: bool = True,
                 on_ready=None) -> None:
        # Per-launch, memory-only credentials (never logged/persisted).
        self.token = secrets.token_urlsafe(32)
        self.instance_id = f"inst-{uuid.uuid4().hex[:12]}"
        # Run-scoped cancellation flags (memory only). A run_id present in
        # this set means the user requested cancellation; the run observes
        # it at the next safe boundary and must not execute afterwards.
        self._cancel_flags: set = set()
        # Server-side proposal edits awaiting adoption by the approval loop,
        # keyed by the superseded proposal hash. Memory only; consumed once.
        self._pending_edits: dict = {}
        self._paths = paths
        self._llm_config = llm_config
        self._web_root = web_root
        self._synthesis_factory = synthesis_factory
        self._pipeline_factory = pipeline_factory
        self._picker = picker
        self._open_browser = open_browser
        self._on_ready = on_ready
        # UI-validated session destinations (memory only, never persisted
        # to configuration files). Set only by pick_folder() after the
        # existing backend validation accepts the selected path; consumed
        # by _execute_run()/journals() with revalidation on every use.
        self._session_destinations: dict = {}
        self._lock = threading.RLock()
        self._active_run_id = ""
        self._active_source_mode = "examples"
        self._active_retrieval_mode = "lexical"
        self._app_language = "en"
        self._staging_language = "en"
        self._audit_language = "en"
        self._paused_run = None
        self._pending_approval = None
        self._pending_consent = None
        self._pending_vault_sync: _PendingSyncApproval | None = None
        self._vault_sync_run_info: dict[str, dict] = {}
        self._reports = {}
        from .events import RunEventLog
        from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
            VectorEmbeddingService,
        )
        from src.living_authenticity.knowledge.vault.sync import VaultSyncSession

        self._vault_sync_session = VaultSyncSession(stage_observer=self._sink)
        self._embedder: VectorEmbeddingService | None = None
        self.events = RunEventLog()
        self._bootstrap_from_paths()

    def _bootstrap_from_paths(self) -> None:
        """Initialize session destinations from valid configured paths if present on disk."""
        try:
            paths = self.paths()
        except Exception:
            return
        if not isinstance(paths, dict):
            return
        vault = paths.get("obsidian", {}).get("vault", "")
        if isinstance(vault, str) and vault.strip():
            candidate = Path(vault.strip())
            if candidate.is_dir() and not candidate.is_symlink():
                self._session_destinations["vault"] = str(candidate)
                self._active_source_mode = "personal_vault"
        staging = paths.get("staging", {}).get("root", "")
        if isinstance(staging, str) and staging.strip():
            candidate = Path(staging.strip())
            if candidate.is_dir() and not candidate.is_symlink():
                self._session_destinations["staging"] = str(candidate)
        audit = paths.get("audit", {}).get("root", "")
        if isinstance(audit, str) and audit.strip():
            candidate = Path(audit.strip())
            if candidate.is_dir() and not candidate.is_symlink():
                self._session_destinations["audit"] = str(candidate)
        vector_db = paths.get("vector_db", {}).get("lancedb", "")
        if isinstance(vector_db, str) and vector_db.strip():
            candidate = Path(vector_db.strip())
            if candidate.is_dir() and not candidate.is_symlink():
                self._session_destinations["vector_db"] = str(candidate)
        bge_m3 = paths.get("models", {}).get("bge_m3", "")
        if isinstance(bge_m3, str) and bge_m3.strip():
            candidate = Path(bge_m3.strip())
            if candidate.exists() and not candidate.is_symlink():
                self._session_destinations["embedding_model"] = str(candidate)
        try:
            from Config.settings import load_languages
            langs = load_languages()
            lang_sec = langs.get("language", {})
            if isinstance(lang_sec, dict):
                if lang_sec.get("app") in LANGUAGE_MODES:
                    self._app_language = lang_sec["app"]
                if lang_sec.get("staging") in LANGUAGE_MODES:
                    self._staging_language = lang_sec["staging"]
                if lang_sec.get("audit") in LANGUAGE_MODES:
                    self._audit_language = lang_sec["audit"]
        except Exception:
            pass

    # ---- token / auth -------------------------------------------------

    def check_token(self, candidate) -> bool:
        if not isinstance(candidate, str):
            return False
        return secrets.compare_digest(candidate, self.token)

    # ---- configuration -------------------------------------------------

    def paths(self):
        if self._paths is None:
            from Config.settings import load_paths

            self._paths = load_paths()
        return self._paths

    def llm_config(self):
        if self._llm_config is None:
            from Config.settings import load_llm

            self._llm_config = load_llm()
        return self._llm_config

    # ---- events --------------------------------------------------------

    def _sink(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        event = dict(payload)
        # Attribute run-scoped events to the active run when the emitter
        # left run_id empty (e.g. pipeline-internal stage events). Events
        # already carrying an identity are never rewritten, and unknown or
        # future kinds are never attributed to a run they may not belong to.
        if (not event.get("run_id")
                and event.get("kind") in _RUN_SCOPED_EVENT_KINDS):
            event["run_id"] = self._active_run_id
        if not event.get("timestamp"):
            event["timestamp"] = _utcnow()
        self.events.append(event)

    # ---- approval seam -------------------------------------------------

    def register_approval(self, request, note=None, proposal=None,
                          confidence=None) -> None:
        """Publish one pending approval bound to the exact request."""
        body = ""
        if note is not None:
            body = getattr(note, "markdown", "") or ""
        payload = {
            "kind": "human_review_required",
            "stage": "human_review",
            "message": "explicit decision requested",
            "proposal_hash": getattr(request, "proposal_hash", ""),
            "query_unit_id": getattr(request, "query_unit_id", ""),
            "proposal_action": getattr(request, "proposal_action", ""),
            "title": getattr(request, "title", ""),
            "body": body,
            "confidence_level": getattr(request, "confidence_level", ""),
            "confidence_score": getattr(request, "confidence_score", 0.0),
            "confidence_score_band": getattr(confidence, "score_band", ""),
            # Obsidian knowledge class (Observation/Core/Concept), distinct
            # from the proposal action. Already computed by classification;
            # exposed read-only for the UI's CLASS field. Not authorization.
            "classification_type": getattr(proposal, "classification_type", ""),
        }
        with self._lock:
            key = getattr(request, "proposal_hash", "")
            if not key:
                raise AdapterError("backend_error",
                                   "approval request missing proposal hash")
            pending = _Pending(self._active_run_id, key, payload,
                               proposal=proposal, note=note,
                               confidence=confidence)
            self._pending_approval = pending
        self._sink(payload)

    def _approval_reader(self):
        """Blocking reader handed to the gate; timeout rejects (fail closed)."""
        with self._lock:
            pending = self._pending_approval
        if pending is None:
            return ""
        decision = pending.wait(APPROVAL_TIMEOUT_SECONDS)
        with self._lock:
            self._pending_approval = None
            cancelled = (
                getattr(pending, "state", None) == ApprovalState.CANCELLED
                or decision == "cancelled"
                or (bool(self._active_run_id) and self._active_run_id in self._cancel_flags)
            )
        if cancelled:
            return "cancel"
        if decision in ("approve", "reject"):
            return "y" if decision == "approve" else "n"
        return ""

    def approve(self, proposal_hash, query_unit_id, decision):
        """Answer the single pending approval exactly and only once."""
        if decision not in APPROVE_DECISIONS:
            raise AdapterError("validation_error",
                               "decision must be approve or reject")
        with self._lock:
            if bool(self._active_run_id) and self._active_run_id in self._cancel_flags:
                raise AdapterError("conflict", "run has been cancelled")
            pending = self._pending_approval
            if pending is None or pending.key != proposal_hash:
                raise AdapterError(
                    "validation_error",
                    "no pending approval for this proposal hash")
            if getattr(pending, "state", None) == ApprovalState.CANCELLED:
                raise AdapterError("conflict", "approval request was cancelled")
            if (pending.payload.get("query_unit_id")
                    != (query_unit_id or "")):
                raise AdapterError(
                    "validation_error",
                    "approval does not match the pending request")
            if not pending.resolve(decision):
                raise AdapterError("conflict" if getattr(pending, "state", None) in (ApprovalState.CANCELLED, ApprovalState.TIMED_OUT) else "validation_error",
                                   "approval already answered")
        self._sink({"kind": "approval_decided", "stage": "human_review",
                    "message": decision, "proposal_hash": proposal_hash})
        return {"proposal_hash": proposal_hash,
                "approved": decision == "approve",
                "reason": "explicit UI decision" if decision == "approve"
                else "rejected via UI"}

    def edit_proposal(self, title=None, body=None, reason=None,
                      client_hash=None):
        """Apply a server-side edit to the pending CREATE proposal.

        The authoritative proposal/note are resolved from the current
        pending approval (never from client state). The edit is validated
        and its content hash recomputed by the existing proposal-editing
        contract; a client-supplied hash is ignored. The edited proposal
        supersedes the pending one: the approval loop adopts it and
        requires a fresh explicit approval of the new hash before any
        execution. Returns the new review state for the UI.
        """
        from src.living_authenticity.knowledge.governance.approval.editing import (
            ProposalEditError,
            apply_proposal_edit,
        )

        with self._lock:
            pending = self._pending_approval
        if pending is None:
            raise AdapterError("validation_error",
                               "no proposal is currently awaiting review")
        proposal = pending.proposal
        note = pending.note
        confidence = pending.confidence
        if proposal is None or note is None:
            raise AdapterError(
                "backend_error",
                "pending proposal is not available for editing")
        try:
            edited = apply_proposal_edit(
                proposal, note, title=title, body=body, reason=reason,
                confidence=confidence, client_hash=client_hash)
        except ProposalEditError as exc:
            raise AdapterError("validation_error", str(exc))

        with self._lock:
            # Supersede the current pending approval: record the edit keyed
            # by the old hash so the approval loop adopts it and re-requests
            # approval of the new content. The old hash can no longer be
            # approved to authorize the edited version.
            self._pending_edits[pending.key] = edited
            pending.resolve("superseded")
        self._sink({"kind": "status", "stage": "human_review",
                    "message": "proposal edited; re-approval required",
                    "proposal_hash": edited.proposal_hash})
        return {
            "proposal_hash": edited.proposal_hash,
            "supersedes": edited.previous_hash,
            "query_unit_id": edited.proposal.query_unit_id,
            "proposal_action": edited.proposal.action,
            "title": edited.proposal.title,
            "body": edited.note.markdown,
            "confidence_level": getattr(confidence, "level", "")
            if confidence is not None else "",
            "review_state": "awaiting_reapproval",
            "reason": "edited proposal requires fresh explicit approval",
        }

    def consume_pending_edit(self, superseded_hash):
        """Return and clear a pending edit for ``superseded_hash`` (or None)."""
        with self._lock:
            return self._pending_edits.pop(superseded_hash, None)

    # ---- per-call cloud privacy consent gate ---------------------------

    def _consent_confirm(self, question, prompt):
        """Injected into Synthesis: per-call y/N consent; fail closed.

        Each synthesis call creates a new pending consent bound to the
        exact prompt digest; it is consumed once and never reused, so
        consenting to one call never authorizes a later one. Timeout or
        an unavailable answer declines the call.
        """
        digest = hashlib.sha256(
            prompt.encode("utf-8")).hexdigest()[:16]
        payload = {
            "kind": "consent_required",
            "stage": "llm_synthesis",
            "message": question,
            "consent_id": digest,
        }
        with self._lock:
            pending = _Pending(self._active_run_id, digest, payload)
            self._pending_consent = pending
        self._sink(payload)
        decision = pending.wait(CONSENT_TIMEOUT_SECONDS)
        with self._lock:
            self._pending_consent = None
        return decision == "approve"

    def consent(self, consent_id, decision):
        """Answer one pending cloud-privacy consent; mismatch fails closed."""
        if decision not in APPROVE_DECISIONS:
            raise AdapterError("validation_error",
                               "decision must be approve or reject")
        with self._lock:
            pending = self._pending_consent
            if pending is None or pending.key != consent_id:
                raise AdapterError(
                    "validation_error",
                    "no pending consent for this consent id")
            if not pending.resolve(decision):
                raise AdapterError("validation_error",
                                   "consent already answered")
        return {"consent_id": consent_id,
                "approved": decision == "approve"}

    # ---- run orchestration ----------------------------------------------

    def start_run(self, input_text, source_mode="personal_vault",
                  retrieval_mode="lexical", app_language="en",
                  staging_language="en", audit_language="en",
                  source_type="chat_input", input_id=None):
        """Begin one bounded text-input run; returns immediately.

        Language fields are validated at the boundary and never silently
        ignored. ``app_language`` is the interface language (kept separate
        from generated content). ``staging_language`` / ``audit_language`` are
        the requested content languages; the pipeline records them but does
        not translate generated content (no translation provider exists), so
        the effective output language is reported honestly on the run.
        """
        if not isinstance(input_text, str):
            raise AdapterError(
                "validation_error",
                f"input_text must be a string, got {type(input_text).__name__}")
        if not isinstance(source_type, str) or source_type not in ("chat_input",):
            raise AdapterError(
                "validation_error",
                f"unsupported source_type '{source_type}': only 'chat_input' is currently accepted")
        if source_mode not in SOURCE_MODES:
            raise AdapterError(
                "validation_error",
                "source_mode must be one of: " + ", ".join(SOURCE_MODES))
        from src.living_authenticity.knowledge.analysis.retrieval.strategy import (
            RetrievalStrategyRegistry,
            UnsupportedStrategyError,
        )

        try:
            strategy_defn = RetrievalStrategyRegistry().validate(retrieval_mode)
            retrieval_mode = strategy_defn.name
        except UnsupportedStrategyError as exc:
            raise AdapterError("validation_error", str(exc))
        for name, value in (("app_language", app_language),
                            ("staging_language", staging_language),
                            ("audit_language", audit_language)):
            if value not in LANGUAGE_MODES:
                raise AdapterError(
                    "validation_error",
                    "%s must be one of: %s"
                    % (name, ", ".join(LANGUAGE_MODES)))
        with self._lock:
            if self._active_run_id:
                raise AdapterError("processing_error",
                                   "a run is already active")
            run_id = "run-" + uuid.uuid4().hex[:12]
            self._active_run_id = run_id
        worker = threading.Thread(
            target=self._run_thread,
            args=(run_id, input_text, source_mode, retrieval_mode,
                  staging_language, audit_language, source_type, input_id),
            daemon=True)
        worker.start()
        return {"run_id": run_id, "accepted": True, "reason": "",
                "retrieval_mode": retrieval_mode,
                "app_language": app_language,
                "staging_language": staging_language,
                "audit_language": audit_language,
                "source_type": source_type}

    def _run_thread(self, run_id, input_text, mode_value,
                    retrieval_mode="lexical", staging_language="en",
                    audit_language="en", source_type="chat_input",
                    input_id=None):
        from src.living_authenticity.knowledge.input.processor import (
            process_input,
            record_input_failure_audit,
            InputEvaluationResult,
            InputOutcome,
        )
        from src.living_authenticity.knowledge.governance.audit import AuditWriter
        from src.living_authenticity.knowledge.integration.guard import (
            resolve_authorized_audit_root,
        )

        try:
            with self._lock:
                if not self._active_run_id:
                    self._active_run_id = run_id
            self._sink({"kind": "run_started", "stage": "input",
                        "message": "run initiated", "run_id": run_id})

            if self._cancelled(run_id):
                self._sink({"kind": "stopped", "stage": "input",
                            "message": "run cancelled by user request",
                            "run_id": run_id, "outcome": "STOPPED"})
                self._sink({"kind": "run_rejected", "stage": "input",
                            "message": "run cancelled before downstream processing",
                            "run_id": run_id, "outcome": "STOPPED",
                            "recovery_options": ["fix_input", "stop"]})
                cancelled_result = InputEvaluationResult(
                    outcome=InputOutcome.STOPPED,
                    valid=False,
                    can_proceed=False,
                    input_id=input_id or "inp-cancelled",
                    run_id=run_id,
                    source_type=source_type,
                    operating_mode=mode_value,
                    validation_status="stopped",
                    classification_category="cancellation",
                    reason="run cancelled by user request",
                    error_code="stopped_by_user",
                    message_key="ui.recovery.stoppedTitle",
                    recovery_options=("fix_input", "stop"),
                )
                session_audit = self._session_destinations.get("audit", "")
                authorized_audit = (session_audit.strip() if isinstance(session_audit, str) else "")
                if not authorized_audit:
                    authorized_audit = resolve_authorized_audit_root(self.paths())
                if authorized_audit and Path(authorized_audit).is_dir():
                    writer = AuditWriter(authorized_audit)
                    record_input_failure_audit(
                        writer,
                        run_id=run_id,
                        validation_result=cancelled_result,
                        operating_mode=mode_value,
                        input_text=input_text,
                    )
                with self._lock:
                    self._reports[run_id] = {
                        "run_id": run_id,
                        "accepted": False,
                        "outcome": "STOPPED",
                        "reason": "run cancelled by user",
                        "processed": 0,
                        "executed": 0,
                        "held": 0,
                        "terminal_outcome": "stopped_by_user",
                    }
                return

            self._sink({"kind": "stage_started", "stage": "input",
                        "message": "validating input and establishing provenance",
                        "run_id": run_id})

            input_result = process_input(
                input_text,
                source_type=source_type,
                source_mode=mode_value,
                input_id=input_id,
                run_id=run_id,
            )

            prov = input_result.provenance
            if not input_result.valid or prov is None:
                reason = input_result.reason or "input validation failed"
                outcome_str = input_result.outcome.value
                terminal_outcome = (
                    "input_clarification_required"
                    if input_result.outcome == InputOutcome.NEEDS_CLARIFICATION
                    else "input_validation_failed"
                )

                self._sink({
                    "kind": "stopped",
                    "stage": "input",
                    "message": f"input validation failed: {reason}",
                    "run_id": run_id,
                    "error_code": input_result.error_code,
                    "outcome": outcome_str,
                })
                self._sink({
                    "kind": "run_rejected",
                    "stage": "input",
                    "message": f"input rejected: {reason}",
                    "run_id": run_id,
                    "error_code": input_result.error_code,
                    "outcome": outcome_str,
                    "recovery_options": list(input_result.recovery_options),
                })

                session_audit = self._session_destinations.get("audit", "")
                authorized_audit = (session_audit.strip()
                                    if isinstance(session_audit, str) else "")
                if not authorized_audit:
                    authorized_audit = resolve_authorized_audit_root(self.paths())
                if authorized_audit:
                    try:
                        writer = AuditWriter(authorized_audit)
                        record_input_failure_audit(
                            writer,
                            run_id=run_id,
                            validation_result=input_result,
                            operating_mode=mode_value,
                            input_text=input_text,
                        )
                    except Exception:
                        pass

                with self._lock:
                    self._reports[run_id] = {
                        "run_id": run_id,
                        "accepted": False,
                        "outcome": outcome_str,
                        "reason": reason,
                        "error_code": input_result.error_code,
                        "message_key": input_result.message_key,
                        "recovery_options": list(input_result.recovery_options),
                        "processed": 0,
                        "executed": 0,
                        "held": 0,
                        "audit_persisted": True,
                        "terminal_outcome": terminal_outcome,
                    }
                return

            if self._cancelled(run_id):
                self._sink({"kind": "stopped", "stage": "input",
                            "message": "run cancelled by user after validation",
                            "run_id": run_id, "outcome": "STOPPED"})
                self._sink({"kind": "run_rejected", "stage": "input",
                            "message": "run stopped by user",
                            "run_id": run_id, "outcome": "STOPPED",
                            "recovery_options": ["fix_input", "stop"]})
                cancelled_result = InputEvaluationResult(
                    outcome=InputOutcome.STOPPED,
                    valid=False,
                    can_proceed=False,
                    input_id=prov.input_id,
                    run_id=run_id,
                    source_type=source_type,
                    operating_mode=mode_value,
                    content_hash=prov.input_hash,
                    validation_status="stopped",
                    classification_category="cancellation",
                    reason="run cancelled by user after validation",
                    error_code="stopped_by_user",
                    message_key="ui.recovery.stoppedTitle",
                    recovery_options=("fix_input", "stop"),
                )
                session_audit = self._session_destinations.get("audit", "")
                authorized_audit = (session_audit.strip() if isinstance(session_audit, str) else "")
                if not authorized_audit:
                    authorized_audit = resolve_authorized_audit_root(self.paths())
                if authorized_audit and Path(authorized_audit).is_dir():
                    writer = AuditWriter(authorized_audit)
                    record_input_failure_audit(
                        writer,
                        run_id=run_id,
                        validation_result=cancelled_result,
                        operating_mode=mode_value,
                        input_text=input_text,
                    )
                with self._lock:
                    self._reports[run_id] = {
                        "run_id": run_id,
                        "accepted": False,
                        "outcome": "STOPPED",
                        "reason": "run cancelled by user",
                        "processed": 0,
                        "executed": 0,
                        "held": 0,
                        "terminal_outcome": "stopped_by_user",
                    }
                return

            self._sink({"kind": "stage_completed", "stage": "input",
                        "message": f"input accepted (id={prov.input_id}, type={prov.resolved_type}, bytes={prov.byte_length})",
                        "run_id": run_id,
                        "input_id": prov.input_id,
                        "input_hash": prov.input_hash,
                        "outcome": "ACCEPTED"})

            report = self._execute_run(
                run_id, input_text, mode_value,
                retrieval_mode=retrieval_mode,
                staging_language=staging_language,
                audit_language=audit_language,
                input_provenance=prov,
            )
            summary = self._report_summary(report)
            with self._lock:
                sync_info = self._vault_sync_run_info.pop(run_id, None)
                if sync_info is not None:
                    summary["vault_sync"] = sync_info
                self._reports[run_id] = summary
            if summary.get("audit_warning"):
                self._sink({"kind": "warning", "stage": "audit",
                            "message": summary["audit_warning"]})
            self._sink({"kind": "run_completed", "stage": "",
                        "message": "run finished" + (" (with audit warnings)" if summary.get("audit_warning") else "")})
        except Exception as exc:  # bounded; never leaks a raw traceback
            with self._lock:
                existing = self._reports.get(run_id, {})
                rep = {"run_id": run_id, "error": type(exc).__name__}
                if "outcome" in existing:
                    rep["outcome"] = existing["outcome"]
                if "reason" in existing:
                    rep["reason"] = existing["reason"]
                if "terminal_outcome" in existing:
                    rep["terminal_outcome"] = existing["terminal_outcome"]
                self._reports[run_id] = rep
                self._paused_run = {
                    "run_id": run_id,
                    "input_text": input_text,
                    "source_mode": mode_value,
                    "retrieval_mode": retrieval_mode,
                    "error": type(exc).__name__,
                    "error_message": str(exc),
                    "timestamp": _utcnow(),
                }
            self._sink({"kind": "run_rejected", "stage": "paused",
                        "message": "run paused on error: " + (str(exc) or type(exc).__name__)})
        finally:
            with self._lock:
                if self._pending_approval and getattr(self._pending_approval, "run_id", "") == run_id:
                    self._pending_approval.resolve("cancelled")
                    self._pending_approval = None
                if self._pending_vault_sync and self._pending_vault_sync.run_id == run_id:
                    self._pending_vault_sync.retire()
                    self._pending_vault_sync = None
                self._active_run_id = ""
                self._cancel_flags.discard(run_id)

    def request_cancel(self, run_id):
        """Request cooperative cancellation of the active run.

        Stops at the next safe boundary (between units, or immediately
        before the controlled write). Late requests after completion
        report the actual terminal state instead of pretending to stop.
        Cancellation is recorded through the audit trail (errors/).
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise AdapterError("validation_error", "run_id is required")
        with self._lock:
            if run_id in self._reports:
                return {"run_id": run_id,
                        "status": "already_completed",
                        "reason": "run has already reached a terminal state"}
            if run_id != self._active_run_id:
                raise AdapterError(
                    "validation_error", "unknown or inactive run_id")
            self._cancel_flags.add(run_id)
            if self._pending_approval and getattr(self._pending_approval, "run_id", "") == run_id:
                self._pending_approval.resolve("cancelled")
            if self._pending_vault_sync and self._pending_vault_sync.run_id == run_id:
                self._pending_vault_sync.resolve("cancelled")
        self._sink({"kind": "status", "stage": "cancellation",
                    "message": "cancellation requested"})
        return {"run_id": run_id, "status": "cancel_requested",
                "reason": "cancellation will take effect at the next "
                          "safe boundary"}

    def decide_vault_sync(self, run_id: str, vault_id: str, decision: str) -> dict:
        """Validate and record explicit operator decision for pending Vault sync."""
        if not isinstance(run_id, str) or not run_id.strip():
            raise AdapterError("validation_error", "run_id is required")
        if not isinstance(vault_id, str) or not vault_id.strip():
            raise AdapterError("validation_error", "vault_id is required")
        if decision not in ("approve", "reject"):
            raise AdapterError("validation_error", "decision must be 'approve' or 'reject'")

        with self._lock:
            pending = self._pending_vault_sync
            if pending is None or pending.run_id != run_id:
                raise AdapterError("not_found", f"no pending vault sync approval for run_id '{run_id}'")
            if pending.vault_id != vault_id:
                raise AdapterError("unprocessable_entity", f"vault_id mismatch: expected '{pending.vault_id}', got '{vault_id}'")

            if not pending.resolve(decision):
                raise AdapterError("conflict", f"vault sync approval for run '{run_id}' is already {pending.state.value}")

        return {
            "status": "ok",
            "run_id": run_id,
            "vault_id": vault_id,
            "decision": decision,
        }

    def _cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancel_flags

    @staticmethod
    def _report_summary(report):
        if isinstance(report, dict):
            return dict(report)
        units = list(getattr(report, "units", ()))
        receipts = list(getattr(report, "audit_receipts", ()))
        failed_receipts = [r for r in receipts if not r.get("persisted", True)]
        audit_warning = ""
        if failed_receipts:
            audit_warning = (
                f"Staging artifact created, but {len(failed_receipts)} audit record(s) "
                f"failed to persist to disk: {failed_receipts[0].get('reason', 'persistence error')}"
            )
        return {
            "run_id": "",
            "accepted": bool(getattr(report, "accepted", False)),
            "reason": str(getattr(report, "rejection_reason", "")),
            "processed": int(getattr(report, "files_processed", 0)),
            "executed": sum(
                1 for u in units if getattr(u, "execution_executed", False)),
            "held": sum(
                1 for u in units
                if not getattr(u, "execution_executed", False)),
            "audit_persisted": len(failed_receipts) == 0,
            "audit_warning": audit_warning,
        }

    def run_status(self, run_id):
        with self._lock:
            summary = dict(self._reports.get(run_id, {}))
            summary["run_id"] = run_id
            summary["active"] = run_id == self._active_run_id
        return summary

    def _load_knowledge_corpus(self, mode_value, pipeline, paths, run_id=""):
        """Load corpus units, core units, and optional vault context according to mode.

        Test Mode ("examples"):
        - Evaluates strictly against built-in synthetic examples:
          `examples/bounded_local_integration/corpus` and `core`.
        - NEVER reads, discovers, indexes, or touches the personal Vault.
        - vault_context is None.

        Vault Mode ("personal_vault"):
        - Evaluates against the researcher's configured personal Obsidian Vault
          (`session_destination("vault")` or `paths["obsidian"]["vault"]`).
        - Ingests notes via `read_vault_units(vault_root, pipeline)`.
        - Ingests Core notes via `resolve_vault_core_root(vault_root)` and `read_vault_units`.
        - Provides informational bounded context via `read_vault_context(vault_root)`.
        - If unconfigured or empty, reports honest status and runs against empty baseline.
        """
        from Config.settings import ROOT as REPOSITORY_ROOT
        from src.living_authenticity.knowledge.integration.indexing import ingest_units

        if mode_value == "examples":
            examples_root = Path(REPOSITORY_ROOT) / "examples" / "bounded_local_integration"
            corpus_dir = examples_root / "corpus"
            core_dir = examples_root / "core"
            corpus_units = []
            core_units = []
            if corpus_dir.is_dir():
                try:
                    corpus_units = ingest_units(str(corpus_dir), pipeline, max_files=50)
                except Exception:
                    corpus_units = []
            if core_dir.is_dir():
                try:
                    core_units = ingest_units(str(core_dir), pipeline, max_files=50)
                except Exception:
                    core_units = []
            self._sink({
                "kind": "status",
                "stage": "retrieval",
                "run_id": run_id,
                "message": (
                    f"Test Mode: evaluated against synthetic examples "
                    f"({len(corpus_units)} corpus unit(s), {len(core_units)} core unit(s))"
                ),
            })
            return corpus_units, core_units, None

        # Vault Mode ("personal_vault")
        from src.living_authenticity.knowledge.vault.context import (
            read_vault_units,
            read_vault_context,
            resolve_vault_core_root,
        )
        vault_root = self.session_destination("vault") or paths.get("obsidian", {}).get("vault", "")
        if not vault_root or not Path(vault_root).is_dir():
            self._sink({
                "kind": "status",
                "stage": "retrieval",
                "run_id": run_id,
                "message": "Vault Mode: Vault root is not configured or not a directory; evaluated against empty baseline corpus.",
            })
            self._sink({
                "kind": "warning",
                "stage": "retrieval",
                "run_id": run_id,
                "message": "Vault path is unconfigured or not found. Select your Vault folder via the top menu.",
            })
            return [], [], None

        try:
            vault_units, vault_files, vault_skipped = read_vault_units(vault_root, pipeline)
        except Exception as exc:
            self._sink({
                "kind": "status",
                "stage": "retrieval",
                "run_id": run_id,
                "message": f"Vault Mode: failed reading vault units ({exc}); evaluated against empty baseline.",
            })
            return [], [], None

        core_units = []
        core_root = resolve_vault_core_root(vault_root)
        if core_root and Path(core_root).is_dir():
            try:
                core_units_list, _, _ = read_vault_units(core_root, pipeline)
                core_units = list(core_units_list)
            except Exception:
                core_units = []

        vault_context = None
        try:
            excerpts, _ = read_vault_context(vault_root)
            if excerpts:
                vault_context = excerpts
        except Exception:
            vault_context = None

        self._sink({
            "kind": "status",
            "stage": "retrieval",
            "run_id": run_id,
            "message": (
                f"Vault Mode: evaluated against authentic Vault "
                f"({len(vault_units)} vault unit(s), {len(core_units)} core unit(s), {vault_files} file(s) discovered, {vault_skipped} skipped)"
            ),
        })
        return list(vault_units), core_units, vault_context

    def _execute_run(self, run_id, input_text, mode_value,
                     retrieval_mode="lexical", staging_language="en",
                     audit_language="en", input_provenance=None):
        from src.living_authenticity.knowledge import integration as _integration
        from src.living_authenticity.knowledge.integration.guard import (
            resolve_authorized_audit_failed,
            resolve_authorized_audit_passed,
            resolve_authorized_audit_root,
            resolve_authorized_staging_root,
            resolve_guarded_roots,
        )
        from src.living_authenticity.knowledge.orchestration import (
            EvidenceFirstPipeline,
        )

        paths = self.paths()
        from src.living_authenticity.knowledge.analysis.retrieval.strategy import (
            RetrievalStrategyRegistry,
            UnsupportedStrategyError,
        )

        try:
            strategy_defn = RetrievalStrategyRegistry().validate(retrieval_mode)
            effective_retrieval = strategy_defn.name
        except UnsupportedStrategyError as exc:
            raise AdapterError("validation_error", str(exc))

        vector_components = None
        vector_service = None
        vector_retriever = None
        temporary_cache = None
        expected = 0
        got = 0
        sync_requested = False
        sync_decision = None

        if effective_retrieval == "semantic":
            from src.living_authenticity.knowledge.integration import (
                vector as _vector,
            )

            vector_db = (
                self.session_destination("vector_db")
                or paths.get("vector_db", {}).get("lancedb", "")
                or paths.get("vector_db", {}).get("root", "")
            )
            if not vector_db:
                raise AdapterError(
                    "validation_error",
                    "semantic retrieval requested but no vector database selected",
                )
            vault_dest = (
                self.session_destination("vault")
                or paths.get("obsidian", {}).get("vault", "")
                or paths.get("vault", {}).get("root", "")
            )
            audit_dest = (
                self.session_destination("audit")
                or paths.get("audit", {}).get("root", "")
            )
            staging_dest = (
                self.session_destination("staging")
                or paths.get("staging", {}).get("root", "")
            )
            eligible, reason, _check = _vector.check_vector_store_eligible(
                vector_db,
                _vector.resolve_production_vector_db(paths),
                vault_root=vault_dest,
                audit_root=audit_dest,
                staging_root=staging_dest,
            )
            if not eligible:
                raise AdapterError(
                    "validation_error",
                    f"semantic retrieval requested but vector database is not eligible: {reason}",
                )
            if mode_value == "personal_vault" and not vault_dest:
                raise AdapterError(
                    "validation_error",
                    "semantic retrieval in Vault mode requires a configured Vault",
                )

            if vault_dest:
                from src.living_authenticity.knowledge.vault.identity import (
                    compute_vault_id,
                    resolve_vault_index_dir,
                )
                active_vault_id = compute_vault_id(vault_dest)
                target_vector_db = str(resolve_vault_index_dir(vector_db, vault_dest))

                # Check Vault / index freshness and handle explicit sync approval
                if (
                    self._vault_sync_session.is_authorized
                    and self._vault_sync_session.vault_root == str(Path(vault_dest).resolve())
                ):
                    try:
                        changes = self._vault_sync_session.check_changes()
                    except Exception as exc:
                        raise AdapterError(
                            "validation_error",
                            f"Vault vector index freshness check failed: {exc}",
                        ) from exc

                    if changes.get("stale", False):
                        sync_requested = True
                        pending = _PendingSyncApproval(
                            run_id=run_id,
                            vault_id=active_vault_id,
                            vault_root=str(Path(vault_dest).resolve()),
                            diff=changes,
                            manifest=getattr(self._vault_sync_session, "_manifest", {}),
                        )
                        with self._lock:
                            self._pending_vault_sync = pending

                        self._sink({
                            "kind": "sync_approval_required",
                            "stage": "synchronization",
                            "run_id": run_id,
                            "vault_id": active_vault_id,
                            "message": (
                                f"Vault changes detected ({len(changes.get('added', []))} added, "
                                f"{len(changes.get('modified', []))} modified, "
                                f"{len(changes.get('removed', []))} removed). "
                                "Explicit synchronization approval required."
                            ),
                            "diff": changes,
                        })

                        # Wait for human decision or cancellation
                        while not pending.wait(timeout=0.25):
                            if self._cancelled(run_id):
                                pending.resolve("cancelled")
                                break

                        if pending.state == ApprovalState.CANCELLED or self._cancelled(run_id):
                            decision = "cancelled"
                        elif pending.state == ApprovalState.APPROVED:
                            decision = "approve"
                        elif pending.state == ApprovalState.REJECTED:
                            decision = "reject"
                        else:
                            decision = "cancelled"

                        sync_decision = decision
                        if decision == "approve":
                            # Atomic transition to reconciling before running reconcile
                            if not pending.transition_to_reconciling():
                                decision = "cancelled"
                            else:
                                self._sink({
                                    "kind": "status",
                                    "stage": "synchronization",
                                    "status": "reconciling",
                                    "run_id": run_id,
                                    "vault_id": active_vault_id,
                                    "message": "User approved persistent index synchronization; updating index explicitly.",
                                })
                                try:
                                    reconcile_res = self._vault_sync_session.reconcile()
                                except Exception as exc:
                                    session_audit = self._session_destinations.get("audit", "")
                                    auth_audit = session_audit.strip() if isinstance(session_audit, str) else ""
                                    if not auth_audit:
                                        auth_audit = resolve_authorized_audit_root(paths)
                                    if auth_audit and Path(auth_audit).is_dir():
                                        from src.living_authenticity.knowledge.governance.audit import AuditWriter
                                        from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
                                        writer = AuditWriter(auth_audit)
                                        rec = AuditRecord(
                                            run_id=run_id,
                                            operating_mode=mode_value,
                                            vault_id=active_vault_id,
                                            terminal_outcome="infrastructure_error",
                                            validation_result="FAILED",
                                            stop_reason=str(exc),
                                            errors=(str(exc),),
                                        )
                                        writer.append(rec)
                                    with self._lock:
                                        self._reports[run_id] = {
                                            "run_id": run_id,
                                            "accepted": False,
                                            "outcome": "FAILED",
                                            "reason": f"Vault vector index reconciliation failed: {exc}",
                                            "processed": 0,
                                            "executed": 0,
                                            "held": 0,
                                            "terminal_outcome": "infrastructure_error",
                                        }
                                    raise AdapterError(
                                        "validation_error",
                                        f"Vault vector index reconciliation failed: {exc}",
                                    ) from exc

                                if not reconcile_res.get("fresh", False):
                                    err_msg = reconcile_res.get("error") or "reconciliation failed to produce a fresh index"
                                    session_audit = self._session_destinations.get("audit", "")
                                    auth_audit = session_audit.strip() if isinstance(session_audit, str) else ""
                                    if not auth_audit:
                                        auth_audit = resolve_authorized_audit_root(paths)
                                    if auth_audit and Path(auth_audit).is_dir():
                                        from src.living_authenticity.knowledge.governance.audit import AuditWriter
                                        from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
                                        writer = AuditWriter(auth_audit)
                                        rec = AuditRecord(
                                            run_id=run_id,
                                            operating_mode=mode_value,
                                            vault_id=active_vault_id,
                                            terminal_outcome="infrastructure_error",
                                            validation_result="FAILED",
                                            stop_reason=err_msg,
                                            errors=(err_msg,),
                                        )
                                        writer.append(rec)
                                    with self._lock:
                                        self._reports[run_id] = {
                                            "run_id": run_id,
                                            "accepted": False,
                                            "outcome": "FAILED",
                                            "reason": f"Vault vector index is not fresh: {err_msg}",
                                            "processed": 0,
                                            "executed": 0,
                                            "held": 0,
                                            "terminal_outcome": "infrastructure_error",
                                        }
                                    raise AdapterError(
                                        "validation_error",
                                        f"Vault vector index is not fresh: {err_msg}",
                                    )

                                if self._cancelled(run_id):
                                    decision = "cancelled"

                        if decision == "reject":
                            self._sink({
                                "kind": "stopped",
                                "stage": "synchronization",
                                "run_id": run_id,
                                "vault_id": active_vault_id,
                                "terminal_outcome": "sync_rejected_by_user",
                                "outcome": "STOPPED",
                                "reason": "Vault synchronization rejected by human operator",
                                "message": "User rejected persistent index update; run stopped safely.",
                                "metadata": {"fresh": False, "status": "stale"},
                            })
                            session_audit = self._session_destinations.get("audit", "")
                            auth_audit = session_audit.strip() if isinstance(session_audit, str) else ""
                            if not auth_audit:
                                auth_audit = resolve_authorized_audit_root(paths)
                            if auth_audit and Path(auth_audit).is_dir():
                                from src.living_authenticity.knowledge.governance.audit import AuditWriter
                                from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
                                writer = AuditWriter(auth_audit)
                                rec = AuditRecord(
                                    run_id=run_id,
                                    operating_mode=mode_value,
                                    vault_id=active_vault_id,
                                    terminal_outcome="sync_rejected_by_user",
                                    validation_result="STOPPED",
                                    stop_reason="Vault synchronization rejected by human operator",
                                )
                                writer.append(rec)
                            with self._lock:
                                self._reports[run_id] = {
                                    "run_id": run_id,
                                    "accepted": False,
                                    "outcome": "STOPPED",
                                    "reason": "Vault synchronization rejected by human operator",
                                    "processed": 0,
                                    "executed": 0,
                                    "held": 0,
                                    "terminal_outcome": "sync_rejected_by_user",
                                }
                            return {
                                "run_id": run_id,
                                "accepted": False,
                                "outcome": "STOPPED",
                                "terminal_outcome": "sync_rejected_by_user",
                                "reason": "Vault synchronization rejected by human operator",
                            }

                        elif decision == "cancelled":
                            self._sink({
                                "kind": "stopped",
                                "stage": "cancellation",
                                "run_id": run_id,
                                "terminal_outcome": "stopped_by_user",
                                "outcome": "STOPPED",
                                "reason": "run cancelled by user request",
                                "message": "Run cancelled by user request during synchronization.",
                            })
                            session_audit = self._session_destinations.get("audit", "")
                            auth_audit = session_audit.strip() if isinstance(session_audit, str) else ""
                            if not auth_audit:
                                auth_audit = resolve_authorized_audit_root(paths)
                            if auth_audit and Path(auth_audit).is_dir():
                                from src.living_authenticity.knowledge.governance.audit import AuditWriter
                                from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
                                writer = AuditWriter(auth_audit)
                                rec = AuditRecord(
                                    run_id=run_id,
                                    operating_mode=mode_value,
                                    vault_id=active_vault_id,
                                    terminal_outcome="stopped_by_user",
                                    validation_result="STOPPED",
                                    stop_reason="run cancelled by user request",
                                )
                                writer.append(rec)
                            with self._lock:
                                self._reports[run_id] = {
                                    "run_id": run_id,
                                    "accepted": False,
                                    "outcome": "STOPPED",
                                    "reason": "run cancelled by user request",
                                    "processed": 0,
                                    "executed": 0,
                                    "held": 0,
                                    "terminal_outcome": "stopped_by_user",
                                }
                            return {
                                "run_id": run_id,
                                "accepted": False,
                                "outcome": "STOPPED",
                                "terminal_outcome": "stopped_by_user",
                                "reason": "run cancelled by user request",
                            }
            else:
                target_vector_db = vector_db
                active_vault_id = ""

            model_path = (
                self.session_destination("embedding_model")
                or paths.get("models", {}).get("bge_m3", "")
                or paths.get("models", {}).get("root", "")
            )
            if self._embedder is not None:
                service = self._embedder
                from src.living_authenticity.database.lancedb_manager import LanceDBManager
                from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
                    LanceDBVectorRetriever,
                )
                store = LanceDBManager(target_vector_db)
                store.create_knowledge_vector_table()
                retriever = LanceDBVectorRetriever(
                    embedding_service=service,
                    db_manager=store,
                    vault_id=active_vault_id,
                )
            else:
                if not model_path:
                    raise AdapterError(
                        "validation_error",
                        "semantic retrieval requested but no embedding model selected",
                    )
                try:
                    service, store, retriever = _vector.build_vector_components(
                        target_vector_db, model_path=model_path, vault_id=active_vault_id
                    )
                except Exception as exc:
                    raise AdapterError(
                        "validation_error",
                        f"semantic retrieval requested but embedding model could not load ({type(exc).__name__}: {exc})",
                    )
            try:
                from src.living_authenticity.database import schema as _schema

                expected = int(getattr(_schema, "VECTOR_DIMENSION", 0))
            except Exception:
                expected = 0
            got = int(getattr(service, "dimension", 0))
            if expected and got and expected != got:
                raise AdapterError(
                    "validation_error",
                    f"semantic retrieval requested but embedding dimension {got} mismatches schema {expected}",
                )

            if mode_value == "personal_vault" and vault_dest:
                rows = store.show_all()
                if not rows:
                    raise AdapterError(
                        "validation_error",
                        f"semantic retrieval requested but dedicated Vault index for {active_vault_id} holds 0 vectors. Please synchronize the Vault first.",
                    )

            vector_service = service
            vector_retriever = retriever
            vector_components = (service, retriever)

        if self._pipeline_factory is not None:
            pipe = self._pipeline_factory(self._sink)
        elif vector_components is not None:
            _service, _retriever = vector_components
            pipe = EvidenceFirstPipeline(
                stage_observer=self._sink, retriever=_retriever
            )
        else:
            pipe = EvidenceFirstPipeline(stage_observer=self._sink)

        if effective_retrieval == "semantic":
            self._sink(
                {
                    "kind": "status",
                    "stage": "retrieval",
                    "run_id": run_id,
                    "message": (
                        "retrieval strategy: semantic "
                        "(isolated vector similarity with temporary embedding cache)"
                    ),
                }
            )
        else:
            self._sink(
                {
                    "kind": "status",
                    "stage": "retrieval",
                    "run_id": run_id,
                    "message": (
                        "retrieval strategy: lexical "
                        "(deterministic token-overlap; no embedding required)"
                    ),
                }
            )

        # Knowledge-language contract (honest). The requested content
        # languages are recorded on the run, but the pipeline has no
        # translation provider, so generated Audit/Staging content is NOT
        # rewritten or translated. Existing records keep their stored
        # language. This never alters authorization or source meaning.
        self._sink(
            {
                "kind": "status",
                "stage": "localization",
                "message": (
                    "requested content languages: "
                    f"staging={staging_language} "
                    f"audit={audit_language}; no translation "
                    "provider is configured, so generated "
                    "content language is unchanged"
                ),
            }
        )
        source_label = (
            f"{input_provenance.source_type}:{input_provenance.input_id}"
            if input_provenance
            else "<text-input>"
        )
        self._sink(
            {
                "kind": "stage_started",
                "stage": "ingestion",
                "run_id": run_id,
                "message": "ingesting and extracting knowledge units",
            }
        )
        source_type = (
            getattr(input_provenance, "source_type", "")
            if input_provenance
            else "chat_input"
        )
        kind = (
            source_type if source_type in ("chat_input", "text") else "chat_input"
        )
        ingestion = pipe.ingestion.ingest_text(
            input_text,
            source=source_label,
            kind=kind,
            observer=self._sink,
            input_provenance=input_provenance,
        )
        self._sink(
            {
                "kind": "stage_completed",
                "stage": "ingestion",
                "run_id": run_id,
                "message": (
                    f"ingestion completed ({len(ingestion.knowledge_units)} unit(s) extracted)"
                ),
            }
        )

        if effective_retrieval == "semantic":
            from src.living_authenticity.knowledge.analysis.retrieval.cache import (
                TemporaryEmbeddingCache,
            )

            provider_n = (
                getattr(vector_service, "provider_name", "embedding_service")
                if vector_service
                else "embedding_service"
            )
            model_n = (
                getattr(vector_service, "model_name", "local_embedding_model")
                if vector_service
                else "local_embedding_model"
            )
            model_v = (
                getattr(vector_service, "model_version", "1.0")
                if vector_service
                else "1.0"
            )
            dim_val = got or expected or 1024
            temporary_cache = TemporaryEmbeddingCache(
                run_id=run_id,
                input_ref=source_label,
                provider_name=provider_n,
                model_name=model_n,
                model_version=model_v,
                dimension=dim_val,
                chunk_count=len(ingestion.knowledge_units),
            )
            if hasattr(pipe, "retriever") and hasattr(
                pipe.retriever, "set_cache"
            ):
                pipe.retriever.set_cache(temporary_cache)
            self._sink(
                {
                    "kind": "status",
                    "stage": "retrieval",
                    "run_id": run_id,
                    "message": (
                        f"temporary embedding cache allocated (scope={temporary_cache.scope_id}, "
                        f"chunks={len(ingestion.knowledge_units)}, dim={temporary_cache.dimension})"
                    ),
                }
            )

        try:
            # Portable UI-first resolution: a UI-validated session destination
            # (set by pick_folder() this launch) takes precedence; otherwise
            # fall back to the config-declared location. The chosen value is
            # revalidated below by run_bounded_integration's own guards, so a
            # stale or tampered session value fails closed instead of writing
            # somewhere unexpected.
            session_staging = self._session_destinations.get("staging", "")
            session_audit = self._session_destinations.get("audit", "")
            staging_arg = (
                session_staging.strip() if isinstance(session_staging, str) else ""
            )
            if not staging_arg:
                staging_arg = resolve_authorized_staging_root(paths)
            authorized_audit = (
                session_audit.strip() if isinstance(session_audit, str) else ""
            )
            if not authorized_audit:
                authorized_audit = resolve_authorized_audit_root(paths)
            authorized_passed = resolve_authorized_audit_passed(paths)
            authorized_failed = resolve_authorized_audit_failed(paths)
            llm_config = self.llm_config()
            if self._synthesis_factory is not None:
                synthesis = self._synthesis_factory(
                    confirm=self._consent_confirm, config=llm_config
                )
            else:
                from src.living_authenticity.llm.orchestration import (
                    build_synthesis,
                )
                from src.living_authenticity.llm.registry import build_provider

                synthesis = build_synthesis(
                    build_provider(llm_config),
                    confirm=self._consent_confirm,
                    config=llm_config,
                )
            corpus_units, core_units, vault_ctx = self._load_knowledge_corpus(
                mode_value, pipe, paths, run_id=run_id
            )
            report = _integration.run_bounded_integration(
                "",
                staging_arg,
                pipeline=pipe,
                corpus=corpus_units,
                core_units=core_units,
                max_files=1,
                llm_config=llm_config,
                synthesis=synthesis,
                ingestion=ingestion,
                vault_context=vault_ctx,
                approval_gate=_PublishingGate(self),
                approval_reader=self._approval_reader,
                run_id=run_id,
                event_sink=self._sink,
                operating_mode=mode_value,
                audit_root=authorized_audit,
                authorized_audit_root=authorized_audit,
                authorized_audit_passed=authorized_passed,
                authorized_audit_failed=authorized_failed,
                guarded_roots=resolve_guarded_roots(paths),
                should_cancel=lambda: self._cancelled(run_id),
            )
            with self._lock:
                self._vault_sync_run_info[run_id] = {
                    "requested": sync_requested,
                    "decision": sync_decision,
                    "index_fresh": self._vault_sync_session.is_fresh if self._vault_sync_session.is_authorized else False,
                }
            return report
        finally:
            if temporary_cache is not None:
                cleanup_ok = temporary_cache.cleanup()
                if cleanup_ok:
                    self._sink(
                        {
                            "kind": "status",
                            "stage": "retrieval",
                            "run_id": run_id,
                            "message": (
                                f"temporary embedding cache released successfully (scope={temporary_cache.scope_id})"
                            ),
                        }
                    )
                else:
                    self._sink(
                        {
                            "kind": "warning",
                            "stage": "retrieval",
                            "run_id": run_id,
                            "message": (
                                f"temporary embedding cache cleanup failed: {temporary_cache.cleanup_error}"
                            ),
                        }
                    )


    # ---- journals (read-only observation) -------------------------------

    def journals(self, limit=MAX_JOURNAL_ENTRIES):
        """Correlate persisted audit records with staging artifacts.

        Read-only: scans nothing the user did not configure; correlation
        uses record/artifact identifiers, never filenames or positions.
        1:1 paired slots:
        - Reads passed/ and failed/ partitions, plus emergency audit records
          specifically from errors/audit_emergency.jsonl (unrelated internal
          error logs remain strictly excluded)
        - Computes digests and newspaper summary
        - Each audit slot is paired with a staging slot (or 'Unavailable' if missing/rejected)
        """
        from src.living_authenticity.knowledge.integration.guard import (
            resolve_authorized_audit_root,
            resolve_authorized_staging_root,
        )
        from src.living_authenticity.security.path_boundary import PathBoundary


        paths = self.paths()
        session_audit = self.session_destination("audit")
        session_staging = self.session_destination("staging")
        audit_root = (session_audit
                      if session_audit else
                      resolve_authorized_audit_root(paths))
        staging_root = (session_staging
                        if session_staging else
                        resolve_authorized_staging_root(paths))
        audit_items = []
        if audit_root:
            try:
                boundary = PathBoundary(audit_root)
            except Exception:
                boundary = None

            if boundary is not None:
                for category in ("passed", "failed", "errors"):
                    log = Path(audit_root) / category / "audit.jsonl"
                    try:
                        validated_log = boundary.validate(str(log))
                    except Exception:
                        continue
                    audit_items.extend(self._read_audit_log(validated_log, category))

                emergency_log = Path(audit_root) / "errors" / "audit_emergency.jsonl"
                try:
                    validated_emerg = boundary.validate(str(emergency_log))
                except Exception:
                    validated_emerg = None
                if validated_emerg and validated_emerg.is_file():
                    audit_items.extend(self._read_audit_log(validated_emerg, "errors", is_emergency=True))

        audit_items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
        try:
            limit_val = max(1, min(int(limit), MAX_JOURNAL_ENTRIES))
        except (TypeError, ValueError):
            limit_val = MAX_JOURNAL_ENTRIES
        selected_audits = audit_items[:limit_val]

        paired_audits = []
        paired_staging = []

        staging_boundary = None
        if staging_root:
            try:
                staging_boundary = PathBoundary(staging_root)
            except Exception:
                staging_boundary = None

        for item in selected_audits:
            reference = item.get("artifact_reference") or ""
            artifact_p = item.get("artifact_path") or (
                str(Path(staging_root) / reference)
                if reference and staging_root else "")

            validated_artifact_p = None
            if artifact_p and staging_boundary is not None:
                try:
                    validated_artifact_p = staging_boundary.validate(artifact_p)
                except Exception:
                    validated_artifact_p = None

            is_staged = bool(validated_artifact_p and validated_artifact_p.is_file())
            staging_excerpt = ""
            staging_title = reference or "Unavailable"

            if is_staged and validated_artifact_p is not None:
                try:
                    with validated_artifact_p.open("r", encoding="utf-8") as f:
                        lines = [f.readline() for _ in range(10)]
                        staging_excerpt = "".join(lines).strip()[:300]
                except OSError:
                    staging_excerpt = ""
            else:
                staging_excerpt = "No staged artifact produced (terminal outcome: %s)." % (
                    item.get("outcome") or item.get("storage_category") or "none"
                )

            action = item.get("proposal_action") or "KNOWLEDGE_PROPOSAL"
            outcome = item.get("outcome") or item.get("storage_category") or "evaluated"
            is_emergency = bool(item.get("emergency", False))
            if is_emergency:
                summary = f"Emergency audit record: action {action} reached terminal outcome: {outcome}."
                title = f"Emergency Audit: {action}"
            else:
                summary = f"Proposal action {action} reached terminal outcome: {outcome}."
                title = f"Audit: {action}"
            raw_digest = item.get("record_digest") or hashlib.sha256(
                json.dumps(item, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()

            audit_slot = dict(item)
            audit_slot.update({
                "staging_available": is_staged,
                "digest": raw_digest[:16],
                "title": title,
                "summary": summary,
                "emergency": is_emergency,
            })

            staging_slot = {
                "record_id": item.get("record_id", ""),
                "run_id": item.get("run_id", ""),
                "proposal_hash": item.get("proposal_hash", ""),
                "timestamp": item.get("timestamp", ""),
                "storage_category": item.get("storage_category", ""),
                "outcome": "staged" if is_staged else "unavailable",
                "artifact_reference": reference if is_staged else "",
                "artifact_path": str(validated_artifact_p) if is_staged and validated_artifact_p else "",
                "staging_available": is_staged,
                "title": staging_title,
                "excerpt": staging_excerpt,
                "emergency": is_emergency,
            }

            paired_audits.append(audit_slot)
            paired_staging.append(staging_slot)

        # Check for unreconciled staging artifacts (e.g. terminal audit write failure)
        if staging_root and staging_boundary is not None and Path(staging_root).is_dir():
            referenced = {item.get("artifact_reference") for item in selected_audits if item.get("artifact_reference")}
            try:
                for sf in sorted(Path(staging_root).glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                    try:
                        sf_val = staging_boundary.validate(str(sf))
                    except Exception:
                        continue
                    if sf.name not in referenced:
                        try:
                            with sf_val.open("r", encoding="utf-8") as h:
                                lines = [h.readline() for _ in range(5)]
                                orphan_excerpt = "".join(lines).strip()[:300]
                        except OSError:
                            orphan_excerpt = ""
                        stat = sf.stat()
                        from datetime import datetime, timezone
                        ts = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                        stem = sf.stem.strip()
                        is_proposal_hash = len(stem) == 64 and all(c in "0123456789abcdefABCDEF" for c in stem)
                        if is_proposal_hash:
                            orphan_id = f"staged-{stem[:12]}"
                            paired_staging.append({
                                "record_id": orphan_id,
                                "run_id": "",
                                "proposal_hash": stem,
                                "timestamp": ts,
                                "storage_category": "staging_unreconciled",
                                "outcome": "staged_audit_pending",
                                "artifact_reference": sf.name,
                                "artifact_path": str(sf),
                                "staging_available": True,
                                "title": f"Staged Artifact (Audit Pending Persistence): {sf.name}",
                                "excerpt": orphan_excerpt or "Staging artifact exists on disk; terminal audit record is pending persistence.",
                            })
                            paired_audits.append({
                                "record_id": orphan_id,
                                "run_id": "",
                                "proposal_hash": stem,
                                "timestamp": ts,
                                "storage_category": "audit_pending",
                                "outcome": "staged_audit_pending",
                                "artifact_reference": sf.name,
                                "artifact_path": str(sf),
                                "staging_available": True,
                                "proposal_action": "CREATE",
                                "record_digest": stem[:16],
                                "digest": stem[:16],
                                "title": f"Staged Artifact (Audit Pending Persistence): {sf.name}",
                                "summary": "Staging artifact was created on disk, but terminal audit record persistence did not complete.",
                            })
                        else:
                            unlinked_id = f"unlinked-{stem[:12]}"
                            file_digest = hashlib.sha256(sf.name.encode("utf-8")).hexdigest()[:16]
                            paired_staging.append({
                                "record_id": unlinked_id,
                                "run_id": "",
                                "proposal_hash": "",
                                "timestamp": ts,
                                "storage_category": "staging_unlinked",
                                "outcome": "staging_unlinked",
                                "artifact_reference": sf.name,
                                "artifact_path": str(sf),
                                "staging_available": True,
                                "title": f"Unlinked Staging File: {sf.name}",
                                "excerpt": orphan_excerpt or f"Unlinked file in staging root: {sf.name}",
                            })
                            paired_audits.append({
                                "record_id": unlinked_id,
                                "run_id": "",
                                "proposal_hash": "",
                                "timestamp": ts,
                                "storage_category": "staging_unlinked",
                                "outcome": "staging_unlinked",
                                "artifact_reference": sf.name,
                                "artifact_path": str(sf),
                                "staging_available": True,
                                "proposal_action": "UNKNOWN",
                                "record_digest": file_digest,
                                "digest": file_digest,
                                "title": f"Unlinked Staging File: {sf.name}",
                                "summary": f"File {sf.name} was found in the staging directory without a corresponding audit trail record or proposal hash.",
                            })
            except Exception:
                pass

        return {"audits": paired_audits,
                "staging": paired_staging}

    @staticmethod
    def _read_audit_log(log_path, category, is_emergency=False):
        items = []
        if not Path(log_path).is_file():
            return items
        try:
            with Path(log_path).open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    items.append({
                        "record_id": str(record.get("record_id", "")),
                        "run_id": str(record.get("run_id", "")),
                        "proposal_hash": str(record.get("proposal_hash", "")),
                        "timestamp": str(record.get("timestamp", "")),
                        "storage_category": category,
                        "outcome": str(record.get("terminal_outcome", "") or record.get("outcome", "")),
                        "artifact_reference": str(
                            record.get("artifact_reference", "")),
                        "artifact_path": str(record.get("artifact_path", "")),
                        "staging_available": False,
                        "proposal_action": str(record.get("proposal_action", "")),
                        "record_digest": str(record.get("record_digest", "")),
                        "narrative": str(record.get("narrative", "")),
                        "narrative_language": str(record.get("narrative_language", "")),
                        "emergency": bool(is_emergency or record.get("emergency", False)),
                    })
        except OSError:
            return items
        return items

    # ---- folder selection -----------------------------------------------

    def pick_folder(self, purpose):
        """Native dialog + backend validation; returns a typed result.

        A validated selection is remembered as this launch's session
        destination, and persisted to Config/paths.local.yaml.
        """
        from .picker import (
            STATUS_CONFIGURED,
            normalize_purpose,
            tkinter_pick,
            validate_purpose,
        )

        canonical = normalize_purpose(purpose)
        choose = self._picker if self._picker is not None else tkinter_pick
        status, path, reason = choose(canonical)
        if status != STATUS_CONFIGURED:
            return (status, "", reason)
        result = validate_purpose(canonical, path, self.paths())
        if (result[0] == STATUS_CONFIGURED
                and isinstance(path, str) and path.strip()):
            clean_path = path.strip()
            with self._lock:
                prev_vault = self._session_destinations.get("vault", "")
                self._session_destinations[canonical] = clean_path
                if canonical == "vault":
                    self._active_source_mode = "personal_vault"
                    if prev_vault and prev_vault != clean_path and self._vault_sync_session is not None:
                        self._vault_sync_session.revoke()
            try:
                from Config.settings import save_paths_local
                mapping = {}
                if canonical == "vault":
                    mapping = {"obsidian": {"vault": clean_path}}
                elif canonical == "staging":
                    mapping = {"staging": {"root": clean_path}}
                elif canonical == "audit":
                    mapping = {
                        "audit": {
                            "root": clean_path,
                            "passed": str(Path(clean_path) / "passed"),
                            "failed": str(Path(clean_path) / "failed"),
                        }
                    }
                    try:
                        from src.living_authenticity.knowledge.governance.audit.persistence import (
                            AuditWriter,
                        )
                        writer = AuditWriter(clean_path)
                        writer.ensure_directories()
                    except Exception:
                        pass
                elif canonical == "vector_db":
                    mapping = {"vector_db": {"lancedb": clean_path}}
                elif canonical == "embedding_model":
                    mapping = {"models": {"bge_m3": clean_path}}
                if mapping:
                    save_paths_local(mapping)
                    self._paths = None
            except Exception:
                pass
        return result

    def session_destination(self, purpose):
        """Return this launch's UI-validated destination, or ``""``."""
        with self._lock:
            value = self._session_destinations.get(purpose, "")
        return value.strip() if isinstance(value, str) else ""

    # ---- comprehensive state & config ------------------------------------

    def state(self) -> dict:
        """Return the comprehensive active state of all folders, modes, and LLM."""
        from .picker import STATUS_CONFIGURED, STATUS_UNAVAILABLE, display_for

        folders = {}
        for purpose in ("vault", "staging", "audit", "vector_db", "embedding_model", "local_llm"):
            configured_path = self.session_destination(purpose)
            if configured_path and Path(configured_path).exists():
                folders[purpose] = {
                    "status": STATUS_CONFIGURED,
                    "display": display_for(configured_path),
                    "configured": True,
                }
            else:
                folders[purpose] = {
                    "status": STATUS_UNAVAILABLE,
                    "display": "",
                    "configured": False,
                }

        llm_cfg = self.llm_config()
        provider = llm_cfg.get("provider", "local") if isinstance(llm_cfg, dict) else "local"
        model_name = ""
        endpoint = ""
        if isinstance(llm_cfg, dict):
            provider_cfg = llm_cfg.get(provider, {})
            if isinstance(provider_cfg, dict):
                model_name = provider_cfg.get("model", "")
                endpoint = provider_cfg.get("endpoint", "")

        return {
            "app": APP_NAME,
            "version": VERSION,
            "instance_id": self.instance_id,
            "modes": {
                "source_mode": self._active_source_mode,
                "retrieval_mode": self._active_retrieval_mode,
                "app_language": self._app_language,
                "staging_language": self._staging_language,
                "audit_language": self._audit_language,
            },
            "folders": folders,
            "llm": {
                "provider": provider,
                "model": model_name,
                "endpoint": endpoint,
                "local_available": bool(self.session_destination("local_llm") or provider == "local"),
                "cloud_available": bool(provider == "cloud" and endpoint),
            },
            "active_run_id": self._active_run_id,
            "paused_run": self._paused_run,
            "pending_sync_approval": (
                {
                    "run_id": self._pending_vault_sync.run_id,
                    "vault_id": self._pending_vault_sync.vault_id,
                    "diff": self._pending_vault_sync.diff,
                    "message": "Vault changes detected requiring synchronization approval",
                    "created_at": self._pending_vault_sync.created_at,
                }
                if self._pending_vault_sync and self._pending_vault_sync.state == ApprovalState.PENDING
                else None
            ),
        }

    def update_config(self, updates: dict) -> dict:
        """Update and persist application configuration."""
        if not isinstance(updates, dict):
            raise AdapterError("validation_error", "updates must be a JSON object")

        with self._lock:
            if "source_mode" in updates and updates["source_mode"] in SOURCE_MODES:
                self._active_source_mode = updates["source_mode"]
            if "retrieval_mode" in updates and updates["retrieval_mode"] in ("lexical", "semantic"):
                self._active_retrieval_mode = updates["retrieval_mode"]
            if "app_language" in updates and updates["app_language"] in LANGUAGE_MODES:
                self._app_language = updates["app_language"]
            if "staging_language" in updates and updates["staging_language"] in LANGUAGE_MODES:
                self._staging_language = updates["staging_language"]
            if "audit_language" in updates and updates["audit_language"] in LANGUAGE_MODES:
                self._audit_language = updates["audit_language"]

        loc_updates = {}
        if "app_language" in updates and updates["app_language"] in LANGUAGE_MODES:
            loc_updates["app"] = updates["app_language"]
        if "staging_language" in updates and updates["staging_language"] in LANGUAGE_MODES:
            loc_updates["staging"] = updates["staging_language"]
        if "audit_language" in updates and updates["audit_language"] in LANGUAGE_MODES:
            loc_updates["audit"] = updates["audit_language"]
        if loc_updates:
            try:
                from Config.settings import save_localization_local
                save_localization_local({"language": loc_updates})
            except Exception:
                pass

        if "llm" in updates and isinstance(updates["llm"], dict):
            try:
                from Config.settings import save_llm_local
                save_llm_local(updates["llm"])
                self._llm_config = None
            except Exception:
                pass

        return {"success": True, "modes": {
            "source_mode": self._active_source_mode,
            "retrieval_mode": self._active_retrieval_mode,
            "app_language": self._app_language,
            "staging_language": self._staging_language,
            "audit_language": self._audit_language,
        }}

    def translate_text(
        self,
        text: str,
        target_lang: str = "en",
        source_lang: str = "",
        source_kind: str = "dynamic_text",
        source_id: str = "",
    ) -> dict:
        """Translate text between en and fa using LLM or deterministic fallback."""
        import dataclasses
        from src.living_authenticity.localization.config import normalize_language
        from src.living_authenticity.localization.present import (
            build_translation_record,
            TranslationStatus,
        )

        from src.living_authenticity.localization.bidi_core import contains_persian

        effective_target = normalize_language(
            target_lang if target_lang in LANGUAGE_MODES else ("en" if self._app_language == "fa" else "fa")
        )
        if source_lang and source_lang in LANGUAGE_MODES:
            effective_source = normalize_language(source_lang)
        elif isinstance(text, str) and text.strip():
            effective_source = "fa" if contains_persian(text) else "en"
        else:
            effective_source = "en"
        kind = source_kind or "dynamic_text"
        src_id = source_id or ""

        if not isinstance(text, str) or not text.strip():
            rec = build_translation_record(
                source_kind=kind,
                source_id=src_id,
                source_text=text or "",
                source_language=effective_source,
                target_language=effective_target,
                translated_text="",
                translator="empty",
                status=TranslationStatus.UNAVAILABLE,
            )
            d = dataclasses.asdict(rec)
            d["target_lang"] = rec.target_language
            d["persisted"] = False
            d["source_text"] = text or ""
            return d

        if effective_source == effective_target:
            rec = build_translation_record(
                source_kind=kind,
                source_id=src_id,
                source_text=text,
                source_language=effective_source,
                target_language=effective_target,
                translated_text=text,
                translator="identity",
                status=TranslationStatus.CURRENT,
            )
            d = dataclasses.asdict(rec)
            d["target_lang"] = rec.target_language
            d["persisted"] = False
            d["source_text"] = text
            return d

        translated = ""
        translator_name = ""
        try:
            llm_cfg = self.llm_config()
            from src.living_authenticity.llm.registry import build_provider
            provider = build_provider(llm_cfg)
            prompt = (
                f"You are a professional translator. Translate the following text into "
                f"{'Persian (Farsi)' if effective_target == 'fa' else 'English'}.\n"
                f"Provide ONLY the translation, without explanation or introductory text:\n\n"
                f"{text}"
            )
            resp_text = provider.complete(prompt)
            if resp_text:
                translated = resp_text.strip()
                p_name = getattr(provider, "name", "llm-provider")
                translator_name = f"llm-{p_name}"
        except Exception:
            translated = ""

        if not translated:
            translations_en_to_fa = {
                "Summary Of Recent Audits": "خلاصه بررسی‌های اخیر",
                "Summary Of Recent Staging": "خلاصه یادداشت‌های اخیر آماده‌سازی",
                "Processing Result": "نتیجه پردازش",
                "Workflow": "گردش کار",
                "Current Audit": "بررسی جاری",
                "Database": "پایگاه داده",
                "AI": "هوش مصنوعی",
                "Knowledge Language": "زبان دانش",
                "Test Mode": "حالت آزمایشی",
                "Vault Mode": "حالت یادداشت‌دان",
                "Approved": "تایید شده",
                "Rejected": "رد شده",
                "Staged": "آماده‌سازی شده",
                "Unavailable": "در دسترس نیست",
                "Evidence Verification": "بررسی شواهد",
                "Synthesizing Note": "تولید یادداشت",
                "Waiting for Human Review": "در انتظار بازبینی انسانی",
                "Proposal Type": "نوع پیشنهاد",
                "Confidence": "میزان اطمینان",
                "Class": "کلاس",
            }
            translations_fa_to_en = {v: k for k, v in translations_en_to_fa.items()}

            if effective_target == "fa":
                matched = translations_en_to_fa.get(text.strip())
                if matched:
                    translated = matched
                    translator_name = "static-dictionary-v1"
                else:
                    translated = f"«{text.strip()}» (ترجمه خودکار در دسترس نیست — مدل زبانی متصل نیست)"
                    translator_name = "offline-fallback"
            else:
                matched = translations_fa_to_en.get(text.strip())
                if matched:
                    translated = matched
                    translator_name = "static-dictionary-v1"
                else:
                    translated = f"{text.strip()} (translation unavailable — LLM not connected)"
                    translator_name = "offline-fallback"

        status = (
            TranslationStatus.CURRENT
            if (translated and "متصل نیست" not in translated and "not connected" not in translated)
            else TranslationStatus.UNAVAILABLE
        )

        rec = build_translation_record(
            source_kind=kind,
            source_id=src_id,
            source_text=text,
            source_language=effective_source,
            target_language=effective_target,
            translated_text=translated,
            translator=translator_name,
            status=status,
        )
        d = dataclasses.asdict(rec)
        d["target_lang"] = rec.target_language
        d["persisted"] = False
        d["source_text"] = text
        return d

    def accept_proposal(self, run_id: str = "") -> dict:
        """Acknowledge and accept a non-actionable proposal (NEEDS_REVIEW / DO_NOT_IMPORT)."""
        with self._lock:
            pending = self._pending_approval
            if pending is not None:
                action = pending.payload.get("proposal_action", "")
                if action in ("NEEDS_REVIEW", "DO_NOT_IMPORT"):
                    pending.resolve("reject")
                    self._pending_approval = None
                    self._sink({
                        "kind": "status",
                        "stage": "human_review",
                        "message": f"proposal {action} acknowledged by human",
                    })
                    return {"accepted": True, "action": action, "run_id": run_id}
            self._sink({
                "kind": "status",
                "stage": "human_review",
                "message": "review findings acknowledged",
            })
            return {"accepted": True, "action": "acknowledged", "run_id": run_id}

    def fix_paused_run(
        self,
        run_id: str = "",
        input_text: str | None = None,
        source_mode: str | None = None,
        retrieval_mode: str | None = None,
    ) -> dict:
        """Fix input or config for a paused/errored run and restart it."""
        with self._lock:
            paused = self._paused_run
            if not paused or (run_id and paused.get("run_id") != run_id):
                raise AdapterError("validation_error", "no paused run matching run_id")
            text = input_text if input_text is not None else paused.get("input_text", "")
            src_mode = source_mode if source_mode is not None else paused.get("source_mode", self._active_source_mode)
            ret_mode = retrieval_mode if retrieval_mode is not None else paused.get("retrieval_mode", self._active_retrieval_mode)
            self._paused_run = None

        return self.start_run(
            input_text=text,
            source_mode=src_mode,
            retrieval_mode=ret_mode,
            app_language=self._app_language,
            staging_language=self._staging_language,
            audit_language=self._audit_language,
        )

    def stop_paused_run(self, run_id: str = "") -> dict:
        """Stop and cleanly terminate a paused/errored run."""
        with self._lock:
            paused = self._paused_run
            if paused and (not run_id or paused.get("run_id") == run_id):
                self._paused_run = None
                self._sink({
                    "kind": "stopped",
                    "stage": "",
                    "message": "paused run stopped by user",
                })
                return {"stopped": True, "run_id": run_id or paused.get("run_id", "")}
        return {"stopped": True, "run_id": run_id}

    def reveal_folder(self, purpose: str) -> dict:
        """List files in the synced directory for the given purpose without leaking secrets."""
        from .picker import display_for, normalize_purpose
        canonical = normalize_purpose(purpose)
        folder = self.session_destination(canonical)
        if not folder or not Path(folder).is_dir():
            return {"purpose": canonical, "display": "", "items": []}

        items = []
        try:
            for child in Path(folder).iterdir():
                if child.name.startswith("."):
                    continue
                try:
                    stat = child.stat()
                    items.append({
                        "name": child.name,
                        "is_dir": child.is_dir(),
                        "size": stat.st_size if child.is_file() else 0,
                        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    })
                except OSError:
                    continue
        except OSError:
            pass

        items.sort(key=lambda x: x["name"])
        return {
            "purpose": canonical,
            "display": display_for(folder),
            "items": items[:100],
        }

    def authorize_vault_sync(self, vault_root: str | None = None) -> dict:
        """Explicitly authorize synchronization for the selected Vault."""
        paths = self.paths()
        vault = (
            vault_root
            or self.session_destination("vault")
            or paths.get("obsidian", {}).get("vault", "")
            or paths.get("vault", {}).get("root", "")
        )
        if not vault:
            raise AdapterError("validation_error", "No Vault folder is configured.")
        vector_db = (
            self.session_destination("vector_db")
            or paths.get("vector_db", {}).get("lancedb", "")
            or paths.get("vector_db", {}).get("root", "")
        )
        if not vector_db:
            raise AdapterError("validation_error", "No Database folder is configured for the vector index.")
        model_path = (
            self.session_destination("embedding_model")
            or paths.get("models", {}).get("bge_m3", "")
            or paths.get("models", {}).get("root", "")
        )
        embedder = self._embedder
        if embedder is None and not model_path:
            raise AdapterError("validation_error", "No embedding model configured.")
        staging_root = self.session_destination("staging") or paths.get("staging", {}).get("root", "")
        audit_root = self.session_destination("audit") or paths.get("audit", {}).get("root", "")

        from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
        pipe = EvidenceFirstPipeline(stage_observer=self._sink)
        try:
            report = self._vault_sync_session.authorize(
                vault_root=vault,
                vector_db_root=vector_db,
                embedder=embedder,
                model_path=model_path if embedder is None else None,
                pipeline=pipe,
                staging_root=staging_root,
                audit_root=audit_root,
                auto_watch=False,
            )
            if not report.get("fresh", False):
                err = report.get("error") or "Initial synchronization failed to produce a fresh index"
                raise AdapterError("validation_error", f"Vault synchronization authorization failed: {err}")
            return {
                "indexed": report.get("fresh", False),
                "vault_id": report.get("vault_id", ""),
                "files_discovered": report.get("indexed_notes", 0),
                "notes_indexed": report.get("indexed_vectors", 0),
                "files_skipped": 0,
                "status": report.get("status", ""),
                "last_synced_at": report.get("last_synced_at", ""),
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("validation_error", f"Vault synchronization authorization failed: {exc}")

    def revoke_vault_sync(self) -> dict:
        """Revoke synchronization authorization for the active Vault."""
        return self._vault_sync_session.revoke()

    def get_vault_sync_status(self) -> dict:
        """Return the current synchronization and freshness status."""
        return self._vault_sync_session.get_status()

    def reconcile_vault_sync(self) -> dict:
        """Explicitly reconcile changes for the authorized Vault."""
        return self._vault_sync_session.reconcile()

    def check_vault_sync_changes(self) -> dict:
        """Inspect current Vault changes against recorded index manifest without mutating."""
        return self._vault_sync_session.check_changes()

    def index_vault(self) -> dict:
        """Run initial vector index on configured Vault notes into isolated vector DB."""
        return self.authorize_vault_sync()

    def close(self) -> None:
        """Shut down background monitor threads cleanly."""
        if self._vault_sync_session is not None:
            self._vault_sync_session.close()

    # ---- health ---------------------------------------------------------

    def health(self):
        return {"app": APP_NAME, "version": VERSION,
                "instance_id": self.instance_id}


__all__ = [
    "Launcher",
    "AdapterError",
    "APP_NAME",
    "VERSION",
    "TOKEN_HEADER",
    "SOURCE_MODES",
    "APPROVE_DECISIONS",
    "MAX_TEXT_BYTES",
    "MAX_BODY_BYTES",
    "APPROVAL_TIMEOUT_SECONDS",
    "CONSENT_TIMEOUT_SECONDS",
    "MAX_JOURNAL_ENTRIES",
]
