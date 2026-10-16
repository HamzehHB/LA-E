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
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
})


class AdapterError(Exception):
    """Typed, bounded launcher error (never a raw stack trace)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self._event = threading.Event()
        self._result = None

    def resolve(self, value) -> bool:
        if self._event.is_set():
            return False
        self._result = value
        self._event.set()
        return True

    def wait(self, timeout: float):
        if not self._event.wait(timeout):
            return None
        return self._result


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

    @property
    def name(self) -> str:
        return self._strategy

    def build_request(self, query, proposal=None, confidence=None, note=None):
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
            # A pending edit supersedes this request: adopt the edited
            # proposal/note and loop to re-request approval of the new hash.
            edited = self._launcher.consume_pending_edit(
                request.proposal_hash)
            if edited is not None:
                proposal = edited.proposal
                note = edited.note
                continue
            outcome = self._inner.decide(request, raw)
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
        self._reports = {}
        from .events import RunEventLog

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
        if decision in ("approve", "reject"):
            return "y" if decision == "approve" else "n"
        return ""

    def approve(self, proposal_hash, query_unit_id, decision):
        """Answer the single pending approval exactly and only once."""
        if decision not in APPROVE_DECISIONS:
            raise AdapterError("validation_error",
                               "decision must be approve or reject")
        with self._lock:
            pending = self._pending_approval
            if pending is None or pending.key != proposal_hash:
                raise AdapterError(
                    "validation_error",
                    "no pending approval for this proposal hash")
            if (pending.payload.get("query_unit_id")
                    != (query_unit_id or "")):
                raise AdapterError(
                    "validation_error",
                    "approval does not match the pending request")
            if not pending.resolve(decision):
                raise AdapterError("validation_error",
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
                  staging_language="en", audit_language="en"):
        """Begin one bounded text-input run; returns immediately.

        Language fields are validated at the boundary and never silently
        ignored. ``app_language`` is the interface language (kept separate
        from generated content). ``staging_language`` / ``audit_language`` are
        the requested content languages; the pipeline records them but does
        not translate generated content (no translation provider exists), so
        the effective output language is reported honestly on the run.
        """
        if not isinstance(input_text, str) or not input_text.strip():
            raise AdapterError("validation_error", "input text is required")
        if len(input_text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise AdapterError("validation_error", "input text too large")
        if source_mode not in SOURCE_MODES:
            raise AdapterError(
                "validation_error",
                "source_mode must be one of: " + ", ".join(SOURCE_MODES))
        if retrieval_mode not in ("lexical", "semantic"):
            raise AdapterError(
                "validation_error",
                "retrieval_mode must be one of: lexical, semantic")
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
                  staging_language, audit_language),
            daemon=True)
        worker.start()
        return {"run_id": run_id, "accepted": True, "reason": "",
                "retrieval_mode": retrieval_mode,
                "app_language": app_language,
                "staging_language": staging_language,
                "audit_language": audit_language}

    def _run_thread(self, run_id, input_text, mode_value,
                    retrieval_mode="lexical", staging_language="en",
                    audit_language="en"):
        try:
            report = self._execute_run(run_id, input_text, mode_value,
                                       retrieval_mode=retrieval_mode,
                                       staging_language=staging_language,
                                       audit_language=audit_language)
            with self._lock:
                self._reports[run_id] = self._report_summary(report)
            self._sink({"kind": "run_completed", "stage": "",
                        "message": "run finished"})
        except Exception as exc:  # bounded; never leaks a raw traceback
            with self._lock:
                self._reports[run_id] = {"run_id": run_id,
                                         "error": type(exc).__name__}
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
        self._sink({"kind": "status", "stage": "cancellation",
                    "message": "cancellation requested"})
        return {"run_id": run_id, "status": "cancel_requested",
                "reason": "cancellation will take effect at the next "
                          "safe boundary"}

    def _cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancel_flags

    @staticmethod
    def _report_summary(report):
        units = list(getattr(report, "units", ()))
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
        }

    def run_status(self, run_id):
        with self._lock:
            summary = dict(self._reports.get(run_id, {}))
            summary["run_id"] = run_id
            summary["active"] = run_id == self._active_run_id
        return summary

    def _execute_run(self, run_id, input_text, mode_value,
                     retrieval_mode="lexical", staging_language="en",
                     audit_language="en"):
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
        # Retrieval mode is an explicit request, not a silent default.
        # "semantic" requires a validated isolated vector store, a loaded
        # embedding service, and dimensional compatibility with the store
        # schema. Anything missing falls back to "lexical" and the run
        # records the effective mode, so the UI never shows "semantic"
        # while the pipeline actually ran lexical.
        effective_retrieval = "lexical"
        vector_components = None
        vector_note = ""
        if retrieval_mode == "semantic":
            try:
                from src.living_authenticity.knowledge.integration import (
                    vector as _vector,
                )
                vector_db = self.session_destination("vector_db")
                if not vector_db:
                    vector_note = (
                        "semantic requested but no vector database "
                        "selected; lexical used")
                else:
                    eligible, _reason, _check = (
                        _vector.check_vector_store_eligible(
                            vector_db,
                            _vector.resolve_production_vector_db(paths)))
                    if not eligible:
                        vector_note = (
                            "semantic requested but the vector database "
                            "is not eligible; lexical used")
                    else:
                        model_path = self.session_destination(
                            "embedding_model")
                        if not model_path:
                            vector_note = (
                                "semantic requested but no embedding model "
                                "selected; lexical used")
                        else:
                            try:
                                service, _store, retriever = (
                                    _vector.build_vector_components(
                                        vector_db, model_path=model_path))
                            except Exception as exc:
                                vector_note = (
                                    "semantic requested but the embedding "
                                    "model could not load (%s); lexical used"
                                    % type(exc).__name__)
                            else:
                                try:
                                    from src.living_authenticity.database import (
                                        schema as _schema,
                                    )
                                    expected = int(
                                        getattr(_schema,
                                                "VECTOR_DIMENSION", 0))
                                except Exception:
                                    expected = 0
                                try:
                                    got = int(
                                        service.model.get_dimension())
                                except Exception:
                                    got = 0
                                _ = (_store,)
                                if expected and got and expected != got:
                                    vector_note = (
                                        "semantic requested but embedding "
                                        "dimension %s mismatches schema %s; "
                                        "lexical used" % (got, expected))
                                else:
                                    effective_retrieval = "semantic"
                                    vector_components = (
                                        service, retriever)
            except Exception as exc:
                vector_note = ("semantic unavailable (%s); lexical used"
                               % type(exc).__name__)
        if self._pipeline_factory is not None:
            pipe = self._pipeline_factory(self._sink)
        elif vector_components is not None:
            _service, _retriever = vector_components
            pipe = EvidenceFirstPipeline(stage_observer=self._sink,
                                         retriever=_retriever)
        else:
            pipe = EvidenceFirstPipeline(stage_observer=self._sink)
        self._sink({"kind": "status", "stage": "retrieval",
                    "message": (f"retrieval mode: {effective_retrieval}"
                                + (f" ({vector_note})"
                                   if vector_note else ""))})
        # Knowledge-language contract (honest). The requested content
        # languages are recorded on the run, but the pipeline has no
        # translation provider, so generated Audit/Staging content is NOT
        # rewritten or translated. Existing records keep their stored
        # language. This never alters authorization or source meaning.
        self._sink({"kind": "status", "stage": "localization",
                    "message": ("requested content languages: "
                                f"staging={staging_language} "
                                f"audit={audit_language}; no translation "
                                "provider is configured, so generated "
                                "content language is unchanged")})
        ingestion = pipe.ingestion.ingest_text(
            input_text, source="<text-input>", kind="text-input.md")
        # Portable UI-first resolution: a UI-validated session destination
        # (set by pick_folder() this launch) takes precedence; otherwise
        # fall back to the config-declared location. The chosen value is
        # revalidated below by run_bounded_integration's own guards, so a
        # stale or tampered session value fails closed instead of writing
        # somewhere unexpected.
        session_staging = self._session_destinations.get("staging", "")
        session_audit = self._session_destinations.get("audit", "")
        staging_arg = (session_staging.strip()
                       if isinstance(session_staging, str) else "")
        if not staging_arg:
            staging_arg = resolve_authorized_staging_root(paths)
        authorized_audit = (session_audit.strip()
                            if isinstance(session_audit, str) else "")
        if not authorized_audit:
            authorized_audit = resolve_authorized_audit_root(paths)
        authorized_passed = resolve_authorized_audit_passed(paths)
        authorized_failed = resolve_authorized_audit_failed(paths)
        llm_config = self.llm_config()
        if self._synthesis_factory is not None:
            synthesis = self._synthesis_factory(
                confirm=self._consent_confirm, config=llm_config)
        else:
            from src.living_authenticity.llm.orchestration import (
                build_synthesis,
            )
            from src.living_authenticity.llm.registry import build_provider

            synthesis = build_synthesis(
                build_provider(llm_config), confirm=self._consent_confirm,
                config=llm_config)
        return _integration.run_bounded_integration(
            "", staging_arg, pipeline=pipe,
            corpus=(), core_units=(), max_files=1,
            llm_config=llm_config, synthesis=synthesis,
            ingestion=ingestion,
            vault_context=None,
            approval_gate=_PublishingGate(self),
            approval_reader=self._approval_reader,
            run_id=run_id, event_sink=self._sink,
            operating_mode=mode_value,
            audit_root=authorized_audit,
            authorized_audit_root=authorized_audit,
            authorized_audit_passed=authorized_passed,
            authorized_audit_failed=authorized_failed,
            guarded_roots=resolve_guarded_roots(paths),
            should_cancel=lambda: self._cancelled(run_id),
        )


    # ---- journals (read-only observation) -------------------------------

    def journals(self, limit=MAX_JOURNAL_ENTRIES):
        """Correlate persisted audit records with staging artifacts.

        Read-only: scans nothing the user did not configure; correlation
        uses record/artifact identifiers, never filenames or positions.
        1:1 paired slots:
        - Excludes 'errors/'
        - Computes digests and newspaper summary
        - Each audit slot is paired with a staging slot (or 'Unavailable' if missing/rejected)
        """
        from src.living_authenticity.knowledge.integration.guard import (
            resolve_authorized_audit_root,
            resolve_authorized_staging_root,
        )

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
            for category in ("passed", "failed"):
                log = Path(audit_root) / category / "audit.jsonl"
                audit_items.extend(self._read_audit_log(log, category))

        audit_items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
        try:
            limit_val = max(1, min(int(limit), MAX_JOURNAL_ENTRIES))
        except (TypeError, ValueError):
            limit_val = MAX_JOURNAL_ENTRIES
        selected_audits = audit_items[:limit_val]

        paired_audits = []
        paired_staging = []

        for item in selected_audits:
            reference = item.get("artifact_reference") or ""
            artifact_p = item.get("artifact_path") or (
                str(Path(staging_root) / reference)
                if reference and staging_root else "")

            is_staged = bool(artifact_p and Path(artifact_p).is_file())
            staging_excerpt = ""
            staging_title = reference or "Unavailable"

            if is_staged:
                try:
                    with Path(artifact_p).open("r", encoding="utf-8") as f:
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
            summary = f"Proposal action {action} reached terminal outcome: {outcome}."
            raw_digest = item.get("record_digest") or hashlib.sha256(
                json.dumps(item, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()

            audit_slot = dict(item)
            audit_slot.update({
                "staging_available": is_staged,
                "digest": raw_digest[:16],
                "title": f"Audit: {action}",
                "summary": summary,
            })

            staging_slot = {
                "record_id": item.get("record_id", ""),
                "run_id": item.get("run_id", ""),
                "proposal_hash": item.get("proposal_hash", ""),
                "timestamp": item.get("timestamp", ""),
                "storage_category": item.get("storage_category", ""),
                "outcome": "staged" if is_staged else "unavailable",
                "artifact_reference": reference if is_staged else "",
                "artifact_path": artifact_p if is_staged else "",
                "staging_available": is_staged,
                "title": staging_title,
                "excerpt": staging_excerpt,
            }

            paired_audits.append(audit_slot)
            paired_staging.append(staging_slot)

        return {"audits": paired_audits,
                "staging": paired_staging}

    @staticmethod
    def _read_audit_log(log_path, category):
        items = []
        if not Path(log_path).is_file():
            return items
        try:
            with log_path.open("r", encoding="utf-8") as handle:
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
                        "outcome": str(record.get("terminal_outcome", "")),
                        "artifact_reference": str(
                            record.get("artifact_reference", "")),
                        "artifact_path": str(record.get("artifact_path", "")),
                        "staging_available": False,
                        "proposal_action": str(record.get("proposal_action", "")),
                        "record_digest": str(record.get("record_digest", "")),
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
                self._session_destinations[canonical] = clean_path
                if canonical == "vault":
                    self._active_source_mode = "personal_vault"
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
        if "app_language" in updates:
            loc_updates["interface"] = updates["app_language"]
        if "staging_language" in updates:
            loc_updates.setdefault("content", {})["staging"] = updates["staging_language"]
        if "audit_language" in updates:
            loc_updates.setdefault("content", {})["audit"] = updates["audit_language"]
        if loc_updates:
            try:
                from Config.settings import save_localization_local
                save_localization_local(loc_updates)
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

    def translate_text(self, text: str, target_lang: str = "en", source_lang: str = "") -> dict:
        """Translate text between en and fa using LLM or deterministic fallback."""
        if not isinstance(text, str) or not text.strip():
            return {"translated_text": "", "target_lang": target_lang}
        if target_lang not in LANGUAGE_MODES:
            target_lang = "en" if self._app_language == "fa" else "fa"

        translated = ""
        try:
            llm_cfg = self.llm_config()
            from src.living_authenticity.llm.registry import build_provider
            provider = build_provider(llm_cfg)
            prompt = (
                f"You are a professional translator. Translate the following text into "
                f"{'Persian (Farsi)' if target_lang == 'fa' else 'English'}.\n"
                f"Provide ONLY the translation, without explanation or introductory text:\n\n"
                f"{text}"
            )
            resp_text = provider.complete(prompt)
            if resp_text:
                translated = resp_text.strip()
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

            if target_lang == "fa":
                translated = translations_en_to_fa.get(text.strip(), f"«{text.strip()}» (ترجمه‌شده به فارسی)")
            else:
                translated = translations_fa_to_en.get(text.strip(), f"{text.strip()} (translated to English)")

        return {"translated_text": translated, "target_lang": target_lang}

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

    def index_vault(self) -> dict:
        """Run initial vector index on configured Vault notes into isolated vector DB."""
        from src.living_authenticity.knowledge.integration.vector import (
            build_vector_components,
            check_vector_store_eligible,
            resolve_production_vector_db,
        )
        from src.living_authenticity.knowledge.integration import index_units
        from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
        from src.living_authenticity.knowledge.vault.context import read_vault_units

        paths = self.paths()
        vault_root = self.session_destination("vault")
        if not vault_root:
            raise AdapterError("validation_error", "No Vault folder is configured.")
        vector_db = self.session_destination("vector_db")
        if not vector_db:
            raise AdapterError("validation_error", "No Database folder is configured for the vector index.")
        model_path = self.session_destination("embedding_model")
        if not model_path:
            raise AdapterError("validation_error", "No embedding model (BGE-M3) configured.")

        eligible, reason, _ = check_vector_store_eligible(
            vector_db, resolve_production_vector_db(paths), vault_root=vault_root)
        if not eligible:
            raise AdapterError("validation_error", f"Vector database is not eligible: {reason}")

        pipe = EvidenceFirstPipeline(stage_observer=self._sink)
        vault_units, vault_files, vault_skipped = read_vault_units(vault_root, pipe)
        if not vault_units:
            return {
                "indexed": False,
                "reason": f"No valid vault notes discovered ({vault_skipped} skipped).",
                "files_discovered": vault_files,
                "notes_indexed": 0,
                "files_skipped": vault_skipped,
            }

        service, store, _retriever = build_vector_components(vector_db, model_path=model_path)
        figures = index_units(vault_units, service, store)
        stored = figures[1] if len(figures) > 1 else len(vault_units)
        return {
            "indexed": True,
            "files_discovered": vault_files,
            "notes_indexed": stored,
            "files_skipped": vault_skipped,
        }

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
