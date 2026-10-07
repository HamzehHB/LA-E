"""Review-interaction contract: every candidate is shown, then reviewed."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.decision.confidence.outcome import (
    ConfidenceAssessment,
)
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.analysis.classification.result import (
    ClassificationResult,
)
from src.living_authenticity.knowledge.integration import review_display
from src.living_authenticity.knowledge.integration.unit_loop import (
    _handle_unit,
)

from tests._synthetic_synthesis import SyntheticSynthesis


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


class _QuietGate:
    def request_approval(self, *args, **kwargs):
        raise AssertionError("non-CREATE review never needs the gate")


def _needs_review_unit(action="NEEDS_REVIEW"):
    query = KnowledgeUnit(
        id="ku-boundary", source="synthetic-source",
        original_text="synthetic observation text",
        meaning="synthetic observation text",
        cleaned_text="synthetic observation text",
        normalized_text="synthetic observation text", position=0)
    proposal = Proposal(
        query_unit_id="ku-boundary", query_source="synthetic-source",
        query_position=0, action=action, title="Synthetic proposal",
        destination="synthetic/destination.md",
        reason="synthetic proposal reason",
        classification_type="Observation")
    classification = ClassificationResult(
        unit_id="ku-boundary", source="synthetic-source", position=0,
        proposed_type="Observation", rationale="synthetic rationale")
    confidence = ConfidenceAssessment(
        query_unit_id="ku-boundary", query_source="synthetic-source",
        query_position=0, proposal_action=action,
        level="weak", basis="synthetic basis")

    retrieval = _Obj(candidates=(), note="synthetic retrieval note")
    relation = _Obj(proposals=())
    core = _Obj(relevance="unresolved", basis="synthetic core")
    filter_outcome = _Obj(verdict="needs_review", basis="synthetic filter")
    return _StubUnitResult(query, proposal, classification, retrieval,
                           (), relation, core, confidence, filter_outcome)


def test_non_create_block_presses_enter_not_approval(capsys):
    unit = _needs_review_unit()
    block = review_display.render_review_block(
        proposal=unit.proposal, query=unit.query_unit,
        non_executable=True)
    assert "Press Enter to continue" in block
    assert "requires no approval" in block
    assert "Approve this action? [y/N]:" not in block
    capsys.readouterr()


def test_create_block_still_asks_for_approval(capsys):
    unit = _needs_review_unit("CREATE")
    block = review_display.render_review_block(
        proposal=unit.proposal, query=unit.query_unit)
    assert "Approve this action? [y/N]:" in block
    assert "Press Enter to continue" not in block
    capsys.readouterr()


def test_each_candidate_reaches_review_one_at_a_time(capsys, monkeypatch):
    import json

    raw = json.dumps({"candidates": [
        {"title": "first", "body": "first body text",
         "suggested_type": "Observation", "reason": "r1",
         "uncertainty": "u1"},
        {"title": "second", "body": "second body text",
         "suggested_type": "Observation", "reason": "r2",
         "uncertainty": "u2"},
    ]})
    reads: list = []
    monkeypatch.setattr(
        "builtins.input", lambda: reads.append(1) or "")
    synthesis = SyntheticSynthesis(raw=raw)
    unit = _needs_review_unit()
    entries: list = []
    audits: list = []
    _handle_unit("synthetic-child", unit, gate=_QuietGate(),
                 validator=None, runner=None,
                 pinned_staging="", guarded=(), schema_version="",
                 approval_reader=None, entries=entries, audits=audits,
                 authorized_staging="", synthesis=synthesis,
                 details=[], vault_context=None)
    assert len(entries) == 2
    assert reads and len(reads) == 2
    assert len(audits) == 2
    assert all(a.approval_approved is False for a in audits)
    assert all(a.execution_executed is False for a in audits)
    assert all(a.validation_result == "held_non_executable" for a in audits)
