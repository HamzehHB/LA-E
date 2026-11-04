"""Top-level bounded local-integration run entry point (composition only)."""
import os
from pathlib import Path
from typing import Any

from src.living_authenticity.security.path_boundary import PathBoundary

from .guard import (
    _ALLOWED_SOURCE_SUFFIXES,
    check_staging_eligible,
    resolve_authorized_staging_root,
    resolve_guarded_roots,
)
from .outcome import IntegrationRunReport
from .unit_loop import _handle_unit


def _is_link_or_junction(path: Path) -> bool:
    """Safe check for symbolic links and Windows junctions. Fails closed on error."""
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction") and callable(path.is_junction):
            if path.is_junction():
                return True
        elif hasattr(os.path, "isjunction") and callable(os.path.isjunction):
            if os.path.isjunction(str(path)):
                return True
    except (OSError, PermissionError):
        # Fail closed on inaccessible or uncheckable reparse points
        return True
    return False


class StructuralValidationError(ValueError):
    """Raised when an ingested document violates expected schema or frontmatter structure."""
    pass


def _classify_ingestion_exception(exc: BaseException) -> str:
    """Classify an ingestion exception as 'failed_validation' or 'failed_operational'.

    Operational/Infrastructure:
        FileNotFoundError, PermissionError, OSError, IOError, UnicodeDecodeError,
        UnicodeError, TimeoutError, and unexpected internal pipeline/runtime/dependency
        failures (RuntimeError, AttributeError, unclassified Exception, unexpected ValueError/KeyError).
    Structural/Validation:
        Only when the failure represents an explicit domain structural/validation contract violation
        (StructuralValidationError or subclasses).
    """
    if isinstance(exc, StructuralValidationError):
        return "failed_validation"
    return "failed_operational"


def _persist_audits(audit_writer, audits, audit_receipts, staging_root=None) -> bool:
    """Append every in-memory audit record; receipts stay inspectable.

    Resilient persistence:
    1. Tries primary append (standard buffered write, durability: "written").
    2. On failure, attempts emergency journal in the errors/ partition (flushed + fsync, durability: "confirmed").
    3. If emergency journal also fails and an artifact was executed,
       quarantines the artifact into staging_root/.unreconciled/.
    Returns True if all records are successfully recorded (written or confirmed),
    False if persistence failed or writer is unconfigured.
    """
    if not audits:
        return True

    if audit_writer is None:
        for record in list(audits):
            rec_id = getattr(record, "record_id", "")
            prior = next(
                (r for r in audit_receipts if isinstance(r, dict) and r.get("record_id") == rec_id),
                None,
            )
            if prior is None:
                audit_receipts.append({
                    "record_id": rec_id,
                    "persisted": False,
                    "emergency": False,
                    "mode": "unconfigured",
                    "durability": "unconfigured",
                    "reason": "no audit writer configured",
                })
        return False

    from src.living_authenticity.knowledge.governance.audit.persistence import (
        persist_record_resilient,
        validate_persistence_receipt,
    )
    from src.living_authenticity.knowledge.governance.execution.executor import (
        quarantine_artifact,
    )
    import dataclasses
    from src.living_authenticity.knowledge.governance.audit.narrative import (
        generate_lifecycle_narrative,
    )

    all_ok = True
    for i, record in enumerate(list(audits)):
        rec_id = getattr(record, "record_id", "")
        # Prevent duplicate persistence attempts for records already processed
        prior_receipt = next(
            (r for r in audit_receipts if isinstance(r, dict) and r.get("record_id") == rec_id),
            None,
        )
        if prior_receipt is not None:
            is_valid, _ = validate_persistence_receipt(
                prior_receipt,
                require_emergency=bool(prior_receipt.get("emergency")),
                expected_record_id=rec_id or None,
            )
            if not is_valid or not prior_receipt.get("persisted"):
                all_ok = False
            continue

        art_path = getattr(record, "artifact_path", "") or getattr(record, "artifact_reference", "")
        run_id = getattr(record, "run_id", "")
        run_history = [
            r for r in audits[:i + 1]
            if getattr(r, "run_id", "") == run_id or not run_id
        ]
        res = persist_record_resilient(
            audit_writer, record, receipts=audit_receipts, history=run_history
        )
        if not res.get("persisted"):
            all_ok = False
            q_res = None
            is_q = False
            is_partial = False
            target_p = ""
            if art_path and staging_root:
                res["artifact_path"] = art_path
                q_res = quarantine_artifact(staging_root, art_path, collision_safe=True)
                is_q = bool(getattr(q_res, "success", False) or (isinstance(q_res, str) and q_res and getattr(q_res, "status", "") != "partial_cleanup_failed"))
                is_partial = getattr(q_res, "status", "") == "partial_cleanup_failed"
                target_p = getattr(q_res, "target_path", "") or (str(q_res) if is_q else "")
                res["quarantined"] = is_q
                res["quarantine_path"] = target_p if (is_q or is_partial) else ""
                res["quarantine_status"] = getattr(q_res, "status", "quarantined" if is_q else "quarantine_failed")
                if is_partial:
                    res["source_removed"] = False
            else:
                res["quarantined"] = False
                res["quarantine_status"] = "quarantine_failed"

            if is_q:
                reason_msg = "audit persistence failed: artifact quarantined"
            elif is_partial:
                reason_msg = "audit persistence failed: partial quarantine (source cleanup failed)"
            else:
                reason_msg = "audit persistence failed; quarantine failed"

            # Only update the in-memory audit record if it is an actual
            # dataclass instance (not a MagicMock or other test double).
            if dataclasses.is_dataclass(record) and not isinstance(record, type):
                final_ref = target_p if is_q else art_path
                updated_record = dataclasses.replace(
                    record,
                    terminal_outcome="audit_persistence_failed",
                    artifact_reference=final_ref,
                    artifact_path=art_path,
                    destination=target_p if (is_q or is_partial) else getattr(record, "destination", ""),
                    execution_reason=reason_msg,
                )
                updated_narrative = generate_lifecycle_narrative(
                    updated_record, language=getattr(record, "narrative_language", "en") or "en"
                )
                audits[i] = dataclasses.replace(updated_record, narrative=updated_narrative)
    return all_ok


