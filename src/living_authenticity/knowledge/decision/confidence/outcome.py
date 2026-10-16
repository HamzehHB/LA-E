"""Immutable confidence outcome for the analytical confidence stage."""

from dataclasses import dataclass, field

# Conservative ordinal evidence-strength vocabulary for the confidence
# stage. The values are ordered from strongest to weakest observation,
# but they are explicitly NOT numeric scores and NOT probabilities.
# The repository does not yet define a justified numeric confidence
# scale, so no number is produced; uncertainty is preserved as an
# explicit ordinal level instead of being manufactured.
#
# - "sufficiently_supported": the available evidence is consistent and
#   complete enough to describe the proposal's support, for a human
#   reviewer's information.
# - "weak": evidence is present but has recorded gaps or uncertainties.
# - "contradictory_or_unresolved": available evidence contradicts
#   itself or leaves the question unresolved.
# - "insufficient_evidence": required evidence is missing; unknown
#   stays unknown and is never converted into confidence.
CONFIDENCE_LEVELS = (
    "sufficiently_supported",
    "weak",
    "contradictory_or_unresolved",
    "insufficient_evidence",
)


@dataclass(frozen=True)
class ConfidenceAssessment:
    """Descriptive, non-authoritative evidence-strength assessment.

    This object describes how strongly the existing evidence supports
    an already-produced proposal. It is informational only:

    - it is not authorization, approval, or execution permission;
    - a high level never means approved, safe, or executable;
    - a low level never executes DO_NOT_IMPORT or NEEDS_REVIEW;
    - it never mutates the Proposal or any earlier outcome;
    - Human Review remains a separate, mandatory stage.
    """

    query_unit_id: str
    query_source: str
    query_position: int
    proposal_action: str = ""
    level: str = "insufficient_evidence"
    score: float = 0.0
    strategy: str = ""
    basis: str = ""
    uncertainties: tuple = field(default_factory=tuple)
    note: str = ""
    is_authoritative: bool = False
    requires_human_review: bool = True

    @property
    def is_sufficiently_supported(self) -> bool:
        """True only for the conservative sufficiently_supported level."""
        return self.level == "sufficiently_supported"

    @property
    def score_band(self) -> str:
        """Mapping per Knowledge-Schema.yaml §16: low, moderate, high."""
        if self.score >= 0.80:
            return "high"
        if self.score >= 0.60:
            return "moderate"
        return "low"

    def __post_init__(self) -> None:
        """Enforce conservative invariants on direct construction.

        An unsupported level coerces to insufficient_evidence, and the
        non-authoritative / human-review flags are fixed because a
        confidence assessment is never approval, authorization, or
        execution.
        """
        if self.level not in CONFIDENCE_LEVELS:
            object.__setattr__(self, "level", "insufficient_evidence")
        try:
            s = float(self.score)
        except (TypeError, ValueError):
            s = 0.0
        s = max(0.0, min(1.0, round(s, 2)))
        object.__setattr__(self, "score", s)
        object.__setattr__(self, "is_authoritative", False)
        object.__setattr__(self, "requires_human_review", True)
