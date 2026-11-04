"""Deterministic proposal builder over analytical stage evidence."""

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
from src.living_authenticity.knowledge.analysis.relations.outcome import (
    RelationDetectionResult,
)
from src.living_authenticity.knowledge.analysis.retrieval.result import RetrievalResult

from .outcome import PROPOSAL_ACTIONS, Proposal


def _unit_title(unit: KnowledgeUnit) -> str:
    text = (
        unit.cleaned_text or unit.normalized_text
        or unit.meaning or unit.original_text or ""
    )
    single = " ".join(text.split())
    if len(single) > 120:
        return single[:117] + "..."
    return single


def _comparison_summary(comparisons: tuple) -> str:
    if not comparisons:
        return "no comparison evidence"
    cats = sorted({c.category for c in comparisons})
    return "comparisons: " + ",".join(cats)


def _relation_summary(relation) -> str:
    if relation is None:
        return "no relation evidence"
    if not relation.proposals:
        return "no relation proposals"
    rels = sorted({p.relation for p in relation.proposals})
    return "relations: " + ",".join(rels)


def _candidate_refs(retrieval) -> tuple:
    if retrieval is None:
        return ()
    ordered = sorted(
        retrieval.candidates,
        key=lambda c: (c.source, c.position, c.unit_id),
    )
    refs = tuple(
        "candidate:" + c.unit_id + "@" + c.source + "#" + str(c.position)
        for c in ordered
    )
    return refs


class ProposalBuilder:
    """Build immutable proposals from analytical stage results."""

    def __init__(self, strategy: str = "analytical_proposal") -> None:
        self._strategy = strategy

    @property
    def name(self) -> str:
        return self._strategy

    def build(self, query, classification=None, retrieval=None,
              comparisons=(), relation=None, core=None,
              destination: str = "") -> Proposal:
        if not isinstance(query, KnowledgeUnit):
            raise TypeError("query must be a KnowledgeUnit")
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
        if destination is None:
            destination = ""
        if not isinstance(destination, str):
            raise TypeError("destination must be a string")
        has_cands = bool(retrieval is not None and retrieval.candidates)
        action, reason, uncerts = _decide_action(
            classification, items, relation, core, has_cands
        )
        if action not in PROPOSAL_ACTIONS:
            action = "NEEDS_REVIEW"
        ev: list = []
        if classification is not None:
            ev.append("classification:" + (classification.proposed_type or "u"))
        ordered_cmps = sorted(
            items,
            key=lambda c: (c.candidate_source, c.candidate_position,
                           c.candidate_unit_id),
        )
        for item in ordered_cmps:
            ev.append("comparison:" + item.candidate_unit_id + "=" + item.category)
        if relation is not None:
            ordered_props = sorted(
                relation.proposals,
                key=lambda p: (p.candidate_source, p.candidate_position,
                               p.candidate_unit_id),
            )
            for prop in ordered_props:
                ev.append("relation:" + prop.candidate_unit_id + "=" + prop.relation)
        if core is not None:
            ev.append("core:" + core.relevance)
        prov = (
            "query:" + query.id + "@" + query.source + "#" + str(query.position),
            "classification:" + (classification.classifier if classification else "n"),
            "retrieval:" + (retrieval.strategy if retrieval else "none"),
            "core:" + (core.strategy if core else "none"),
        )
        shared: list = []
        for item in items:
            if item.shared_terms:
                shared.extend(tuple(item.shared_terms))
        shared_terms = tuple(sorted(set(shared)))
        if shared_terms:
            tail = "Shared terms: " + ",".join(shared_terms)
        else:
            tail = "No shared terms recorded."
        return Proposal(
            query_unit_id=query.id,
            query_source=query.source,
            query_position=query.position,
            action=action,
            title=_unit_title(query),
            destination=destination,
            reason=reason,
            evidence=tuple(ev),
            relevant_candidates=_candidate_refs(retrieval),
            uncertainties=uncerts,
            provenance=prov,
            classification_type=(classification.proposed_type if classification else ""),
            comparison_summary=_comparison_summary(items),
            relation_summary=_relation_summary(relation),
            core_relevance=(core.relevance if core else "insufficient_evidence"),
            strategy=self._strategy,
            note=(
                "Non-authoritative proposal for human review only. "
                "Not approval, authorization, or execution. "
                "Destination is inert data. " + tail
            ),
            is_authoritative=False,
            requires_human_review=True,
        )


