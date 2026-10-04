"""Deterministic Obsidian note generation from proposal evidence.

Copies meaning, provenance, evidence, uncertainties, and summaries
already recorded on the Proposal into a proposed Markdown
representation. No analysis, retrieval, comparison, classification,
or decision is performed here. No filesystem, network, or model use.
"""
from src.living_authenticity.knowledge.analysis.classification.result import (
    CLASSIFICATION_TYPES,
    ClassificationResult,
)
from src.living_authenticity.knowledge.decision.confidence.outcome import (
    CONFIDENCE_LEVELS,
    ConfidenceAssessment,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.decision.proposal.outcome import (
    PROPOSAL_ACTIONS,
    Proposal,
)

from .base_generator import ObsidianNoteGenerator
from .outcome import GeneratedNote

_SAFE_ACTIONS = tuple(PROPOSAL_ACTIONS)


def _frontmatter_pairs(proposal, classification, confidence):
    pairs = []
    pairs.append(("title", proposal.title))
    ptype = proposal.classification_type or ""
    if classification is not None and classification.proposed_type:
        ptype = classification.proposed_type
    pairs.append(("proposed_type", ptype))
    pairs.append(("proposal_action", proposal.action))
    pairs.append(("confidence", confidence.level if confidence else ""))
    pairs.append(("proposed_location", proposal.destination))
    pairs.append(("query_unit_id", proposal.query_unit_id))
    pairs.append(("query_source", proposal.query_source))
    pairs.append(("query_position", str(proposal.query_position)))
    return tuple(pairs)


def _yaml_scalar(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_frontmatter(pairs) -> str:
    lines = ["---"]
    for key in sorted(k for k, _ in pairs):
        value = next(v for k, v in pairs if k == key)
        lines.append(key + ": " + _yaml_scalar(value))
    lines.append("---")
    return "\n".join(lines)


def _render_body(query, proposal, classification, confidence,
                 retrieval=None, comparisons=(), relation=None, core=None,
                 filter_outcome=None) -> str:
    lines = []
    lines.append("# " + (proposal.title or "Untitled proposed note"))
    lines.append("")
    lines.append(
        "> PROPOSED NOTE -- NON-AUTHORITATIVE. "
        "Requires human review; not approval, authorization, or execution."
    )
    lines.append("")
    lines.append("## Source meaning")
    lines.append("")
    meaning = (
        query.cleaned_text or query.normalized_text
        or query.meaning or query.original_text or ""
    )
    lines.append(meaning if meaning.strip() else "(no source meaning recorded)")
    lines.append("")
    lines.append("## Proposal")
    lines.append("")
    lines.append("Action: " + proposal.action)
    lines.append("Reason: " + (proposal.reason or "(no reason recorded)"))
    ptype = proposal.classification_type
    if classification is not None and classification.proposed_type:
        ptype = classification.proposed_type
    lines.append("Proposed type: " + (ptype or "(unresolved)"))
    lines.append(
        "Confidence: "
        + (confidence.level if confidence else "(not assessed)")
    )
    lines.append(
        "Proposed location (inert, not a destination to write): "
        + (proposal.destination or "(none proposed)")
    )
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    if proposal.evidence:
        for item in sorted(set(proposal.evidence)):
            lines.append("- " + item)
    else:
        lines.append("- (no evidence recorded)")
    if proposal.relevant_candidates:
        lines.append("")
        lines.append("Relevant candidates:")
        lines.append("")
        for item in sorted(set(proposal.relevant_candidates)):
            lines.append("- " + item)
    lines.append("")
    lines.append("## Analysis")
    lines.append("")
    lines.append("Comparison: "
                 + (proposal.comparison_summary or "(none recorded)"))
    lines.append("Relations: "
                 + (proposal.relation_summary or "(none recorded)"))
    lines.append("Core relevance: "
                 + (proposal.core_relevance or "(none recorded)"))
    if filter_outcome is not None:
        lines.append("Filter verdict: " + str(filter_outcome.verdict))
        lines.append("Filter basis: "
                     + (filter_outcome.basis or "(none recorded)"))
    lines.append("")
    lines.append("## Retrieved Context")
    lines.append("")
    if retrieval is not None and getattr(
            retrieval, "candidates", ()):
        for candidate in list(retrieval.candidates)[:10]:
            lines.append("- " + str(getattr(candidate, "unit_id", ""))
                         + " @ " + str(getattr(candidate, "source", ""))
                         + ": " + str(getattr(candidate, "text", ""))[:1000])
    else:
        lines.append("- (no retrieval candidates recorded)")
    lines.append("")
    lines.append("## Proposed Knowledge")
    lines.append("")
    lines.append("Title: " + (proposal.title or "(untitled)"))
    lines.append("Type: " + (ptype or "(unresolved)"))
    if classification is not None:
        lines.append("Classification rationale: "
                     + (classification.rationale or "(none recorded)"))
    if confidence is not None:
        lines.append("Confidence basis: "
                     + (confidence.basis or "(none recorded)"))
    lines.append("")
    lines.append("## Uncertainty")
    lines.append("")
    gaps = list(proposal.uncertainties)
    if confidence is not None:
        gaps.extend(confidence.uncertainties)
    if filter_outcome is not None:
        gaps.extend(list(filter_outcome.uncertainties))
    if gaps:
        for item in sorted(set(gaps)):
            lines.append("- " + item)
    else:
        lines.append("- (none recorded)")
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    if proposal.provenance:
        for item in proposal.provenance:
            lines.append("- " + item)
    else:
        lines.append("- (no provenance recorded)")
    return "\n".join(lines)


class DeterministicObsidianGenerator(ObsidianNoteGenerator):
    """Render proposals as deterministic Obsidian Markdown text."""

    def __init__(self, strategy: str = "deterministic_obsidian") -> None:
        self._strategy = strategy

    @property
    def name(self) -> str:
        return self._strategy

    def generate(self, query, proposal=None, classification=None,
                 confidence=None) -> GeneratedNote:
        """Render ``proposal`` plus supporting context as one note."""
        if not isinstance(query, KnowledgeUnit):
            raise TypeError("query must be a KnowledgeUnit")
        if not isinstance(proposal, Proposal):
            raise TypeError("proposal must be a Proposal")
        if proposal.query_unit_id != query.id:
            raise ValueError("proposal query_unit_id mismatch")
        if classification is not None and not isinstance(
            classification, ClassificationResult
        ):
            raise TypeError("classification must be ClassificationResult")
        if confidence is not None and not isinstance(
            confidence, ConfidenceAssessment
        ):
            raise TypeError("confidence must be ConfidenceAssessment")
        if classification is not None and classification.unit_id != query.id:
            raise ValueError("classification unit_id mismatch")
        if confidence is not None and confidence.query_unit_id != query.id:
            raise ValueError("confidence query_unit_id mismatch")
        if proposal.action not in _SAFE_ACTIONS:
            raise ValueError("unsupported proposal action")
        ptype = proposal.classification_type or ""
        if classification is not None and classification.proposed_type:
            ptype = classification.proposed_type
        if ptype and ptype not in CLASSIFICATION_TYPES:
            raise ValueError("unsupported proposed type")
        if confidence is not None and confidence.level not in CONFIDENCE_LEVELS:
            raise ValueError("unsupported confidence level")
        pairs = _frontmatter_pairs(proposal, classification, confidence)
        front = _render_frontmatter(pairs)
        body = _render_body(query, proposal, classification, confidence)
        markdown = front + "\n\n" + body + "\n"
        gaps = list(proposal.uncertainties)
        if confidence is not None:
            gaps.extend(confidence.uncertainties)
        return GeneratedNote(
            query_unit_id=query.id,
            query_source=query.source,
            query_position=query.position,
            title=proposal.title,
            body=body,
            proposed_type=ptype,
            proposal_action=proposal.action,
            confidence_level=(confidence.level if confidence else ""),
            destination=proposal.destination,
            frontmatter=pairs,
            evidence=tuple(sorted(set(proposal.evidence))),
            uncertainties=tuple(sorted(set(gaps))),
            provenance=tuple(proposal.provenance),
            markdown=markdown,
            strategy=self._strategy,
            note=(
                "Proposed Obsidian representation for human review only; "
                "not an authoritative note and not execution."
            ),
            is_authoritative=False,
            requires_human_review=True,
        )


class DefaultOutputGenerator(DeterministicObsidianGenerator):
    """Default generator alias preserving deterministic behavior."""


