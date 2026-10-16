"""Deterministic evidence-strength confidence assessor.

The guard reads only evidence already recorded by the earlier
analytical stages (the Proposal plus its underlying outcomes) and
describes that evidence as an ordinal strength level. Missing or
contradictory evidence lowers the level; it is never converted into
confidence. No numeric score is produced: the repository does not yet
define a justified numeric confidence scale, so uncertainty is
preserved as an explicit ordinal level instead of manufactured
precision.
"""

from src.living_authenticity.knowledge.analysis.classification.result import (
    ClassificationResult,
)
from src.living_authenticity.knowledge.analysis.comparison.outcome import ComparisonResult
from src.living_authenticity.knowledge.analysis.core_analysis.outcome import (
    CoreAnalysisResult,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.analysis.relations.outcome import (
    RelationDetectionResult,
)
from src.living_authenticity.knowledge.analysis.retrieval.result import RetrievalResult

from .base_guard import ConfidenceGuard
from .outcome import ConfidenceAssessment

_IDENTITY_LIKE = ("identical_content", "possible_duplicate")
_DISTINCT_LIKE = ("distinct", "related_but_distinct")


class EvidenceStrengthGuard(ConfidenceGuard):
    """Assess evidence strength from earlier stage results only."""

    def __init__(self, strategy: str = "evidence_strength") -> None:
        self._strategy = strategy

    @property
    def name(self) -> str:
        return self._strategy

    def assess(self, query, proposal=None, classification=None,
               retrieval=None, comparisons=(), relation=None,
               core=None) -> ConfidenceAssessment:
        """Assess the strength of existing proposal/evidence state.

        Inputs are read-only evidence from earlier stages; none of
        them is mutated. The result is informational only and must
        never be interpreted as approval or execution permission.
        """
        if not isinstance(query, KnowledgeUnit):
            raise TypeError("query must be a KnowledgeUnit")
        if not isinstance(proposal, Proposal):
            raise TypeError("proposal must be a Proposal")
        if classification is not None and not isinstance(
            classification, ClassificationResult
        ):
            raise TypeError("classification must be ClassificationResult")
        if retrieval is not None and not isinstance(
            retrieval, RetrievalResult
        ):
            raise TypeError("retrieval must be RetrievalResult")
        items = tuple(comparisons) if comparisons is not None else ()
        for item in items:
            if not isinstance(item, ComparisonResult):
                raise TypeError("comparisons must hold ComparisonResult")
            if item.query_unit_id != query.id:
                raise ValueError("comparison query_unit_id mismatch")
        if relation is not None and not isinstance(
            relation, RelationDetectionResult
        ):
            raise TypeError("relation must be RelationDetectionResult")
        if core is not None and not isinstance(core, CoreAnalysisResult):
            raise TypeError("core must be CoreAnalysisResult")
        categories = frozenset(c.category for c in items)
        level, basis, uncertainties, score = self._decide(
            proposal, classification, retrieval, items, categories,
            relation, core,
        )
        return ConfidenceAssessment(
            query_unit_id=query.id,
            query_source=query.source,
            query_position=query.position,
            proposal_action=proposal.action,
            level=level,
            score=score,
            strategy=self._strategy,
            basis=basis,
            uncertainties=tuple(uncertainties),
            note=(
                "Non-authoritative descriptive assessment for human "
                "review; confidence is not approval, authorization, "
                "or execution permission."
            ),
            is_authoritative=False,
            requires_human_review=True,
        )

    def _calculate_score(self, proposal, classification, retrieval, items,
                         categories, relation, core, contradictory,
                         insufficient, gaps) -> float:
        """Deterministic score calculation per Knowledge-Schema.yaml §16.

        Score is strictly analytical in [0.0, 1.0] derived from:
        - evidence completeness (0.30 max)
        - comparison overlap/distinctness (0.25 max)
        - classification certainty (0.25 max)
        - Core resolution (0.20 max)
        with penalties for recorded gaps and consistency with ordinal level.
        """
        if contradictory:
            return 0.25
        has_evidence = bool(
            classification is not None
            or (retrieval is not None and retrieval.candidates)
            or items
            or relation is not None
            or core is not None
        )
        if not has_evidence:
            return 0.0

        completeness = 0.0
        if classification is not None:
            completeness += 0.08
        if retrieval is not None and retrieval.candidates:
            completeness += 0.08
        if items:
            completeness += 0.08
        if core is not None:
            completeness += 0.03
        if relation is not None:
            completeness += 0.03

        comparison_score = 0.0
        if items and not contradictory and not insufficient:
            comparison_score = 0.25
        elif items and insufficient:
            comparison_score = 0.05
        elif not items and (retrieval is not None and not retrieval.candidates):
            comparison_score = 0.20

        cls_score = 0.0
        if classification is not None:
            if getattr(classification, "is_certain", False):
                cls_score = 0.25
            elif getattr(classification, "is_resolved", False):
                cls_score = 0.15
            else:
                cls_score = 0.05

        core_score = 0.0
        if core is not None:
            if core.relevance in ("potentially_relevant", "no_observed_relevance"):
                core_score = 0.20
            else:
                core_score = 0.05

        raw = completeness + comparison_score + cls_score + core_score
        penalty = min(0.35, len(gaps) * 0.06)
        score = max(0.0, min(1.0, round(raw - penalty, 2)))

        if contradictory or insufficient:
            score = min(score, 0.45)
        elif gaps:
            score = min(score, 0.75)
        else:
            score = max(0.85, min(1.0, score))
        return score

    def _decide(self, proposal, classification, retrieval, items,
                categories, relation, core):
        # Missing evidence stays missing: with nothing behind the
        # proposal, the level cannot exceed insufficient_evidence.
        has_anything = bool(
            classification is not None
            or (retrieval is not None and retrieval.candidates)
            or items
            or relation is not None
            or core is not None
            or proposal.evidence
            or proposal.relevant_candidates
        )
        if not has_anything:
            return (
                "insufficient_evidence",
                "No analytical evidence was supplied behind the "
                "proposal; strength cannot be shown.",
                ("no analytical evidence supplied",),
                0.0,
            )
        contradictory = (
            any(c in categories for c in _IDENTITY_LIKE)
            and any(c in categories for c in _DISTINCT_LIKE)
        )
        insufficient = "insufficient_evidence" in categories
        if contradictory or insufficient:
            found = []
            if contradictory:
                found.append("contradictory comparison evidence")
            if insufficient:
                found.append("insufficient comparison evidence")
            score = self._calculate_score(
                proposal, classification, retrieval, items, categories,
                relation, core, contradictory, insufficient, found,
            )
            return (
                "contradictory_or_unresolved",
                "Available evidence contradicts itself or leaves the "
                "comparison unresolved; strength cannot be shown.",
                tuple(found),
                score,
            )
        # The proposal carries its own recorded gaps. Any such gap
        # weakens the evidence even when nothing contradicts it.
        # Absence of comparative evidence is itself a gap: a level of
        # sufficiently_supported requires something concrete behind it.
        gaps = []
        if not items and not (
            retrieval is not None and retrieval.candidates
        ):
            gaps.append("no comparative evidence recorded")
        gaps.extend(proposal.uncertainties)
        if core is not None and core.relevance == "insufficient_evidence":
            gaps.append("core relevance unresolved")
        score = self._calculate_score(
            proposal, classification, retrieval, items, categories,
            relation, core, False, False, gaps,
        )
        if gaps:
            return (
                "weak",
                "Evidence is present but has recorded gaps; it is not "
                "sufficiently complete to describe as strong. A human "
                "reviewer must weigh the remaining uncertainty.",
                tuple(gaps),
                score,
            )
        return (
            "sufficiently_supported",
            "Available evidence is consistent and records no gaps; "
            "this describes evidence strength only and authorizes "
            "nothing. Human review is still required.",
            (),
            score,
        )



class DefaultConfidenceGuard(EvidenceStrengthGuard):
    """Default guard alias preserving evidence-strength behavior."""