DefaultProposalBuilder = ProposalBuilder

def _decide_action(classification, comparisons, relation, core,
                   has_candidates: bool) -> tuple:
    """Return (action, reason, uncertainties) deterministically."""
    uncertainties: list = []
    cats = {c.category for c in comparisons}
    if classification is None:
        uncertainties.append("classification evidence missing")
    elif not classification.is_certain or not classification.proposed_type:
        uncertainties.append("classification unresolved")
    if not comparisons and has_candidates:
        uncertainties.append("comparison evidence missing")
    if relation is None and has_candidates:
        uncertainties.append("relation evidence missing")
    if core is None:
        uncertainties.append("core analysis evidence missing")
    has_identical = "identical_content" in cats
    has_possible_dup = "possible_duplicate" in cats
    has_distinct_like = bool("distinct" in cats or "related_but_distinct" in cats)

    contradictory = bool(has_possible_dup and has_distinct_like and not has_identical)
    if contradictory:
        uncertainties.append("contradictory comparison evidence")
    insufficient = any(
        c.category == "insufficient_evidence" for c in comparisons
    )
    if insufficient:
        uncertainties.append("insufficient comparison evidence")
    identity_like = any(
        c.category in ("identical_content", "possible_duplicate")
        for c in comparisons
    )
    core_relevant = core is not None and core.relevance == "potentially_relevant"
    core_unresolved = core is None or core.relevance == "insufficient_evidence"
    if core_unresolved:
        uncertainties.append("core relevance unresolved")
    if has_identical:
        return (
            "DO_NOT_IMPORT",
            "Comparison reports identical content with existing "
            "knowledge; importing would risk duplication.",
            tuple(uncertainties),
        )
    if contradictory or insufficient:
        return (
            "NEEDS_REVIEW",
            "Contradictory or insufficient analytical evidence; "
            "human review is required.",
            tuple(uncertainties),
        )
    if identity_like:
        return (
            "DO_NOT_IMPORT",
            "Comparison reports identity-like overlap with existing "
            "knowledge; importing would risk duplication.",
            tuple(uncertainties),
        )
    if core_relevant or core_unresolved:
        if core_relevant:
            uncertainties.append("core relevance potentially relevant")
        return (
            "NEEDS_REVIEW",
            "Core relevance is unresolved or potentially relevant; "
            "a non-authoritative proposal needs human review.",
            tuple(uncertainties),
        )
    if classification is None:
        return (
            "NEEDS_REVIEW",
            "Classification is unresolved; safe import cannot be shown.",
            tuple(uncertainties),
        )
    if not classification.is_certain or not classification.proposed_type:
        return (
            "NEEDS_REVIEW",
            "Classification is unresolved; safe import cannot be shown.",
            tuple(uncertainties),
        )
    if not has_candidates and not comparisons:
        return (
            "CREATE",
            "No existing candidates and no conflicting evidence; "
            "a new draft is proposed for human review.",
            tuple(uncertainties),
        )
    only_distinct = bool(comparisons) and all(
        c.category == "distinct" for c in comparisons
    )
    if only_distinct:
        rel_ok = True
        if relation is not None and relation.proposals:
            rel_ok = all(
                p.relation == "no_proposed_relation"
                for p in relation.proposals
            )
        if rel_ok:
            return (
                "CREATE",
                "Evidence shows distinct content with no conflicting "
                "signals; a new draft is proposed for human review.",
                tuple(uncertainties),
            )
    uncertainties.append("evidence does not safely justify CREATE")
    return (
        "NEEDS_REVIEW",
        "Available evidence does not safely justify CREATE or "
        "DO_NOT_IMPORT; human review is required.",
        tuple(uncertainties),
    )
