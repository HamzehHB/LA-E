"""End-to-end security invariant tests (synthetic, deterministic).

End-to-end proofs over the integrated loop: analytical output cannot
cross into authority, adversarial or degenerate input fails bounded,
and the controlled-execution boundary stays CREATE-only, approval-bound,
evidence-bound, and staging-confined under hostile conditions.
"""
import dataclasses
import pathlib

import pytest

from src.living_authenticity.knowledge.analysis.classification.result import (
    ClassificationResult,
)
from src.living_authenticity.knowledge.decision.confidence.outcome import (
    ConfidenceAssessment,
)
from src.living_authenticity.knowledge.decision.filter_outcome import FilterOutcome
from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
    ExplicitApprovalGate,
)
from src.living_authenticity.knowledge.governance.audit import build_audit_record
from src.living_authenticity.knowledge.governance.execution import execute_create
from src.living_authenticity.knowledge.governance.execution.outcome import (
    ExecutionResult,
)
from src.living_authenticity.knowledge.governance.revalidation import (
    revalidate_proposal,
)
from src.living_authenticity.knowledge.ingestion.cleaning.cleaner import Cleaner
from src.living_authenticity.knowledge.ingestion.extraction.extractor_registry import (
    ExtractorRegistry,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.ingestion.chunking.chunker_registry import (
    ChunkerRegistry,
)
from src.living_authenticity.knowledge.ingestion.metadata.extractor import (
    MetadataExtractor,
)
from src.living_authenticity.knowledge.ingestion.parser.obsidian_parser import (
    ObsidianParser,
)
from src.living_authenticity.knowledge.ingestion.pipeline import IngestionPipeline
from src.living_authenticity.knowledge.ingestion.readers.reader_registry import (
    ReaderRegistry,
)
from src.living_authenticity.knowledge.ingestion.batch import BatchIngestor
from src.living_authenticity.knowledge.orchestration import (
    EvidenceFirstPipeline,
    IntegratedUnitResult,
)
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.output.evidence_note import (
    DefaultOutputGenerator,
)
from src.living_authenticity.security import PathBoundary


def _query(uid="u1", source="src", position=0):
    text = "A calm morning observation about attention and focus"
    return KnowledgeUnit(
        id=uid, source=source, original_text=text, meaning=text,
        cleaned_text=text, normalized_text=text, context="",
        proposed_type="", position=position,
    )


def _proposal(**over):
    base = {
        "query_unit_id": "u1", "query_source": "src",
        "query_position": 0, "action": "CREATE",
        "title": "Title One", "destination": "Inbox suggestion",
        "reason": "evidence supports a new draft",
        "provenance": ("query:u1@src#0",),
    }
    base.update(over)
    return Proposal(**base)


def _confidence(level="weak", action="CREATE"):
    return ConfidenceAssessment(
        query_unit_id="u1", query_source="src", query_position=0,
        proposal_action=action, level=level,
    )


def _chain(dest="Inbox suggestion", answer="y", level="weak"):
    """Full governance chain: request -> outcome -> note -> revalidation."""
    gate = ExplicitApprovalGate()
    query = _query()
    prop = _proposal(destination=dest)
    conf = _confidence(level=level, action=prop.action)
    request = gate.build_request(query, prop, conf)
    outcome = gate.decide(request, answer)
    note = DefaultOutputGenerator().generate(query, proposal=prop, confidence=conf)
    revalidation = revalidate_proposal(request, outcome, prop, conf)
    return request, outcome, prop, conf, note, revalidation


def _ingestion_pipeline():
    return IngestionPipeline(
        reader_registry=ReaderRegistry(),
        chunker_registry=ChunkerRegistry(),
        extractor_registry=ExtractorRegistry(),
        cleaner=Cleaner(),
        metadata_extractor=MetadataExtractor(),
        parser=ObsidianParser(),
    )


# === invariants (continued below) ===

def test_certain_classification_cannot_authorize_execution(tmp_path):
    """Invariant: Classification cannot authorize execution."""
    query = KnowledgeUnit(
        id="u1", source="src",
        original_text="Core:\nA governing principle of calm attention.",
        meaning="A governing principle of calm attention.",
        cleaned_text="A governing principle of calm attention.",
        normalized_text="A governing principle of calm attention.",
        context="Core", proposed_type="", position=0,
    )
    result = EvidenceFirstPipeline().run_unit(query, corpus=[], core_units=[])
    assert result.classification.is_certain is True
    assert result.classification.proposed_type == "Core"
    fields = {f.name for f in dataclasses.fields(ClassificationResult)}
    for forbidden in ("approved", "authorized", "execution_allowed",
                      "human_approved", "is_authoritative"):
        assert forbidden not in fields
    gate = ExplicitApprovalGate()
    conf = _confidence(action=result.proposal.action)
    request = gate.build_request(query, result.proposal, conf)
    outcome = gate.decide(request, "n")  # the human did not approve
    execution = execute_create(
        request, outcome, result.proposal, conf, result.generated_note, tmp_path)
    assert execution.executed is False
    assert list(tmp_path.iterdir()) == []


def test_high_confidence_cannot_bypass_missing_approval(tmp_path):
    """Invariant: Confidence cannot authorize execution."""
    request, outcome, prop, conf, note, _rv = _chain(
        level="sufficiently_supported", answer="n")
    assert conf.level == "sufficiently_supported"
    result = execute_create(request, outcome, prop, conf, note, tmp_path)
    assert result.executed is False and result.permitted is False
    assert result.failed_check == "approval_binding"
    assert list(tmp_path.iterdir()) == []


def test_removing_confidence_evidence_invalidates_execution(tmp_path):
    """Approval is bound to the exact evidence shown; removing it must
    invalidate execution even though the approval record itself exists."""
    request, outcome, prop, conf, note, _rv = _chain()
    first = execute_create(request, outcome, prop, conf, note, tmp_path)
    assert first.executed is True
    (tmp_path / first.artifact_path).unlink()
    stripped = execute_create(request, outcome, prop, None, note, tmp_path)
    assert stripped.executed is False
    assert "confidence" in stripped.reason
    assert list(tmp_path.iterdir()) == []


# Hostile destination forms: traversal, absolute, UNC, and escape attempts
HOSTILE_DESTINATIONS = (
    "../../escaped.md",
    "..\\..\\escaped.md",
    "C:/absolute/escaped.md",
    "\\\\server\\share\\escaped.md",
    "/absolute/unix/escaped.md",
    "Staging/../outside.md",
)


def test_execution_destination_forms_cannot_leave_staging(tmp_path):
    """Invariant: CREATE remains confined to the approved staging
    boundary; the proposed destination is inert data for every hostile
    path form."""
    for destination in HOSTILE_DESTINATIONS:
        # Each destination form is tested in its own clean staging subdirectory
        staging_dir = tmp_path / f"staging_{abs(hash(destination))}"
        staging_dir.mkdir()
        request, outcome, prop, conf, note, _rv = _chain(dest=destination)
        result = execute_create(request, outcome, prop, conf, note, staging_dir)
        assert result.executed is True
        assert result.destination == destination  # preserved verbatim as data
        artifact = request.proposal_hash + ".md"
        assert [p.name for p in staging_dir.iterdir()] == [artifact]
        assert (staging_dir / artifact).is_file()
        # No escape at any enclosing level for any hostile form
        for enclosing in (staging_dir, tmp_path, tmp_path.parent):
            assert not (enclosing / "escaped.md").exists()
            assert not (enclosing / "outside.md").exists()


def test_core_conflict_resolves_conservatively_and_stays_non_executable(tmp_path):
    """Core analysis cannot modify Core; a Core-adjacent unit resolves
    conservatively to NEEDS_REVIEW, and even explicit approval cannot
    make that non-CREATE proposal execute."""
    core_text = "Calmness is the foundational principle of attention."
    core = KnowledgeUnit(
        id="core-1", source="vault/Core.md", original_text=core_text,
        meaning=core_text, cleaned_text=core_text, normalized_text=core_text,
        context="Core", proposed_type="Core", position=0,
    )
    query_text = "Calmness challenges the foundational principle of attention."
    query = KnowledgeUnit(
        id="u1", source="inbox", original_text=query_text, meaning=query_text,
        cleaned_text=query_text, normalized_text=query_text, context="",
        proposed_type="", position=1,
    )
    result = EvidenceFirstPipeline().run_unit(query, corpus=[], core_units=[core])
    assert result.core_analysis.relevance == "potentially_relevant"
    assert result.core_analysis.is_authoritative is False
    assert result.proposal.action == "NEEDS_REVIEW"
    gate = ExplicitApprovalGate()
    conf = _confidence(action="NEEDS_REVIEW")
    request = gate.build_request(query, result.proposal, conf)
    outcome = gate.decide(request, "y")  # human approves -- still not executable
    execution = execute_create(
        request, outcome, result.proposal, conf, result.generated_note, tmp_path)
    assert execution.executed is False
    assert execution.failed_check == "action_eligibility"
    assert list(tmp_path.iterdir()) == []


def test_analytical_outcomes_cannot_substitute_approval(tmp_path):
    """Only an ApprovalOutcome produced by the explicit human gate can
    stand at the execution boundary; analytical records are rejected by
    type, so no analytical stage output can create an execution path."""
    request, _outcome, prop, conf, note, _rv = _chain()
    substitutes = (
        None,
        prop,
        conf,
        FilterOutcome(query_unit_id="u1", query_source="src", query_position=0),
        ClassificationResult(unit_id="u1", source="src", position=0),
        ExecutionResult(),
    )
    for bad in substitutes:
        with pytest.raises(TypeError):
            execute_create(request, bad, prop, conf, note, tmp_path)
        with pytest.raises(TypeError):
            revalidate_proposal(request, bad, prop, conf)
    assert list(tmp_path.iterdir()) == []


def test_audit_never_claims_success_after_failed_execution(tmp_path, monkeypatch):
    """Interrupted execution: no false success in the result, no false
    success in the audit record, approval fact neither invented nor
    erased, and the true revalidation state preserved."""
    import os
    request, outcome, prop, conf, note, revalidation = _chain()

    def _boom(*args, **kwargs):
        raise OSError("simulated staging write failure")

    monkeypatch.setattr(pathlib.Path, "write_text", _boom)
    monkeypatch.setattr(os, "write", _boom)
    execution = execute_create(request, outcome, prop, conf, note, tmp_path)
    monkeypatch.undo()
    assert execution.executed is False
    record = build_audit_record(request, outcome, revalidation, execution)
    assert record.outcome != "executed"
    assert record.execution_executed is False
    assert record.artifact_reference == ""
    assert record.approval_approved is True
    assert record.revalidation_valid is True
    assert list(tmp_path.iterdir()) == []


def test_oversized_input_stays_bounded_and_deterministic(tmp_path):
    """Oversized input: bounded, deterministic, fully traceable 0..n
    behavior; every extracted unit is analyzed exactly once and no
    filesystem artifact appears outside the synthetic input file."""
    paragraph = "Observation:\nPeople often lose the ability to enjoy calmness.\n"
    big = paragraph * 1200
    note = tmp_path / "big.md"
    note.write_text(big, encoding="utf-8")
    pipeline = EvidenceFirstPipeline()
    first = pipeline.run_file(str(note))
    second = pipeline.run_file(str(note))
    assert first == second
    assert first.ingestion is not None
    assert len(first.ingestion.knowledge_units) == len(first.units)
    assert len(first.units) >= 1
    for item in first.units:
        assert isinstance(item, IntegratedUnitResult)
        assert item.query_unit.id.startswith("ku-")
        assert item.proposal.is_authoritative is False
        assert item.filter_outcome.requires_human_review is True
    assert [p.name for p in tmp_path.iterdir()] == ["big.md"]


def test_invalid_encoding_fails_bounded_in_batch(tmp_path):
    """Invalid encoding: one bounded, inspectable failure; sibling files
    still process; no partially ingested result is fabricated."""
    good = tmp_path / "good.md"
    good.write_text(
        "Observation:\nPeople often lose the ability to enjoy calmness.\n",
        encoding="utf-8")
    bad = tmp_path / "broken.md"
    bad.write_bytes(b"\xff\xfe\x00broken-encoding")
    outcomes = BatchIngestor(
        _ingestion_pipeline(), PathBoundary(tmp_path)
    ).ingest_many([str(good), str(bad), str(good)])
    assert [o.status for o in outcomes] == ["success", "failed", "success"]
    assert outcomes[1].result is None
    assert outcomes[1].error
    assert outcomes[0].is_success and outcomes[2].is_success

def test_hostile_approval_variations_cannot_reach_execution(tmp_path):
    """End-to-end hostile approval verification:
    Forged, transferred, stale, default-rejected, or invalid inputs
    must fail revalidation/execution and never write to staging.
    """
    gate = ExplicitApprovalGate()
    query_a = _query(uid="u1")
    prop_a = _proposal(query_unit_id="u1", title="Title A")
    conf_a = _confidence(action="CREATE")
    req_a = gate.build_request(query_a, prop_a, conf_a)
    note_a = DefaultOutputGenerator().generate(query_a, proposal=prop_a, confidence=conf_a)

    query_b = _query(uid="u2")
    prop_b = _proposal(query_unit_id="u2", title="Title B")
    conf_b = _confidence(action="CREATE")
    req_b = gate.build_request(query_b, prop_b, conf_b)
    outcome_b_approved = gate.decide(req_b, "y")

    # 1. Transferable approval attempt: outcome from proposal B applied to proposal A
    res_transfer = execute_create(req_a, outcome_b_approved, prop_a, conf_a, note_a, tmp_path)
    assert res_transfer.executed is False
    assert res_transfer.failed_check == "approval_binding"
    assert list(tmp_path.iterdir()) == []

    # 2. Forged hash approval: approval outcome manually forged with incorrect hash
    forged_outcome = dataclasses.replace(outcome_b_approved, proposal_hash="0" * 64)
    res_forged = execute_create(req_b, forged_outcome, prop_b, conf_b, note_a, tmp_path)
    assert res_forged.executed is False
    assert res_forged.failed_check in ("approval_binding", "proposal_identity")
    assert list(tmp_path.iterdir()) == []

    # 3. Default rejection and non-accepted inputs in end-to-end chain
    for non_accepted in ("", "  ", "n", "no", "NO", "1", "true", "ok", "APPROVED", "yes please"):
        out_reject = gate.decide(req_a, non_accepted)
        res_reject = execute_create(req_a, out_reject, prop_a, conf_a, note_a, tmp_path)
        assert res_reject.executed is False
        assert res_reject.failed_check == "approval_binding"
        assert list(tmp_path.iterdir()) == []

    # 4. Stale approval: approved, but live proposal content modified before execution
    outcome_a_approved = gate.decide(req_a, "yes")
    tampered_prop = dataclasses.replace(prop_a, title="Tampered Title After Approval")
    res_stale = execute_create(req_a, outcome_a_approved, tampered_prop, conf_a, note_a, tmp_path)
    assert res_stale.executed is False
    assert res_stale.failed_check in ("proposal_contents", "proposal_identity")
    assert list(tmp_path.iterdir()) == []
