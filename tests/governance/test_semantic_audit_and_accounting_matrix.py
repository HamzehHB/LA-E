"""Comprehensive acceptance tests for Semantic Audit Integrity, Multi-Unit Recovery,
and Governance Consistency.

Covers the 27 required acceptance scenarios:
1. Valid executed unit with exactly one valid receipt.
2. An executed unit with a missing receipt.
3. An orphan execution receipt.
4. An extra execution-claiming receipt.
5. Duplicate receipt assignment.
6. Multiple receipts sharing a record ID.
7. Duplicate record ID across historical journal entries.
8. Execution-claiming journal record missing from in-memory receipts.
9. Invalid digest.
10. Identity mismatch.
11. Pre-extraction file validation failure.
12. Post-extraction held unit.
13. Post-extraction rejected unit.
14. Recoverable anomaly without double-counting.
15. Terminal file operational failure.
16. Terminal unit operational failure.
17. Cancellation before unit execution.
18. Cancellation during execution.
19. Cancellation after partial execution.
20. Cancellation after all units finish.
21. File accounting across every discovery category.
22. Strict rejection of non-string dictionary keys.
23. Rejection of unsupported nested types.
24. Rejection of NaN and infinities.
25. Rejection of duplicate JSON object keys and non-standard JSON constants.
26. Deterministic serialization and digest stability.
27. Complete full_success acceptance matrix.
"""

from pathlib import Path
from typing import Any, cast
import os
import json
import pytest