def _run_one(child, ingestion, pipe, corpus_units, cores, gate, validator,
             runner, pinned, guarded, schema_version, approval_reader,
             entries, audits, authorized, synthesis, details,
             vault_context=None, operating_mode="", run_id="",
             audit_writer=None, audit_receipts=None,
             audit_guard_error="", llm_config=None, event_sink=None,
             should_cancel=None, audit_language="en",
             interactive: bool = False,
             allow_test_output: bool = False,
             audit_requested: bool = False,
             resumed_from_run_id: str = "", resumed_from_record_id: str = "") -> str:
    """Ingest (unless already ingested), analyse, then govern every unit.

    Returns:
        "completed": Ingestion succeeded and units processed.
        "failed_operational": File I/O or system operational failure.
        "failed_validation": Pre-extraction structural or frontmatter failure.
        "cancelled": Cooperative cancellation halted before/during processing.
    """
    def _emit(kind, stage, message):
        if event_sink is not None:
            try:
                event_sink({"kind": kind, "run_id": run_id, "stage": stage,
                            "message": message, "timestamp": ""})
            except Exception:
                pass

    import inspect as _inspect
    if ingestion is None:
        _emit("stage_started", "ingestion", str(child))
        try:
            rf_kwargs = {"corpus": corpus_units, "core_units": cores}
            rf_sig = _inspect.signature(pipe.run_file)
            has_var_rf = any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in rf_sig.parameters.values())
            if "run_id" in rf_sig.parameters or has_var_rf:
                rf_kwargs["run_id"] = run_id
            if "should_cancel" in rf_sig.parameters or has_var_rf:
                rf_kwargs["should_cancel"] = should_cancel
            ingestion = pipe.run_file(str(child), **rf_kwargs)
        except Exception as exc:
            cls_outcome = _classify_ingestion_exception(exc)
            from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
            from src.living_authenticity.security.diagnostic import (
                safe_exception_diagnostic,
                sanitize_path,
            )
            import uuid as _uuid
            rec_id = f"aud-{_uuid.uuid4().hex[:12]}"
            safe_stop_reason, safe_errors = safe_exception_diagnostic(
                exc, stage="ingestion", category=cls_outcome
            )
            safe_child = sanitize_path(child)
            if cls_outcome == "failed_validation":
                _emit("stage_failed", "ingestion", f"validation error reading {safe_child}: {safe_stop_reason}")
                audits.append(AuditRecord(
                    record_id=rec_id,
                    run_id=run_id,
                    query_source=str(safe_child),
                    terminal_outcome="validation_failed",
                    validation_result="malformed_structure",
                    stop_reason=safe_stop_reason,
                    errors=safe_errors,
                    operating_mode=operating_mode,
                    resumed_from_run_id=resumed_from_run_id,
                    resumed_from_record_id=resumed_from_record_id,
                ))
                return "failed_validation"
            else:
                _emit("stage_failed", "ingestion", f"operational error reading {safe_child}: {safe_stop_reason}")
                audits.append(AuditRecord(
                    record_id=rec_id,
                    run_id=run_id,
                    query_source=str(safe_child),
                    terminal_outcome="infrastructure_error",
                    validation_result="ingestion_failed",
                    stop_reason=safe_stop_reason,
                    errors=safe_errors,
                    operating_mode=operating_mode,
                    resumed_from_run_id=resumed_from_run_id,
                    resumed_from_record_id=resumed_from_record_id,
                ))
                return "failed_operational"
        _emit("stage_completed", "ingestion", str(child))
    else:
        _emit("stage_started", "ingestion", "text input")
        try:
            ri_kwargs = {"corpus": corpus_units, "core_units": cores}
            ri_sig = _inspect.signature(pipe.run_ingestion)
            has_var_ri = any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in ri_sig.parameters.values())
            if "run_id" in ri_sig.parameters or has_var_ri:
                ri_kwargs["run_id"] = run_id
            if "should_cancel" in ri_sig.parameters or has_var_ri:
                ri_kwargs["should_cancel"] = should_cancel
            ingestion = pipe.run_ingestion(ingestion, **ri_kwargs)
        except Exception as exc:
            cls_outcome = _classify_ingestion_exception(exc)
            from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
            from src.living_authenticity.security.diagnostic import (
                safe_exception_diagnostic,
            )
            import uuid as _uuid
            rec_id = f"aud-{_uuid.uuid4().hex[:12]}"
            safe_stop_reason, safe_errors = safe_exception_diagnostic(
                exc, stage="ingestion", category=cls_outcome
            )
            if cls_outcome == "failed_validation":
                _emit("stage_failed", "ingestion", f"validation error reading text input: {safe_stop_reason}")
                audits.append(AuditRecord(
                    record_id=rec_id,
                    run_id=run_id,
                    query_source="<text-input>",
                    terminal_outcome="validation_failed",
                    validation_result="malformed_structure",
                    stop_reason=safe_stop_reason,
                    errors=safe_errors,
                    operating_mode=operating_mode,
                    resumed_from_run_id=resumed_from_run_id,
                    resumed_from_record_id=resumed_from_record_id,
                ))
                return "failed_validation"
            else:
                _emit("stage_failed", "ingestion", f"operational error reading text input: {safe_stop_reason}")
                audits.append(AuditRecord(
                    record_id=rec_id,
                    run_id=run_id,
                    query_source="<text-input>",
                    terminal_outcome="infrastructure_error",
                    validation_result="ingestion_failed",
                    stop_reason=safe_stop_reason,
                    errors=safe_errors,
                    operating_mode=operating_mode,
                    resumed_from_run_id=resumed_from_run_id,
                    resumed_from_record_id=resumed_from_record_id,
                ))
                return "failed_operational"
        _emit("stage_completed", "ingestion", "text input")

    has_units = bool(hasattr(ingestion, "units") and ingestion.units)
    if not has_units:
        if should_cancel is not None and should_cancel():
            _emit("stopped", "cancellation", "run cancelled during analysis")
            raw_kus = getattr(getattr(ingestion, "ingestion", None), "knowledge_units", None) or getattr(ingestion, "knowledge_units", None)
            if raw_kus or not audits:
                from src.living_authenticity.knowledge.governance.audit.outcome import build_audit_record
                from src.living_authenticity.knowledge.governance.approval.outcome import (
                    ApprovalOutcome,
                    ApprovalRequest,
                )
                import uuid as _uuid
                record_id = f"aud-{_uuid.uuid4().hex[:12]}"
                req = ApprovalRequest(
                    query_unit_id="",
                    query_source=str(child),
                    query_position=0,
                    proposal_action="NONE",
                    title="Cancelled during analysis",
                )
                out = ApprovalOutcome(
                    query_unit_id="",
                    proposal_hash="",
                    approved=False,
                    reason="run cancelled during analysis",
                )
                trace = {
                    "run_id": run_id,
                    "record_id": record_id,
                    "outcome": "cancelled",
                    "validation_result": "cancelled",
                    "stop_reason": "run cancelled during analysis",
                    "operating_mode": operating_mode,
                    "human_review_state": "cancelled",
                }
                audits.append(build_audit_record(req, out, None, None, None, trace=trace))
                if not any(getattr(u, "outcome", "") == "cancelled" for u in entries):
                    from src.living_authenticity.knowledge.integration.outcome import IntegrationUnitEntry
                    entries.append(IntegrationUnitEntry(
                        source_file=str(child),
                        outcome="cancelled",
                        reason="run cancelled during analysis",
                        human_review_state="cancelled",
                        record_id=record_id,
                    ))
            return "cancelled"
        return "completed"

    for unit_result in ingestion.units:
        _handle_unit(
            child, unit_result,
            gate, validator, runner, pinned, guarded,
            schema_version, approval_reader, entries, audits,
            authorized, synthesis, details=details,
            vault_context=vault_context,
            operating_mode=operating_mode, run_id=run_id,
            audit_writer=audit_writer,
            audit_receipts=audit_receipts,
            audit_guard_error=audit_guard_error, llm_config=llm_config,
            event_sink=event_sink, should_cancel=should_cancel,
            audit_language=audit_language,
            interactive=interactive,
            allow_test_output=allow_test_output,
            audit_requested=audit_requested,
            resumed_from_run_id=resumed_from_run_id,
            resumed_from_record_id=resumed_from_record_id,
        )
        if should_cancel is not None and should_cancel():
            _emit("stopped", "cancellation",
                  "run cancelled before next unit")
            break

    if any(getattr(u, "outcome", "") == "cancelled" for u in entries):
        return "cancelled"
    return "completed"


