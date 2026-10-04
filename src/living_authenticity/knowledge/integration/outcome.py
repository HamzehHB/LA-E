"""Bounded local-integration outcome value objects (inert, no execution).

``IntegrationUnitEntry`` and ``IntegrationRunReport`` are frozen,
in-memory traces. Path values are copied inert strings for
traceability only: they are never resolved, dereferenced, or used as
filesystem destinations by this package.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class IntegrationUnitEntry:
    """Trace of one KnowledgeUnit attempt inside a bounded run."""

    source_file: str = ""
    query_unit_id: str = ""
    query_source: str = ""
    query_position: int = 0
    proposal_hash: str = ""
    proposal_action: str = ""
    approval_approved: bool = False
    revalidation_valid: bool = False
    execution_attempted: bool = False
    execution_permitted: bool = False
    execution_executed: bool = False
    outcome: str = ""
    reason: str = ""
    artifact_reference: str = ""

    # Fields for display/inspection (populated for all proposals, not just CREATE)
    query_text: str = ""
    proposal_title: str = ""
    proposal_body: str = ""
    proposal_reason: str = ""
    proposed_type: str = ""
    uncertainty: str = ""
    note_markdown: str = ""
    proposed_destination: str = ""
    llm_candidates: tuple = field(default_factory=tuple)
    confidence_level: str = ""
    confidence_basis: str = ""
    filter_verdict: str = ""
    filter_basis: str = ""
    retrieval_candidates: tuple = field(default_factory=tuple)
    comparison_results: tuple = field(default_factory=tuple)
    relation_results: tuple = field(default_factory=tuple)
    core_relevance: str = ""
    core_basis: str = ""
    classification_type: str = ""
    classification_rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "approval_approved", bool(self.approval_approved))
        object.__setattr__(self, "revalidation_valid", bool(self.revalidation_valid))
        object.__setattr__(self, "execution_attempted", bool(self.execution_attempted))
        object.__setattr__(self, "execution_permitted", bool(self.execution_permitted))
        object.__setattr__(self, "execution_executed", bool(self.execution_executed))


@dataclass(frozen=True)
class IntegrationUnitDetail:
    """Inspectable in-memory references to one unit's intermediates.

    Non-authoritative and never persisted: each field is a reference (or
    a bounded copy) of an object the run already produced, present so an
    operator can inspect exactly what each stage contributed. Nothing
    here authorizes, executes, or mutates anything.
    """

    source_file: str = ""
    query_unit: object = None
    retrieval: object = None
    comparisons: tuple = ()
    relation: object = None
    core: object = None
    classification: object = None
    proposal: object = None
    confidence: object = None
    filter_outcome: object = None
    llm_result: object = None
    prompt_sha256: str = ""
    candidate_index: int = 0
    human_decision: object = None
    revalidation: object = None
    execution: object = None
    audit: object = None
    note_markdown: str = ""
    is_authoritative: bool = False
    requires_human_review: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "comparisons", tuple(self.comparisons or ()))
        object.__setattr__(self, "is_authoritative", False)
        object.__setattr__(self, "requires_human_review", True)


@dataclass(frozen=True)
class IntegrationRunReport:
    """In-memory report of one bounded local-integration run (no persistence)."""

    source_root: str = ""
    staging_root: str = ""
    max_files: int = 1
    files_seen: int = 0
    files_processed: int = 0
    files_skipped: int = 0
    accepted: bool = False
    rejection_reason: str = ""
    rejection_check: str = ""
    units: tuple = field(default_factory=tuple)
    audits: tuple = field(default_factory=tuple)
    details: tuple = field(default_factory=tuple)
    is_authoritative: bool = False
    requires_human_review: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", tuple(self.units))
        object.__setattr__(self, "audits", tuple(self.audits))
        object.__setattr__(self, "details", tuple(self.details))
        object.__setattr__(self, "is_authoritative", False)
        object.__setattr__(self, "requires_human_review", True)


__all__ = ("IntegrationRunReport", "IntegrationUnitDetail", "IntegrationUnitEntry")


