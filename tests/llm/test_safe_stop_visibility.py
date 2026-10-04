"""Safe-stop visibility contract: LLM failure still shows pipeline evidence."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit

from tests._synthetic_synthesis import FailingSynthesis


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _make_unit():
    query = KnowledgeUnit(
        id="ku-safe-stop", source="synthetic-source",
        original_text="synthetic observation text",
        meaning="synthetic observation text",
        cleaned_text="synthetic observation text",
        normalized_text="synthetic observation text", position=0)
    proposal = _Obj(action="NEEDS_REVIEW", title="Synthetic proposal",
                    destination="synthetic/destination.md",
                    reason="synthetic proposal reason",
                    classification_type="Observation")
    classification = _Obj(proposed_type="Observation",
                          rationale="synthetic rationale",
                          unit_id="ku-safe-stop")
    retrieval = _Obj(candidates=())
    relation = _Obj(proposals=())
    core = _Obj(relevance="unresolved", basis="synthetic core basis")
    confidence = _Obj(level="low", basis="synthetic confidence basis")
    filter_outcome = _Obj(verdict="needs_review", basis="synthetic basis")
    unit = _Obj(query_unit=query, proposal=proposal,
                classification=classification, retrieval_result=retrieval,
                comparisons=(), relation_result=relation,
                core_analysis=core, confidence=confidence,
                filter_outcome=filter_outcome)
    return unit


def test_llm_safe_stop_exposes_reason_and_evidence_without_side_effects():
    unit = _make_unit()
    entries: list = []
    audits: list = []

    class _Gate:
        def request_approval(self, *args, **kwargs):
            raise AssertionError("safe stop must not request approval")

    class _Runner:
        def execute(self, *args, **kwargs):
            raise AssertionError("safe stop must not execute")

    class _Validator:
        def revalidate(self, *args, **kwargs):
            raise AssertionError("safe stop must not revalidate")

    _handle_unit("synthetic-child", unit, gate=_Gate(),
                 validator=_Validator(), runner=_Runner(),
                 pinned_staging="", guarded=(), schema_version="",
                 approval_reader=None, entries=entries, audits=audits,
                 authorized_staging="", synthesis=FailingSynthesis(),
                 details=[])
    assert len(entries) == 1
    entry = entries[0]
    assert entry.outcome == "llm_safe_stop"
    assert entry.reason in ("llm_unavailable", "llm_timeout",
                            "llm_invalid_output", "llm_privacy_declined")
    assert entry.query_text.strip() != ""
    assert entry.proposal_reason.strip() != ""
    assert entry.classification_type == "Observation"
    assert entry.approval_approved is False
    assert entry.execution_executed is False
    assert entry.artifact_reference == ""
