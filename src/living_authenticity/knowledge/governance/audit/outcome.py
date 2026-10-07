"""Audit / traceability record (observational only).

Builds a frozen, deterministic, in-memory record linking one proposal
identity to its human approval, fresh revalidation result, and
controlled-execution outcome. The record never authorizes, executes,
or mutates knowledge: it only copies fields that already exist on the
supplied contracts.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuditRecord:
    """Inspectable trace of one proposal lifecycle attempt."""

    record_id: str = ""
    run_id: str = ""
    operating_mode: str = ""
    input_hash: str = ""
    proposal_hash: str = ""
    query_unit_id: str = ""
    query_source: str = ""
    query_position: int = 0
    proposal_action: str = ""
    approval_approved: bool = False
    approval_proposal_hash: str = ""
    approval_query_unit_id: str = ""
    approval_timestamp: str = ""
    approval_strategy: str = ""
    revalidation_performed: bool = False
    revalidation_valid: bool = False
    revalidation_failed_check: str = ""
    revalidation_reason: str = ""
    execution_attempted: bool = False
    execution_permitted: bool = False
    execution_executed: bool = False
    execution_proposal_hash: str = ""
    execution_reason: str = ""
    execution_failed_check: str = ""
    artifact_reference: str = ""
    artifact_sha256: str = ""
    destination: str = ""
    provenance: tuple = field(default_factory=tuple)
    retrieval_strategy: str = ""
    retrieval_candidates: tuple = field(default_factory=tuple)
    retrieval_note: str = ""
    evidence_hash: str = ""
    llm_provider: str = ""
    llm_model: str = ""
    llm_config_hash: str = ""
    prompt_sha256: str = ""
    synthesis_result_id: str = ""
    validation_result: str = ""
    stop_reason: str = ""
    human_review_state: str = ""
    # Temporal + lifecycle contract (all aware, local system clock):
    # ``timestamp`` is record creation time; ``audit_phase`` is one of
    # "terminal" (single final record), "pre_execution" (checkpoint
    # persisted before ControlledExecutor), "post_execution" (record
    # describing the executed attempt); ``execution_timestamp`` and
    # ``artifact_created_at`` are set only when execution actually wrote
    # an artifact; ``artifact_path`` is the absolute staging path of the
    # created artifact (``artifact_reference`` keeps the bare filename).
    timestamp: str = ""
    audit_phase: str = "terminal"
    execution_timestamp: str = ""
    artifact_created_at: str = ""
    artifact_path: str = ""
    errors: tuple = field(default_factory=tuple)
    warnings: tuple = field(default_factory=tuple)
    core_relevance: str = ""
    core_basis: str = ""
    is_authoritative: bool = False
    requires_human_review: bool = True

    def __post_init__(self) -> None:
        from .clock import local_now_iso

        object.__setattr__(self, "approval_approved", bool(self.approval_approved))
        object.__setattr__(self, "revalidation_performed", bool(self.revalidation_performed))
        object.__setattr__(self, "revalidation_valid", bool(self.revalidation_valid))
        object.__setattr__(self, "execution_attempted", bool(self.execution_attempted))
        object.__setattr__(self, "execution_permitted", bool(self.execution_permitted))
        object.__setattr__(self, "execution_executed", bool(self.execution_executed))
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "retrieval_candidates",
                           tuple(self.retrieval_candidates))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        stamp = self.timestamp if isinstance(self.timestamp, str) else ""
        object.__setattr__(self, "timestamp", stamp or local_now_iso())
        phase = self.audit_phase if isinstance(self.audit_phase, str) else ""
        object.__setattr__(self, "audit_phase",
                           phase if phase in (
                               "terminal", "pre_execution",
                               "post_execution") else "terminal")
        object.__setattr__(self, "is_authoritative", False)
        object.__setattr__(self, "requires_human_review", True)

    @property
    def identity_consistent(self) -> bool:
        """True when all present stage identities agree with the proposal.

        Missing stages (``None`` at build time) are not inconsistencies:
        only identities actually recorded are compared. A ``False`` value
        is itself the traceable inconsistency signal; it never blocks,
        repairs, or authorizes anything.
        """
        if self.approval_proposal_hash and (
            self.approval_proposal_hash != self.proposal_hash
        ):
            return False
        if self.approval_query_unit_id and (
            self.approval_query_unit_id != self.query_unit_id
        ):
            return False
        if self.execution_proposal_hash and (
            self.execution_proposal_hash != self.proposal_hash
        ):
            return False
        return True

    @property
    def outcome(self) -> str:
        """Human-readable lifecycle state derived from stored flags."""
        if self.execution_executed:
            return "executed"
        if self.execution_attempted:
            return "rejected"
        if self.revalidation_performed and not self.revalidation_valid:
            return "revalidation_failed"
        if not self.approval_approved:
            return "not_approved"
        return "not_executed"


def build_audit_record(request, outcome, revalidation=None, execution=None,
                       proposal=None, trace=None) -> AuditRecord:
    """Build one AuditRecord from existing lifecycle contracts.

    ``request`` (ApprovalRequest) and ``outcome`` (ApprovalOutcome) are
    required. ``revalidation`` (RevalidationResult), ``execution``
    (ExecutionResult), and ``proposal`` (Proposal) are optional: ``None``
    means that stage/object was not supplied, which the record preserves
    faithfully instead of inventing a result. Provenance prefers the
    execution record when present and otherwise preserves the supplied
    proposal provenance without inventing any entry. No filesystem,
    network, or knowledge-store side effects.

    ``trace`` is an optional plain mapping of traceability evidence
    (identifiers and hashes only — never credentials or raw secrets);
    unknown keys are ignored so callers cannot smuggle authority.
    """
    from src.living_authenticity.knowledge.governance.approval.outcome import (
        ApprovalOutcome,
        ApprovalRequest,
    )

    if not isinstance(request, ApprovalRequest):
        raise TypeError("request must be an ApprovalRequest")
    if not isinstance(outcome, ApprovalOutcome):
        raise TypeError("outcome must be an ApprovalOutcome")
    if revalidation is not None:
        from src.living_authenticity.knowledge.governance.revalidation.outcome import (
            RevalidationResult,
        )

        if not isinstance(revalidation, RevalidationResult):
            raise TypeError("revalidation must be a RevalidationResult")
    if execution is not None:
        from src.living_authenticity.knowledge.governance.execution.outcome import (
            ExecutionResult,
        )

        if not isinstance(execution, ExecutionResult):
            raise TypeError("execution must be an ExecutionResult")
    if proposal is not None:
        from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal

        if not isinstance(proposal, Proposal):
            raise TypeError("proposal must be a Proposal")
    provenance = ()
    if execution is not None:
        provenance = tuple(execution.provenance)
    elif proposal is not None:
        provenance = tuple(proposal.provenance)
    info = trace if isinstance(trace, dict) else {}

    def _text(key) -> str:
        value = info.get(key, "")
        return value if isinstance(value, str) else ""

    def _tuple(key) -> tuple:
        value = info.get(key, ())
        if isinstance(value, (tuple, list)):
            return tuple(item for item in value if isinstance(item, dict))
        return ()

    def _str_tuple(key) -> tuple:
        value = info.get(key, ())
        if isinstance(value, (tuple, list)):
            return tuple(item for item in value if isinstance(item, str))
        return ()

    retrieval_candidates = _tuple("retrieval_candidates")
    if not retrieval_candidates and proposal is not None:
        evidence = tuple(getattr(proposal, "evidence", ()) or ())
        retrieval_candidates = tuple(
            {"unit_id": str(item)} for item in evidence
            if isinstance(item, str) and item)
    default_errors = _str_tuple("errors")
    if not default_errors:
        # Preserve failure/stopping reasons as the error channel where
        # one exists; held/rejected outcomes are not errors by themselves.
        if execution is not None and execution.attempted and not execution.executed:
            default_errors = (execution.reason,) if execution.reason else ()
        elif revalidation is not None and not revalidation.valid:
            default_errors = (revalidation.reason,) if revalidation.reason else ()
        elif _text("stop_reason"):
            default_errors = (_text("stop_reason"),)
    return AuditRecord(
        record_id=_text("record_id"),
        run_id=_text("run_id"),
        operating_mode=_text("operating_mode"),
        input_hash=_text("input_hash"),
        proposal_hash=request.proposal_hash,
        query_unit_id=request.query_unit_id,
        query_source=request.query_source,
        query_position=request.query_position,
        proposal_action=request.proposal_action,
        approval_approved=bool(outcome.approved),
        approval_proposal_hash=outcome.proposal_hash,
        approval_query_unit_id=outcome.query_unit_id,
        approval_timestamp=outcome.timestamp,
        approval_strategy=outcome.strategy,
        revalidation_performed=revalidation is not None,
        revalidation_valid=bool(revalidation.valid) if revalidation is not None else False,
        revalidation_failed_check=revalidation.failed_check if revalidation is not None else "",
        revalidation_reason=revalidation.reason if revalidation is not None else "",
        execution_attempted=bool(execution.attempted) if execution is not None else False,
        execution_permitted=bool(execution.permitted) if execution is not None else False,
        execution_executed=bool(execution.executed) if execution is not None else False,
        execution_proposal_hash=execution.proposal_hash if execution is not None else "",
        execution_reason=execution.reason if execution is not None else "",
        execution_failed_check=execution.failed_check if execution is not None else "",
        artifact_reference=execution.artifact_path if execution is not None else "",
        artifact_sha256=_text("artifact_sha256"),
        destination=execution.destination if execution is not None else request.destination,
        provenance=provenance,
        retrieval_strategy=_text("retrieval_strategy"),
        retrieval_candidates=retrieval_candidates,
        retrieval_note=_text("retrieval_note"),
        evidence_hash=_text("evidence_hash"),
        llm_provider=_text("llm_provider"),
        llm_model=_text("llm_model"),
        llm_config_hash=_text("llm_config_hash"),
        prompt_sha256=_text("prompt_sha256"),
        synthesis_result_id=_text("synthesis_result_id"),
        validation_result=_text("validation_result"),
        stop_reason=_text("stop_reason"),
        human_review_state=_text("human_review_state"),
        timestamp=_text("timestamp"),
        audit_phase=_text("audit_phase") or "terminal",
        execution_timestamp=_text("execution_timestamp"),
        artifact_created_at=_text("artifact_created_at"),
        artifact_path=_text("artifact_path"),
        errors=default_errors,
        warnings=_str_tuple("warnings"),
        core_relevance=_text("core_relevance"),
        core_basis=_text("core_basis"),
    )


def record_audit_for_execution(request, outcome, revalidation, execution,
                               proposal=None, trace=None) -> AuditRecord:
    """Record the audit trace for one completed controlled-execution attempt.

    Minimal deterministic connection from the existing controlled flow to
    the observational audit record: the caller passes the exact objects
    already produced by approval, fresh revalidation, and
    ``ControlledExecutor``. This function executes nothing, writes
    nothing, and authorizes nothing; it only forwards the supplied
    contracts to :func:`build_audit_record`.
    """
    return build_audit_record(request, outcome, revalidation, execution,
                              proposal, trace)
