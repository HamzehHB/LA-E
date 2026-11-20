"""Integrated pipeline tests: ordering, boundaries, inspectability."""
import dataclasses
from typing import Any
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.orchestration import (
    STAGE_ORDER, EvidenceFirstPipeline)
from src.living_authenticity.knowledge.decision import (
    FilterOutcome, KnowledgeFilter)
from src.living_authenticity.knowledge.decision.proposal.outcome import PROPOSAL_ACTIONS


def _unit(uid="q1", text="A calm morning observation about attention and focus"):
    return KnowledgeUnit(id=uid, source="inbox", original_text=text,
        meaning=text, cleaned_text=text, normalized_text=text,
        context="", proposed_type="", position=1)


def _other(uid="c1", text="A calm morning observation about attention and focus"):
    return KnowledgeUnit(id=uid, source="vault", original_text=text,
        meaning=text, cleaned_text=text, normalized_text=text,
        context="", proposed_type="", position=2)




from tests._test_core_helpers import build_test_validated_core


_SHARED_VAL_CORE = build_test_validated_core(
    [KnowledgeUnit(id="core-1", source="core.md", original_text="Epistemic anchor thesis.",
        meaning="Epistemic anchor thesis.", cleaned_text="Epistemic anchor thesis.",
        normalized_text="epistemic anchor thesis.", context="", proposed_type="Core", position=0,
        is_core=True, core_hash="mock_test_core_hash", core_source_path="core.md")],
    source_path="core.md", core_hash="mock_test_core_hash"
)


def _core_unit():
    return _SHARED_VAL_CORE.units[0]


def _pipeline_with_core(core_u=None, retriever=None):
    val_core = _SHARED_VAL_CORE if core_u is None else build_test_validated_core([core_u], source_path=core_u.source, core_hash=core_u.core_hash)
    if retriever is not None:
        return EvidenceFirstPipeline(validated_core=val_core, retriever=retriever)
    return EvidenceFirstPipeline(validated_core=val_core)
def test_stage_order_contract():
    assert STAGE_ORDER == ("ingestion", "retrieval", "comparison", "relation",
        "core", "classification", "proposal", "confidence",
        "knowledge_filter", "representation")
    assert STAGE_ORDER.index("retrieval") < STAGE_ORDER.index("classification")
    assert STAGE_ORDER.index("classification") < STAGE_ORDER.index("proposal")
    assert STAGE_ORDER.index("proposal") < STAGE_ORDER.index("confidence")
    assert STAGE_ORDER.index("confidence") < STAGE_ORDER.index("knowledge_filter")


def test_full_path_exposes_intermediates():
    pipe = _pipeline_with_core()
    result = pipe.run_unit(_unit(), corpus=[_other()], core_units=[_core_unit()])
    assert result is not None
    assert result.retrieval_result is not None
    assert result.comparisons
    assert result.relation_result is not None
    assert result.core_analysis is not None
    assert result.classification is not None
    assert result.proposal is not None
    assert result.confidence is not None
    assert result.filter_outcome is not None
    assert result.generated_note is not None
    assert result.final_outcome == result.proposal.action == result.filter_outcome.recommended_action
    assert result.retrieval_result.query_unit_id == "q1"
    assert result.proposal.query_unit_id == "q1"
    assert result.filter_outcome.query_unit_id == "q1"

def test_classification_not_early_gate_and_cross_type(tmp_path):
    pipe = _pipeline_with_core()
    query = _unit(text="Observation: i observed that people often share calm morning rituals")
    other = _other(text="Research: this study shows calm morning rituals improve focus")
    out = pipe.run_unit(query, corpus=[other], core_units=[_core_unit()])
    assert out is not None
    assert out.retrieval_result.candidates, "cross-type evidence must remain retrievable"
    texts = " ".join(c.text for c in out.retrieval_result.candidates)
    assert "study" in texts


def test_provenance_evidence_identity_preserved():
    pipe = _pipeline_with_core()
    query = _unit()
    other = _other()
    out = pipe.run_unit(query, corpus=[other], core_units=[_core_unit()])
    assert out is not None
    assert out.retrieval_result.query_source == "inbox"
    assert out.proposal.provenance[0].startswith("query:q1@inbox#")
    assert out.comparisons[0].candidate_unit_id == "c1"
    assert out.relation_result.proposals[0].candidate_unit_id == "c1"
    assert out.classification.unit_id == "q1"
    assert out.generated_note.query_unit_id == "q1"