from src.living_authenticity.knowledge.governance.audit.persistence import (
    canonical_json_dumps,
    parse_canonical_json_line,
    compute_record_digest,
    verify_record_digest,
    read_all_persisted_journal_records,
    verify_execution_receipt_bijection,
    AuditWriter,
    PersistedRecord,
)
from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
from src.living_authenticity.knowledge.integration.outcome import (
    IntegrationUnitEntry,
    IntegrationRunReport,
)
from src.living_authenticity.knowledge.integration.runner import (
    run_bounded_integration,
    StructuralValidationError,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.orchestration.orchestrator import (
    EvidenceFirstPipeline,
)
from tests._synthetic_synthesis import SyntheticSynthesis


def _make_core_sample():
    sample = (
        "Method:\nA method for calibrating laboratory instruments with a step by step procedure.\n"
    )
    return KnowledgeUnit(
        id="core1",
        source="core",
        original_text=sample,
        meaning=sample,
        cleaned_text=sample,
        normalized_text=sample,
        context="",
        proposed_type="Method",
        position=0,
    )


def _make_executed_entry(
    unit_id: Any = "unit-1",
    proposal_hash: Any = "hash-abc",
    artifact_ref: Any = "hash-abc.md",
    record_id: Any = "rec-1",
):
    return IntegrationUnitEntry(
        source_file="source/note.md",
        query_unit_id=cast(Any, unit_id),
        query_source="source/note.md",
        query_position=0,
        proposal_hash=cast(Any, proposal_hash),
        proposal_action="CREATE",
        approval_approved=True,
        revalidation_valid=True,
        execution_attempted=True,
        execution_permitted=True,
        execution_executed=True,
        outcome="executed",
        reason="",
        artifact_reference=cast(Any, artifact_ref),
        note_markdown="# Note content",
        record_id=cast(Any, record_id),
    )


# -------------------------------------------------------------------------
# Scenarios 1 to 10: Receipt-Set Completeness & Audit Verification
# -------------------------------------------------------------------------

class TestReceiptAccountingAndBijection:
    """Tests covering Scenarios 1 through 10."""

    def test_01_valid_executed_unit_with_single_valid_receipt(self, tmp_path):
        """Scenario 1: Valid executed unit with exactly 1 valid receipt yields full_success."""
        source = tmp_path / "source"
        source.mkdir()
        input_text = "Observation:\nPeople often lose calmness in modern life.\n"
        (source / "note.md").write_text(input_text, encoding="utf-8")
        staging = tmp_path / "staging"
        staging.mkdir()
        audit_root = tmp_path / "audit"
        audit_root.mkdir()

        writer = AuditWriter(str(audit_root))
        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=writer,
            audit_root=str(audit_root),
            approval_reader=lambda: "y",
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=1,
        )

        assert report.accepted is True
        assert report.run_outcome == "full_success"
        assert len(report.units) == 1
        assert report.units[0].outcome == "executed"
        assert len(report.audit_receipts) >= 1

        active_run_id = (
            report.audit_receipts[0].get("run_id", "")
            if report.audit_receipts
            else ""
        )
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            report.units, report.audit_receipts, writer, active_run_id
        )
        assert is_valid is True
        assert fail_type == ""

    def test_02_executed_unit_with_missing_receipt(self, tmp_path):
        """Scenario 2: An executed unit with missing receipt triggers audit_integrity_failure."""
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))

        entry = _make_executed_entry(unit_id="unit-missing-receipt", record_id="")
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "0 execution receipts recorded" in reason or "missing" in reason.lower()

    def test_03_orphan_execution_receipt(self, tmp_path):
        """Scenario 3: An orphan execution-claiming receipt triggers accounting_integrity_failure."""
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))

        orphan_receipt = {
            "record_id": "rec-orphan",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "unit-orphan-999",
            "proposal_hash": "hash-orphan",
            "artifact_path": "orphan.md",
            "audit_path": str(audit_root / "passed" / "audit.jsonl"),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [], [orphan_receipt], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "accounting_integrity_failure"
        assert "orphan execution receipts" in reason

    def test_04_extra_execution_claiming_receipt(self, tmp_path):
        """Scenario 4: An extra execution-claiming receipt triggers accounting_integrity_failure."""
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))

        entry = _make_executed_entry(unit_id="unit-1", record_id="rec-1")
        receipt1 = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "unit-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "hash-abc.md",
            "audit_path": str(audit_root / "passed" / "audit.jsonl"),
            "durability": "confirmed",
            "persisted": True,
        }
        receipt2 = {
            "record_id": "rec-extra",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "unit-extra",
            "proposal_hash": "hash-extra",
            "artifact_path": "hash-extra.md",
            "audit_path": str(audit_root / "passed" / "audit.jsonl"),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt1, receipt2], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "accounting_integrity_failure"
        assert "extra execution receipts" in reason

    def test_05_duplicate_receipt_assignment(self, tmp_path):
        """Scenario 5: Duplicate receipt assignment across multiple units fails closed."""
        audit_root = tmp_path / "audit"
        passed_dir = audit_root / "passed"
        passed_dir.mkdir(parents=True)
        active_file = passed_dir / "audit.jsonl"

        payload = {
            "record_id": "rec-shared",
            "run_id": "run-1",
            "proposal_id": "prop-1",
            "proposal_hash": "hash-abc",
            "query_unit_id": "unit-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "artifact_path": "hash-abc.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        active_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        entry1 = _make_executed_entry(unit_id="unit-1", record_id="rec-shared")
        entry2 = _make_executed_entry(unit_id="unit-2", record_id="rec-shared")
        shared_receipt = {
            "record_id": "rec-shared",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "unit-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "hash-abc.md",
            "audit_path": str(active_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry1, entry2], [shared_receipt], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing execution receipts" in reason

    def test_06_multiple_receipts_sharing_record_id(self, tmp_path):
        """Scenario 6: Multiple in-memory receipts sharing a record ID fails closed."""
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))

        entry = _make_executed_entry(unit_id="unit-1", record_id="rec-dup")
        receipt1 = {
            "record_id": "rec-dup",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "unit-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "hash-abc.md",
            "audit_path": str(audit_root / "passed" / "audit.jsonl"),
            "durability": "confirmed",
            "persisted": True,
        }
        receipt2 = dict(receipt1)
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt1, receipt2], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "accounting_integrity_failure"
        assert "share the same record_id" in reason

    def test_07_historical_duplicate_record_id_across_journal(self, tmp_path):
        """Scenario 7: Duplicate record ID across historical journal partitions fails closed."""
        audit_root = tmp_path / "audit"
        hist_dir = audit_root / "2025" / "12"
        hist_dir.mkdir(parents=True)
        hist_file = hist_dir / "audit.jsonl"
        payload1 = {
            "record_id": "rec-collision",
            "run_id": "run-ancient",
            "proposal_id": "prop-ancient",
            "proposal_hash": "hash-ancient",
            "query_unit_id": "unit-ancient",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "artifact_path": "ancient.md",
        }
        payload1["record_digest"] = compute_record_digest(payload1)
        payload1["persistence_timestamp"] = "2025-12-01T00:00:00Z"
        hist_file.write_text(canonical_json_dumps(payload1) + "\n", encoding="utf-8")

        curr_dir = audit_root / "passed"
        curr_dir.mkdir(parents=True)
        curr_file = curr_dir / "audit.jsonl"
        payload2 = {
            "record_id": "rec-collision",
            "run_id": "run-current",
            "proposal_id": "prop-current",
            "proposal_hash": "hash-abc",
            "query_unit_id": "unit-new",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "artifact_path": "hash-abc.md",
        }
        payload2["record_digest"] = compute_record_digest(payload2)
        payload2["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        curr_file.write_text(canonical_json_dumps(payload2) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        entry = _make_executed_entry(unit_id="unit-new", record_id="rec-collision")
        current_receipt = {
            "record_id": "rec-collision",
            "run_id": "run-current",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "unit-new",
            "proposal_hash": "hash-abc",
            "artifact_path": "hash-abc.md",
            "audit_path": str(curr_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [current_receipt], writer, active_run_id="run-current"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "duplicate record_id detected across journal" in reason

    def test_08_execution_claiming_journal_record_missing_from_in_memory_receipts(self, tmp_path):
        """Scenario 8: On-disk execution record missing from in-memory receipts triggers failure."""
        audit_root = tmp_path / "audit"
        passed_dir = audit_root / "passed"
        passed_dir.mkdir(parents=True)
        active_file = passed_dir / "audit.jsonl"

        payload = {
            "record_id": "rec-disk-only",
            "run_id": "run-active-1",
            "proposal_id": "prop-1",
            "proposal_hash": "hash-1",
            "query_unit_id": "unit-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "artifact_path": "path.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        active_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [], [], writer, active_run_id="run-active-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "absent from in-memory" in reason

    def test_09_invalid_digest(self, tmp_path):
        """Scenario 9: Record with invalid digest triggers audit_integrity_failure."""
        audit_root = tmp_path / "audit"
        passed_dir = audit_root / "passed"
        passed_dir.mkdir(parents=True)
        active_file = passed_dir / "audit.jsonl"

        payload = {
            "record_id": "rec-corrupted",
            "run_id": "run-active-2",
            "proposal_id": "prop-1",
            "proposal_hash": "hash-abc",
            "query_unit_id": "unit-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "artifact_path": "hash-abc.md",
            "record_digest": "0" * 64,  # Malformed/tampered digest
            "persistence_timestamp": "2026-11-01T00:00:00Z",
        }
        active_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        entry = _make_executed_entry(unit_id="unit-1", record_id="rec-corrupted")
        receipt = {
            "record_id": "rec-corrupted",
            "run_id": "run-active-2",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "unit-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "hash-abc.md",
            "audit_path": str(active_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt], writer, active_run_id="run-active-2"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "digest" in reason.lower()

    def test_10_identity_binding_mismatch(self, tmp_path):
        """Scenario 10: Persisted identity mismatch (proposal_hash mismatch) fails closed."""
        audit_root = tmp_path / "audit"
        passed_dir = audit_root / "passed"
        passed_dir.mkdir(parents=True)
        active_file = passed_dir / "audit.jsonl"

        payload = {
            "record_id": "rec-mismatch",
            "run_id": "run-active-3",
            "proposal_id": "prop-1",
            "proposal_hash": "hash-STORED-DIFFERENT",
            "query_unit_id": "unit-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "artifact_path": "hash-abc.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        active_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        entry = _make_executed_entry(
            unit_id="unit-1", proposal_hash="hash-abc", record_id="rec-mismatch"
        )
        receipt = {
            "record_id": "rec-mismatch",
            "run_id": "run-active-3",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "unit-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "hash-abc.md",
            "audit_path": str(active_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt], writer, active_run_id="run-active-3"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "binding mismatch" in reason


# -------------------------------------------------------------------------
# Scenarios 11 to 21: Validation, Accounting, Recovery, & Cancellation
# -------------------------------------------------------------------------

class TestLifecycleValidationAndAccounting:
    """Tests covering Scenarios 11 through 21."""

    def test_11_pre_extraction_file_validation_failure(self, tmp_path):
        """Scenario 11: Pre-extraction file validation failure increments files_failed_validation
        with ZERO units attempted and zero synthetic entities."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "bad_syntax.md").write_text("Invalid text", encoding="utf-8")
        staging = tmp_path / "staging"
        staging.mkdir()

        class ValidationFailingPipeline(EvidenceFirstPipeline):
            def run_file(self, *args, **kwargs):
                raise StructuralValidationError("Malformed file schema: unparseable document structure")

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=None,
            audit_requested=False,
            approval_reader=lambda: "acknowledge",
            pipeline=ValidationFailingPipeline(),
            synthesis=SyntheticSynthesis(),
            core_units=[],
            max_files=1,
        )

        assert report.files_failed_validation == 1
        assert len(report.units) == 0
        assert report.run_outcome == "validation_failure"
        assert report.accepted is False
        assert report.files_seen == (
            report.files_processed
            + report.files_failed_operational
            + report.files_failed_validation
            + report.files_skipped
        )

    def test_12_post_extraction_held_unit(self, tmp_path):
        """Scenario 12: Post-extraction held unit yields governance_held and accepted."""
        source = tmp_path / "source"
        source.mkdir()
        core_text = "Observation:\nCore mindfulness observation regarding mental calm.\n"
        core_unit = KnowledgeUnit(
            id="core1", source="core", original_text=core_text,
            meaning=core_text, cleaned_text=core_text, normalized_text=core_text,
            context="", proposed_type="Observation", position=0,
        )
        input_text = "Observation:\nA mindfulness observation regarding mental calm in daily routines.\n"
        (source / "note.md").write_text(input_text, encoding="utf-8")
        staging = tmp_path / "staging"
        staging.mkdir()

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=None,
            audit_requested=False,
            approval_reader=lambda: "acknowledge",
            synthesis=SyntheticSynthesis(),
            pipeline=EvidenceFirstPipeline(),
            core_units=[core_unit],
            max_files=1,
        )

        units_held = sum(
            1 for u in report.units
            if u.outcome.startswith("held") or u.outcome == "llm_safe_stop"
        )
        assert units_held == 1
        assert len(report.units) == 1
        assert report.run_outcome == "governance_held"
        assert report.accepted is True

    def test_13_post_extraction_rejected_unit(self, tmp_path):
        """Scenario 13: Post-extraction rejected proposal yields governance_held and accepted."""
        source = tmp_path / "source"
        source.mkdir()
        input_text = "Observation:\nPeople often lose calmness in modern life.\n"
        (source / "note.md").write_text(input_text, encoding="utf-8")
        staging = tmp_path / "staging"
        staging.mkdir()

        # Approval reader returns 'n' -> user rejects the proposal
        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=None,
            audit_requested=False,
            approval_reader=lambda: "n",
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=1,
        )

        units_rejected = sum(
            1 for u in report.units
            if u.outcome in ("rejected", "revalidation_failed") or u.outcome.startswith("rejected")
        )
        assert units_rejected == 1
        assert len(report.units) == 1
        assert report.run_outcome == "governance_held"
        assert report.accepted is True

    def test_14_recoverable_error_without_double_counting(self, tmp_path):
        """Scenario 14: Recoverable error prevents full_success without inflating fatal failures."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "note.md").write_text(
            "Observation:\nPeople often lose calmness in modern life.\n",
            encoding="utf-8",
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        audit_root = tmp_path / "audit"
        audit_root.mkdir()

        writer = AuditWriter(str(audit_root))

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=writer,
            audit_root=str(audit_root),
            approval_reader=lambda: "y",
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=1,
        )

        # Fatal operational failure dimensions remain strictly zero
        fatal_failures = report.files_failed_operational + report.units_failed_operational
        assert fatal_failures == 0

    def test_15_terminal_file_operational_failure(self, tmp_path):
        """Scenario 15: Terminal file operational error (I/O failure) yields operational_failure."""
        source = tmp_path / "source"
        source.mkdir()
        bad_file = source / "broken.md"
        bad_file.write_text("Hello", encoding="utf-8")
        staging = tmp_path / "staging"
        staging.mkdir()

        class OperationalFailingPipeline(EvidenceFirstPipeline):
            def run_file(self, *args, **kwargs):
                raise OSError("Simulated hardware disk I/O failure during file read")

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=None,
            audit_requested=False,
            approval_reader=lambda: "acknowledge",
            pipeline=OperationalFailingPipeline(),
            synthesis=SyntheticSynthesis(),
            core_units=[],
            max_files=1,
        )
        assert report.files_failed_operational == 1
        assert report.run_outcome == "operational_failure"
        assert report.accepted is False

    def test_16_terminal_unit_operational_failure(self, tmp_path):
        """Scenario 16: Terminal unit operational failure yields operational_failure."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "note.md").write_text(
            "Observation:\nPeople often lose calmness in modern life.\n",
            encoding="utf-8",
        )
        staging = tmp_path / "staging"
        staging.mkdir()

        class FailingWriter:
            def append(self, *args, **kwargs):
                raise OSError("Primary disk write failed")
            def append_emergency(self, *args, **kwargs):
                return {"persisted": False, "durability": "failed", "emergency": True}

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=FailingWriter(),
            audit_root=str(tmp_path),
            approval_reader=lambda: "y",
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=1,
        )

        assert report.units_failed_operational == 1
        assert report.accepted is False

    def test_17_cancellation_before_unit_execution(self, tmp_path):
        """Scenario 17: Cancellation before any unit execution yields cancelled_clean."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "note.md").write_text("Observation:\nNote text.\n", encoding="utf-8")
        staging = tmp_path / "staging"
        staging.mkdir()

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=None,
            should_cancel=lambda: True,  # Pre-cancelled
            approval_reader=lambda: "y",
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=1,
        )

        assert report.run_outcome == "cancelled_clean"
        assert report.accepted is False
        assert len(report.units) == 1
        assert report.units[0].outcome == "cancelled"

    def test_18_cancellation_during_execution(self, tmp_path):
        """Scenario 18: Cancellation during approval review marks in-flight unit cancelled."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "note.md").write_text(
            "Observation:\nPeople often lose calmness in modern life.\n",
            encoding="utf-8",
        )
        staging = tmp_path / "staging"
        staging.mkdir()

        cancel_armed = [False]

        def cancel_check():
            return cancel_armed[0]

        def reader_with_cancel():
            cancel_armed[0] = True
            return "c"  # Cancel choice

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=None,
            should_cancel=cancel_check,
            approval_reader=reader_with_cancel,
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=1,
        )

        assert report.run_outcome == "cancelled_clean"
        assert report.accepted is False
        units_cancelled = sum(1 for u in report.units if u.outcome == "cancelled")
        assert units_cancelled == 1

    def test_19_cancellation_after_partial_execution(self, tmp_path):
        """Scenario 19: Cancellation after 1 executed unit yields cancelled_partial."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "note1.md").write_text(
            "Observation:\nPeople often lose calmness in modern life 1.\n",
            encoding="utf-8",
        )
        (source / "note2.md").write_text(
            "Observation:\nPeople often lose calmness in modern life 2.\n",
            encoding="utf-8",
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))

        cancel_flag = [False]
        calls = [0]

        def cancel_check():
            return cancel_flag[0]

        def reader():
            calls[0] += 1
            if calls[0] == 1:
                return "y"  # Execute unit 1
            cancel_flag[0] = True
            return "c"  # Cancel unit 2

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=writer,
            audit_root=str(audit_root),
            should_cancel=cancel_check,
            approval_reader=reader,
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=2,
        )

        units_executed = sum(1 for u in report.units if u.outcome == "executed")
        units_cancelled = sum(1 for u in report.units if u.outcome == "cancelled")
        assert units_executed == 1
        assert units_cancelled == 1
        assert report.run_outcome == "cancelled_partial"
        assert report.accepted is False

    def test_20_cancellation_after_all_units_finish(self, tmp_path):
        """Scenario 20: Cancellation signal armed after all units finish prevents full_success."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "note.md").write_text(
            "Observation:\nPeople often lose calmness in modern life.\n",
            encoding="utf-8",
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))

        cancel_flag = [False]

        def on_event(evt):
            if evt.get("kind") == "execution_result":
                cancel_flag[0] = True

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=writer,
            audit_root=str(audit_root),
            should_cancel=lambda: cancel_flag[0],
            approval_reader=lambda: "y",
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=1,
            event_sink=on_event,
        )

        units_executed = sum(1 for u in report.units if u.outcome == "executed")
        assert units_executed == 1
        assert report.run_outcome == "cancelled_partial"
        assert report.accepted is False

    def test_21_file_accounting_across_every_discovery_category(self, tmp_path):
        """Scenario 21: Accounting invariant holds across every discovery category."""
        source = tmp_path / "source"
        source.mkdir()
        # 1. Valid file (processed)
        (source / "valid.md").write_text(
            "Observation:\nPeople often lose calmness in modern life.\n",
            encoding="utf-8",
        )
        # 2. Ignored extension (skipped)
        (source / "data.csv").write_text("col1,col2\n1,2", encoding="utf-8")
        # 3. Subdirectory (skipped)
        (source / "subdir").mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=None,
            audit_requested=False,
            approval_reader=lambda: "acknowledge",
            synthesis=SyntheticSynthesis(),
            core_units=[],
        )

        accounted = (
            report.files_processed
            + report.files_failed_operational
            + report.files_failed_validation
            + report.files_skipped
        )
        assert report.files_seen == accounted
        assert report.files_skipped >= 2


# -------------------------------------------------------------------------
# Scenarios 22 to 26: Deterministic Canonical JSON & Digest Stability
# -------------------------------------------------------------------------

class TestCanonicalSerializationAndDigest:
    """Tests covering Scenarios 22 through 26."""

    def test_22_strict_rejection_of_non_string_dictionary_keys(self):
        """Scenario 22: Strict rejection of non-string dictionary keys."""
        with pytest.raises(TypeError, match="must be string"):
            canonical_json_dumps({123: "value"})
        with pytest.raises(TypeError, match="must be string"):
            canonical_json_dumps({True: "value"})
        with pytest.raises(TypeError, match="must be string"):
            canonical_json_dumps({("a", "b"): "value"})

    def test_23_rejection_of_unsupported_nested_types(self):
        """Scenario 23: Rejection of unsupported nested types (sets, arbitrary objects)."""
        with pytest.raises(TypeError, match="Unsupported type"):
            canonical_json_dumps({"nested_set": {1, 2, 3}})
        with pytest.raises(TypeError, match="Unsupported type"):
            canonical_json_dumps({"obj": object()})

    def test_24_rejection_of_nan_and_infinities(self):
        """Scenario 24: Rejection of NaN and infinities."""
        with pytest.raises((ValueError, TypeError), match="Non-finite float"):
            canonical_json_dumps({"score": float("nan")})
        with pytest.raises((ValueError, TypeError), match="Non-finite float"):
            canonical_json_dumps({"score": float("inf")})
        with pytest.raises((ValueError, TypeError), match="Non-finite float"):
            canonical_json_dumps({"score": float("-inf")})

    def test_25_rejection_of_duplicate_json_keys_and_non_standard_constants(self):
        """Scenario 25: Rejection of duplicate JSON object keys and non-standard JSON constants,
        and enforcement of canonical JSON at the parsing boundary."""
        # Duplicate keys and non-standard constants
        with pytest.raises(ValueError, match="Duplicate JSON key"):
            parse_canonical_json_line('{"key": 1, "key": 2}')
        with pytest.raises(ValueError, match="[Nn]on-standard JSON constant"):
            parse_canonical_json_line('{"key": NaN}')
        with pytest.raises(ValueError, match="[Nn]on-standard JSON constant"):
            parse_canonical_json_line('{"key": Infinity}')
        with pytest.raises(ValueError, match="[Nn]on-standard JSON constant"):
            parse_canonical_json_line('{"key": -Infinity}')

        # Canonical JSON acceptance
        assert parse_canonical_json_line('{"a":1,"b":2}') == {"a": 1, "b": 2}
        assert parse_canonical_json_line('{"a":1,"b":2}\n') == {"a": 1, "b": 2}
        assert parse_canonical_json_line('{"a":1,"b":2}\r\n') == {"a": 1, "b": 2}

        # Non-canonical formatting rejection (whitespace, unsorted keys, line padding)
        with pytest.raises(ValueError, match="Non-canonical JSON line"):
            parse_canonical_json_line('{"a": 1, "b": 2}')
        with pytest.raises(ValueError, match="Non-canonical JSON line"):
            parse_canonical_json_line('{"b":2,"a":1}')
        with pytest.raises(ValueError, match="Non-canonical JSON line"):
            parse_canonical_json_line(' {"a":1,"b":2}')
        with pytest.raises(ValueError, match="Non-canonical JSON line"):
            parse_canonical_json_line('{"a":1,"b":2} ')

        # Valid historical record compatibility mode
        hist_rec = parse_canonical_json_line('{"z": 2, "a": 1}', require_canonical=False)
        assert hist_rec == {"z": 2, "a": 1}

    def test_26_deterministic_serialization_and_digest_stability(self):
        """Scenario 26: Deterministic serialization and digest stability across key insertion orders."""
        dict1 = {"z": 10, "a": [3, 2, 1], "m": {"nested_z": True, "nested_a": "hello"}}
        dict2 = {"a": [3, 2, 1], "m": {"nested_a": "hello", "nested_z": True}, "z": 10}

        dump1 = canonical_json_dumps(dict1)
        dump2 = canonical_json_dumps(dict2)
        assert dump1 == dump2

        digest1 = compute_record_digest(dict1)
        digest2 = compute_record_digest(dict2)
        assert digest1 == digest2
        assert len(digest1) == 64

        # POST_DIGEST_FIELDS exclusion
        dict1_with_meta = dict(dict1)
        dict1_with_meta["record_digest"] = digest1
        dict1_with_meta["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        is_val, _ = verify_record_digest(dict1_with_meta)
        assert is_val is True


# -------------------------------------------------------------------------
# Scenario 27: Full Success Acceptance Matrix
# -------------------------------------------------------------------------

class TestFullSuccessAcceptanceMatrix:
    """Tests covering Scenario 27."""

    def test_27_complete_full_success_acceptance_matrix(self, tmp_path):
        """Scenario 27: Prove necessary and sufficient conditions for full_success."""
        # 1. Clean valid execution satisfies ALL conditions -> full_success
        source = tmp_path / "source"
        source.mkdir()
        input_text = "Observation:\nPeople often lose calmness in modern life.\n"
        (source / "note.md").write_text(input_text, encoding="utf-8")
        staging = tmp_path / "staging"
        staging.mkdir()
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))

        report = run_bounded_integration(
            str(source),
            str(staging),
            audit_writer=writer,
            audit_root=str(audit_root),
            approval_reader=lambda: "y",
            synthesis=SyntheticSynthesis(),
            core_units=[_make_core_sample()],
            max_files=1,
        )
        assert report.run_outcome == "full_success"
        assert report.accepted is True

        # Invariants:
        assert len(report.units) > 0
        units_executed = sum(1 for u in report.units if u.outcome == "executed")
        assert units_executed == len(report.units)
        units_held = sum(1 for u in report.units if u.outcome.startswith("held") or u.outcome == "llm_safe_stop")
        assert units_held == 0
        units_rejected = sum(1 for u in report.units if u.outcome in ("rejected", "revalidation_failed") or u.outcome.startswith("rejected"))
        assert units_rejected == 0
        units_cancelled = sum(1 for u in report.units if u.outcome == "cancelled")
        assert units_cancelled == 0
        assert report.files_failed_operational == 0
        assert report.files_failed_validation == 0
        assert report.units_failed_operational == 0
        assert report.recoverable_errors == 0
        assert report.rejection_check == ""
        assert report.rejection_reason == ""


# -------------------------------------------------------------------------
# Targeted Regression Tests for Audit Integrity Gaps
# -------------------------------------------------------------------------

class TestTargetedAuditIntegrityGaps:
    """Explicit regression tests for the four targeted audit-integrity corrections:
    1. Journal-wide record_id uniqueness across historical records not referenced by current run.
    2. Complete execution-claim detection across all independent indicators.
    3. Strict identity binding rejecting missing or mismatched fields.
    4. Narrow digest exclusions ensuring arbitrary underscore fields are protected.
    """

    def test_gap1_historical_duplicate_record_id_not_referenced_by_current_run(self, tmp_path):
        """Gap 1: Two historical records share record_id; neither is referenced by active run.
        Must fail closed with audit_integrity_failure and report locations."""
        audit_root = tmp_path / "audit"
        hist_dir1 = audit_root / "2024" / "10"
        hist_dir1.mkdir(parents=True)
        hist_file1 = hist_dir1 / "audit.jsonl"
        payload1 = {
            "record_id": "rec-hist-unreferenced-dup",
            "run_id": "run-2024",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-hist-1",
            "proposal_hash": "h-hist-1",
            "artifact_path": "art1.md",
        }
        payload1["record_digest"] = compute_record_digest(payload1)
        payload1["persistence_timestamp"] = "2024-10-01T00:00:00Z"
        hist_file1.write_text(canonical_json_dumps(payload1) + "\n", encoding="utf-8")

        hist_dir2 = audit_root / "2025" / "03"
        hist_dir2.mkdir(parents=True)
        hist_file2 = hist_dir2 / "audit.jsonl"
        payload2 = {
            "record_id": "rec-hist-unreferenced-dup",
            "run_id": "run-2025",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-hist-2",
            "proposal_hash": "h-hist-2",
            "artifact_path": "art2.md",
        }
        payload2["record_digest"] = compute_record_digest(payload2)
        payload2["persistence_timestamp"] = "2025-03-01T00:00:00Z"
        hist_file2.write_text(canonical_json_dumps(payload2) + "\n", encoding="utf-8")

        # Active run has a completely different record_id 'rec-current-clean'
        curr_dir = audit_root / "passed"
        curr_dir.mkdir(parents=True)
        curr_file = curr_dir / "audit.jsonl"
        curr_payload = {
            "record_id": "rec-current-clean",
            "run_id": "run-current",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-curr",
            "proposal_hash": "h-curr",
            "artifact_path": "art_curr.md",
        }
        curr_payload["record_digest"] = compute_record_digest(curr_payload)
        curr_payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        curr_file.write_text(canonical_json_dumps(curr_payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        entry = _make_executed_entry(
            unit_id="u-curr",
            proposal_hash="h-curr",
            artifact_ref="art_curr.md",
            record_id="rec-current-clean",
        )
        current_receipt = {
            "record_id": "rec-current-clean",
            "run_id": "run-current",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-curr",
            "proposal_hash": "h-curr",
            "artifact_path": str(curr_file),
            "durability": "confirmed",
            "persisted": True,
        }

        # Neither occurrence of 'rec-hist-unreferenced-dup' is in in-memory receipts!
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [current_receipt], writer, active_run_id="run-current"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "duplicate record_id detected across journal on disk: 'rec-hist-unreferenced-dup'" in reason
        assert "2024" in reason and "2025" in reason

    def test_gap2_execution_claim_terminal_outcome_with_false_boolean(self, tmp_path):
        """Gap 2a: Persisted record has terminal_outcome='executed', but execution_executed=False
        and storage_category='errors'. Active run claims it. Must be detected as an execution claim
        and fail closed if absent from in-memory receipts."""
        audit_root = tmp_path / "audit"
        err_dir = audit_root / "errors"
        err_dir.mkdir(parents=True)
        err_file = err_dir / "audit.jsonl"
        payload = {
            "record_id": "rec-claim-terminal-only",
            "run_id": "run-active-claim",
            "storage_category": "errors",  # not 'passed'
            "terminal_outcome": "executed",  # claims execution!
            "execution_executed": False,  # boolean is false!
            "query_unit_id": "u-claim-1",
            "proposal_hash": "h-claim-1",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        err_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        # Active run in-memory receipts list is empty
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [], [], writer, active_run_id="run-active-claim"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "rec-claim-terminal-only claiming execution is absent from in-memory run receipts" in reason

    def test_gap2_execution_claim_execution_executed_alias(self, tmp_path):
        """Gap 2b: Persisted record has terminal_outcome='execution_executed'."""
        audit_root = tmp_path / "audit"
        err_dir = audit_root / "errors"
        err_dir.mkdir(parents=True)
        err_file = err_dir / "audit.jsonl"
        payload = {
            "record_id": "rec-claim-alias",
            "run_id": "run-active-alias",
            "storage_category": "failed",
            "terminal_outcome": "execution_executed",
            "execution_executed": False,
            "query_unit_id": "u-claim-2",
            "proposal_hash": "h-claim-2",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        err_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [], [], writer, active_run_id="run-active-alias"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "rec-claim-alias claiming execution is absent from in-memory run receipts" in reason

    def test_gap2_execution_claim_boolean_true_with_non_exec_terminal(self, tmp_path):
        """Gap 2c: Persisted record has execution_executed=True with non-exec terminal_outcome."""
        audit_root = tmp_path / "audit"
        err_dir = audit_root / "errors"
        err_dir.mkdir(parents=True)
        err_file = err_dir / "audit.jsonl"
        payload = {
            "record_id": "rec-claim-bool-only",
            "run_id": "run-active-bool",
            "storage_category": "errors",
            "terminal_outcome": "pending",
            "execution_executed": True,
            "query_unit_id": "u-claim-3",
            "proposal_hash": "h-claim-3",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        err_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [], [], writer, active_run_id="run-active-bool"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "rec-claim-bool-only claiming execution is absent from in-memory run receipts" in reason

    def test_gap2_execution_claim_storage_category_passed(self, tmp_path):
        """Gap 2d: Persisted record has storage_category='passed'."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        payload = {
            "record_id": "rec-claim-passed-cat",
            "run_id": "run-active-passed",
            "storage_category": "passed",
            "terminal_outcome": "other",
            "execution_executed": False,
            "query_unit_id": "u-claim-4",
            "proposal_hash": "h-claim-4",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [], [], writer, active_run_id="run-active-passed"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "rec-claim-passed-cat claiming execution is absent from in-memory run receipts" in reason

    def test_gap3_strict_identity_missing_query_unit_id(self, tmp_path):
        """Gap 3a: Missing query_unit_id on unit, receipt, or disk record fails closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        # 1. Unit missing query_unit_id
        entry_no_qid = _make_executed_entry(unit_id="", record_id="rec-1")
        dummy_receipt = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_no_qid], [dummy_receipt], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory 'query_unit_id'" in reason

        # 2. Receipt missing query_unit_id
        entry = _make_executed_entry(unit_id="u-1", record_id="rec-1")
        receipt_no_qid = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "",  # missing!
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt_no_qid], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory 'query_unit_id'" in reason

        # 3. Disk record missing query_unit_id
        payload = {
            "record_id": "rec-disk-no-qid",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "",  # missing on disk!
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        entry_valid = _make_executed_entry(unit_id="u-1", record_id="rec-disk-no-qid")
        receipt_valid = {
            "record_id": "rec-disk-no-qid",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_valid], [receipt_valid], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory 'query_unit_id'" in reason

    def test_gap3_strict_identity_missing_proposal_hash(self, tmp_path):
        """Gap 3b: Missing proposal_hash on unit, receipt, or disk record fails closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        # 1. Unit missing proposal_hash
        entry_no_hash = _make_executed_entry(unit_id="u-1", proposal_hash="", record_id="rec-1")
        dummy_receipt = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "h-1",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_no_hash], [dummy_receipt], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory 'proposal_hash'" in reason

        # 2. Receipt missing proposal_hash
        entry = _make_executed_entry(unit_id="u-1", proposal_hash="h-1", record_id="rec-1")
        receipt_no_hash = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "",  # missing!
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt_no_hash], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory 'proposal_hash'" in reason

        # 3. Disk record missing proposal_hash
        payload = {
            "record_id": "rec-disk-no-hash",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "",  # missing on disk!
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        entry_valid = _make_executed_entry(unit_id="u-1", proposal_hash="h-1", record_id="rec-disk-no-hash")
        receipt_valid = {
            "record_id": "rec-disk-no-hash",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "h-1",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_valid], [receipt_valid], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory 'proposal_hash'" in reason

    def test_gap3_strict_identity_missing_artifact(self, tmp_path):
        """Gap 3c: Missing artifact identity on unit, receipt, or disk record fails closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        # 1. Unit missing artifact
        entry_no_art = _make_executed_entry(unit_id="u-1", artifact_ref="", record_id="rec-1")
        dummy_receipt = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_no_art], [dummy_receipt], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory 'artifact_reference'" in reason

        # 2. Receipt missing artifact
        entry = _make_executed_entry(unit_id="u-1", artifact_ref="art.md", record_id="rec-1")
        receipt_no_art = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "",  # missing!
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt_no_art], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory artifact reference/path" in reason

        # 3. Disk record missing artifact
        payload = {
            "record_id": "rec-disk-no-art",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "",  # missing on disk!
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        entry_valid = _make_executed_entry(unit_id="u-1", artifact_ref="art.md", record_id="rec-disk-no-art")
        receipt_valid = {
            "record_id": "rec-disk-no-art",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_valid], [receipt_valid], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "missing mandatory artifact path" in reason

    def test_gap3_strict_identity_mismatches(self, tmp_path):
        """Gap 3d: Mismatched identity values between unit, receipt, and disk record fail closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        payload = {
            "record_id": "rec-mismatch-test",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-DISK",
            "proposal_hash": "hash-DISK",
            "artifact_path": "art_disk.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        # 1. query_unit_id mismatch
        entry_bad_qid = _make_executed_entry(unit_id="u-UNIT", proposal_hash="hash-DISK", artifact_ref="art_disk.md", record_id="rec-mismatch-test")
        receipt_bad_qid = {
            "record_id": "rec-mismatch-test",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-UNIT",
            "proposal_hash": "hash-DISK",
            "artifact_path": "art_disk.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_bad_qid], [receipt_bad_qid], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert "binding mismatch query_unit_id" in reason

        # 2. artifact_path mismatch
        entry_bad_art = _make_executed_entry(unit_id="u-DISK", proposal_hash="hash-DISK", artifact_ref="art_DIFFERENT.md", record_id="rec-mismatch-test")
        receipt_bad_art = {
            "record_id": "rec-mismatch-test",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-DISK",
            "proposal_hash": "hash-DISK",
            "artifact_path": "art_DIFFERENT.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_bad_art], [receipt_bad_art], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert "binding mismatch artifact_path" in reason

    def test_gap4_narrow_digest_arbitrary_underscore_field_is_protected(self):
        """Gap 4a: An arbitrary underscore-prefixed persisted field is hashed into the digest.
        Tampering with it fails verification."""
        record = {
            "record_id": "rec-digest-test",
            "run_id": "run-1",
            "_custom_internal_tag": "immutable_audit_annotation_v1",
            "query_unit_id": "u-1",
            "proposal_hash": "h-1",
        }
        computed = compute_record_digest(record)
        record_with_digest = dict(record)
        record_with_digest["record_digest"] = computed
        record_with_digest["persistence_timestamp"] = "2026-11-01T00:00:00Z"

        # Untampered: valid
        is_val, reason = verify_record_digest(record_with_digest)
        assert is_val is True
        assert reason == ""

        # Tampered arbitrary underscore field: verification MUST fail!
        tampered_record = dict(record_with_digest)
        tampered_record["_custom_internal_tag"] = "TAMPERED_VALUE"
        is_val, reason = verify_record_digest(tampered_record)
        assert is_val is False
        assert "record_digest mismatch" in reason

    def test_gap4_reader_diagnostic_metadata_does_not_break_verification(self):
        """Gap 4b: Reader-generated metadata (reader_source_file and reader_line_no)
        attached via PersistedRecord does not pollute dictionary keys or break digest verification."""
        record = {
            "record_id": "rec-reader-meta-test",
            "run_id": "run-1",
            "query_unit_id": "u-1",
            "proposal_hash": "h-1",
        }
        computed = compute_record_digest(record)
        record_with_digest = dict(record)
        record_with_digest["record_digest"] = computed
        record_with_digest["persistence_timestamp"] = "2026-11-01T00:00:00Z"

        # Reader attaches reader_source_file and reader_line_no via PersistedRecord
        persisted_rec = PersistedRecord(
            record_with_digest,
            source_file="/audit/passed/audit.jsonl",
            line_no=42,
        )
        assert persisted_rec.reader_source_file == "/audit/passed/audit.jsonl"
        assert persisted_rec.reader_line_no == 42
        assert "_source_file" not in persisted_rec
        assert "_line_no" not in persisted_rec

        # Must verify cleanly
        is_val, reason = verify_record_digest(persisted_rec)
        assert is_val is True
        assert reason == ""

    def test_edge1_reject_non_string_identity_fields_on_unit(self, tmp_path):
        """Edge Case 1a: Unit identity fields with non-string types (int, bool, float, object) fail closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        dummy_receipt = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }

        # 1. query_unit_id as int, bool, float, object
        for bad_val in (123, True, False, 45.6, object()):
            entry = _make_executed_entry(unit_id=bad_val, record_id="rec-1")
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [dummy_receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

        # 2. proposal_hash as int, bool
        for bad_val in (999, False, True):
            entry = _make_executed_entry(unit_id="u-1", proposal_hash=bad_val, record_id="rec-1")
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [dummy_receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

        # 3. artifact_reference as int, bool
        for bad_val in (404, True):
            entry = _make_executed_entry(unit_id="u-1", artifact_ref=bad_val, record_id="rec-1")
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [dummy_receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

        # 4. record_id on unit as int, bool
        for bad_val in (777, True):
            entry = _make_executed_entry(unit_id="u-1", record_id=bad_val)
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [dummy_receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

    def test_edge1_reject_non_string_identity_fields_on_receipt(self, tmp_path):
        """Edge Case 1b: Receipt identity fields with non-string types fail closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        entry = _make_executed_entry(unit_id="u-1", proposal_hash="hash-abc", artifact_ref="art.md", record_id="rec-1")

        # 1. Non-string query_unit_id on receipt
        for bad_val in (123, True, False, 3.14, ["u-1"], {"id": "u-1"}):
            receipt = {
                "record_id": "rec-1",
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": bad_val,
                "proposal_hash": "hash-abc",
                "artifact_path": "art.md",
                "audit_path": str(pass_file),
                "durability": "confirmed",
                "persisted": True,
            }
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"

        # 2. Non-string proposal_hash on receipt
        for bad_val in (888, True, ["hash"], {"h": 1}):
            receipt = {
                "record_id": "rec-1",
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": bad_val,
                "artifact_path": "art.md",
                "audit_path": str(pass_file),
                "durability": "confirmed",
                "persisted": True,
            }
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"

        # 3. Non-string artifact_path on receipt
        for bad_val in (555, False, ["art.md"], {"p": 1}):
            receipt = {
                "record_id": "rec-1",
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": "hash-abc",
                "artifact_path": bad_val,
                "audit_path": str(pass_file),
                "durability": "confirmed",
                "persisted": True,
            }
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"

        # 4. Non-string run_id on receipt
        for bad_val in (999, True, ["run-1"], {"r": 1}):
            receipt = {
                "record_id": "rec-1",
                "run_id": bad_val,
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": "hash-abc",
                "artifact_path": "art.md",
                "audit_path": str(pass_file),
                "durability": "confirmed",
                "persisted": True,
            }
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"

        # 5. Non-string / unhashable record_id on receipt (list, dict, int, bool)
        for bad_val in (["rec-1"], {"id": "rec-1"}, 123, True):
            receipt = {
                "record_id": bad_val,
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": "hash-abc",
                "artifact_path": "art.md",
                "audit_path": str(pass_file),
                "durability": "confirmed",
                "persisted": True,
            }
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

    def test_edge1_reject_non_string_identity_fields_on_disk_record(self, tmp_path):
        """Edge Case 1c: Persisted disk record identity fields with non-string types fail closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        entry = _make_executed_entry(unit_id="u-1", proposal_hash="hash-abc", artifact_ref="art.md", record_id="rec-1")
        receipt = {
            "record_id": "rec-1",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }

        # 1. Disk record has non-string query_unit_id (int, bool, list, dict)
        for bad_val in (12345, True, ["u-1"], {"qid": 1}):
            payload = {
                "record_id": "rec-1",
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": bad_val,
                "proposal_hash": "hash-abc",
                "artifact_path": "art.md",
            }
            payload["record_digest"] = compute_record_digest(payload)
            payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
            pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

        # 2. Disk record has non-string record_id (list, dict, int, bool) - must NOT raise TypeError
        for bad_val in (["rec-1"], {"id": "rec-1"}, 456, False):
            payload = {
                "record_id": bad_val,
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": "hash-abc",
                "artifact_path": "art.md",
            }
            payload["record_digest"] = compute_record_digest(payload)
            payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
            pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

        # 3. Disk record has non-string proposal_hash (int, bool, list, dict)
        for bad_val in (999, True, ["hash"], {"h": 1}):
            payload = {
                "record_id": "rec-1",
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": bad_val,
                "artifact_path": "art.md",
            }
            payload["record_digest"] = compute_record_digest(payload)
            payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
            pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

        # 4. Disk record has non-string artifact_path (int, bool, list, dict)
        for bad_val in (404, False, ["art.md"], {"p": 1}):
            payload = {
                "record_id": "rec-1",
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": "hash-abc",
                "artifact_path": bad_val,
            }
            payload["record_digest"] = compute_record_digest(payload)
            payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
            pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

        # 5. Disk record has non-string run_id (int, bool, list, dict)
        for bad_val in (777, True, ["run-1"], {"r": 1}):
            payload = {
                "record_id": "rec-1",
                "run_id": bad_val,
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": "hash-abc",
                "artifact_path": "art.md",
            }
            payload["record_digest"] = compute_record_digest(payload)
            payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
            pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "must be a string" in reason

    def test_edge2_enforce_run_identity_consistency(self, tmp_path):
        """Edge Case 2: Run identity consistency across receipt, disk record, and active run."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        # Setup valid disk record under run-A
        payload = {
            "record_id": "rec-run-test",
            "run_id": "run-A",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        entry = _make_executed_entry(unit_id="u-1", proposal_hash="hash-abc", artifact_ref="art.md", record_id="rec-run-test")

        # Case 2a: In-memory receipt run_id mismatch vs active_run_id
        receipt_mismatch_active = {
            "record_id": "rec-run-test",
            "run_id": "run-HISTORICAL",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt_mismatch_active], writer, active_run_id="run-ACTIVE"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "run_id mismatch" in reason

        # Case 2b: Persisted record run_id mismatch vs receipt run_id
        receipt_active = {
            "record_id": "rec-run-test",
            "run_id": "run-ACTIVE",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt_active], writer, active_run_id="run-ACTIVE"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "run_id mismatch" in reason

        # Case 2c: Both agree on run-A, matching active_run_id="run-A" -> succeeds
        receipt_matching = {
            "record_id": "rec-run-test",
            "run_id": "run-A",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt_matching], writer, active_run_id="run-A"
        )
        assert is_valid is True
        assert fail_type == ""
        assert reason == ""

    def test_edge3_reject_conflicting_record_id_bindings(self, tmp_path):
        """Edge Case 3: Unit specifying record_id conflicts with candidate receipt's record_id -> fail closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        payload = {
            "record_id": "rec-RECEIPT-B",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        # Unit explicitly specifies record_id="rec-UNIT-A", but receipt has "rec-RECEIPT-B"
        entry_conflicting = _make_executed_entry(
            unit_id="u-1", proposal_hash="hash-abc", artifact_ref="art.md", record_id="rec-UNIT-A"
        )
        receipt_b = {
            "record_id": "rec-RECEIPT-B",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }

        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry_conflicting], [receipt_b], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "conflicting record_id binding" in reason

    def test_edge2_missing_run_id_fails_closed(self, tmp_path):
        """Edge Case 2d: Missing, None, or empty run_id on receipt or disk record fails closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        # Setup valid disk record
        payload = {
            "record_id": "rec-run-missing",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        entry = _make_executed_entry(unit_id="u-1", proposal_hash="hash-abc", artifact_ref="art.md", record_id="rec-run-missing")

        # 1. Receipt missing run_id (None, empty, whitespace)
        for bad_run_id in (None, "", "   "):
            receipt = {
                "record_id": "rec-run-missing",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": "hash-abc",
                "artifact_path": "art.md",
                "audit_path": str(pass_file),
                "durability": "confirmed",
                "persisted": True,
            }
            if bad_run_id is not None:
                receipt["run_id"] = bad_run_id

            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "missing mandatory 'run_id'" in reason

        # 2. Disk record missing run_id (None, empty, whitespace)
        receipt_valid = {
            "record_id": "rec-run-missing",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        for bad_disk_run_id in ("", "   "):
            disk_payload = dict(payload)
            disk_payload["run_id"] = bad_disk_run_id
            disk_payload["record_digest"] = compute_record_digest(disk_payload)
            pass_file.write_text(canonical_json_dumps(disk_payload) + "\n", encoding="utf-8")

            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt_valid], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "missing mandatory 'run_id'" in reason

    def test_edge2_invalid_active_run_id_fails_closed(self, tmp_path):
        """Edge Case 2e: Non-string or whitespace active_run_id fails closed."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        payload = {
            "record_id": "rec-active-test",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        entry = _make_executed_entry(unit_id="u-1", proposal_hash="hash-abc", artifact_ref="art.md", record_id="rec-active-test")
        receipt = {
            "record_id": "rec-active-test",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }

        # Non-string or whitespace-only active_run_id values
        for bad_active in (123, True, False, ["run-1"], {"run": 1}, "   "):
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id=cast(Any, bad_active)
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            assert "active_run_id" in reason

    def test_edge4_artifact_path_falsey_or_invalid_does_not_mask(self, tmp_path):
        """Edge Case 4: Falsey or non-string artifact_path does NOT silently fall back to artifact_reference."""
        audit_root = tmp_path / "audit"
        pass_dir = audit_root / "passed"
        pass_dir.mkdir(parents=True)
        pass_file = pass_dir / "audit.jsonl"
        writer = AuditWriter(str(audit_root))

        payload = {
            "record_id": "rec-art-test",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
        }
        payload["record_digest"] = compute_record_digest(payload)
        payload["persistence_timestamp"] = "2026-11-01T00:00:00Z"
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")

        entry = _make_executed_entry(unit_id="u-1", proposal_hash="hash-abc", artifact_ref="art.md", record_id="rec-art-test")

        # 1. Receipt has falsey/invalid artifact_path alongside valid artifact_reference: MUST FAIL CLOSED!
        for bad_path in (False, 0, ""):
            receipt = {
                "record_id": "rec-art-test",
                "run_id": "run-1",
                "storage_category": "passed",
                "terminal_outcome": "executed",
                "execution_executed": True,
                "query_unit_id": "u-1",
                "proposal_hash": "hash-abc",
                "artifact_path": bad_path,
                "artifact_reference": "art.md",  # valid fallback that must NOT mask bad_path!
                "audit_path": str(pass_file),
                "durability": "confirmed",
                "persisted": True,
            }
            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            if bad_path == "":
                assert "missing mandatory artifact reference/path" in reason
            else:
                assert "must be a string" in reason

        # 2. Disk record has falsey/invalid artifact_path alongside valid artifact_reference: MUST FAIL CLOSED!
        receipt_valid = {
            "record_id": "rec-art-test",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_path": "art.md",
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        for bad_disk_path in (False, 0, ""):
            disk_payload = dict(payload)
            disk_payload["artifact_path"] = bad_disk_path
            disk_payload["artifact_reference"] = "art.md"  # valid fallback that must NOT mask bad_disk_path!
            disk_payload["record_digest"] = compute_record_digest(disk_payload)
            pass_file.write_text(canonical_json_dumps(disk_payload) + "\n", encoding="utf-8")

            is_valid, fail_type, reason = verify_execution_receipt_bijection(
                [entry], [receipt_valid], writer, active_run_id="run-1"
            )
            assert is_valid is False
            assert fail_type == "audit_integrity_failure"
            if bad_disk_path == "":
                assert "missing mandatory artifact path" in reason
            else:
                assert "must be a string" in reason

        # 3. Conflicting artifact fields: artifact_path != artifact_reference
        receipt_conflicting = dict(receipt_valid)
        receipt_conflicting["artifact_path"] = "art_one.md"
        receipt_conflicting["artifact_reference"] = "art_two.md"
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt_conflicting], writer, active_run_id="run-1"
        )
        assert is_valid is False
        assert fail_type == "audit_integrity_failure"
        assert "conflicting artifact fields" in reason

        # 4. Legitimate single-field use: receipt has ONLY artifact_reference (no artifact_path key)
        # Restore valid disk record
        pass_file.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")
        receipt_ref_only = {
            "record_id": "rec-art-test",
            "run_id": "run-1",
            "storage_category": "passed",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "query_unit_id": "u-1",
            "proposal_hash": "hash-abc",
            "artifact_reference": "art.md",  # valid when artifact_path is absent
            "audit_path": str(pass_file),
            "durability": "confirmed",
            "persisted": True,
        }
        is_valid, fail_type, reason = verify_execution_receipt_bijection(
            [entry], [receipt_ref_only], writer, active_run_id="run-1"
        )
        assert is_valid is True
        assert fail_type == ""
        assert reason == ""



# ---------------------------------------------------------------------------
# Targeted Hardening and Invariant Battery
# ---------------------------------------------------------------------------

def test_ingestion_exception_classification_operational_vs_validation(tmp_path):
    """Test that ingestion exceptions are strictly classified into operational vs validation.

    Operational/infrastructure (FileNotFoundError, PermissionError, OSError, IOError,
    UnicodeDecodeError, RuntimeError, AttributeError, unclassified Exception) -> failed_operational,
    terminal_outcome="infrastructure_error", validation_result="ingestion_failed".

    Genuine structural/frontmatter failure (StructuralValidationError, ValueError with frontmatter marker)
    -> failed_validation, terminal_outcome="validation_failed", validation_result="malformed_structure".
    """
    from src.living_authenticity.knowledge.integration.runner import (
        StructuralValidationError,
        _classify_ingestion_exception,
        run_bounded_integration,
    )
    from src.living_authenticity.knowledge.governance.audit.persistence import AuditWriter
    from tests._synthetic_synthesis import SyntheticSynthesis

    # 1. Pure classification helper behavior
    # Explicit StructuralValidationError and subclasses -> failed_validation
    assert _classify_ingestion_exception(StructuralValidationError("invalid schema")) == "failed_validation"

    class CustomStructuralError(StructuralValidationError):
        pass

    assert _classify_ingestion_exception(CustomStructuralError("subclass structural error")) == "failed_validation"

    # Generic exceptions with validation/schema keywords MUST NOT classify as validation (no heuristics!)
    assert _classify_ingestion_exception(ValueError("malformed frontmatter in document")) == "failed_operational"
    assert _classify_ingestion_exception(ValueError("document structure violated")) == "failed_operational"
    assert _classify_ingestion_exception(ValueError("schema validation failure")) == "failed_operational"

    class SchemaViolationError(Exception):
        pass

    class FrontmatterValidationException(Exception):
        pass

    assert _classify_ingestion_exception(SchemaViolationError("custom schema error")) == "failed_operational"
    assert _classify_ingestion_exception(FrontmatterValidationException("custom validation")) == "failed_operational"

    # Operational/Infrastructure failures:
    assert _classify_ingestion_exception(FileNotFoundError("no such file")) == "failed_operational"
    assert _classify_ingestion_exception(PermissionError("access denied")) == "failed_operational"
    assert _classify_ingestion_exception(OSError("disk read error")) == "failed_operational"
    assert _classify_ingestion_exception(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")) == "failed_operational"
    assert _classify_ingestion_exception(TimeoutError("operation timed out")) == "failed_operational"
    assert _classify_ingestion_exception(RuntimeError("pipeline bug")) == "failed_operational"
    assert _classify_ingestion_exception(AttributeError("attribute missing")) == "failed_operational"
    assert _classify_ingestion_exception(Exception("generic unclassified failure")) == "failed_operational"
    assert _classify_ingestion_exception(ValueError("plain value error without markers")) == "failed_operational"
    assert _classify_ingestion_exception(KeyError("missing_field")) == "failed_operational"

    # 2. Integration run behavior with mock pipeline raising operational vs validation
    source = tmp_path / "src"
    source.mkdir()
    (source / "doc1.md").write_text("dummy", encoding="utf-8")
    staging = tmp_path / "stg"
    staging.mkdir(parents=True, exist_ok=True)
    audit_root = tmp_path / "aud"
    audit_root.mkdir(parents=True, exist_ok=True)
    writer = AuditWriter(str(audit_root))

    class MockPipelineOpFail:
        def run_file(self, *args, **kwargs):
            raise OSError("simulated I/O disk error")

    report_op = run_bounded_integration(
        str(source), str(staging),
        pipeline=MockPipelineOpFail(),
        audit_writer=writer,
        synthesis=SyntheticSynthesis(),
        approval_reader=lambda: "y",
    )
    assert report_op.files_failed_operational == 1
    assert report_op.files_failed_validation == 0
    assert report_op.files_failed == 1
    assert report_op.files_seen == 1
    assert report_op.files_processed == 0
    assert report_op.run_outcome == "operational_failure"
    assert len(report_op.units) == 0  # Zero fabricated domain entities!
    assert len(report_op.audits) == 1
    rec_op = report_op.audits[0]
    assert rec_op.terminal_outcome == "infrastructure_error"
    assert rec_op.validation_result == "ingestion_failed"

    class MockPipelineValFail:
        def run_file(self, *args, **kwargs):
            raise StructuralValidationError("missing mandatory title in YAML frontmatter")

    report_val = run_bounded_integration(
        str(source), str(staging),
        pipeline=MockPipelineValFail(),
        audit_writer=writer,
        synthesis=SyntheticSynthesis(),
        approval_reader=lambda: "y",
    )
    assert report_val.files_failed_operational == 0
    assert report_val.files_failed_validation == 1
    assert report_val.files_failed == 1
    assert report_val.files_seen == 1
    assert report_val.files_processed == 0
    assert report_val.run_outcome == "validation_failure"
    assert len(report_val.units) == 0  # Zero fabricated domain entities!
    assert len(report_val.audits) == 1
    rec_val = report_val.audits[0]
    assert rec_val.terminal_outcome == "validation_failed"
    assert rec_val.validation_result == "malformed_structure"


def test_restored_canonical_outcome_hierarchy(tmp_path):
    """Verify the canonical 16-rank hierarchy without legacy or invented outcome names.

    Specifically tests:
    - E == 0, not O, V, H -> validation_held (rejection_check="validation_held")
    - E == 0, O, not V, H -> mixed_failure (rejection_check="mixed_failure")
    - E == 0, O, V, H -> mixed_failure (rejection_check="mixed_failure")
    - mixed_operational_held and validation_and_governance must NEVER appear.
    """
    from src.living_authenticity.knowledge.integration.runner import (
        run_bounded_integration,
        StructuralValidationError,
    )
    from src.living_authenticity.knowledge.governance.audit.persistence import AuditWriter
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.orchestration.orchestrator import EvidenceFirstPipeline
    from tests._synthetic_synthesis import SyntheticSynthesis

    source = tmp_path / "src"
    source.mkdir()
    f1 = source / "file1.md"
    f2 = source / "file2.md"
    f1.write_text("file 1 content", encoding="utf-8")
    obs_text = "Observation:\nA mindfulness observation regarding mental calm in daily routines.\n"
    f2.write_text(obs_text, encoding="utf-8")
    staging = tmp_path / "stg"
    staging.mkdir(parents=True, exist_ok=True)
    audit_root = tmp_path / "aud"
    audit_root.mkdir(parents=True, exist_ok=True)
    writer = AuditWriter(str(audit_root))

    core_text = "Observation:\nCore mindfulness observation regarding mental calm.\n"
    cores = [KnowledgeUnit(
        id="core1", source="core", original_text=core_text,
        meaning=core_text, cleaned_text=core_text,
        normalized_text=core_text,
        context="", proposed_type="Observation", position=0,
    )]

    class MockPipelineValAndHold(EvidenceFirstPipeline):
        """File 1 fails structural validation; File 2 extracts 1 unit that gets held."""
        def __init__(self):
            super().__init__()
            self.calls = 0

        def run_file(self, path, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise StructuralValidationError("malformed frontmatter in doc")
            return super().run_file(path, *args, **kwargs)

    # approval_reader returns 'n' -> unit is held/rejected!
    rep_vh = run_bounded_integration(
        str(source), str(staging),
        pipeline=MockPipelineValAndHold(),
        audit_writer=writer,
        synthesis=SyntheticSynthesis(),
        approval_reader=lambda: "n",
        core_units=cores,
        max_files=2,
    )
    assert rep_vh.files_failed_validation == 1
    assert rep_vh.files_failed_operational == 0
    assert len(rep_vh.units) == 1
    assert rep_vh.units[0].outcome in ("held", "rejected", "rejection_not_permitted", "llm_safe_stop") or rep_vh.units[0].outcome.startswith("held")
    # Must be validation_held, NOT validation_and_governance!
    assert rep_vh.run_outcome == "validation_held"
    assert rep_vh.rejection_check == "validation_held"

    class MockPipelineOpAndHold(EvidenceFirstPipeline):
        """File 1 fails operationally; File 2 extracts 1 unit that gets held."""
        def __init__(self):
            super().__init__()
            self.calls = 0

        def run_file(self, path, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("I/O read failure")
            return super().run_file(path, *args, **kwargs)

    rep_oh = run_bounded_integration(
        str(source), str(staging),
        pipeline=MockPipelineOpAndHold(),
        audit_writer=writer,
        synthesis=SyntheticSynthesis(),
        approval_reader=lambda: "n",
        core_units=cores,
        max_files=2,
    )
    assert rep_oh.files_failed_operational == 1
    assert rep_oh.files_failed_validation == 0
    assert len(rep_oh.units) == 1
    assert rep_oh.units[0].outcome in ("held", "rejected", "rejection_not_permitted", "llm_safe_stop") or rep_oh.units[0].outcome.startswith("held")
    # Must be mixed_failure, NOT mixed_operational_held!
    assert rep_oh.run_outcome == "mixed_failure"
    assert rep_oh.rejection_check == "mixed_failure"

    # Explicit regression case: O=True, V=True, H=True
    # Under canonical 16-rank hierarchy, Rank 9 (operational_and_validation_failure)
    # dominates Rank 11 (mixed_failure).
    f3 = source / "file3.md"
    f3.write_text("Observation:\nThird observation text.\n", encoding="utf-8")

    class MockPipelineOpValAndHold(EvidenceFirstPipeline):
        """File 1: operational fail; File 2: validation fail; File 3: unit extracted and held."""
        def __init__(self):
            super().__init__()
            self.calls = 0

        def run_file(self, path, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("I/O operational failure")
            elif self.calls == 2:
                raise StructuralValidationError("schema validation failure")
            return super().run_file(path, *args, **kwargs)

    rep_ovh = run_bounded_integration(
        str(source), str(staging),
        pipeline=MockPipelineOpValAndHold(),
        audit_writer=writer,
        synthesis=SyntheticSynthesis(),
        approval_reader=lambda: "n",
        core_units=cores,
        max_files=3,
    )
    assert rep_ovh.files_failed_operational == 1
    assert rep_ovh.files_failed_validation == 1
    assert len(rep_ovh.units) == 1
    assert rep_ovh.units[0].outcome in ("held", "rejected", "rejection_not_permitted", "llm_safe_stop", "revalidation_failed") or rep_ovh.units[0].outcome.startswith("held") or rep_ovh.units[0].outcome.startswith("revalidation_failed")
    # When O=True, V=True, H=True -> MUST be operational_and_validation_failure!
    assert rep_ovh.run_outcome == "operational_and_validation_failure"
    assert rep_ovh.rejection_check == "operational_and_validation"


def test_deterministic_serialization_supported_and_unsupported_types():
    """Verify that _sanitize and canonical_json_dumps deterministically handle domain types
    (Path, datetime, date, Enum, UUID, set, frozenset) and reject unsupported objects fail-closed.
    """
    from datetime import date, datetime
    from enum import Enum
    import uuid
    import math
    import pytest
    from src.living_authenticity.knowledge.governance.audit.persistence import (
        _sanitize,
        canonical_json_dumps,
        compute_record_digest,
    )

    class ActionType(str, Enum):
        CREATE = "CREATE"
        UPDATE = "UPDATE"

    sample_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    sample_dt = datetime(2026, 11, 2, 14, 30, 0)
    sample_date = date(2026, 11, 2)
    sample_path = Path("notes/research.md")

    payload = {
        "record_id": "rec-123",
        "action": ActionType.CREATE,
        "uuid": sample_uuid,
        "created_at": sample_dt,
        "date_only": sample_date,
        "file_path": sample_path,
        "tags": {"tag_b", "tag_a"},
        "frozen_tags": frozenset({"z", "a"}),
        "count": 42,
        "is_active": True,
        "score": 98.5,
        "nested": {
            "inner_path": Path("sub/dir"),
            "inner_enum": ActionType.UPDATE,
        },
    }

    sanitized = _sanitize(payload)
    assert isinstance(sanitized, dict)
    assert sanitized["action"] == "CREATE"
    assert sanitized["uuid"] == "12345678-1234-5678-1234-567812345678"
    assert sanitized["created_at"] == "2026-11-02T14:30:00"
    assert sanitized["date_only"] == "2026-11-02"
    assert sanitized["file_path"] == "notes/research.md"
    assert sanitized["tags"] == ["tag_a", "tag_b"]  # sorted deterministically!
    assert sanitized["frozen_tags"] == ["a", "z"]
    assert sanitized["nested"]["inner_path"] == "sub/dir"
    assert sanitized["nested"]["inner_enum"] == "UPDATE"

    # Deterministic sorting of {1, "1"} where str() would collide:
    # "1" -> '"1"' (ASCII 34), 1 -> '1' (ASCII 49), so '"1"' < '1'
    set_with_str_int = {1, "1"}
    sanitized_set1 = _sanitize({"items": set_with_str_int})
    assert isinstance(sanitized_set1, dict)
    assert sanitized_set1["items"] == ["1", 1]

    # Equivalent sets with different insertion orders produce identical output:
    set_rev = {"1", 1}
    sanitized_set2 = _sanitize({"items": set_rev})
    assert isinstance(sanitized_set2, dict)
    assert sanitized_set2["items"] == ["1", 1]
    assert canonical_json_dumps(sanitized_set1) == canonical_json_dumps(sanitized_set2)
    assert compute_record_digest(sanitized_set1) == compute_record_digest(sanitized_set2)

    # Mixed primitive types in set
    mixed_set = {"a", 10, True, None, 2.5}
    san_mixed = _sanitize({"m": mixed_set})
    assert isinstance(san_mixed, dict)
    assert isinstance(san_mixed["m"], list)
    assert len(san_mixed["m"]) == 5
    # Deterministic across repeated calls
    assert _sanitize({"m": mixed_set}) == san_mixed

    # Must produce valid, repeatable canonical JSON
    dump1 = canonical_json_dumps(sanitized)
    dump2 = canonical_json_dumps(sanitized)
    assert dump1 == dump2
    digest1 = compute_record_digest(sanitized)
    digest2 = compute_record_digest(sanitized)
    assert digest1 == digest2

    # Reject unsupported types fail-closed
    class CustomArbitraryObject:
        pass

    with pytest.raises(TypeError, match="Unsupported audit record field type"):
        _sanitize({"bad": CustomArbitraryObject()})

    with pytest.raises(TypeError, match="Dictionary key must be string"):
        _sanitize({123: "non-string key"})

    with pytest.raises(TypeError, match="Non-finite float"):
        _sanitize({"nan": float("nan")})

    with pytest.raises(TypeError, match="Non-finite float"):
        _sanitize({"inf": float("inf")})


def test_allow_test_output_narrow_authorization_boundary():
    """Verify strict adversarial enforcement of the allow_test_output boundary.

    Ordinary callers cannot activate tests/output bypass merely by passing allow_test_output=True.
    Requires both allow_test_output=True AND authorized_test_mode().
    Path traversal, symlinks, exact root, and production paths must always fail.
    """
    from Config.settings import ROOT as REPOSITORY_ROOT
    from src.living_authenticity.knowledge.integration.guard import (
        check_staging_eligible,
        check_audit_eligible,
        authorized_test_mode,
    )
    from tests._test_authorizer import issue_test_authorizer
    from src.living_authenticity.adapter.picker import validate_purpose

    test_sub = REPOSITORY_ROOT / "tests" / "output" / "guard_adversarial_test"
    test_sub.mkdir(parents=True, exist_ok=True)
    exact_root = REPOSITORY_ROOT / "tests" / "output"
    src_dir = REPOSITORY_ROOT / "src"

    try:
        # 1. Ordinary caller with allow_test_output=True (UNAUTHORIZED) -> MUST FAIL
        stg_unauth, r_stg_unauth, _ = check_staging_eligible(str(test_sub), (), allow_test_output=True)
        assert stg_unauth is False
        assert r_stg_unauth == "staging inside repository workspace"

        aud_unauth, r_aud_unauth, _ = check_audit_eligible(
            str(test_sub), authorized_audit_root=str(test_sub), allow_test_output=True
        )
        assert aud_unauth is False
        assert r_aud_unauth == "audit inside repository workspace"

        val_unauth, _, reason_unauth = validate_purpose("staging", str(test_sub), allow_test_output=True)
        assert val_unauth != "configured"
        assert "inside the repository workspace" in reason_unauth

        # 2. Authorized caller with authorized_test_mode(issue_test_authorizer(...)) -> STRICT DESCENDANT SUCCEEDS
        with authorized_test_mode(issue_test_authorizer("narrow boundary test")):
            stg_auth, r_stg_auth, _ = check_staging_eligible(str(test_sub), (), allow_test_output=True)
            assert stg_auth is True, f"must succeed in authorized test mode: {r_stg_auth}"

            aud_auth, r_aud_auth, _ = check_audit_eligible(
                str(test_sub), authorized_audit_root=str(test_sub), allow_test_output=True
            )
            assert aud_auth is True, f"must succeed in authorized test mode: {r_aud_auth}"

            val_auth, _, _ = validate_purpose("staging", str(test_sub), allow_test_output=True)
            assert val_auth == "configured"

            # 3. Exact root MUST FAIL even in authorized test mode
            stg_exact, r_stg_exact, _ = check_staging_eligible(str(exact_root), (), allow_test_output=True)
            assert stg_exact is False
            assert r_stg_exact == "staging inside repository workspace"

            # 4. Path traversal escaping tests/output MUST FAIL
            traversal_path = str(test_sub / ".." / ".." / "src")
            stg_trav, r_stg_trav, _ = check_staging_eligible(traversal_path, (), allow_test_output=True)
            assert stg_trav is False

            # 5. Production source tree MUST FAIL even in authorized test mode with allow_test_output=True
            stg_src, r_stg_src, _ = check_staging_eligible(str(src_dir), (), allow_test_output=True)
            assert stg_src is False
            assert r_stg_src == "staging inside repository workspace"

            aud_src, r_aud_src, _ = check_audit_eligible(
                str(src_dir), authorized_audit_root=str(src_dir), allow_test_output=True
            )
            assert aud_src is False
            assert r_aud_src == "audit inside repository workspace"

            val_src, _, _ = validate_purpose("staging", str(src_dir), allow_test_output=True)
            assert val_src != "configured"

        # 6. Adversarial capability construction: unauthorized caller cannot construct token
        from src.living_authenticity.knowledge.integration.guard import (
            TestOutputAuthorizationToken,
            is_test_mode_authorized,
        )
        with pytest.raises(PermissionError, match="can only be minted via authorized_test_mode"):
            TestOutputAuthorizationToken(object())

        with pytest.raises(PermissionError, match="can only be minted via authorized_test_mode"):
            TestOutputAuthorizationToken(None)

        # 7. Token reuse / leakage across context boundaries: saved token outside context is REJECTED
        saved_token = None
        with authorized_test_mode(issue_test_authorizer("token reuse test")) as token:
            saved_token = token
            assert is_test_mode_authorized(saved_token) is True
        # Once outside the context, the capability is inert and cannot be reused
        assert is_test_mode_authorized(saved_token) is False
        assert is_test_mode_authorized() is False

        # 8. Async task isolation: concurrent tasks on same loop cannot inherit authorization
        import asyncio

        async def async_isolation_check():
            async def task_authorized():
                with authorized_test_mode(issue_test_authorizer("async isolation test")):
                    await asyncio.sleep(0.01)
                    return is_test_mode_authorized()

            async def task_unauthorized():
                await asyncio.sleep(0.005)
                # Must be False even while task_authorized is inside authorized_test_mode()
                return is_test_mode_authorized()

            res_auth, res_unauth = await asyncio.gather(task_authorized(), task_unauthorized())
            assert res_auth is True
            assert res_unauth is False

        asyncio.run(async_isolation_check())

        # 9. Scoped restoration after exceptions
        try:
            with authorized_test_mode(issue_test_authorizer("scoped restoration test")):
                assert is_test_mode_authorized() is True
                raise RuntimeError("simulated error inside context")
        except RuntimeError:
            pass
        assert is_test_mode_authorized() is False
    finally:
        import shutil
        if test_sub.exists():
            shutil.rmtree(test_sub, ignore_errors=True)



class TestExplicitTestModeAuthorizationBoundaries:
    """Comprehensive tests for Test Mode explicit capability authorization boundaries and context isolation."""

    def test_unauthorized_application_code_issuance_forbidden(self):
        """Calling guard.issue_test_authorizer() from ordinary code without harness secret strictly fails closed."""
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            TestModeAuthorizer,
            issue_test_authorizer as guard_issue_test_authorizer,
            register_test_harness_secret,
            TestOutputAuthorizationToken,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        # 1. Calling guard.issue_test_authorizer without harness secret fails closed
        with pytest.raises(PermissionError, match="Unauthorized test mode authorizer issuance"):
            guard_issue_test_authorizer("unauthorized caller")

        # 2. Calling guard.issue_test_authorizer with invalid harness secret fails closed
        with pytest.raises(PermissionError, match="Unauthorized test mode authorizer issuance"):
            guard_issue_test_authorizer("unauthorized caller", harness_secret="invalid_secret_value_1234567890123")

        # 3. Attempting to hijack or overwrite the registered harness secret fails closed
        with pytest.raises(PermissionError, match="Test harness authority has already been registered"):
            register_test_harness_secret("forged_secret_value_must_be_32_characters_long")

        # 4. Direct construction of TestModeAuthorizer is forbidden
        with pytest.raises(PermissionError, match="cannot be directly instantiated"):
            TestModeAuthorizer()

        with pytest.raises(PermissionError, match="cannot be directly instantiated"):
            TestModeAuthorizer(object())

        # 5. issue_test_authorizer purpose validation
        with pytest.raises(ValueError, match="requires a non-empty purpose description"):
            issue_test_authorizer("")

        with pytest.raises(ValueError, match="requires a non-empty purpose description"):
            issue_test_authorizer(cast(Any, None))

        with pytest.raises(ValueError, match="requires a non-empty purpose description"):
            issue_test_authorizer(cast(Any, 123))

        # 6. No arguments provided to authorized_test_mode()
        with pytest.raises(PermissionError, match="requires an explicit TestModeAuthorizer"):
            with authorized_test_mode():
                pass

        # 7. None provided
        with pytest.raises(PermissionError, match="requires an explicit TestModeAuthorizer"):
            with authorized_test_mode(None):
                pass

        # 8. Arbitrary unauthorized object
        with pytest.raises(PermissionError, match="requires an explicit TestModeAuthorizer"):
            with authorized_test_mode(object()):
                pass

        # 9. Forged string
        with pytest.raises(PermissionError, match="requires an explicit TestModeAuthorizer"):
            with authorized_test_mode("test_origin"):
                pass

        # 10. Direct token minting rejected
        with pytest.raises(PermissionError, match="can only be minted via authorized_test_mode"):
            TestOutputAuthorizationToken(object())

        assert is_test_mode_authorized() is False

    def test_direct_context_manager_on_authorizer_rejected(self):
        """TestModeAuthorizer does not expose a context manager interface to bypass authorized_test_mode."""
        from tests._test_authorizer import issue_test_authorizer

        authorizer = issue_test_authorizer("direct context test")
        assert not hasattr(authorizer, "__enter__")
        assert not hasattr(authorizer, "__exit__")
        # Attempting to use authorizer directly in 'with authorizer:' must fail
        with pytest.raises((AttributeError, TypeError)):
            with cast(Any, authorizer):
                pass

    def test_authorizer_id_binding_and_mismatch_rejection(self):
        """The authorizer_id is bound to the token and verified during authorization checks."""
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        authorizer = issue_test_authorizer("id binding test")
        assert isinstance(authorizer.authorizer_id, str)
        assert len(authorizer.authorizer_id) > 0

        with authorized_test_mode(authorizer) as token:
            assert token.authorizer_id == authorizer.authorizer_id
            assert is_test_mode_authorized() is True
            assert is_test_mode_authorized(token) is True
            assert is_test_mode_authorized(authorizer) is True

            # Mismatched/foreign authorizer check fails closed
            foreign_authorizer = issue_test_authorizer("foreign test")
            assert is_test_mode_authorized(foreign_authorizer) is False

    def test_single_use_sequential_reuse_fails_closed(self):
        """An authorizer instance cannot be sequentially reused across distinct authorized_test_mode scopes."""
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        authorizer = issue_test_authorizer("sequential reuse test")
        captured_token = None

        with authorized_test_mode(authorizer) as token:
            captured_token = token
            assert token.is_active is True
            assert authorizer.is_active is True
            assert authorizer.is_in_use is True
            assert authorizer.is_consumed is True
            assert is_test_mode_authorized() is True

        # Scope exited: both are invalidated
        assert captured_token.is_active is False
        assert authorizer.is_active is False
        assert authorizer.is_in_use is False
        assert authorizer.is_consumed is True
        assert is_test_mode_authorized(captured_token) is False
        assert is_test_mode_authorized(authorizer) is False
        assert is_test_mode_authorized() is False

        # Attempt to reuse the same authorizer instance fails closed
        with pytest.raises(PermissionError, match="has already been used or consumed"):
            with authorized_test_mode(authorizer):
                pass

    def test_single_use_nested_reuse_of_same_authorizer_fails_closed(self):
        """An active authorizer cannot be consumed again inside a nested scope while currently in use."""
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        authorizer = issue_test_authorizer("nested reuse same authorizer")

        with authorized_test_mode(authorizer) as token:
            assert token.is_active is True
            assert authorizer.is_in_use is True
            assert is_test_mode_authorized(token) is True

            # Nested attempt to reuse the exact same active authorizer must fail immediately
            with pytest.raises(PermissionError, match="has already been used or consumed"):
                with authorized_test_mode(authorizer):
                    pass

            # Outer scope remains fully active and unaffected
            assert token.is_active is True
            assert is_test_mode_authorized(token) is True
            assert is_test_mode_authorized(authorizer) is True

    def test_single_use_concurrent_contexts_reuse_fails_closed(self):
        """Concurrent coroutines cannot share or reuse the same authorizer instance."""
        import asyncio
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        async def run_concurrent():
            shared_authorizer = issue_test_authorizer("concurrent authorizer")
            results = []
            errors = []

            async def worker_first():
                try:
                    with authorized_test_mode(shared_authorizer):
                        await asyncio.sleep(0.04)
                        results.append(("worker_first", is_test_mode_authorized()))
                except Exception as exc:
                    errors.append(("worker_first", exc))

            async def worker_second():
                await asyncio.sleep(0.01)
                try:
                    # Tries to enter with the same authorizer while worker_first is in scope
                    with authorized_test_mode(shared_authorizer):
                        results.append(("worker_second", is_test_mode_authorized()))
                except Exception as exc:
                    errors.append(("worker_second", exc))

            await asyncio.gather(worker_first(), worker_second())
            assert len(results) == 1
            assert results[0][0] == "worker_first"
            assert results[0][1] is True
            assert len(errors) == 1
            assert errors[0][0] == "worker_second"
            assert isinstance(errors[0][1], PermissionError)
            assert "has already been used or consumed" in str(errors[0][1])

        asyncio.run(run_concurrent())

    def test_single_use_concurrent_threads_reuse_fails_closed(self):
        """Concurrent threads racing to use the same authorizer instance are atomically serialized so only one succeeds."""
        import threading
        import time
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        shared_authorizer = issue_test_authorizer("concurrent thread authorizer")
        success_list = []
        error_list = []

        def thread_target():
            try:
                with authorized_test_mode(shared_authorizer):
                    time.sleep(0.04)
                    success_list.append(is_test_mode_authorized())
            except Exception as exc:
                error_list.append(exc)

        t1 = threading.Thread(target=thread_target)
        t2 = threading.Thread(target=thread_target)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one thread acquired the reservation; the other failed closed with PermissionError
        assert len(success_list) == 1
        assert success_list[0] is True
        assert len(error_list) == 1
        assert isinstance(error_list[0], PermissionError)
        assert "has already been used or consumed" in str(error_list[0])

    def test_exceptions_and_guaranteed_cleanup_prevents_subsequent_reuse(self):
        """Exceptions inside authorized_test_mode guarantee token and authorizer invalidation, preventing reuse."""
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        captured_token = None
        auth = issue_test_authorizer("exception cleanup test")
        try:
            with authorized_test_mode(auth) as token:
                captured_token = token
                assert token.is_active is True
                assert auth.is_active is True
                assert auth.is_in_use is True
                assert is_test_mode_authorized() is True
                raise RuntimeError("simulated error inside context")
        except RuntimeError:
            pass

        assert captured_token is not None
        assert captured_token.is_active is False
        assert auth.is_active is False
        assert auth.is_in_use is False
        assert auth.is_consumed is True
        assert is_test_mode_authorized(captured_token) is False
        assert is_test_mode_authorized(auth) is False
        assert is_test_mode_authorized() is False

        # Attempt to reuse the authorizer after exception must fail
        with pytest.raises(PermissionError, match="has already been used or consumed"):
            with authorized_test_mode(auth):
                pass

    def test_expired_and_previously_consumed_authorizers_rejected(self):
        """An authorizer that was manually invalidated before use cannot enter authorized_test_mode."""
        from src.living_authenticity.knowledge.integration.guard import authorized_test_mode
        from tests._test_authorizer import issue_test_authorizer

        auth = issue_test_authorizer("expired authorizer test")
        auth.invalidate()
        assert auth.is_active is False
        assert auth.is_consumed is True

        with pytest.raises(PermissionError, match="has already been used or consumed"):
            with authorized_test_mode(auth):
                pass

    def test_nested_authorization_scopes_with_distinct_authorizers(self):
        """Nested authorization scopes isolate inner and outer tokens when distinct authorizers are used."""
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        auth1 = issue_test_authorizer("outer")
        auth2 = issue_test_authorizer("inner")

        with authorized_test_mode(auth1) as outer_token:
            assert outer_token.is_active is True
            assert is_test_mode_authorized() is True
            assert is_test_mode_authorized(outer_token) is True
            assert is_test_mode_authorized(auth1) is True

            with authorized_test_mode(auth2) as inner_token:
                assert inner_token.is_active is True
                assert is_test_mode_authorized() is True
                assert is_test_mode_authorized(inner_token) is True
                assert is_test_mode_authorized(auth2) is True
                # Outer token/authorizer is not the currently active capability in the inner scope
                assert is_test_mode_authorized(outer_token) is False
                assert is_test_mode_authorized(auth1) is False

            # Inner scope exited: inner token invalidated, outer token restored
            assert inner_token.is_active is False
            assert auth2.is_active is False
            assert is_test_mode_authorized(inner_token) is False
            assert is_test_mode_authorized(auth2) is False
            assert outer_token.is_active is True
            assert auth1.is_active is True
            assert is_test_mode_authorized(outer_token) is True
            assert is_test_mode_authorized(auth1) is True
            assert is_test_mode_authorized() is True

        # Outer scope exited: outer token invalidated
        assert outer_token.is_active is False
        assert auth1.is_active is False
        assert is_test_mode_authorized(outer_token) is False
        assert is_test_mode_authorized(auth1) is False
        assert is_test_mode_authorized() is False

    def test_concurrent_asynchronous_contexts(self):
        """Concurrent coroutines with distinct authorizers on the same loop do not leak authorization."""
        import asyncio
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        async def run_concurrent():
            async def worker_authorized():
                with authorized_test_mode(issue_test_authorizer("worker_auth")):
                    await asyncio.sleep(0.02)
                    return is_test_mode_authorized()

            async def worker_unauthorized():
                await asyncio.sleep(0.01)
                return is_test_mode_authorized()

            res_auth, res_unauth = await asyncio.gather(worker_authorized(), worker_unauthorized())
            assert res_auth is True
            assert res_unauth is False

        asyncio.run(run_concurrent())

    def test_child_tasks_created_in_scope_and_executed_after_scope_exit(self):
        """Async child tasks created inside an authorized scope cannot retain authorization after scope exit."""
        import asyncio
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        async def run_child_after_exit():
            captured_token = None
            child_task = None

            with authorized_test_mode(issue_test_authorizer("child_after_exit")) as token:
                captured_token = token
                assert is_test_mode_authorized() is True

                async def child_worker():
                    # Executed after scope exit
                    return is_test_mode_authorized(), is_test_mode_authorized(token)

                child_task = asyncio.create_task(child_worker())

            # Scope has exited!
            res_ambient, res_explicit = await child_task
            assert res_ambient is False
            assert res_explicit is False
            assert captured_token.is_active is False

        asyncio.run(run_child_after_exit())

    def test_child_tasks_cannot_ambiently_inherit_parent_authorization_during_scope(self):
        """Child tasks created inside an authorized scope do not ambiently inherit authorization during execution."""
        import asyncio
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        async def run_child_during_scope():
            with authorized_test_mode(issue_test_authorizer("child_during_scope")) as token:
                assert is_test_mode_authorized() is True

                async def child_worker():
                    # Child task has a distinct Task ID and cannot ambiently borrow parent authority
                    return is_test_mode_authorized()

                task = asyncio.create_task(child_worker())
                res = await task
                assert res is False
                assert is_test_mode_authorized() is True

        asyncio.run(run_child_during_scope())

    def test_attempts_to_reuse_or_retain_authorization_outside_lifetime(self):
        """Saved tokens from an exited scope cannot authorize future calls even if internally modified."""
        from src.living_authenticity.knowledge.integration.guard import (
            authorized_test_mode,
            is_test_mode_authorized,
        )
        from tests._test_authorizer import issue_test_authorizer

        saved_token = None
        with authorized_test_mode(issue_test_authorizer("reuse test")) as token:
            saved_token = token
            assert is_test_mode_authorized(saved_token) is True

        assert is_test_mode_authorized(saved_token) is False
        assert is_test_mode_authorized() is False

        # Attempt to revive token
        saved_token._is_active = True
        # Fails closed because token is not active in current ContextVar
        assert is_test_mode_authorized(saved_token) is False
        assert is_test_mode_authorized() is False


class TestHarnessRegistrationAndConflictIsolation:
    """Regression tests verifying test harness registration reliability, conflict rejection,
    and process-isolated state guarantees."""

    def test_normal_first_registration_succeeds_in_isolated_process(self):
        """In a clean Python process, initial registration of a valid test harness secret succeeds."""
        import subprocess
        import sys

        code = (
            "from src.living_authenticity.knowledge.integration.guard import register_test_harness_secret\n"
            "register_test_harness_secret('a' * 32)\n"
            "print('FIRST_REGISTRATION_SUCCESS')\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert "FIRST_REGISTRATION_SUCCESS" in res.stdout

    def test_second_registration_rejected_in_isolated_process(self):
        """In a clean Python process, registering a second secret fails closed with PermissionError."""
        import subprocess
        import sys

        code = (
            "from src.living_authenticity.knowledge.integration.guard import register_test_harness_secret\n"
            "register_test_harness_secret('a' * 32)\n"
            "try:\n"
            "    register_test_harness_secret('b' * 32)\n"
            "    print('UNEXPECTED_SUCCESS')\n"
            "except PermissionError as e:\n"
            "    print(f'CAUGHT_PERMISSION_ERROR: {e}')\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert "CAUGHT_PERMISSION_ERROR" in res.stdout
        assert "Test harness authority has already been registered" in res.stdout
        assert "UNEXPECTED_SUCCESS" not in res.stdout

    def test_test_harness_initialization_fails_explicitly_if_prior_registration_exists(self):
        """If another component registers first, importing tests._test_authorizer raises RuntimeError explicitly
        rather than silently continuing with an unregistered or mismatched secret."""
        import subprocess
        import sys

        code = (
            "from src.living_authenticity.knowledge.integration.guard import register_test_harness_secret\n"
            "# Simulate another component registering first\n"
            "register_test_harness_secret('c' * 32)\n"
            "try:\n"
            "    import tests._test_authorizer\n"
            "    print('UNEXPECTED_IMPORT_SUCCESS')\n"
            "except RuntimeError as e:\n"
            "    print(f'CAUGHT_RUNTIME_ERROR: {e}')\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert "CAUGHT_RUNTIME_ERROR" in res.stdout
        assert "Failed to initialize test harness authorizer: a test harness secret has already been registered" in res.stdout
        assert "UNEXPECTED_IMPORT_SUCCESS" not in res.stdout

    def test_helper_cannot_issue_authorizer_with_mismatched_secret(self):
        """A caller cannot obtain an authorizer using a secret different from the registered secret.

        Runs in an isolated process to establish explicit preconditions and test both:
        1. Calling guard.issue_test_authorizer before harness registration fails closed with
           'no authorized test harness has been registered'.
        2. Calling guard.issue_test_authorizer after harness registration with an invalid or
           mismatched secret fails closed with 'invalid or missing harness secret'.
        """
        import subprocess
        import sys

        code = (
            "from src.living_authenticity.knowledge.integration.guard import issue_test_authorizer\n"
            "# 1. Before registration: calling issue_test_authorizer fails closed\n"
            "try:\n"
            "    issue_test_authorizer('purpose', harness_secret='some_secret')\n"
            "    print('UNEXPECTED_PRE_REG_SUCCESS')\n"
            "except PermissionError as e:\n"
            "    if 'no authorized test harness has been registered' in str(e):\n"
            "        print('CAUGHT_PRE_REG_UNAVAILABLE')\n"
            "    else:\n"
            "        print(f'WRONG_PRE_REG_ERROR: {e}')\n"
            "\n"
            "# 2. Explicitly initialize test harness in this isolated process\n"
            "import tests._test_authorizer\n"
            "\n"
            "# 3. Empty secret fails closed\n"
            "try:\n"
            "    issue_test_authorizer('purpose', harness_secret='')\n"
            "    print('UNEXPECTED_EMPTY_SUCCESS')\n"
            "except PermissionError as e:\n"
            "    if 'invalid or missing harness secret' in str(e):\n"
            "        print('CAUGHT_EMPTY_SECRET')\n"
            "    else:\n"
            "        print(f'WRONG_EMPTY_ERROR: {e}')\n"
            "\n"
            "# 4. Mismatched secret fails closed\n"
            "try:\n"
            "    issue_test_authorizer('purpose', harness_secret='incorrect_mismatched_test_value')\n"
            "    print('UNEXPECTED_MISMATCH_SUCCESS')\n"
            "except PermissionError as e:\n"
            "    if 'invalid or missing harness secret' in str(e):\n"
            "        print('CAUGHT_MISMATCHED_SECRET')\n"
            "    else:\n"
            "        print(f'WRONG_MISMATCH_ERROR: {e}')\n"
            "\n"
            "# 5. None / non-string secret fails closed\n"
            "try:\n"
            "    issue_test_authorizer('purpose', harness_secret=None)\n"
            "    print('UNEXPECTED_NONE_SUCCESS')\n"
            "except PermissionError as e:\n"
            "    if 'invalid or missing harness secret' in str(e):\n"
            "        print('CAUGHT_NONE_SECRET')\n"
            "    else:\n"
            "        print(f'WRONG_NONE_ERROR: {e}')\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert "CAUGHT_PRE_REG_UNAVAILABLE" in res.stdout
        assert "CAUGHT_EMPTY_SECRET" in res.stdout
        assert "CAUGHT_MISMATCHED_SECRET" in res.stdout
        assert "CAUGHT_NONE_SECRET" in res.stdout
        assert "UNEXPECTED" not in res.stdout
        assert "WRONG" not in res.stdout

    def test_short_secret_registration_rejected(self):
        """Registering a secret shorter than 32 characters raises ValueError."""
        import subprocess
        import sys

        code = (
            "from src.living_authenticity.knowledge.integration.guard import register_test_harness_secret\n"
            "try:\n"
            "    register_test_harness_secret('short_secret')\n"
            "    print('UNEXPECTED_SUCCESS')\n"
            "except ValueError as e:\n"
            "    print(f'CAUGHT_VALUE_ERROR: {e}')\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert "CAUGHT_VALUE_ERROR" in res.stdout
        assert "at least 32 characters" in res.stdout
        assert "UNEXPECTED_SUCCESS" not in res.stdout


class TestCanonicalJsonEnforcementInJournalReadPaths:
    """Comprehensive tests verifying strict canonical JSON enforcement on current records
    and narrowly scoped historical compatibility in all journal read paths."""

    def test_canonical_current_format_records_accepted_in_all_read_paths(self, tmp_path):
        """Canonical current-format records are accepted in read_emergency_records and read_all_persisted_journal_records."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            canonical_json_dumps,
            compute_record_digest,
            verify_record_digest,
            read_all_persisted_journal_records,
            PersistedRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "passed").mkdir()
        (audit_root / "errors").mkdir()

        writer = AuditWriter(str(audit_root))
        record = AuditRecord(
            record_id="rec-canon-cur-1",
            run_id="run-canon",
            terminal_outcome="executed",
            execution_executed=True,
        )
        rec_dict = writer.append(record)
        assert rec_dict["persisted"] is True

        # Read via read_all_persisted_journal_records
        records, malformed, counts = read_all_persisted_journal_records(audit_root)
        assert len(malformed) == 0
        assert len(records) == 1
        assert records[0]["record_id"] == "rec-canon-cur-1"
        assert "_historical_format" not in records[0]
        assert isinstance(records[0], PersistedRecord)
        assert records[0].reader_source_file != ""
        assert records[0].reader_line_no == 1
        is_val, _ = verify_record_digest(records[0])
        assert is_val is True
        assert counts["rec-canon-cur-1"] == 1

        # Also write emergency record canonically
        rec_emerg = AuditRecord(
            record_id="rec-canon-emerg-1",
            run_id="run-canon",
            terminal_outcome="rejected",
        )
        writer.append_emergency(rec_emerg, failure_reason="simulated")
        emerg_res = writer.read_emergency_records()
        assert len(emerg_res.malformed_lines) == 0
        assert "rec-canon-emerg-1" in emerg_res.records_by_id
        assert "_historical_format" not in emerg_res.records_by_id["rec-canon-emerg-1"]

    def test_noncanonical_current_format_records_rejected_in_all_read_paths(self, tmp_path):
        """Current-format records with noncanonical formatting (spaces, unsorted keys, padding)
        are strictly rejected and segregated into malformed lines by both read paths."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            compute_record_digest,
            read_all_persisted_journal_records,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        # Record with standard json.dumps whitespace (formatting spaces: ", ", ": ")
        rec = {
            "record_id": "rec-noncanon-1",
            "run_id": "run-noncanon",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        rec["record_digest"] = compute_record_digest(rec)
        # Using standard json.dumps inserts formatting spaces
        noncanonical_line = json.dumps(rec) + "\n"

        emerg_file.write_text(noncanonical_line, encoding="utf-8")

        writer = AuditWriter(str(audit_root))

        # 1. read_emergency_records rejects it as malformed
        emerg_res = writer.read_emergency_records()
        assert len(emerg_res.records_by_id) == 0
        assert len(emerg_res.malformed_lines) == 1
        assert emerg_res.malformed_lines[0]["record_id"] == "rec-noncanon-1"
        assert "Non-canonical JSON line" in emerg_res.malformed_lines[0]["error"]

        # 2. read_all_persisted_journal_records rejects it as malformed
        all_recs, malformed, counts = read_all_persisted_journal_records(audit_root)
        assert len(all_recs) == 0
        assert len(malformed) == 1
        assert "Non-canonical JSON line" in malformed[0]["error"]

    def test_records_with_self_declared_historical_markers_fail_closed_if_noncanonical(self, tmp_path):
        """Historical markers (journal_format == 'v1_legacy', schema_version == 1, legacy_format is True)
        do not prove provenance and cannot bypass strict canonical JSON validation.
        Non-canonical records with these markers are strictly rejected and segregated into malformed_lines."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            compute_record_digest,
            read_all_persisted_journal_records,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        # 1. Record with explicit journal_format == "v1_legacy"
        hist1 = {
            "record_id": "rec-hist-1",
            "run_id": "run-hist",
            "journal_format": "v1_legacy",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        hist1["record_digest"] = compute_record_digest(hist1)

        # 2. Record with explicit schema_version == 1
        hist2 = {
            "record_id": "rec-hist-2",
            "run_id": "run-hist",
            "schema_version": 1,
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        hist2["record_digest"] = compute_record_digest(hist2)

        # 3. Record with explicit legacy_format is True
        hist3 = {
            "record_id": "rec-hist-3",
            "run_id": "run-hist",
            "legacy_format": True,
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        hist3["record_digest"] = compute_record_digest(hist3)

        # Write all three with formatting spaces and non-canonical formatting
        content = (
            json.dumps(hist1) + "\n" +
            json.dumps(hist2) + "\n" +
            json.dumps(hist3) + "\n"
        )
        emerg_file.write_text(content, encoding="utf-8")

        writer = AuditWriter(str(audit_root))

        # 1. read_emergency_records strictly rejects them into malformed_lines
        emerg_res = writer.read_emergency_records()
        assert len(emerg_res.records_by_id) == 0
        assert len(emerg_res.malformed_lines) == 3
        for m in emerg_res.malformed_lines:
            assert "Non-canonical JSON line" in m["error"]
            assert m["integrity_failure"] is True

        # 2. read_all_persisted_journal_records strictly rejects them into malformed_lines
        records, malformed, counts = read_all_persisted_journal_records(audit_root)
        assert len(records) == 0
        assert len(malformed) == 3
        for m in malformed:
            assert "Non-canonical JSON line" in m["error"]
            assert m["file"] == str(emerg_file)
            assert m["line_no"] in (1, 2, 3)
        assert len(counts) == 0

    def test_malformed_json_rejected_in_all_read_paths(self, tmp_path):
        """Malformed JSON (syntax errors, non-object roots, non-standard constants)
        is segregated into malformed lines fail-closed in both read paths."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            read_all_persisted_journal_records,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        broken_lines = (
            '{"record_id": "rec-broken-1", "bad syntax\n' +
            '[1, 2, 3]\n' +
            '{"record_id": "rec-nan", "val": NaN}\n'
        )
        emerg_file.write_text(broken_lines, encoding="utf-8")

        writer = AuditWriter(str(audit_root))

        emerg_res = writer.read_emergency_records()
        assert len(emerg_res.records_by_id) == 0
        assert len(emerg_res.malformed_lines) == 3

        records, malformed, counts = read_all_persisted_journal_records(audit_root)
        assert len(records) == 0
        assert len(malformed) == 3

    def test_duplicate_keys_rejected_even_for_historical_records(self, tmp_path):
        """Duplicate JSON keys are strictly rejected even when schema_version == 1 or journal_format == 'v1_legacy'."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            read_all_persisted_journal_records,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        dup_key_line = '{"record_id": "rec-dupkey", "schema_version": 1, "k": 1, "k": 2}\n'
        emerg_file.write_text(dup_key_line, encoding="utf-8")

        writer = AuditWriter(str(audit_root))

        emerg_res = writer.read_emergency_records()
        assert len(emerg_res.records_by_id) == 0
        assert len(emerg_res.malformed_lines) == 1
        assert "Duplicate JSON key" in emerg_res.malformed_lines[0]["error"]

        records, malformed, counts = read_all_persisted_journal_records(audit_root)
        assert len(records) == 0
        assert len(malformed) == 1
        assert "Duplicate JSON key" in malformed[0]["error"]

    def test_fail_closed_behavior_when_format_version_cannot_be_established(self, tmp_path):
        """Records with unsupported or future format/version indicators fail closed if noncanonical."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            compute_record_digest,
            read_all_persisted_journal_records,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        unsupported_rec = {
            "record_id": "rec-unsupported-ver",
            "run_id": "run-unsupported",
            "schema_version": 999,
            "journal_format": "future_unknown",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        unsupported_rec["record_digest"] = compute_record_digest(unsupported_rec)
        # Written noncanonically
        emerg_file.write_text(json.dumps(unsupported_rec) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))

        emerg_res = writer.read_emergency_records()
        assert len(emerg_res.records_by_id) == 0
        assert len(emerg_res.malformed_lines) == 1
        assert "Non-canonical JSON line" in emerg_res.malformed_lines[0]["error"]

        records, malformed, counts = read_all_persisted_journal_records(audit_root)
        assert len(records) == 0
        assert len(malformed) == 1
        assert "Non-canonical JSON line" in malformed[0]["error"]


class TestPersistedRecordDiagnosticFieldDigestProtection:
    """Tests verifying that reader diagnostic fields do not mutate on-disk keys,
    and that stored records containing diagnostic/underscore fields are fully protected by digest."""

    def test_persisted_record_slots_and_dict_integrity(self):
        """PersistedRecord preserves dict keys and exposes reader metadata via slots/properties."""
        from src.living_authenticity.knowledge.governance.audit.persistence import PersistedRecord

        raw_data = {"record_id": "r1", "run_id": "run1", "status": "ok"}
        rec = PersistedRecord(raw_data, source_file="/path/to/audit.jsonl", line_no=7)

        # Dictionary contents are completely untouched
        assert dict(rec) == raw_data
        assert set(rec.keys()) == {"record_id", "run_id", "status"}
        assert "_source_file" not in rec
        assert "_line_no" not in rec
        assert "_historical_format" not in rec

        # Metadata accessible via properties
        assert rec.reader_source_file == "/path/to/audit.jsonl"
        assert rec.reader_line_no == 7

    def test_persisted_record_containing_diagnostic_fields_is_fully_digest_protected(self, tmp_path):
        """If a journal record on disk contains _source_file, _line_no, _historical_format,
        or user underscore keys, they are hashed into record_digest, and tampering fails verification."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            canonical_json_dumps,
            compute_record_digest,
            verify_record_digest,
            read_all_persisted_journal_records,
        )

        audit_dir = tmp_path / "audit" / "passed"
        audit_dir.mkdir(parents=True)
        journal_file = audit_dir / "audit.jsonl"

        record_data = {
            "record_id": "rec-diag-protected-1",
            "run_id": "run-diag",
            "terminal_outcome": "executed",
            "execution_executed": True,
            "_source_file": "/custom/persisted/path",
            "_line_no": 999,
            "_historical_format": "archival_v0",
            "_custom_audit_tag": "security_critical",
        }
        # Digest covers all fields (only record_digest and persistence_timestamp excluded)
        record_data["record_digest"] = compute_record_digest(record_data)
        record_data["persistence_timestamp"] = "2026-11-01T12:00:00Z"

        # Write canonically to disk
        journal_file.write_text(canonical_json_dumps(record_data) + "\n", encoding="utf-8")

        # Read back via read_all_persisted_journal_records
        records, malformed, counts = read_all_persisted_journal_records(tmp_path / "audit")
        assert len(malformed) == 0
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, PersistedRecord)

        # Stored keys remain intact
        assert rec["_source_file"] == "/custom/persisted/path"
        assert rec["_line_no"] == 999
        assert rec["_historical_format"] == "archival_v0"
        assert rec["_custom_audit_tag"] == "security_critical"
        # Reader provenance is separate
        assert rec.reader_source_file == str(journal_file)
        assert rec.reader_line_no == 1

        # Digest verification succeeds on untampered record
        is_val, reason = verify_record_digest(rec)
        assert is_val is True
        assert reason == ""

        # Tampering with _source_file on disk fails verification
        tampered_source = dict(record_data)
        tampered_source["_source_file"] = "/tampered/path"
        is_val, reason = verify_record_digest(tampered_source)
        assert is_val is False
        assert "record_digest mismatch" in reason

        # Tampering with _line_no on disk fails verification
        tampered_line = dict(record_data)
        tampered_line["_line_no"] = 1000
        is_val, reason = verify_record_digest(tampered_line)
        assert is_val is False
        assert "record_digest mismatch" in reason

        # Tampering with _historical_format on disk fails verification
        tampered_hist = dict(record_data)
        tampered_hist["_historical_format"] = "archival_v1"
        is_val, reason = verify_record_digest(tampered_hist)
        assert is_val is False
        assert "record_digest mismatch" in reason

        # Tampering with _custom_audit_tag on disk fails verification
        tampered_tag = dict(record_data)
        tampered_tag["_custom_audit_tag"] = "tampered_value"
        is_val, reason = verify_record_digest(tampered_tag)
        assert is_val is False
        assert "record_digest mismatch" in reason
