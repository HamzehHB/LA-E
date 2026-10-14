"""Audit / traceability record tests (synthetic data only)."""
import dataclasses

import pytest

from src.living_authenticity.knowledge.governance.approval.explicit_gate import ExplicitApprovalGate
from src.living_authenticity.knowledge.governance.audit import (
    AuditRecord,
    build_audit_record,
    record_audit_for_execution,
)
from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment
from src.living_authenticity.knowledge.governance.execution import execute_create
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.output.evidence_note import DefaultOutputGenerator
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.governance.revalidation import revalidate_proposal


def _query():
    return KnowledgeUnit(
        id="u1", source="src", original_text="hello",
        meaning="hello", position=0,
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


def _confidence():
    return ConfidenceAssessment(
        query_unit_id="u1", query_source="src", query_position=0,
        proposal_action="CREATE", level="weak",
    )


def _chain(answer="y"):
    gate = ExplicitApprovalGate()
    query = _query()
    prop = _proposal()
    conf = _confidence()
    request = gate.build_request(query, prop, conf)
    outcome = gate.decide(request, answer, timestamp="2026-01-01T00:00:00+00:00")
    revalidation = revalidate_proposal(request, outcome, prop, conf)
    note = DefaultOutputGenerator().generate(query, proposal=prop, confidence=conf)
    return (request, outcome, prop, conf, note, revalidation)


def test_valid_audit_record_success_path(tmp_path):
    request, outcome, prop, conf, note, revalidation = _chain()
    execution = execute_create(request, outcome, prop, conf, note, tmp_path)
    assert execution.executed is True
    record = build_audit_record(request, outcome, revalidation, execution)
    assert isinstance(record, AuditRecord)
    assert record.proposal_hash == request.proposal_hash
    assert record.approval_proposal_hash == outcome.proposal_hash
    assert record.approval_timestamp == "2026-01-01T00:00:00+00:00"
    assert record.revalidation_valid is True
    assert record.execution_executed is True
    assert record.artifact_reference == request.proposal_hash + ".md"
def test_lifecycle_helper_records_same_flow(tmp_path):
    request, outcome, prop, conf, note, revalidation = _chain()
    execution = execute_create(request, outcome, prop, conf, note, tmp_path)
    record = record_audit_for_execution(request, outcome, revalidation, execution, prop)
    assert record.proposal_hash == request.proposal_hash
    assert record.execution_proposal_hash == request.proposal_hash
    assert record.identity_consistent is True
    assert record.provenance == tuple(prop.provenance)
    assert record.outcome == "executed"


def test_audit_writer_routes_operational_errors_to_errors_partition(tmp_path):
    """Operational failures land in errors/; executed CREATE in passed/.

    Proves the three-way partition without touching any real destination:
    everything uses a synthetic tmp_path root created by the writer.
    """
    import json

    from src.living_authenticity.knowledge.governance.audit import (
        ERRORS_SUBDIR,
        AuditRecord,
        AuditWriter,
    )

    writer = AuditWriter(str(tmp_path))
    assert writer.errors_dir.endswith(ERRORS_SUBDIR)

    executed = AuditRecord(
        record_id="rec-exec", run_id="run-1", proposal_hash="h1",
        execution_executed=True, terminal_outcome="executed",
    )
    rejected = AuditRecord(
        record_id="rec-rej", run_id="run-1", proposal_hash="h2",
        execution_executed=False, terminal_outcome="rejected",
    )
    infra = AuditRecord(
        record_id="rec-err", run_id="run-1", proposal_hash="h3",
        execution_executed=False, terminal_outcome="llm_safe_stop",
    )
    cancelled = AuditRecord(
        record_id="rec-can", run_id="run-1", proposal_hash="",
        execution_executed=False, terminal_outcome="cancelled",
    )

    receipts = [writer.append(r) for r in (executed, rejected, infra, cancelled)]
    assert [r["storage_category"] for r in receipts] == [
        "passed", "failed", "errors", "errors"]

    def _read(subdir):
        path = tmp_path / subdir / "audit.jsonl"
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert [r["record_id"] for r in _read("passed")] == ["rec-exec"]
    assert [r["record_id"] for r in _read("failed")] == ["rec-rej"]
    assert {r["record_id"] for r in _read("errors")} == {"rec-err", "rec-can"}


def test_audit_writer_rejects_colliding_partitions(tmp_path):
    """passed/failed/errors must resolve to three distinct directories."""
    from src.living_authenticity.knowledge.governance.audit import AuditWriter

    with pytest.raises(ValueError):
        AuditWriter(str(tmp_path),
                    passed_dir=str(tmp_path / "x"),
                    failed_dir=str(tmp_path / "x"),
                    errors_dir=str(tmp_path / "x"))

    request, outcome, prop, conf, _n, revalidation = _chain()
    record = build_audit_record(request, outcome, revalidation, None, prop)
    assert record.provenance == tuple(prop.provenance)
    assert record.execution_attempted is False
    absent = build_audit_record(request, outcome, revalidation, None)
    assert absent.provenance == ()


def test_mismatched_identities_stay_traceable():
    request, outcome, prop, conf, _n, revalidation = _chain()
    other_execution_chain = _chain()
    other_request, _o, _p, _c, _n2, _r = other_execution_chain
    assert other_request.proposal_hash == request.proposal_hash
    import dataclasses as _dc
    from src.living_authenticity.knowledge.governance.execution.outcome import ExecutionResult
    forged = ExecutionResult(
        attempted=True, permitted=False, executed=False,
        proposal_hash="forged-hash", action="CREATE",
        destination=request.destination, artifact_path="",
        reason="forged", failed_check="approval_binding",
        revalidation_valid=False, provenance=tuple(prop.provenance),
    )
    record = build_audit_record(request, outcome, revalidation, forged, prop)
    assert record.execution_proposal_hash == "forged-hash"
    assert record.proposal_hash == request.proposal_hash
    assert record.identity_consistent is False
    assert record.execution_executed is False
    assert record.outcome == "rejected"
    assert _dc.asdict(record)["execution_proposal_hash"] == "forged-hash"


def test_failure_states_not_collapsed(tmp_path):
    request, outcome, prop, conf, note, revalidation = _chain()
    not_run = build_audit_record(request, outcome, None, None, prop)
    assert not_run.revalidation_performed is False
    assert not_run.execution_attempted is False
    assert not_run.outcome == "not_executed"
    failed = revalidate_proposal(request, outcome, _proposal(destination="Other"), conf)
    failed_rev = build_audit_record(request, outcome, failed, None, prop)
    assert failed_rev.revalidation_performed is True
    assert failed_rev.revalidation_valid is False
    assert failed_rev.outcome == "revalidation_failed"
    rejected_chain = _chain(answer="n")
    req2, out2, prop2, conf2, note2, rev2 = rejected_chain
    rejected_exec = execute_create(req2, out2, prop2, conf2, note2, tmp_path)
    assert rejected_exec.attempted is True and rejected_exec.executed is False
    rejected = build_audit_record(req2, out2, rev2, rejected_exec, prop2)
    assert rejected.execution_attempted is True
    assert rejected.execution_executed is False
    assert rejected.outcome == "rejected"


def test_references_stay_inert_data(tmp_path):
    request, outcome, prop, conf, note, revalidation = _chain()
    execution = execute_create(request, outcome, prop, conf, note, tmp_path)
    record = build_audit_record(request, outcome, revalidation, execution, prop)
    assert record.artifact_reference == request.proposal_hash + ".md"
    assert "/" not in record.artifact_reference
    assert "\\" not in record.artifact_reference
    assert ".." not in record.artifact_reference
    before = sorted(p.name for p in tmp_path.iterdir())
    assert before == [record.artifact_reference]
    assert record.outcome == "executed"
    hostile_request = dataclasses.replace(request, destination="../../vault/secret.md")
    hostile = build_audit_record(hostile_request, outcome, revalidation, None, prop)
    assert hostile.destination == "../../vault/secret.md"
    assert hostile.artifact_reference == ""
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert hostile.outcome in ("not_executed", "revalidation_failed", "not_approved")


def test_identity_and_bindings_preserved():
    request, outcome, prop, conf, _n, revalidation = _chain()
    record = build_audit_record(request, outcome, revalidation, None)
    assert record.proposal_hash == request.proposal_hash
    assert record.query_unit_id == request.query_unit_id
    assert record.proposal_action == "CREATE"
    assert record.approval_approved is True
    assert record.approval_proposal_hash == request.proposal_hash
    assert record.approval_query_unit_id == request.query_unit_id
    assert record.revalidation_performed is True
    assert record.revalidation_valid == revalidation.valid
    assert record.revalidation_failed_check == revalidation.failed_check
    assert record.revalidation_reason == revalidation.reason
def test_execution_result_preserved(tmp_path):
    request, outcome, prop, conf, note, revalidation = _chain()
    execution = execute_create(request, outcome, prop, conf, note, tmp_path)
    record = build_audit_record(request, outcome, revalidation, execution)
    assert record.execution_attempted == execution.attempted
    assert record.execution_permitted == execution.permitted
    assert record.execution_executed == execution.executed
    assert record.execution_reason == execution.reason
    assert record.destination == execution.destination
    assert record.provenance == tuple(prop.provenance)


def test_rejected_execution_represented(tmp_path):
    request, outcome, prop, conf, note, revalidation = _chain(answer="n")
    execution = execute_create(request, outcome, prop, conf, note, tmp_path)
    assert execution.executed is False
    assert list(tmp_path.iterdir()) == []
    record = build_audit_record(request, outcome, revalidation, execution)
    assert record.approval_approved is False
    assert record.outcome == "rejected"
    assert record.artifact_reference == ""


def test_revalidation_failure_represented():
    gate = ExplicitApprovalGate()
    query = _query()
    prop = _proposal()
    conf = _confidence()
    request = gate.build_request(query, prop, conf)
    outcome = gate.decide(request, "y", timestamp="t")
    changed = _proposal(destination="Other/place.md")
    failed = revalidate_proposal(request, outcome, changed, conf)
    assert failed.valid is False
    record = build_audit_record(request, outcome, failed, None)
    assert record.revalidation_valid is False
    assert record.revalidation_failed_check == "destination"
    assert record.execution_attempted is False
    assert record.outcome == "revalidation_failed"


def test_audit_cannot_authorize_or_claim():
    request, outcome, _p, _c, _n, revalidation = _chain(answer="n")
    record = build_audit_record(request, outcome, revalidation, None)
    assert record.approval_approved is False
    assert not hasattr(record, "approve")
    assert not hasattr(record, "decide")
    assert not hasattr(record, "execute")
    assert record.outcome != "executed"
    assert record.execution_executed is False
    assert record.artifact_reference == ""
    gate = ExplicitApprovalGate()
    other = gate.build_request(_query(), _proposal(title="Other"), _confidence())
    assert record.proposal_hash != other.proposal_hash


def test_audit_no_side_effects_no_secrets(tmp_path):
    request, outcome, _p, _c, _n, revalidation = _chain()
    before = sorted(p.name for p in tmp_path.iterdir())
    record = build_audit_record(request, outcome, revalidation, None)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    blob = repr(dataclasses.asdict(record))
    from src.living_authenticity.security import contains_secret
    assert contains_secret(blob) is False
    import pathlib
    import src.living_authenticity.knowledge.governance.audit.outcome as mod
    source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    for bad in ("open(", "write_text", "mkdir", "subprocess", "socket", "pickle"):
        assert bad not in source


def test_deterministic_and_immutable():
    request, outcome, _p, _c, _n, revalidation = _chain()
    first = build_audit_record(request, outcome, revalidation, None)
    second = build_audit_record(request, outcome, revalidation, None)
    assert first == second
    assert first.is_authoritative is False
    assert first.requires_human_review is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.proposal_hash = "x"  # type: ignore
    with pytest.raises(TypeError):
        build_audit_record("nope", outcome)  # type: ignore
    with pytest.raises(TypeError):
        build_audit_record(request, "nope")  # type: ignore