def _derive_audit_telemetry(audits, audit_receipts, has_executed_units):
    has_emergency = any(isinstance(r, dict) and r.get("emergency") for r in audit_receipts)
    has_errors = any(
        isinstance(r, dict) and (r.get("storage_category") == "errors" or r.get("partition") == "errors")
        for r in audit_receipts
    )
    mode = "emergency" if (has_emergency or has_errors) else "normal"

    receipt_map = {}
    for r in audit_receipts:
        if isinstance(r, dict) and r.get("persisted"):
            rec_id = r.get("record_id")
            cat = r.get("storage_category") or r.get("partition") or ""
            if rec_id and cat:
                receipt_map[rec_id] = cat

    from src.living_authenticity.knowledge.governance.audit.persistence import (
        audit_storage_category,
    )

    terminal_categories = set()
    for record in audits:
        rec_id = getattr(record, "record_id", "")
        phase = getattr(record, "audit_phase", "")
        term_outcome = getattr(record, "terminal_outcome", "")
        # Distinguish pre-execution checkpoints from terminal outcomes
        if phase == "pre_execution" or term_outcome == "approved_pending_execution":
            continue
        cat = receipt_map.get(rec_id)
        if not cat:
            cat = audit_storage_category(record)
        if cat:
            terminal_categories.add(cat)

    if not terminal_categories:
        for cat in receipt_map.values():
            if cat:
                terminal_categories.add(cat)

    sorted_partitions = sorted(terminal_categories)
    if len(sorted_partitions) > 1:
        partition = "mixed"
    elif len(sorted_partitions) == 1:
        partition = sorted_partitions[0]
    else:
        partition = "passed" if has_executed_units else ("errors" if has_errors else "failed")
        sorted_partitions = [partition]

    return mode, partition, sorted_partitions


