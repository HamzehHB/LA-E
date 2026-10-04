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


def _run_one(child, ingestion, pipe, corpus_units, cores, gate, validator,
             runner, pinned, guarded, schema_version, approval_reader,
             entries, audits, authorized, synthesis, details,
             vault_context=None):
    """Ingest (unless already ingested), analyse, then govern every unit."""
    if ingestion is None:
        try:
            ingestion = pipe.run_file(
                str(child), corpus=corpus_units, core_units=cores)
        except Exception:
            return False
    else:
        ingestion = pipe.run_ingestion(
            ingestion, corpus=corpus_units, core_units=cores)
    for unit_result in ingestion.units:
        _handle_unit(
            child, unit_result,
            gate, validator, runner, pinned, guarded,
            schema_version, approval_reader, entries, audits,
            authorized, synthesis, details=details,
            vault_context=vault_context,
        )
    return True


def run_bounded_integration(source_root, staging_root, pipeline=None,
                            approval_gate=None, corpus=(), core_units=(),
                            max_files: int = 1, guarded_roots=None,
                            executor=None, approval_reader=None,
                            schema_version: str = "",
                            authorized_staging_root=None,
                            synthesis=None, llm_config=None,
                            source_files=None,
                            ingestion=None, vault_context=None) -> IntegrationRunReport:
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

    guarded = tuple(guarded_roots) if guarded_roots is not None else resolve_guarded_roots()
    authorized = (
        authorized_staging_root if authorized_staging_root is not None
        else resolve_authorized_staging_root()
    )
    pipe = pipeline if pipeline is not None else EvidenceFirstPipeline()
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
                    vault_context=vault_context):
            processed = 1
        return IntegrationRunReport(
            source_root=str(source_label), staging_root=pinned,
            max_files=max_files, files_seen=1,
            files_processed=processed, files_skipped=0,
            accepted=True, units=tuple(entries), audits=tuple(audits),
            details=tuple(details),
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
                    vault_context=vault_context):
            processed += 1
        else:
            skipped += 1
    skipped += max(0, len(candidates) - min(len(candidates), max_files))
    return IntegrationRunReport(
        source_root=source_root, staging_root=pinned,
        max_files=max_files, files_seen=len(candidates),
        files_processed=processed, files_skipped=skipped,
        accepted=True, units=tuple(entries), audits=tuple(audits),
        details=tuple(details),
    )