def test_filter_non_authoritative_and_actions_safe():
    pipe = _pipeline_with_core()
    out = pipe.run_unit(_unit(), corpus=[], core_units=[_core_unit()])
    assert out is not None
    assert out.filter_outcome.is_authoritative is False
    assert out.filter_outcome.requires_human_review is True
    assert out.proposal.is_authoritative is False
    assert out.proposal.action in PROPOSAL_ACTIONS
    assert out.final_outcome in PROPOSAL_ACTIONS
    assert out.generated_note.is_authoritative is False
    assert out.generated_note.requires_human_review is True
    assert out.filter_outcome is not None
    payload = dataclasses.asdict(out.filter_outcome)
    for bad in ("approved", "authorized", "execution_allowed", "apply", "commit", "write"):
        assert bad not in payload


def test_run_file_single_input_and_determinism(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("Observation:\nA calm morning observation about attention and focus.\n", encoding="utf-8")
    pipe = _pipeline_with_core()
    first = pipe.run_file(str(note), corpus=[_other()], core_units=[_core_unit()])
    second = pipe.run_file(str(note), corpus=[_other()], core_units=[_core_unit()])
    assert first == second
    assert first.ingestion is not None
    assert first.source == str(note)
    assert len(first.units) >= 1
    assert first.units[0].filter_outcome is not None
    assert first.units[0].generated_note is not None


def test_failures_propagate_and_no_mutation():
    pipe = _pipeline_with_core()
    query = _unit()
    snapshot = dataclasses.astuple(query)
    try:
        pipe.run_unit("bad")  # type: ignore
        raise AssertionError("expected TypeError")
    except TypeError:
        pass
    out = pipe.run_unit(query, corpus=[], core_units=[_core_unit()])
    assert dataclasses.astuple(query) == snapshot
    filt = KnowledgeFilter()
    try:
        filt.filter(query, "bad")  # type: ignore
        raise AssertionError("expected TypeError")
    except TypeError:
        pass
    bad = FilterOutcome(query_unit_id="q", query_source="s", query_position=1,
        verdict="bogus", recommended_action="MERGE")
    assert bad.verdict == "hold_for_review"
    assert bad.recommended_action == "NEEDS_REVIEW"


def test_entry_point_reaches_integrated_pipeline():
    from src.living_authenticity.main import main
    pipeline = main()
    assert isinstance(pipeline, EvidenceFirstPipeline)
    assert pipeline.stage_order.index("retrieval") < pipeline.stage_order.index("classification")
    assert pipeline.ingestion.classifier is None


def test_retriever_internal_type_error_not_retried_or_masked():
    """When a retriever accepts observer but raises TypeError internally,
    the error must NOT be masked and retrieve must NOT be called twice.
    """
    import pytest

    call_count = 0

    class BuggyRetriever:
        def retrieve(self, query, corpus=(), observer=None):
            nonlocal call_count
            call_count += 1
            raise TypeError("internal bug inside retriever: unsupported operand type(s)")

    pipe = EvidenceFirstPipeline(retriever=BuggyRetriever())
    with pytest.raises(TypeError) as excinfo:
        pipe.run_unit(_unit(), corpus=[_other()])

    assert "internal bug inside retriever" in str(excinfo.value)
    assert call_count == 1


def test_orchestrator_set_validated_core_surfaces_retriever_setter_failure():
    """When retriever.set_validated_core() raises an error, orchestrator must surface it immediately."""
    import pytest

    class FailingRetriever:
        def retrieve(self, query, corpus=(), **kwargs):
            return []
        def set_validated_core(self, validated_core: Any = None):
            if validated_core is not None:
                raise RuntimeError("Retriever core validation setter failure")

    retriever = FailingRetriever()
    pipe = EvidenceFirstPipeline(retriever=retriever)
    with pytest.raises(RuntimeError, match="Retriever core validation setter failure"):
        pipe.set_validated_core(_SHARED_VAL_CORE)


def test_runner_run_bounded_integration_surfaces_pipe_set_validated_core_failure(tmp_path):
    """When pipe.set_validated_core() raises an error, run_bounded_integration must surface the error."""
    import pytest
    from src.living_authenticity.knowledge.integration.runner import run_bounded_integration

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "item.md").write_text("# Item\nContent", encoding="utf-8")
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    class FailingPipe(EvidenceFirstPipeline):
        def set_validated_core(self, validated_core: Any = None):
            if validated_core is not None:
                raise RuntimeError("Pipeline core propagation failure")

    pipe = FailingPipe()

    with pytest.raises(RuntimeError, match="Pipeline core propagation failure"):
        run_bounded_integration(
            str(source_dir),
            str(staging_dir),
            pipeline=pipe,
            validated_core=_SHARED_VAL_CORE,
            core_units=[_core_unit()],
            allow_test_output=True,
        )
