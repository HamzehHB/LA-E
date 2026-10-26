"""Top-level bounded local-integration run entry point (composition only)."""
from pathlib import Path

from src.living_authenticity.security.path_boundary import PathBoundary

from .guard import (
    _ALLOWED_SOURCE_SUFFIXES,
    check_staging_eligible,
    resolve_authorized_staging_root,
    resolve_guarded_roots,
)
from .outcome import IntegrationRunReport
from .unit_loop import _handle_unit


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
                q_res = quarantine_artifact(staging_root, art_path)
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
             interactive: bool = False):
    """Ingest (unless already ingested), analyse, then govern every unit."""
    def _emit(kind, stage, message):
        if event_sink is not None:
            try:
                event_sink({"kind": kind, "run_id": run_id, "stage": stage,
                            "message": message, "timestamp": ""})
            except Exception:
                pass

    if ingestion is None:
        _emit("stage_started", "ingestion", str(child))
        try:
            ingestion = pipe.run_file(
                str(child), corpus=corpus_units, core_units=cores)
        except Exception:
            return False
        _emit("stage_completed", "ingestion", str(child))
    else:
        _emit("stage_started", "ingestion", "text input")
        ingestion = pipe.run_ingestion(
            ingestion, corpus=corpus_units, core_units=cores)
        _emit("stage_completed", "ingestion", "text input")
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
        )
        # Cooperative cancellation boundary between units: once a run is
        # cancelled, no further unit is analysed or governed. The current
        # unit has already passed through its own pre-execution boundary,
        # so a cancelled run records its 'cancelled' outcome truthfully.
        if should_cancel is not None and should_cancel():
            _emit("stopped", "cancellation",
                  "run cancelled before next unit")
            break
    return True


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
                            interactive: bool = False) -> IntegrationRunReport:
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
    audit_writer = None
    audit_guard_error = ""
    audit_requested = False
    if isinstance(audit_root, str) and audit_root.strip():
        audit_requested = True
        audit_ok, audit_reason, _audit_check = check_audit_eligible(
            audit_root, guarded, authorized_audit,
            authorized_passed, authorized_failed)
        if not audit_ok:
            return IntegrationRunReport(
                source_root=source_root if isinstance(source_root, str) else "",
                staging_root=staging_root if isinstance(staging_root, str) else "",
                max_files=max_files, accepted=False,
                rejection_reason=audit_reason, rejection_check="audit",
            )
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
        )
        try:
            audit_writer = AuditWriter(audit_root, authorized_passed, authorized_failed)
        except Exception as exc:
            audit_guard_error = str(exc)
            audit_writer = None
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
            evt = {
                "kind": str(payload.get("kind", "")),
                "run_id": str(payload.get("run_id") or active_run_id),
                "stage": str(payload.get("stage", "")),
                "message": str(payload.get("message", "")),
                "timestamp": str(payload.get("timestamp") or
                                 datetime.now(timezone.utc).isoformat()),
            }
            if "operation" in payload:
                evt["operation"] = str(payload["operation"])
            if "phase" in payload:
                evt["phase"] = str(payload["phase"])
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

    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
        return IntegrationRunReport(
            source_root=source_root if isinstance(source_root, str) else "",
            staging_root=staging_root if isinstance(staging_root, str) else "",
            max_files=1, accepted=False,
            rejection_reason="max_files must be a positive integer",
            rejection_check="run_scope",
        )
    eligible, reason, check = check_staging_eligible(
        staging_root, guarded, authorized)
    if not eligible:
        return IntegrationRunReport(
            source_root=source_root if isinstance(source_root, str) else "",
            staging_root=staging_root if isinstance(staging_root, str) else "",
            max_files=max_files, accepted=False,
            rejection_reason=reason, rejection_check=check,
        )
    if ingestion is None:
        if not isinstance(source_root, str) or not source_root.strip():
            return IntegrationRunReport(
                source_root="", staging_root=staging_root,
                max_files=max_files, accepted=False,
                rejection_reason="source root missing",
                rejection_check="source",
            )
        source_path = Path(source_root)
        if source_path.is_symlink() or not source_path.is_dir():
            return IntegrationRunReport(
                source_root=source_root, staging_root=staging_root,
                max_files=max_files, accepted=False,
                rejection_reason="source root missing or not a directory",
                rejection_check="source",
            )
        try:
            source_boundary = PathBoundary(source_root)
        except Exception:
            return IntegrationRunReport(
                source_root=source_root, staging_root=staging_root,
                max_files=max_files, accepted=False,
                rejection_reason="source root invalid",
                rejection_check="source",
            )
    else:
        source_path = None
        source_boundary = None
    if ingestion is not None:
        source_label = getattr(ingestion, "file_path", "") or "<text-input>"
        if _run_one(source_label, ingestion, pipe, corpus_units, cores,
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
                    interactive=interactive):
            processed = 1
        audits_ok = _persist_audits(audit_writer, audits, audit_receipts, staging_root=pinned)
        # Only fail-closed when audit persistence was explicitly requested.
        # When audit_root is not provided, audit_writer is None by design,
        # not by failure — the caller never asked for audit persistence.
        effective_ok = audits_ok or not audit_requested
        accepted = bool(effective_ok)
        if not effective_ok:
            rejection_check = "audit_durability"
            unpersisted = [r for r in audit_receipts if isinstance(r, dict) and not r.get("persisted")]
            artifacts_failed = [r for r in unpersisted if r.get("artifact_path") or r.get("artifact_reference")]
            quarantined = [r for r in artifacts_failed if r.get("quarantined")]
            partial = [r for r in artifacts_failed if r.get("quarantine_status") == "partial_cleanup_failed"]
            if artifacts_failed:
                if len(quarantined) == len(artifacts_failed):
                    rejection_reason = "audit persistence failure: artifact quarantined"
                elif partial or quarantined:
                    rejection_reason = "audit persistence failure: partial quarantine"
                else:
                    rejection_reason = "audit persistence failure: quarantine failed"
            else:
                rejection_reason = "audit persistence failed"
        else:
            rejection_reason = ""
            rejection_check = ""
        return IntegrationRunReport(
            source_root=str(source_label), staging_root=pinned,
            max_files=max_files, files_seen=1,
            files_processed=processed, files_skipped=0,
            accepted=accepted, rejection_reason=rejection_reason,
            rejection_check=rejection_check,
            units=tuple(entries), audits=tuple(audits),
            details=tuple(details), audit_receipts=tuple(audit_receipts),
            staging_language=staging_language,
            audit_language=audit_language,
        )

    # Default: scan source_root

    if source_files is not None:
        candidates = [Path(item) for item in source_files]
    else:
        try:
            children = sorted(source_path.iterdir(), key=lambda p: p.name)  # type: ignore[union-attr]
        except OSError:
            return IntegrationRunReport(
                source_root=source_root, staging_root=staging_root,
                max_files=max_files, accepted=False,
                rejection_reason="source root unreadable",
                rejection_check="source",
            )
        candidates = [
            p for p in children
            if p.suffix.lower() in _ALLOWED_SOURCE_SUFFIXES
        ]
    for child in candidates[:max_files]:
        if child.is_symlink() or not child.is_file():
            skipped += 1
            continue
        try:
            source_boundary.validate(str(child))  # type: ignore[union-attr]
        except Exception:
            skipped += 1
            continue
        if _run_one(child, None, pipe, corpus_units, cores, gate, validator,
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
                    interactive=interactive):
            processed += 1
        else:
            skipped += 1
    skipped += max(0, len(candidates) - min(len(candidates), max_files))
    audits_ok = _persist_audits(audit_writer, audits, audit_receipts, staging_root=pinned)
    # Only fail-closed when audit persistence was explicitly requested.
    effective_ok = audits_ok or not audit_requested
    accepted = bool(effective_ok)
    if not effective_ok:
        rejection_check = "audit_durability"
        if audit_writer is None:
            rejection_reason = "audit persistence failed: no audit writer configured"
        else:
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
            else:
                rejection_reason = "audit persistence failed"
    else:
        rejection_reason = ""
        rejection_check = ""
    return IntegrationRunReport(
        source_root=source_root, staging_root=pinned,
        max_files=max_files, files_seen=len(candidates),
        files_processed=processed, files_skipped=skipped,
        accepted=accepted, rejection_reason=rejection_reason,
        rejection_check=rejection_check,
        units=tuple(entries), audits=tuple(audits),
        details=tuple(details), audit_receipts=tuple(audit_receipts),
        staging_language=staging_language,
        audit_language=audit_language,
    )