def run_bounded_integration(source_root, staging_root, pipeline=None,
                            approval_gate=None, corpus=(), core_units=(),
                            max_files: int = 1, guarded_roots=None,
                            executor=None, approval_reader=None,
                            schema_version: str = "",
                            authorized_staging_root=None,
                            synthesis=None, llm_config=None,
                            source_files=None,
                            ingestion=None, vault_context=None,
                            operating_mode: str = "personal_vault",
                            audit_root: str = "",
                            authorized_audit_root=None,
                            authorized_audit_passed="",
                            authorized_audit_failed="",
                            run_id: str = "",
                            should_cancel=None,
                            event_sink=None,
                            staging_language: str = "en",
                            audit_language: str = "en",
                            interactive: bool = False,
                            allow_test_output: bool | None = None,
                            audit_writer=None,
                            audit_requested: bool | None = None,
                            recoverable_errors: int = 0,
                            resumed_from_run_id: str = "", resumed_from_record_id: str = "") -> IntegrationRunReport:
    """Run one bounded local-integration pass; return an in-memory report.

    Three explicit input shapes, one governance path:

    * default -- scan ``source_root`` for up to ``max_files`` inputs;
    * ``source_files`` -- process exactly the listed files, still
      validated inside ``source_root`` (no discovery);
    * ``ingestion`` -- one already-ingested in-memory result (the
      ``--text`` path), which creates no file anywhere.

    ``vault_context`` is optional bounded excerpts forwarded to the
    mandatory synthesis stage as informational context only.
    """
    from src.living_authenticity.knowledge.governance.execution.executor import (
        ControlledExecutor,
    )
    from src.living_authenticity.knowledge.governance.revalidation.revalidator import (
        Revalidator,
    )
    from src.living_authenticity.knowledge.orchestration.orchestrator import (
        EvidenceFirstPipeline,
    )
    from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
        ExplicitApprovalGate,
    )
    from .guard import (check_audit_eligible, resolve_authorized_audit_root,
                        resolve_authorized_audit_passed,
                        resolve_authorized_audit_failed)

    guarded = tuple(guarded_roots) if guarded_roots is not None else resolve_guarded_roots()
    authorized = (
        authorized_staging_root if authorized_staging_root is not None
        else resolve_authorized_staging_root()
    )
    authorized_audit = (
        authorized_audit_root if authorized_audit_root is not None
        else resolve_authorized_audit_root()
    )
    authorized_passed = (
        authorized_audit_passed if authorized_audit_passed is not None
        else resolve_authorized_audit_passed()
    )
    authorized_failed = (
        authorized_audit_failed if authorized_audit_failed is not None
        else resolve_authorized_audit_failed()
    )
    is_test = bool(allow_test_output)
    audit_guard_error = ""
    if audit_writer is not None:
        if audit_requested is None:
            audit_requested = True
    elif isinstance(audit_root, str) and audit_root.strip():
        if audit_requested is None:
            audit_requested = True
        audit_ok, audit_reason, _audit_check = check_audit_eligible(
            audit_root, guarded, authorized_audit,
            authorized_passed, authorized_failed,
            allow_test_output=is_test)
        if not audit_ok:
            return IntegrationRunReport(
                source_root=source_root if isinstance(source_root, str) else "",
                staging_root=staging_root if isinstance(staging_root, str) else "",
                max_files=max_files, accepted=False,
                rejection_reason=audit_reason, rejection_check="audit",
                run_outcome="audit_integrity_failure",
            )
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
        )
        try:
            audit_writer = AuditWriter(audit_root, authorized_passed, authorized_failed)
        except Exception as exc:
            from src.living_authenticity.security.diagnostic import sanitize_diagnostic_text
            audit_guard_error = sanitize_diagnostic_text(str(exc))
            audit_writer = None
    else:
        if audit_requested is None:
            audit_requested = False
    active_run_id = run_id if isinstance(run_id, str) and run_id else ""
    if not active_run_id:
        import uuid
        active_run_id = "run-" + uuid.uuid4().hex[:12]
    # Optional observational hook (default off). Sink errors are swallowed;
    # no pipeline result, decision, or authorization ever depends on it.
    if event_sink is not None and not callable(event_sink):
        raise TypeError("event_sink must be callable or None")
    from datetime import datetime, timezone

    def emit(payload) -> None:
        """Stamp one observational event; never raises; no-op when off."""
        if event_sink is None:
            return
        try:
            if not isinstance(payload, dict):
                return
            evt: dict[str, Any] = {
                "kind": str(payload.get("kind", "")),
                "run_id": str(payload.get("run_id") or active_run_id),
                "stage": str(payload.get("stage", "")),
                "message": str(payload.get("message", "")),
                "timestamp": str(payload.get("timestamp") or
                                 datetime.now(timezone.utc).isoformat()),
            }
            for k, v in payload.items():
                if k not in evt:
                    evt[k] = v
            event_sink(evt)
        except Exception:
            pass

    emit({"kind": "run_started", "stage": "",
          "message": "bounded integration run started"})
    if staging_language != "en" or audit_language != "en":
        emit({"kind": "status", "stage": "localization",
              "message": f"requested languages: staging={staging_language} audit={audit_language}; "
                         "content language follows source knowledge"})
    mode_value = operating_mode if isinstance(operating_mode, str) else ""
    audit_receipts: list = []
    pipe = pipeline if pipeline is not None else EvidenceFirstPipeline(stage_observer=emit)
    if pipe is not None:
        if getattr(pipe, "_stage_observer", None) is None:
            try:
                setattr(pipe, "_stage_observer", emit)
            except Exception:
                pass
        retriever = getattr(pipe, "retriever", None)
        if retriever is not None and hasattr(retriever, "set_observer") and callable(retriever.set_observer):
            try:
                retriever.set_observer(emit)
            except Exception:
                pass
    gate = approval_gate if approval_gate is not None else ExplicitApprovalGate()
    runner = executor if executor is not None else ControlledExecutor()
    validator = Revalidator()
    if synthesis is None:
        from src.living_authenticity.llm import build_provider
        from src.living_authenticity.llm import build_synthesis

        synthesis = build_synthesis(build_provider(llm_config),
                                    config=llm_config)

    # Initialize all variables used by both ingestion and default paths
    pinned = staging_root
    corpus_units = list(corpus) if corpus is not None else []
    cores = list(core_units) if core_units is not None else []
    entries: list = []
    audits: list = []
    details: list = []
    processed = 0
    skipped = 0
    files_failed_operational = 0
    files_failed_validation = 0
    source_label: str = ""

    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
        return IntegrationRunReport(
            source_root=source_root if isinstance(source_root, str) else "",
            staging_root=staging_root if isinstance(staging_root, str) else "",
            max_files=1, accepted=False,
            rejection_reason="max_files must be a positive integer",
            rejection_check="run_scope",
            run_outcome="accounting_integrity_failure",
        )
    eligible, reason, check = check_staging_eligible(
        staging_root, guarded, authorized,
        allow_test_output=is_test)
    if not eligible:
        return IntegrationRunReport(
            source_root=source_root if isinstance(source_root, str) else "",
            staging_root=staging_root if isinstance(staging_root, str) else "",
            max_files=max_files, accepted=False,
            rejection_reason=reason, rejection_check=check,
            run_outcome="accounting_integrity_failure",
        )

    if ingestion is None:
        if not isinstance(source_root, str) or not source_root.strip():
            return IntegrationRunReport(
                source_root="", staging_root=staging_root,
                max_files=max_files, accepted=False,
                rejection_reason="source root missing",
                rejection_check="source",
                run_outcome="accounting_integrity_failure",
            )
        source_path = Path(source_root)
        if _is_link_or_junction(source_path) or not source_path.is_dir():
            return IntegrationRunReport(
                source_root=source_root, staging_root=staging_root,
                max_files=max_files, accepted=False,
                rejection_reason="source root missing or not a directory",
                rejection_check="source",
                run_outcome="accounting_integrity_failure",
            )
        try:
            source_boundary = PathBoundary(source_root)
        except Exception:
            return IntegrationRunReport(
                source_root=source_root, staging_root=staging_root,
                max_files=max_files, accepted=False,
                rejection_reason="source root invalid",
                rejection_check="source",
                run_outcome="accounting_integrity_failure",
            )
    else:
        source_path = None
        source_boundary = None

    # Execute Candidate Discovery and Processing
    if ingestion is not None:
        files_seen = 1
        source_label = getattr(ingestion, "file_path", "") or "<text-input>"
        res = _run_one(source_label, ingestion, pipe, corpus_units, cores,
                       gate, validator, runner, pinned, guarded,
                       schema_version, approval_reader, entries, audits,
                       authorized, synthesis, details,
                       vault_context=vault_context,
                       operating_mode=mode_value, run_id=active_run_id,
                       audit_writer=audit_writer,
                       audit_receipts=audit_receipts,
                       audit_guard_error=audit_guard_error,
                       llm_config=llm_config, event_sink=emit,
                       should_cancel=should_cancel,
                       audit_language=audit_language,
                       interactive=interactive,
                       allow_test_output=is_test,
                       audit_requested=audit_requested,
                       resumed_from_run_id=resumed_from_run_id,
                       resumed_from_record_id=resumed_from_record_id)
        if res == "completed":
            processed = 1
        elif res == "failed_operational":
            files_failed_operational = 1
        elif res == "failed_validation":
            files_failed_validation = 1
        else:
            skipped = 1
    else:
        # Default: scan source_root or process explicit source_files
        if source_files is not None:
            raw_candidates = [Path(item) for item in source_files]
        else:
            try:
                raw_candidates = sorted(source_path.iterdir(), key=lambda p: p.name)  # type: ignore[union-attr]
            except OSError:
                return IntegrationRunReport(
                    source_root=source_root, staging_root=staging_root,
                    max_files=max_files, accepted=False,
                    rejection_reason="source root unreadable",
                    rejection_check="source",
                    run_outcome="operational_failure",
                )

        files_seen = len(raw_candidates)
        for child in raw_candidates:
            # If already cancelled, remaining files are skipped
            if entries and any(getattr(u, "outcome", "") == "cancelled" for u in entries):
                skipped += 1
                continue
            # max_files check: candidates beyond max_files are categorized as files_skipped
            if processed + files_failed_operational + files_failed_validation >= max_files:
                skipped += 1
                continue
            # Disallowed suffix
            if child.suffix.lower() not in _ALLOWED_SOURCE_SUFFIXES:
                skipped += 1
                continue
            # Symlinks, junctions, or non-regular files
            if _is_link_or_junction(child) or not child.is_file():
                skipped += 1
                continue
            # Boundary security validation
            try:
                source_boundary.validate(str(child))  # type: ignore[union-attr]
            except Exception:
                skipped += 1
                continue

            res = _run_one(child, None, pipe, corpus_units, cores, gate, validator,
                           runner, pinned, guarded, schema_version, approval_reader,
                           entries, audits, authorized, synthesis, details,
                           vault_context=vault_context,
                           operating_mode=mode_value, run_id=active_run_id,
                           audit_writer=audit_writer,
                           audit_receipts=audit_receipts,
                           audit_guard_error=audit_guard_error,
                           llm_config=llm_config, event_sink=emit,
                           should_cancel=should_cancel,
                           audit_language=audit_language,
                           interactive=interactive,
                           allow_test_output=is_test,
                           audit_requested=audit_requested,
                       resumed_from_run_id=resumed_from_run_id,
                       resumed_from_record_id=resumed_from_record_id)
            if res == "completed":
                processed += 1
            elif res == "failed_operational":
                files_failed_operational += 1
            elif res == "failed_validation":
                files_failed_validation += 1
            elif res == "cancelled":
                skipped += 1
            else:
                skipped += 1

    files_failed = files_failed_operational + files_failed_validation
    audits_ok = _persist_audits(audit_writer, audits, audit_receipts, staging_root=pinned)

    # Unit accounting dimensions
    def _is_executed(out: str) -> bool:
        return out == "executed"

    def _is_held(out: str) -> bool:
        return out in ("held", "llm_safe_stop", "timed_out") or out.startswith("held")

    def _is_rejected(out: str) -> bool:
        return (
            out in (
                "rejected", "rejected_staging_guard", "rejected_existing_artifact",
                "revalidation_failed", "validation_rejected"
            )
            or out.startswith("rejected")
            or out.startswith("revalidation_failed")
        )

    def _is_cancelled(out: str) -> bool:
        return out == "cancelled" or out.startswith("cancelled")

    def _is_failed_operational(out: str) -> bool:
        if out in ("operational_error", "audit_persistence_failed", "execution_failed", "infrastructure_error"):
            return True
        return not (_is_executed(out) or _is_held(out) or _is_rejected(out) or _is_cancelled(out))

    units_attempted = len(entries)
    units_executed = sum(1 for u in entries if _is_executed(getattr(u, "outcome", "")))
    units_held = sum(1 for u in entries if _is_held(getattr(u, "outcome", "")))
    units_rejected = sum(1 for u in entries if _is_rejected(getattr(u, "outcome", "")))
    units_cancelled = sum(1 for u in entries if _is_cancelled(getattr(u, "outcome", "")))
    units_failed_operational = sum(1 for u in entries if _is_failed_operational(getattr(u, "outcome", "")))

    # Invariants verification
    files_accounted = processed + files_failed_operational + files_failed_validation + skipped
    file_accounting_holds = (files_seen == files_accounted)

    units_partition_sum = (
        units_executed + units_held + units_rejected + units_failed_operational + units_cancelled
    )
    unit_accounting_holds = (units_attempted == units_partition_sum)

    # Execution receipt bijection verification
    from src.living_authenticity.knowledge.governance.audit.persistence import (
        verify_execution_receipt_bijection,
    )
    is_bij_valid, bij_fail_type, bij_fail_reason = verify_execution_receipt_bijection(
        entries, audit_receipts, audit_writer, active_run_id
    )

    has_audit_failures = any(
        getattr(u, "outcome", "") == "audit_persistence_failed"
        for u in entries
    )
    has_executed_units = (units_executed > 0)
    has_execution_attempt = any(
        getattr(u, "outcome", "") in ("executed", "audit_persistence_failed")
        or (getattr(u, "approval_approved", False) and getattr(u, "revalidation_valid", False) and getattr(u, "execution_attempted", False))
        for u in entries
    )
    requires_durable_audit = bool(audit_requested or has_executed_units or has_execution_attempt)

    # Detect Audit and Accounting Integrity Failures
    audit_int_fail = False
    audit_int_reason = ""
    if requires_durable_audit:
        if bij_fail_type == "audit_integrity_failure":
            audit_int_fail = True
            audit_int_reason = bij_fail_reason
        elif not audits_ok:
            audit_int_fail = True
            audit_int_reason = "audit persistence write failed"
        elif has_audit_failures:
            audit_int_fail = True
            audit_int_reason = "audit persistence failed during execution"
        elif audit_writer is None:
            audit_int_fail = True
            audit_int_reason = "audit persistence unconfigured: no audit writer configured"
    else:
        if has_audit_failures:
            audit_int_fail = True
            audit_int_reason = "audit persistence failed during execution"
        elif bij_fail_type == "audit_integrity_failure" and has_executed_units:
            audit_int_fail = True
            audit_int_reason = bij_fail_reason

    acct_int_fail = False
    acct_int_reason = ""
    if not file_accounting_holds:
        acct_int_fail = True
        acct_int_reason = f"file accounting invariant mismatch: files_seen={files_seen} != {files_accounted}"
    elif not unit_accounting_holds:
        acct_int_fail = True
        acct_int_reason = f"unit accounting invariant mismatch: units_attempted={units_attempted} != {units_partition_sum}"
    elif bij_fail_type == "accounting_integrity_failure":
        acct_int_fail = True
        acct_int_reason = bij_fail_reason

    # Precedence Ladder Resolution (Ranks 1 to 17)
    if audit_int_fail and acct_int_fail:
        # Dual diagnosis: physical storage integrity failure takes precedence, both recorded
        run_outcome = "audit_integrity_failure"
        rejection_check = "audit_and_accounting_integrity"
        rejection_reason = f"dual integrity failure: [audit] {audit_int_reason}; [accounting] {acct_int_reason}"
        accepted = False
    elif audit_int_fail:
        run_outcome = "audit_integrity_failure"
        rejection_check = "audit_durability"
        unpersisted = [r for r in audit_receipts if isinstance(r, dict) and not r.get("persisted")]
        artifacts_failed = [r for r in unpersisted if r.get("artifact_path") or r.get("artifact_reference")]
        if artifacts_failed:
            quarantined = [r for r in artifacts_failed if r.get("quarantined")]
            partial = [r for r in artifacts_failed if r.get("quarantine_status") == "partial_cleanup_failed"]
            if len(quarantined) == len(artifacts_failed):
                rejection_reason = "audit persistence failure: artifact quarantined"
            elif partial or quarantined:
                rejection_reason = "audit persistence failure: partial quarantine"
            else:
                rejection_reason = "audit persistence failure: quarantine failed"
        elif audit_writer is None:
            rejection_reason = "audit persistence failed: no audit writer configured"
        elif has_audit_failures:
            rejection_reason = "audit persistence failed before execution"
        else:
            rejection_reason = audit_int_reason or "audit persistence failed"
        accepted = False
    elif acct_int_fail:
        run_outcome = "accounting_integrity_failure"
        rejection_check = "accounting_integrity"
        rejection_reason = acct_int_reason or "accounting integrity invariant violated"
        accepted = False
    else:
        C = bool(should_cancel and should_cancel()) or (units_cancelled > 0)
        E = units_executed
        A = units_attempted
        O = bool(files_failed_operational > 0 or units_failed_operational > 0)
        V = bool(files_failed_validation > 0)
        H = bool(units_held > 0 or units_rejected > 0)

        if C:
            if E >= 1 and (O or V):
                run_outcome = "cancelled_partial_with_errors"
            elif E >= 1 and not (O or V):
                run_outcome = "cancelled_partial"
            elif E == 0 and (O or V):
                run_outcome = "cancelled_with_errors"
            elif E == 0 and not (O or V) and H:
                run_outcome = "cancelled_with_holds"
            else:
                run_outcome = "cancelled_clean"
            accepted = False
            rejection_check = "cancellation"
            rejection_reason = "run cancelled"
        else:
            if E == 0:
                if O and V:
                    run_outcome = "operational_and_validation_failure"
                    rejection_check = "operational_and_validation"
                    rejection_reason = "operational and validation failures occurred"
                    accepted = False
                elif O and not V and not H:
                    run_outcome = "operational_failure"
                    rejection_check = "operational"
                    rejection_reason = "operational failure occurred"
                    accepted = False
                elif not O and V and not H:
                    run_outcome = "validation_failure"
                    rejection_check = "validation"
                    rejection_reason = "file validation failure"
                    accepted = False
                elif not O and V and H:
                    run_outcome = "validation_held"
                    rejection_check = "validation_held"
                    rejection_reason = "validation failure with governance holds"
                    accepted = False
                elif O and not V and H:
                    run_outcome = "mixed_failure"
                    rejection_check = "mixed_failure"
                    rejection_reason = "mixed operational failure and governance holds"
                    accepted = False
                elif not O and not V and H:
                    run_outcome = "governance_held"
                    rejection_check = ""
                    rejection_reason = ""
                    accepted = True
                else:  # not O and not V and not H
                    if processed > 0 and A == 0:
                        run_outcome = "no_units_extracted"
                        rejection_check = ""
                        rejection_reason = ""
                        accepted = True
                    else:
                        run_outcome = "zero_work_completed"
                        rejection_check = ""
                        rejection_reason = ""
                        accepted = True
            else:  # E >= 1
                if not O and not V and not H and (E == A) and (A >= 1) and is_bij_valid:
                    run_outcome = "full_success"
                    accepted = True
                    rejection_check = ""
                    rejection_reason = ""
                else:
                    run_outcome = "partial_success"
                    accepted = True
                    rejection_check = ""
                    rejection_reason = ""

    # Telemetry and event emissions
    effective_ok = bool(audits_ok and (audit_writer is not None or not requires_durable_audit) and not audit_int_fail)
    if effective_ok and audit_writer is not None:
        mode, partition, sorted_partitions = _derive_audit_telemetry(audits, audit_receipts, has_executed_units)
        if partition == "mixed":
            msg = f"audit records persisted ({mode} mode across mixed partitions: {'+'.join(sorted_partitions)})"
        else:
            msg = f"audit records persisted ({mode} mode in {partition} partition)"
        emit({
            "kind": "audit_persisted",
            "run_id": active_run_id,
            "stage": "audit",
            "message": msg,
            "audit_mode": mode,
            "audit_partition": partition,
            "audit_partitions": sorted_partitions,
        })
    elif audit_int_fail or not effective_ok:
        fail_mode = "unconfigured" if audit_writer is None else "failed"
        fail_msg = "audit persistence unconfigured: no audit writer configured" if audit_writer is None else "audit persistence failed"
        emit({
            "kind": "audit_failed",
            "run_id": active_run_id,
            "stage": "audit",
            "message": fail_msg,
            "audit_mode": fail_mode,
            "audit_partition": "errors",
        })

    source_desc = str(source_root if source_root is not None else (source_label if ingestion is not None else ""))
    return IntegrationRunReport(
        source_root=source_desc,
        staging_root=pinned,
        max_files=max_files,
        files_seen=files_seen,
        files_processed=processed,
        files_skipped=skipped,
        accepted=accepted,
        rejection_reason=rejection_reason,
        rejection_check=rejection_check,
        units=tuple(entries),
        audits=tuple(audits),
        details=tuple(details),
        audit_receipts=tuple(audit_receipts),
        staging_language=staging_language,
        audit_language=audit_language,
        files_failed=files_failed,
        files_failed_operational=files_failed_operational,
        files_failed_validation=files_failed_validation,
        units_failed_operational=units_failed_operational,
        recoverable_errors=recoverable_errors,
        run_outcome=run_outcome,
        resumed_from_run_id=resumed_from_run_id,
        resumed_from_record_id=resumed_from_record_id,
    )
