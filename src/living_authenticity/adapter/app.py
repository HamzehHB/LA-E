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
        # Keys: "staging", "audit". Absent key = not selected this session.
        self._session_destinations: dict = {}
        self._lock = threading.RLock()
        self._active_run_id = ""
        self._pending_approval = None
        self._pending_consent = None
        self._reports = {}
        from .events import RunEventLog

        self.events = RunEventLog()

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
            self._sink({"kind": "run_rejected", "stage": "",
                        "message": "run failed"})
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
        staged_files = {}
        if audit_root:
            for category in ("passed", "failed"):
                log = Path(audit_root) / category / "audit.jsonl"
                audit_items.extend(self._read_audit_log(log, category))
        if staging_root and Path(staging_root).is_dir():
            for child in Path(staging_root).iterdir():
                if child.is_file() and child.suffix == ".md":
                    staged_files[child.name] = str(child)
        for item in audit_items:
            reference = item.get("artifact_reference") or ""
            path = item.get("artifact_path") or (
                str(Path(staging_root) / reference)
                if reference and staging_root else "")
            item["staging_available"] = bool(
                path and Path(path).is_file())
        referenced = set()
        for item in audit_items:
            reference = item.get("artifact_reference") or ""
            if reference:
                referenced.add(reference)
        staging_items = []
        for item in audit_items:
            if not item.get("staging_available"):
                continue
            staging_items.append({
                "record_id": item["record_id"],
                "run_id": item["run_id"],
                "proposal_hash": item["proposal_hash"],
                "timestamp": item["timestamp"],
                "storage_category": item["storage_category"],
                "outcome": "staged",
                "artifact_reference": item["artifact_reference"],
                "staging_available": True,
            })
        for name, path in sorted(staged_files.items()):
            if name in referenced:
                continue
            staging_items.append({
                "record_id": name,
                "run_id": "",
                "proposal_hash": "",
                "timestamp": _utcnow(),
                "storage_category": "",
                "outcome": "audit_missing",
                "artifact_reference": name,
                "staging_available": True,
            })
        audit_items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
        staging_items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
        try:
            limit = max(1, min(int(limit), MAX_JOURNAL_ENTRIES))
        except (TypeError, ValueError):
            limit = MAX_JOURNAL_ENTRIES
        return {"audits": audit_items[:limit],
                "staging": staging_items[:limit]}

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
                    })
        except OSError:
            return items
        return items

    # ---- folder selection -----------------------------------------------

    def pick_folder(self, purpose):
        """Native dialog + backend validation; returns a typed result.

        A validated selection is additionally remembered as this launch's
        session destination (memory only), so later runs and journal reads
        use the user's own validated choice instead of requiring a
        machine-specific config file. ``staging``/``audit`` drive run
        destinations; ``vector_db``/``embedding_model`` drive semantic-
        retrieval availability checks. Every stored value was validated by
        ``validate_purpose``; consumers revalidate before each use.

        The stored value is the validated absolute path (not the safe
        display label); the frontend still only ever receives the
        typed ``(status, display, reason)`` result.
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
            with self._lock:
                self._session_destinations[canonical] = path.strip()
        return result

    def session_destination(self, purpose):
        """Return this launch's UI-validated destination, or ``""``."""
        with self._lock:
            value = self._session_destinations.get(purpose, "")
        return value.strip() if isinstance(value, str) else ""

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
