"""Explicit human-approval gate value objects.

Approval is a human event bound to one exact displayed proposal. It is
not authorization, execution, or persistence, and it never modifies
authoritative knowledge.
"""
from dataclasses import dataclass, field

# Exact accepted inputs (compared after strip().lower()). Everything
# else -- blank, "n", "no", invalid, timeout, interrupted, ambiguous --
# is a rejection. Default is No.
ACCEPTED_APPROVAL_INPUTS = ("y", "yes")


@dataclass(frozen=True)
class ApprovalRequest:
    """One displayed proposal awaiting one fresh human decision."""

    query_unit_id: str
    query_source: str
    query_position: int
    proposal_action: str = ""
    title: str = ""
    destination: str = ""
    reason: str = ""
    confidence_level: str = ""
    proposal_hash: str = ""
    strategy: str = ""
    note: str = ""
    # True when this request was built while bound to a specific
    # generated-note markdown (the exact text shown for review and
    # later written to staging). Requests built without a note keep the
    # historical payload shape and stay verifiable as before.
    note_bound: bool = False
    is_authoritative: bool = False
    requires_human_review: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "note_bound", bool(self.note_bound))
        object.__setattr__(self, "is_authoritative", False)
        object.__setattr__(self, "requires_human_review", True)


@dataclass(frozen=True)
class ApprovalOutcome:
    """Record of a single human approval decision.

    ``approved`` is True only for an explicit accepted input. The
    outcome is bound to ``proposal_hash``: it must never be reused for
    a different proposal. Timestamps are audit metadata only.
    """

    query_unit_id: str
    proposal_hash: str
    proposal_action: str = ""
    approved: bool = False
    input_value: str = ""
    reason: str = ""
    timestamp: str = ""
    strategy: str = ""
    note: str = ""
    is_authoritative: bool = False
    requires_human_review: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved", bool(self.approved))
        object.__setattr__(self, "is_authoritative", False)
        object.__setattr__(self, "requires_human_review", True)
