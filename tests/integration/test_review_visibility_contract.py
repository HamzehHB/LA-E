"""Review-visibility contract: non-CREATE candidates stay inspectable.

CREATE and non-CREATE entries must both expose the underlying
candidate, proposal reason, intermediate evidence, and proposed note
text when available. Display is informational only: nothing here
authorizes, approves, or executes.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.living_authenticity.knowledge.analysis.classification.result import (
    ClassificationResult,
)
from src.living_authenticity.knowledge.decision.confidence.outcome import (
    ConfidenceAssessment,
)
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit

from tests._synthetic_synthesis import SyntheticSynthesis


class _StubGate:
    def request_approval(self, query, proposal, confidence,
                         reader=None, note=None):
        raise AssertionError("non-CREATE must not request approval")

    def build_request(self, *args, **kwargs):
        raise AssertionError("non-CREATE must not request approval")


class _StubValidator:
    def revalidate(self, *args, **kwargs):
        raise AssertionError("non-CREATE must not revalidate")


class _StubRunner:
    def execute(self, *args, **kwargs):
        raise AssertionError("non-CREATE must not execute")


class _StubUnitResult:
    def __init__(self, query, proposal, classification, retrieval,
                 comparisons, relation, core, confidence, filter_outcome):
        self.query_unit = query
        self.proposal = proposal
        self.classification = classification
        self.retrieval_result = retrieval
        self.comparisons = comparisons
        self.relation_result = relation
        self.core_analysis = core
        self.confidence = confidence
        self.filter_outcome = filter_outcome


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _make_needs_review_unit():
    query = KnowledgeUnit(
        id="ku-needs-review", source="synthetic-source",
        original_text="ambiguous synthetic observation text",
        meaning="ambiguous synthetic observation text",
        cleaned_text="ambiguous synthetic observation text",
        normalized_text="ambiguous synthetic observation text",
        position=0,
    )
    proposal = Proposal(
        query_unit_id="ku-needs-review", query_source="synthetic-source",
        query_position=0, action="NEEDS_REVIEW",
        title="Synthetic proposal",
        destination="synthetic/destination.md",
        reason="synthetic proposal reason",
        classification_type="Observation")
    classification = ClassificationResult(
        unit_id="ku-needs-review", source="synthetic-source", position=0,
        proposed_type="Observation",
        rationale="synthetic classification rationale")
    candidate = _Obj(unit_id="vault-unit-1", source="synthetic-vault/note.md",
                     position=0, overlap_score=2, text="synthetic vault excerpt")
    retrieval = _Obj(candidates=(candidate,), note="synthetic retrieval note")
    comparison = _Obj(candidate_unit_id="vault-unit-1", category="related",
                      shared_terms=["synthetic"])
    relation_item = _Obj(candidate_unit_id="vault-unit-1",
                         relation="related", basis="synthetic basis")
    relation = _Obj(proposals=(relation_item,))
    core = _Obj(relevance="unresolved", basis="synthetic core basis")
    confidence = ConfidenceAssessment(
        query_unit_id="ku-needs-review", query_source="synthetic-source",
        query_position=0, proposal_action="NEEDS_REVIEW",
        level="weak", basis="synthetic confidence basis")
    filter_outcome = _Obj(verdict="needs_review",
                          basis="synthetic filter basis")
    return _StubUnitResult(query, proposal, classification, retrieval,
                           (comparison,), relation, core, confidence,
                           filter_outcome)


def test_needs_review_held_entry_stays_fully_inspectable():
    unit = _make_needs_review_unit()
    entries: list = []
    audits: list = []
    _handle_unit("synthetic-child", unit, gate=_StubGate(),
                 validator=_StubValidator(), runner=_StubRunner(),
                 pinned_staging="", guarded=(), schema_version="",
                 approval_reader=None, entries=entries, audits=audits,
                 authorized_staging="",
                 synthesis=SyntheticSynthesis(
                     title="Visible candidate title",
                     body="Visible candidate body text for human review."),
                 details=[])
    assert len(entries) == 1
    entry = entries[0]
    assert entry.proposal_action == "NEEDS_REVIEW"
    assert entry.outcome == "held_non_executable"
    assert entry.reason == "candidate proposal action is not CREATE"
    # The exact previously-hidden information is now visible.
    assert entry.query_text.strip() != ""
    assert entry.proposal_title == "Visible candidate title"
    assert "Visible candidate body" in entry.proposal_body
    assert entry.proposal_reason.strip() != ""
    assert entry.note_markdown.strip() != ""
    assert "Visible candidate body" in entry.note_markdown
    assert entry.proposed_destination == "synthetic/destination.md"
    assert entry.llm_candidates and entry.llm_candidates[0]["title"] == \
        "Visible candidate title"
    assert entry.retrieval_candidates
    assert entry.comparison_results
    assert entry.relation_results
    assert entry.classification_type == "Observation"
    assert entry.confidence_level == "weak"
    assert entry.filter_verdict == "needs_review"
    # Still non-executable: no approval, no execution, no artifact.
    assert entry.approval_approved is False
    assert entry.execution_executed is False
    assert entry.artifact_reference == ""
