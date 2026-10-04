"""Vault-context plumbing contract: excerpts reach synthesis as context."""
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
from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
from src.living_authenticity.llm.context import build_llm_context

from tests._synthetic_synthesis import SyntheticSynthesis


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _StubGate:
    def request_approval(self, *args, **kwargs):
        raise AssertionError("non-CREATE must not request approval")


class _StubValidator:
    def revalidate(self, *args, **kwargs):
        raise AssertionError("non-CREATE must not revalidate")


class _StubRunner:
    def execute(self, *args, **kwargs):
        raise AssertionError("non-CREATE must not execute")


def _make_unit():
    query = KnowledgeUnit(
        id="ku-vault-ctx", source="synthetic-source",
        original_text="synthetic observation text",
        meaning="synthetic observation text",
        cleaned_text="synthetic observation text",
        normalized_text="synthetic observation text", position=0)
    proposal = Proposal(
        query_unit_id="ku-vault-ctx", query_source="synthetic-source",
        query_position=0, action="NEEDS_REVIEW",
        title="Synthetic proposal",
        destination="synthetic/destination.md",
        reason="synthetic proposal reason",
        classification_type="Observation")
    classification = ClassificationResult(
        unit_id="ku-vault-ctx", source="synthetic-source", position=0,
        proposed_type="Observation", rationale="synthetic rationale")
    confidence = ConfidenceAssessment(
        query_unit_id="ku-vault-ctx", query_source="synthetic-source",
        query_position=0, proposal_action="NEEDS_REVIEW",
        level="weak", basis="synthetic basis")
    unit = _Obj(query_unit=query, proposal=proposal,
                classification=classification,
                retrieval_result=_Obj(candidates=()),
                comparisons=(), relation_result=_Obj(proposals=()),
                core_analysis=_Obj(relevance="unresolved",
                                   basis="synthetic core"),
                confidence=confidence,
                filter_outcome=_Obj(verdict="needs_review",
                                    basis="synthetic filter"))
    return unit


def test_vault_context_reaches_synthesis_without_authorizing():
    unit = _make_unit()
    synthesis = SyntheticSynthesis()
    entries: list = []
    audits: list = []
    excerpts = [{"source": "synthetic-vault/note.md",
                 "excerpt": "synthetic vault excerpt for context"}]
    _handle_unit("synthetic-child", unit, gate=_StubGate(),
                 validator=_StubValidator(), runner=_StubRunner(),
                 pinned_staging="", guarded=(), schema_version="",
                 approval_reader=None, entries=entries, audits=audits,
                 authorized_staging="", synthesis=synthesis,
                 details=[], vault_context=excerpts)
    assert synthesis.calls == 1
    assert synthesis.last_vault_context == excerpts
    assert len(entries) == 1
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].approval_approved is False
    assert entries[0].execution_executed is False


def test_vault_context_rendered_as_bounded_section_not_retrieval():
    prompt = build_llm_context(vault_context=(
        {"source": "synthetic-vault/note.md",
         "excerpt": "synthetic vault excerpt"},))
    assert "VAULT CONTEXT" in prompt
    assert "synthetic vault excerpt" in prompt
    assert "not semantic retrieval" in prompt
    prompt_empty = build_llm_context()
    assert "VAULT CONTEXT" not in prompt_empty
