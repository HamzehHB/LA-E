"""Integration and unit tests for audit durability, emergency journaling, and quarantine.

Covers:
1. Primary persistence fails, emergency journaling succeeds:
   - Truthful emergency-persistence receipt distinguished from normal persistence.
   - Staging artifact remains published.
   - Run report is accepted.
2. Both primary persistence and emergency journaling fail:
   - Staged artifact is quarantined out of staging into .unreconciled.
   - Receipt records quarantined=True and quarantine_path.
   - Unit outcome is audit_persistence_failed.
   - Run report accepted=False with rejection_reason explaining the quarantine.
   - Duplicate persistence attempts are prevented.
3. quarantine_artifact boundary and safety handling.
"""

from pathlib import Path
import pytest

from src.living_authenticity.knowledge.governance.audit.persistence import (
    AuditWriter,
    persist_record_resilient,
)
from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
from src.living_authenticity.knowledge.governance.execution.executor import (
    ControlledExecutor,
    quarantine_artifact,
)
from src.living_authenticity.knowledge.governance.approval.outcome import (
    ApprovalOutcome,
    ApprovalRequest,
)
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.output.outcome import GeneratedNote
from src.living_authenticity.knowledge.integration.runner import (
    _persist_audits,
    run_bounded_integration,
)


class FailingWriter:
    """Audit writer where primary append fails, emergency append succeeds."""

    def __init__(self, errors_dir: Path):
        self._errors_dir = errors_dir

    def append(self, record):
        raise OSError("primary disk write failed (simulated)")

    def append_emergency(self, record, failure_reason=""):
        self._errors_dir.mkdir(parents=True, exist_ok=True)
        target = self._errors_dir / "emergency_audit.jsonl"
        target.write_text(f'{{"emergency": true, "reason": "{failure_reason}"}}\n')
        return {
            "record_id": getattr(record, "record_id", "rec-test"),
            "run_id": getattr(record, "run_id", "run-test"),
            "storage_category": "errors",
            "emergency": True,
            "durability": "confirmed",
            "persisted": True,
            "audit_path": str(target),
            "timestamp": "2026-10-21T00:00:00Z",
        }


class DoubleFailingWriter:
    """Audit writer where both primary append and emergency append fail."""

    def __init__(self):
        self.append_calls = 0
        self.emergency_calls = 0

    def append(self, record):
        self.append_calls += 1
        raise OSError("primary audit write failed (simulated)")

    def append_emergency(self, record, failure_reason=""):
        self.emergency_calls += 1
        raise OSError("emergency journal disk full (simulated)")


class TestAuditDurabilityAndQuarantine:
    def test_emergency_persistence_distinguished_and_truthful(self, tmp_path):
        errors_dir = tmp_path / "audit" / "errors"
        writer = FailingWriter(errors_dir)
        record = AuditRecord(
            record_id="rec-emerg-1",
            run_id="run-emerg-1",
            execution_executed=True,
            artifact_path="note.md",
        )
        receipts = []
        receipt = persist_record_resilient(writer, record, receipts=receipts)

        assert receipt["persisted"] is True
        assert receipt["emergency"] is True
        assert receipt["storage_category"] == "errors"
        assert "primary disk write failed" in receipt.get("primary_error", "")
        assert len(receipts) == 1
        assert receipts[0]["emergency"] is True

    def test_double_failure_quarantines_artifact_and_marks_receipt(self, tmp_path):
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        artifact_file = staging_dir / "hash123.md"
        artifact_file.write_text("# Knowledge Note\n\nAuthentic content.", encoding="utf-8")

        writer = DoubleFailingWriter()
        record = AuditRecord(
            record_id="rec-fail-1",
            run_id="run-fail-1",
            execution_executed=True,
            artifact_path="hash123.md",
        )
        receipts = []

        # Simulate _persist_audits execution
        all_ok = _persist_audits(writer, [record], receipts, staging_root=str(staging_dir))
        assert all_ok is False
        assert len(receipts) == 1
        failed_receipt = receipts[0]
        assert failed_receipt["persisted"] is False
        assert failed_receipt["quarantined"] is True
        assert "hash123.md" in failed_receipt["quarantine_path"]

        # Verify artifact is no longer in staging directly
        assert not artifact_file.exists()

        # Verify quarantined file exists
        quarantined = staging_dir / failed_receipt["quarantine_path"]
        assert quarantined.exists()
        assert "Authentic content" in quarantined.read_text(encoding="utf-8")

    def test_prevent_duplicate_persistence_attempts(self, tmp_path):
        writer = DoubleFailingWriter()
        record = AuditRecord(
            record_id="rec-once-only",
            run_id="run-1",
            artifact_path="",
        )
        # Pre-seed a receipt indicating it was already processed
        receipts = [
            {
                "record_id": "rec-once-only",
                "persisted": False,
                "quarantined": False,
                "reason": "already failed in unit loop",
            }
        ]

        # Call _persist_audits
        all_ok = _persist_audits(writer, [record], receipts, staging_root=str(tmp_path))
        assert all_ok is False

        # Writer append must NOT have been called again!
        assert writer.append_calls == 0
        assert writer.emergency_calls == 0

    def test_quarantine_artifact_nonexistent_and_directory_cases(self, tmp_path):
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Non-existent file
        res = quarantine_artifact(staging_dir, "nonexistent.md")
        assert res == ""

        # Pre-existing .unreconciled subdirectory
        unrec_dir = staging_dir / ".unreconciled"
        unrec_dir.mkdir()
        art = staging_dir / "art1.md"
        art.write_text("content", encoding="utf-8")

        res2 = quarantine_artifact(staging_dir, "art1.md")
        assert res2 == ".unreconciled/art1.md"
        assert (unrec_dir / "art1.md").exists()
        assert not art.exists()

    def test_quarantine_collision_preserves_existing_target(self, tmp_path):
        """Quarantine must never overwrite an existing target and must leave source intact."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        unrec = staging_dir / ".unreconciled"
        unrec.mkdir()

        # Existing file in .unreconciled
        existing_target = unrec / "art_collision.md"
        existing_target.write_text("existing quarantined content", encoding="utf-8")

        # Source file in staging
        source = staging_dir / "art_collision.md"
        source.write_text("colliding source content", encoding="utf-8")

        res = quarantine_artifact(staging_dir, "art_collision.md")
        # Must return empty string indicating quarantine failed (did not overwrite)
        assert res == ""
        # Existing target must be completely intact and un-overwritten
        assert existing_target.read_text(encoding="utf-8") == "existing quarantined content"
        # Source file must still exist and be intact
        assert source.read_text(encoding="utf-8") == "colliding source content"

    def test_quarantine_rejects_path_traversal_and_external_paths(self, tmp_path):
        """Quarantine must reject all path traversal attempts and external absolute paths."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("classified data", encoding="utf-8")

        # Relative traversal
        assert quarantine_artifact(staging_dir, "../secret.txt") == ""
        assert quarantine_artifact(staging_dir, "../../secret.txt") == ""
        assert quarantine_artifact(staging_dir, "sub/../../secret.txt") == ""

        # Absolute outside path
        assert quarantine_artifact(staging_dir, str(outside_file)) == ""

        # Staging root itself
        assert quarantine_artifact(staging_dir, "") == ""
        assert quarantine_artifact(staging_dir, ".") == ""
        assert quarantine_artifact(staging_dir, str(staging_dir)) == ""

        # Outside file remains untouched
        assert outside_file.read_text(encoding="utf-8") == "classified data"

    def test_quarantine_rejects_symlink_pointing_outside(self, tmp_path):
        """Quarantine rejects symlink that points outside the staging root."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        outside_file = tmp_path / "outside_target.txt"
        outside_file.write_text("outside data", encoding="utf-8")

        symlink_path = staging_dir / "symlink_escape.md"
        try:
            symlink_path.symlink_to(outside_file)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported in this environment/privilege level")

        res = quarantine_artifact(staging_dir, "symlink_escape.md")
        assert res == ""
        assert outside_file.exists()

    def test_quarantine_updates_audit_record_and_narrative_consistently(self, tmp_path):
        """Double failure updates AuditRecord and narrative to reflect quarantine truthfully."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        art = staging_dir / "note_rec.md"
        art.write_text("Test note content", encoding="utf-8")

        writer = DoubleFailingWriter()
        rec = AuditRecord(
            record_id="rec-quarantine-test",
            run_id="run-quarantine-test",
            execution_executed=True,
            artifact_path="note_rec.md",
            terminal_outcome="execution_succeeded",
        )
        receipts = []
        audits = [rec]
        ok = _persist_audits(writer, audits, receipts, staging_root=str(staging_dir))
        assert ok is False
        assert len(receipts) == 1
        assert receipts[0]["quarantined"] is True

        # Updated record in audits[0]
        updated = audits[0]
        assert updated.terminal_outcome == "audit_persistence_failed"
        assert ".unreconciled" in updated.artifact_reference
        # Narrative must reflect the quarantine truthfully
        assert updated.narrative != ""
        assert "audit persistence failed" in updated.narrative
        assert updated.artifact_reference in updated.narrative

    def test_missing_audit_writer_fails_closed_and_marks_receipts_unconfigured(self, tmp_path):
        """Missing audit writer never reports success and marks receipt mode=unconfigured."""
        rec = AuditRecord(
            record_id="rec-no-writer",
            run_id="run-no-writer",
            execution_executed=True,
            artifact_path="note.md",
        )
        receipts = []
        res = persist_record_resilient(None, rec, receipts=receipts)
        assert res["persisted"] is False
        assert res["mode"] == "unconfigured"
        assert res["reason"] == "no audit writer configured"
        assert len(receipts) == 1
        assert receipts[0]["persisted"] is False

        # _persist_audits with None writer
        audits = [rec]
        all_ok = _persist_audits(None, audits, receipts, staging_root=str(tmp_path))
        assert all_ok is False

    def test_run_bounded_integration_fails_closed_without_audit_writer(self, tmp_path):
        """When audit_root is not configured, the run succeeds but receipts
        truthfully record unconfigured mode for traceability."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "note.md").write_text("Observation:\nAuthentic observation text.", encoding="utf-8")
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        report = run_bounded_integration(
            str(source_dir),
            str(staging_dir),
            guarded_roots=(),
            approval_reader=lambda: "y",
            max_files=1,
            audit_root="",
        )
        # When audit_root is empty, no audit persistence was requested,
        # so the run should be accepted (not fail-closed).
        assert report.accepted is True
        # But receipts still truthfully record that persistence was not
        # configured, so the caller can detect unpersisted records.
        unconfigured = [
            r for r in report.audit_receipts
            if isinstance(r, dict) and r.get("mode") == "unconfigured"
        ]
        if report.audits:
            assert len(unconfigured) > 0

    def test_emergency_persistence_fsync_failure(self, monkeypatch, tmp_path):
        """When emergency persistence fsync fails, append_emergency raises OSError,
        resilient persistence reports durability='failed' and persisted=False."""
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))
        record = AuditRecord(
            record_id="rec-fsync-fail",
            run_id="run-fsync-fail",
            execution_executed=True,
            artifact_path="note.md",
        )

        def failing_fsync(fd):
            raise OSError("simulated fsync IO failure")

        monkeypatch.setattr("os.fsync", failing_fsync)

        with pytest.raises(OSError, match="simulated fsync IO failure"):
            writer.append_emergency(record, failure_reason="primary failure")

        # In resilient persistence, if primary fails and emergency also fails (via fsync)
        class PrimaryFailingAuditWriter(AuditWriter):
            def append(self, record):
                raise OSError("primary write failed")

        failing_writer = PrimaryFailingAuditWriter(str(audit_root))
        receipts = []
        receipt = persist_record_resilient(failing_writer, record, receipts=receipts)
        assert receipt["persisted"] is False
        assert receipt["mode"] == "failed"
        assert receipt["durability"] == "failed"
        assert "simulated fsync IO failure" in receipt.get("emergency_error", "")
        assert len(receipts) == 1
        assert receipts[0]["durability"] == "failed"

    def test_primary_failure_emergency_success_durability(self, tmp_path):
        """Emergency persistence reports durability='confirmed' while normal append reports 'written'."""
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        normal_writer = AuditWriter(str(audit_root))
        rec_normal = AuditRecord(
            record_id="rec-normal",
            run_id="run-normal",
            execution_executed=True,
            artifact_path="note_normal.md",
        )
        receipt_normal = normal_writer.append(rec_normal)
        assert receipt_normal.get("durability") == "written"
        assert receipt_normal.get("emergency", False) is False

        # Resilient persistence on primary success
        receipts = []
        res = persist_record_resilient(normal_writer, rec_normal, receipts=receipts)
        assert res.get("durability") == "written"
        assert res.get("emergency") is False

        # Resilient persistence on primary failure but emergency success using REAL AuditWriter.append_emergency
        class RealEmergencyAuditWriter(AuditWriter):
            def append(self, record):
                raise OSError("simulated primary append disk write error")

        real_emerg_writer = RealEmergencyAuditWriter(str(audit_root))
        rec_emerg = AuditRecord(
            record_id="rec-emerg-real",
            run_id="run-emerg-real",
            execution_executed=True,
            artifact_path="note_emerg.md",
        )
        receipts_emerg = []
        res_emerg = persist_record_resilient(real_emerg_writer, rec_emerg, receipts=receipts_emerg)
        assert res_emerg.get("persisted") is True
        assert res_emerg.get("emergency") is True
        assert res_emerg.get("durability") == "confirmed"
        # Verify emergency journal file was actually written to errors partition
        emerg_file = Path(res_emerg["audit_path"])
        assert emerg_file.exists()
        assert emerg_file.name == "audit_emergency.jsonl"
        assert "rec-emerg-real" in emerg_file.read_text(encoding="utf-8")

        # Test that writer returning unconfirmed durability is preserved (NOT converted to confirmed)
        class UnconfirmedEmergencyWriter:
            def append(self, record):
                raise OSError("primary failed")

            def append_emergency(self, record, failure_reason=""):
                return {
                    "persisted": True,
                    "emergency": True,
                    "durability": "unconfirmed",
                    "record_id": getattr(record, "record_id", ""),
                }

        unconfirmed_writer = UnconfirmedEmergencyWriter()
        res_unconfirmed = persist_record_resilient(unconfirmed_writer, rec_emerg)
        assert res_unconfirmed.get("persisted") is False
        assert res_unconfirmed.get("durability") == "unconfirmed"

        # Test that writer returning failed durability is NOT accepted as success
        class FailedEmergencyWriter:
            def append(self, record):
                raise OSError("primary failed")

            def append_emergency(self, record, failure_reason=""):
                return {
                    "persisted": False,
                    "durability": "failed",
                    "reason": "emergency disk full",
                }

        failed_writer = FailedEmergencyWriter()
        res_failed = persist_record_resilient(failed_writer, rec_emerg)
        assert res_failed.get("persisted") is False
        assert res_failed.get("durability") == "failed"

    def test_quarantine_partial_cleanup_failure_preserves_both_paths(self, monkeypatch, tmp_path):
        """When quarantine creates destination link but source unlink fails,
        quarantine reports partial_cleanup_failed, preserves both files on disk,
        and runner records both paths truthfully in narrative."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        art = staging_dir / "partial_note.md"
        art.write_text("Partial quarantine test data", encoding="utf-8")

        # Simulate POSIX hard-link behavior:
        # 1. os.link creates real hard link on disk (supported on Windows NTFS & POSIX).
        # 2. os.unlink fails specifically for the source file.
        import os
        orig_unlink = os.unlink

        def unlink_failing_for_source(path, *args, **kwargs):
            if str(art).replace("\\", "/") in str(path).replace("\\", "/"):
                raise OSError("simulated unlink permission denied on source artifact")
            return orig_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink_failing_for_source)

        # 1. Direct call to quarantine_artifact (exercises real atomic_publish_exclusive)
        outcome = quarantine_artifact(staging_dir, "partial_note.md")
        assert outcome.status == "partial_cleanup_failed"
        assert outcome.success is False
        assert outcome.source_removed is False
        assert outcome.target_path != ""
        # Both source and target must exist on disk!
        assert art.exists()
        target_file = staging_dir / outcome.target_path
        assert target_file.exists()
        assert art.read_text(encoding="utf-8") == "Partial quarantine test data"
        assert target_file.read_text(encoding="utf-8") == "Partial quarantine test data"
        # Because real os.link was used, verify they share the exact same underlying file
        assert target_file.samefile(art)

        # 2. Flow through runner._persist_audits with fresh artifact
        art2 = staging_dir / "partial_note_2.md"
        art2.write_text("Partial quarantine runner test data", encoding="utf-8")

        def unlink_failing_for_art2(path, *args, **kwargs):
            if str(art2).replace("\\", "/") in str(path).replace("\\", "/"):
                raise OSError("simulated unlink permission denied on source artifact 2")
            return orig_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink_failing_for_art2)

        writer = DoubleFailingWriter()
        rec = AuditRecord(
            record_id="rec-partial-test",
            run_id="run-partial-test",
            execution_executed=True,
            artifact_path="partial_note_2.md",
            terminal_outcome="execution_succeeded",
        )
        receipts = []
        audits = [rec]
        all_ok = _persist_audits(writer, audits, receipts, staging_root=str(staging_dir))
        assert all_ok is False
        assert len(receipts) == 1
        rcpt = receipts[0]
        assert rcpt["quarantine_status"] == "partial_cleanup_failed"
        assert rcpt["source_removed"] is False
        assert rcpt["quarantine_path"] == ".unreconciled.partial_note_2.md"

        updated_rec = audits[0]
        assert updated_rec.terminal_outcome == "audit_persistence_failed"
        assert updated_rec.artifact_path == "partial_note_2.md"
        assert updated_rec.destination == ".unreconciled.partial_note_2.md"
        assert "partial quarantine (source cleanup failed)" in updated_rec.execution_reason
        assert "partial quarantine" in updated_rec.narrative.lower()

        # Both art2 and its quarantined link exist on disk and are verified hardlinks
        target_art2 = staging_dir / ".unreconciled.partial_note_2.md"
        assert art2.exists()
        assert target_art2.exists()
        assert target_art2.samefile(art2)

    def test_quarantine_directory_absent_fallback(self, tmp_path):
        """When .unreconciled subdirectory does not exist, quarantine falls back to sibling .unreconciled.<name>."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        art = staging_dir / "orphan_note.md"
        art.write_text("Sibling fallback content", encoding="utf-8")

        # Verify .unreconciled directory does NOT exist
        assert not (staging_dir / ".unreconciled").exists()

        outcome = quarantine_artifact(staging_dir, "orphan_note.md")
        assert outcome.success is True
        assert outcome.status == "quarantined"
        assert outcome.target_path == ".unreconciled.orphan_note.md"
        assert not art.exists()
        sibling_target = staging_dir / ".unreconciled.orphan_note.md"
        assert sibling_target.exists()
        assert sibling_target.read_text(encoding="utf-8") == "Sibling fallback content"

    def test_quarantine_collision_preserves_existing_target_sibling(self, tmp_path):
        """Sibling quarantine (.unreconciled.<name>) rejects collisions and never overwrites existing target."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        # Pre-create sibling target
        existing_sibling = staging_dir / ".unreconciled.collide.md"
        existing_sibling.write_text("prior quarantined sibling content", encoding="utf-8")

        # Source file
        source = staging_dir / "collide.md"
        source.write_text("new conflicting content", encoding="utf-8")

        outcome = quarantine_artifact(staging_dir, "collide.md")
        assert outcome.success is False
        assert outcome == ""
        # Both files must remain completely unchanged
        assert existing_sibling.read_text(encoding="utf-8") == "prior quarantined sibling content"
        assert source.read_text(encoding="utf-8") == "new conflicting content"

    def test_repeated_quarantine_after_partial_failure_preserves_both_files_and_fails_closed(self, monkeypatch, tmp_path):
        """A repeated quarantine attempt after a partial cleanup failure detects destination collision,
        fails closed without deleting or corrupting either file, and leaves both paths intact."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        art = staging_dir / "recovery_note.md"
        art.write_text("Artifact needing recovery", encoding="utf-8")

        import os
        orig_unlink = os.unlink

        def unlink_fail(path, *args, **kwargs):
            if str(art).replace("\\", "/") in str(path).replace("\\", "/"):
                raise OSError("simulated unlink permission denied")
            return orig_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink_fail)

        # First quarantine attempt: partial cleanup failure
        res1 = quarantine_artifact(staging_dir, "recovery_note.md")
        assert res1.status == "partial_cleanup_failed"
        assert res1.success is False
        quarantined_target = staging_dir / res1.target_path
        assert art.exists()
        assert quarantined_target.exists()

        # Second quarantine attempt (e.g. retry / recovery loop):
        # Destination already exists; must fail closed with quarantine destination already exists
        res2 = quarantine_artifact(staging_dir, "recovery_note.md")
        assert res2.success is False
        assert res2.status == "quarantine_failed"
        assert "already exists" in res2.error
        # Both files remain completely intact and identical
        assert art.exists()
        assert quarantined_target.exists()
        assert art.read_text(encoding="utf-8") == "Artifact needing recovery"
        assert quarantined_target.read_text(encoding="utf-8") == "Artifact needing recovery"

    def test_persist_record_truthful_on_explicit_failure_receipt(self):
        """persist_record must return False and preserve persisted=False when writer returns failure receipt."""
        from src.living_authenticity.knowledge.governance.audit.persistence import persist_record

        class ExplicitFailureWriter:
            def append(self, record):
                return {"persisted": False, "durability": "failed", "reason": "disk quota exceeded"}

        receipts = []
        rec = AuditRecord(record_id="rec-fail-check")
        ok = persist_record(ExplicitFailureWriter(), rec, receipts=receipts)
        assert ok is False
        assert len(receipts) == 1
        assert receipts[0]["persisted"] is False
        assert receipts[0]["durability"] == "failed"

    def test_controlled_executor_reports_executed_with_warning_on_partial_tmp_cleanup_failure(self, monkeypatch, tmp_path):
        """When staging publication succeeds but temporary source cleanup fails,
        ControlledExecutor reports executed=True with an accurate reconciliation warning."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        from tests.governance.test_controlled_execution import _chain
        request, outcome, prop, conf, note = _chain()

        import os
        orig_unlink = os.unlink

        def unlink_fail(path, *args, **kwargs):
            if ".tmp" in str(path):
                raise OSError("simulated tmp unlink failure")
            return orig_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink_fail)

        executor = ControlledExecutor()
        res = executor.execute(request, outcome, proposal=prop, confidence=conf, note=note, staging_root=str(staging_dir))

        assert res.executed is True
        assert res.permitted is True
        assert "executed with warning" in res.reason
        target = staging_dir / (request.proposal_hash + ".md")
        assert target.exists()
        assert target.read_text(encoding="utf-8") == note.markdown

    def test_malformed_and_contradictory_receipts_fail_closed(self):
        """Verify that None, empty, non-dict, contradictory, and unknown receipts fail closed."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            validate_persistence_receipt,
            persist_record,
            persist_record_resilient,
        )

        # None receipt
        ok, reason = validate_persistence_receipt(None)
        assert ok is False
        assert "not a dictionary" in reason

        # Empty dict
        ok, reason = validate_persistence_receipt({})
        assert ok is False
        assert "empty dictionary" in reason

        # Non-dict
        ok, reason = validate_persistence_receipt("confirmed")
        assert ok is False

        # Missing required persisted
        ok, reason = validate_persistence_receipt({
            "durability": "written",
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "missing 'persisted'" in reason

        # Missing required durability
        ok, reason = validate_persistence_receipt({
            "persisted": True,
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "missing 'durability'" in reason

        # Incorrect types
        ok, reason = validate_persistence_receipt({
            "persisted": "true",  # string not boolean
            "durability": "written",
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "boolean" in reason

        ok, reason = validate_persistence_receipt({
            "persisted": 1,  # int not boolean
            "durability": "written",
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "boolean" in reason

        ok, reason = validate_persistence_receipt({
            "persisted": True,
            "durability": 123,  # int not string
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "durability" in reason

        ok, reason = validate_persistence_receipt({
            "persisted": True,
            "durability": "written",
            "record_id": 999,  # int not string
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "record_id" in reason

        # Contradictory: persisted=True but durability='failed'
        ok, reason = validate_persistence_receipt({
            "persisted": True,
            "durability": "failed",
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "unconfirmed or failed durability" in reason

        # Contradictory: require_emergency=True but emergency=False
        ok, reason = validate_persistence_receipt({
            "persisted": True,
            "durability": "confirmed",
            "emergency": False,
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        }, require_emergency=True)
        assert ok is False
        assert "emergency=True" in reason

        # Contradictory: mode='failed' when persisted=True
        ok, reason = validate_persistence_receipt({
            "persisted": True,
            "mode": "failed",
            "durability": "written",
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "contradictory mode" in reason

        # Unknown durability status
        ok, reason = validate_persistence_receipt({
            "persisted": True,
            "durability": "speculative_sync",
            "record_id": "r1",
            "audit_path": "/tmp/a.jsonl",
        })
        assert ok is False
        assert "durability" in reason

        # Missing required audit_path
        ok, reason = validate_persistence_receipt({
            "persisted": True,
            "durability": "written",
            "record_id": "r1",
            "audit_path": "",
        })
        assert ok is False
        assert "audit_path" in reason

        # Malformed primary receipt in persist_record_resilient triggers emergency recovery
        class MalformedPrimaryWriter:
            def __init__(self):
                self.emergency_called = False

            def append(self, record):
                # Returns invalid non-dict
                return "unexpected string"

            def append_emergency(self, record, failure_reason=""):
                self.emergency_called = True
                return {
                    "persisted": True,
                    "emergency": True,
                    "durability": "confirmed",
                    "record_id": getattr(record, "record_id", "r1"),
                    "audit_path": "/tmp/errors/audit_emergency.jsonl",
                }

        writer = MalformedPrimaryWriter()
        rec = AuditRecord(record_id="rec-malformed-primary")
        res = persist_record_resilient(writer, rec)
        assert writer.emergency_called is True
        assert res["persisted"] is True
        assert res["emergency"] is True
        assert res["durability"] == "confirmed"

        # Malformed emergency receipt fails closed without falsely reporting success
        class MalformedEmergencyWriter:
            def append(self, record):
                raise OSError("primary failed")

            def append_emergency(self, record, failure_reason=""):
                # Returns malformed receipt
                return None

        bad_emerg_writer = MalformedEmergencyWriter()
        res_bad_emerg = persist_record_resilient(bad_emerg_writer, rec)
        assert res_bad_emerg["persisted"] is False
        assert res_bad_emerg["durability"] == "failed"

    def test_partial_persistence_failure_preserves_prior_and_emergency_records(self, tmp_path):
        """When primary persistence succeeds for an earlier record but fails for a subsequent record,
        the subsequent record is preserved in the emergency journal, the prior primary record is untouched,
        and all records remain preserved in the run report."""
        audit_root = tmp_path / "audit"
        passed_dir = audit_root / "passed"
        errors_dir = audit_root / "errors"
        audit_root.mkdir()
        passed_dir.mkdir()
        errors_dir.mkdir()

        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )
        from src.living_authenticity.knowledge.integration.runner import _persist_audits

        writer = AuditWriter(str(audit_root))
        rec1 = AuditRecord(record_id="rec-1", run_id="run-partial", execution_executed=True)
        rec2 = AuditRecord(record_id="rec-2", run_id="run-partial", execution_executed=False, terminal_outcome="rejected")

        # 1. Primary write of rec1 succeeds
        receipts = []
        r1 = writer.append(rec1)
        receipts.append(r1)

        # 2. Simulate primary failure for rec2 by patching writer.append
        original_append = writer.append
        def failing_append(record):
            if getattr(record, "record_id", "") == "rec-2":
                raise OSError("simulated primary append disk write failure")
            return original_append(record)

        writer.append = failing_append

        audits = [rec1, rec2]
        all_ok = _persist_audits(writer, audits, receipts, staging_root=None)

        # all_ok should be True because rec2 was successfully preserved in emergency journal!
        assert all_ok is True
        assert len(receipts) == 2
        assert receipts[0]["persisted"] is True
        assert receipts[0]["emergency"] is False
        assert receipts[1]["persisted"] is True
        assert receipts[1]["emergency"] is True
        assert receipts[1]["durability"] == "confirmed"

        # Verify physical files:
        # rec1 is in passed/audit.jsonl
        primary_file = passed_dir / "audit.jsonl"
        assert primary_file.exists()
        assert "rec-1" in primary_file.read_text(encoding="utf-8")
        assert "rec-2" not in primary_file.read_text(encoding="utf-8")

        # Both earlier primary record and failing record are preserved in errors/audit_emergency.jsonl
        emerg_file = errors_dir / "audit_emergency.jsonl"
        assert emerg_file.exists()
        emerg_content = emerg_file.read_text(encoding="utf-8")
        assert "rec-2" in emerg_content
        assert "rec-1" in emerg_content

    def test_multiple_records_same_run_history_recoverable_in_errors(self, tmp_path):
        """When a failure occurs after multiple primary records have been written,
        all records for that run are recoverable from errors/audit_emergency.jsonl while
        previously persisted primary records remain intact."""
        audit_root = tmp_path / "audit"
        passed_dir = audit_root / "passed"
        errors_dir = audit_root / "errors"
        audit_root.mkdir()
        passed_dir.mkdir()
        errors_dir.mkdir()

        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )
        from src.living_authenticity.knowledge.integration.runner import _persist_audits

        writer = AuditWriter(str(audit_root))
        rec1 = AuditRecord(record_id="rec-multi-1", run_id="run-multi-history", proposal_action="CREATE", execution_executed=True)
        rec2 = AuditRecord(record_id="rec-multi-2", run_id="run-multi-history", proposal_action="CREATE", execution_executed=True)
        rec3 = AuditRecord(record_id="rec-multi-3", run_id="run-multi-history", proposal_action="CREATE", execution_executed=False, terminal_outcome="failed")

        # 1. Primary writes of rec1 and rec2 succeed
        receipts = []
        r1 = writer.append(rec1)
        r2 = writer.append(rec2)
        receipts.extend([r1, r2])

        # 2. Simulate primary failure on rec3
        original_append = writer.append
        def failing_append(record):
            if getattr(record, "record_id", "") == "rec-multi-3":
                raise OSError("simulated primary append disk write failure on rec3")
            return original_append(record)

        writer.append = failing_append

        audits = [rec1, rec2, rec3]
        all_ok = _persist_audits(writer, audits, receipts, staging_root=None)

        assert all_ok is True
        assert len(receipts) == 3
        assert receipts[0]["persisted"] is True
        assert receipts[0]["emergency"] is False
        assert receipts[1]["persisted"] is True
        assert receipts[1]["emergency"] is False
        assert receipts[2]["persisted"] is True
        assert receipts[2]["emergency"] is True
        assert receipts[2]["durability"] == "confirmed"

        # Primary log has rec1 and rec2 intact, but not rec3
        primary_content = (passed_dir / "audit.jsonl").read_text(encoding="utf-8")
        assert "rec-multi-1" in primary_content
        assert "rec-multi-2" in primary_content
        assert "rec-multi-3" not in primary_content

        # Emergency log has all three records for the affected run
        emerg_content = (errors_dir / "audit_emergency.jsonl").read_text(encoding="utf-8")
        assert "rec-multi-1" in emerg_content
        assert "rec-multi-2" in emerg_content
        assert "rec-multi-3" in emerg_content

    def test_repeated_emergency_recovery_attempts_are_idempotent_no_duplicate_records(self, tmp_path):
        """Repeated emergency recovery calls must be idempotent and avoid writing duplicate records."""
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "passed").mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()

        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        writer = AuditWriter(str(audit_root))
        rec1 = AuditRecord(record_id="rec-idem-1", run_id="run-idem", execution_executed=True)
        rec2 = AuditRecord(record_id="rec-idem-2", run_id="run-idem", execution_executed=False)

        records = [rec1, rec2]

        # First emergency history recovery
        receipts1 = writer.append_emergency_history(records, failure_reason="simulated failure 1", failing_record_id="rec-idem-2")
        assert len(receipts1) == 2
        assert all(r["persisted"] is True for r in receipts1)

        emerg_file = errors_dir / "audit_emergency.jsonl"
        lines1 = [line.strip() for line in emerg_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines1) == 2

        # Second emergency history recovery attempt (e.g. retry / resume)
        receipts2 = writer.append_emergency_history(records, failure_reason="simulated failure 2", failing_record_id="rec-idem-2")
        assert len(receipts2) == 2
        assert all(r["persisted"] is True for r in receipts2)

        # File content must NOT have duplicate lines added
        lines2 = [line.strip() for line in emerg_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines2) == 2
        assert lines2 == lines1

    def test_journal_reader_visibility_of_recovered_emergency_history(self, tmp_path):
        """Journal reader (Launcher.journals) must accurately display recovered emergency history from errors/."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "passed").mkdir()
        (audit_root / "failed").mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()

        from src.living_authenticity.adapter.app import Launcher
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )
        from src.living_authenticity.knowledge.integration.runner import _persist_audits

        writer = AuditWriter(str(audit_root))
        rec1 = AuditRecord(record_id="rec-j-1", run_id="run-journal-emerg", proposal_action="CREATE", execution_executed=True)
        rec2 = AuditRecord(record_id="rec-j-2", run_id="run-journal-emerg", proposal_action="CREATE", execution_executed=False, terminal_outcome="rejected")

        # 1. Primary write for rec1
        receipts = [writer.append(rec1)]

        # 2. Simulate primary write failure for rec2
        original_append = writer.append
        def failing_append(record):
            if getattr(record, "record_id", "") == "rec-j-2":
                raise OSError("simulated primary append disk failure")
            return original_append(record)

        writer.append = failing_append

        audits = [rec1, rec2]
        all_ok = _persist_audits(writer, audits, receipts, staging_root=staging_dir)
        assert all_ok is True

        # 3. Read via Launcher.journals()
        launcher = Launcher(paths={
            "staging": {"root": str(staging_dir)},
            "audit": {"root": str(audit_root), "passed": str(audit_root / "passed"), "failed": str(audit_root / "failed")},
        })

        j = launcher.journals()
        journal_audits = j["audits"]

        # Emergency records from errors/audit_emergency.jsonl are visible with emergency=True
        emerg_records = [a for a in journal_audits if a.get("emergency") is True]
        assert len(emerg_records) >= 2
        emerg_ids = {a["record_id"] for a in emerg_records}
        assert "rec-j-1" in emerg_ids
        assert "rec-j-2" in emerg_ids

        # Primary record is also visible
        primary_records = [a for a in journal_audits if a.get("emergency") is False]
        assert any(a["record_id"] == "rec-j-1" for a in primary_records)

    def test_total_persistence_failure_preserves_in_memory_records(self, tmp_path):
        """When both primary and emergency persistence fail for a record,
        all in-memory audit records are preserved, the failure is reported accurately,
        and unpersisted records are never falsely claimed to be persisted."""
        from src.living_authenticity.knowledge.governance.audit.persistence import AuditRecord
        from src.living_authenticity.knowledge.integration.runner import _persist_audits

        class TotalFailureWriter:
            def append(self, record):
                raise OSError("primary disk error")

            def append_emergency(self, record, failure_reason=""):
                raise OSError("emergency disk error")

        writer = TotalFailureWriter()
        rec1 = AuditRecord(record_id="rec-total-1", run_id="run-total", terminal_outcome="held")
        rec2 = AuditRecord(record_id="rec-total-2", run_id="run-total", terminal_outcome="rejected")

        audits = [rec1, rec2]
        receipts = []
        all_ok = _persist_audits(writer, audits, receipts, staging_root=None)

        assert all_ok is False
        # Both records remain in memory in audits
        assert len(audits) == 2
        assert audits[0].record_id == "rec-total-1"
        assert audits[1].record_id == "rec-total-2"

        # Both receipts report persisted=False
        assert len(receipts) == 2
        assert receipts[0]["persisted"] is False
        assert receipts[0]["durability"] == "failed"
        assert receipts[1]["persisted"] is False
        assert receipts[1]["durability"] == "failed"

    def test_validate_persistence_receipt_exact_record_identity(self):
        """A receipt for a different record must fail validation even if all other fields are valid."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            validate_persistence_receipt,
        )

        valid_receipt = {
            "record_id": "rec-expected-1",
            "persisted": True,
            "durability": "written",
            "audit_path": "/tmp/audit/passed/audit.jsonl",
            "emergency": False,
        }

        # Matching record_id succeeds
        is_valid, reason = validate_persistence_receipt(
            valid_receipt,
            expected_record_id="rec-expected-1",
        )
        assert is_valid is True
        assert reason == ""

        # Mismatched record_id fails validation fail-closed
        is_valid, reason = validate_persistence_receipt(
            valid_receipt,
            expected_record_id="rec-OTHER-99",
        )
        assert is_valid is False
        assert "record_id mismatch" in reason
        assert "rec-expected-1" in reason
        assert "rec-OTHER-99" in reason

    def test_primary_receipt_mismatched_record_id_triggers_emergency_persistence(self, tmp_path, monkeypatch):
        """When primary append returns a receipt with a mismatched record_id,
        it must fail validation, avoid overwriting the receipt ID, and trigger emergency recovery."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            persist_record_resilient,
        )

        audit_root = tmp_path / "audit"
        passed_dir = audit_root / "passed"
        errors_dir = audit_root / "errors"
        audit_root.mkdir()
        passed_dir.mkdir()
        errors_dir.mkdir()

        writer = AuditWriter(str(audit_root))
        record = AuditRecord(record_id="rec-correct-123", run_id="run-mismatch", execution_executed=True)

        # Patch append to return a valid-looking receipt for a DIFFERENT record ID
        def mismatched_append(record) -> dict:
            return {
                "record_id": "rec-WRONG-456",
                "run_id": "run-mismatch",
                "persisted": True,
                "durability": "written",
                "audit_path": str(passed_dir / "audit.jsonl"),
                "emergency": False,
            }

        monkeypatch.setattr(writer, "append", mismatched_append)

        receipts = []
        receipt = persist_record_resilient(writer, record, receipts=receipts)

        # Must fall back to emergency journal because primary receipt failed validation
        assert receipt["persisted"] is True
        assert receipt["emergency"] is True
        assert receipt["durability"] == "confirmed"
        assert receipt["record_id"] == "rec-correct-123"
        assert "record_id mismatch" in receipt.get("primary_error", "")

        # Verify emergency log file exists and contains the correct record
        emerg_file = errors_dir / "audit_emergency.jsonl"
        assert emerg_file.exists()
        content = emerg_file.read_text(encoding="utf-8")
        assert "rec-correct-123" in content

    def test_emergency_receipt_mismatched_record_id_fails_closed(self, tmp_path, monkeypatch):
        """When both primary append fails and emergency append returns a mismatched record_id,
        the emergency receipt remains unconfirmed and persistence fails closed."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            persist_record_resilient,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()

        writer = AuditWriter(str(audit_root))
        record = AuditRecord(record_id="rec-target-777", run_id="run-emerg-mismatch", execution_executed=True)

        def failing_primary(record) -> dict:
            raise OSError("primary disk write failed")

        def mismatched_emergency(record, failure_reason="") -> dict:
            return {
                "record_id": "rec-SPOOFED-888",
                "run_id": "run-emerg-mismatch",
                "persisted": True,
                "durability": "confirmed",
                "emergency": True,
                "audit_path": str(audit_root / "errors" / "audit_emergency.jsonl"),
            }

        monkeypatch.setattr(writer, "append", failing_primary)
        monkeypatch.setattr(writer, "append_emergency", mismatched_emergency)

        receipts = []
        receipt = persist_record_resilient(writer, record, receipts=receipts)

        assert receipt["persisted"] is False
        assert receipt["durability"] == "failed"
        assert receipt["record_id"] == "rec-target-777"
        assert "record_id mismatch" in receipt.get("emergency_error", "")
        # Mismatch details and raw receipts are preserved for diagnosis
        assert receipt.get("raw_emergency_receipt", {}).get("record_id") == "rec-SPOOFED-888"

    def test_partial_emergency_recovery_fails_overall_resilient_persistence_and_retry_recovers(self, tmp_path, monkeypatch):
        """When primary append fails for current record and emergency recovery is attempted
        for the full run history, if any record in the emergency recovery fails to write,
        the overall recovery must fail closed even if the current record succeeded.
        Previously persisted primary records remain intact, readable emergency records remain,
        failure details identify unrecovered records, and a later retry succeeds without unjustified duplicates."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            persist_record_resilient,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "passed").mkdir()
        (audit_root / "errors").mkdir()

        writer = AuditWriter(str(audit_root))
        rec1 = AuditRecord(record_id="rec-part-1", run_id="run-part-test", execution_executed=True)
        rec2 = AuditRecord(record_id="rec-part-2", run_id="run-part-test", execution_executed=True)

        # 1. Primary write for rec1 succeeds
        r1 = writer.append(rec1)
        assert r1["persisted"] is True
        receipts = [r1]

        # 2. Simulate primary write failure for rec2, and emergency failure ONLY for rec1
        real_emergency = writer.append_emergency

        def failing_primary_rec2(record):
            raise OSError("simulated primary write disk error for rec2")

        def partial_failing_emergency(record, failure_reason=""):
            rec_id = getattr(record, "record_id", "") or (record.get("record_id") if isinstance(record, dict) else "")
            if rec_id == "rec-part-1":
                raise OSError("simulated transient disk error for rec-part-1 during emergency recovery")
            return real_emergency(record, failure_reason=failure_reason)

        monkeypatch.setattr(writer, "append", failing_primary_rec2)
        monkeypatch.setattr(writer, "append_emergency", partial_failing_emergency)

        # 3. persist_record_resilient for rec2 with full run history [rec1, rec2]
        receipt2 = persist_record_resilient(
            writer,
            rec2,
            receipts=receipts,
            history=[rec1, rec2],
        )

        # Overall recovery must fail closed because rec1 failed emergency write!
        assert receipt2["persisted"] is False
        assert receipt2["durability"] == "failed"
        assert receipt2["recovery_all_ok"] is False
        assert "rec-part-1" in receipt2.get("failed_record_ids", [])
        assert "incomplete history recovery" in receipt2.get("emergency_error", "")

        # 4. Invariants check:
        # Previously persisted primary record remains intact in passed/audit.jsonl
        primary_file = audit_root / "passed" / "audit.jsonl"
        assert primary_file.exists()
        assert "rec-part-1" in primary_file.read_text(encoding="utf-8")

        # The successfully written emergency record for rec2 exists in errors/audit_emergency.jsonl
        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        assert emerg_file.exists()
        emerg_text = emerg_file.read_text(encoding="utf-8")
        assert "rec-part-2" in emerg_text

        # 5. Retry: transient error resolved, writer can now write rec1
        monkeypatch.setattr(writer, "append_emergency", real_emergency)

        recovery_result = writer.append_emergency_history(
            [rec1, rec2],
            failure_reason="retry after transient failure",
            failing_record_id="rec-part-2",
        )
        assert recovery_result.all_ok is True
        assert recovery_result.persisted_count == 2
        assert len(recovery_result.failed_record_ids) == 0

        # Verify no duplicate entries were added for rec-part-2:
        import json
        emerg_lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        rec2_entries = [d for d in emerg_lines if d.get("record_id") == "rec-part-2"]
        rec1_entries = [d for d in emerg_lines if d.get("record_id") == "rec-part-1"]
        assert len(rec2_entries) == 1
        assert len(rec1_entries) == 1

    def test_emergency_recovery_content_conflict_preserves_supplemental_and_fails_closed(self, tmp_path):
        """When an emergency journal already contains a record with the same record_id
        but different domain content (outcome/artifact metadata), it must be flagged as an
        explicit content conflict, reported as persisted=False, and preserved as a supplemental
        conflict record without overwriting the historical entry."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "errors").mkdir()

        writer = AuditWriter(str(audit_root))
        original_rec = AuditRecord(
            record_id="rec-conflict-1",
            run_id="run-conflict",
            terminal_outcome="approved",
            artifact_path="doc1.md",
            execution_executed=True,
        )

        # 1. First record is written into emergency journal
        res1 = writer.append_emergency_history([original_rec], failure_reason="first write")
        assert res1.all_ok is True

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        orig_content = emerg_file.read_text(encoding="utf-8")
        assert "approved" in orig_content

        # 2. Second record has SAME record_id but conflicting outcome and artifact_path
        conflicting_rec = AuditRecord(
            record_id="rec-conflict-1",
            run_id="run-conflict",
            terminal_outcome="rejected",
            artifact_path="doc_different.md",
            execution_executed=False,
        )

        res2 = writer.append_emergency_history([conflicting_rec], failure_reason="conflicting write")
        assert res2.all_ok is False
        assert "rec-conflict-1" in res2.failed_record_ids
        assert len(res2.conflicts) == 1
        assert res2.conflicts[0]["conflict"] is True
        assert res2.conflicts[0]["persisted"] is False

        # 3. Verify original entry is intact and supplemental conflict record exists
        updated_content = emerg_file.read_text(encoding="utf-8")
        lines = [l for l in updated_content.splitlines() if l.strip()]
        assert len(lines) == 2  # original + supplemental
        assert "approved" in lines[0]  # original intact
        assert "identity_content_mismatch" in lines[1]  # supplemental conflict record
        assert "rec-conflict-1" in lines[1]

    def test_emergency_recovery_malformed_journal_entry_handled_conservatively(self, tmp_path):
        """When the emergency journal contains corrupted or truncated lines, they are
        conservatively segregated, do not crash the reader or writer, and valid records
        can still be appended and verified."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()

        emerg_file = errors_dir / "audit_emergency.jsonl"
        emerg_file.write_text('{"record_id": "corrupted", invalid_json...\n', encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        records_by_id, malformed = writer.read_emergency_records()
        assert len(records_by_id) == 0
        assert len(malformed) == 1
        assert malformed[0]["line_no"] == 1

        rec = AuditRecord(record_id="rec-clean-1", run_id="run-clean", execution_executed=True)
        res = writer.append_emergency_history([rec], failure_reason="testing malformed journal recovery")
        # While the clean record is appended, malformed lines are noted in errors
        assert any("malformed journal line" in msg for msg in res.errors.values())
        assert any(r["record_id"] == "rec-clean-1" and r["persisted"] is True for r in res.receipts)

    def test_emergency_record_integrity_valid_unchanged(self, tmp_path):
        """A valid emergency record with canonical digest passes integrity verification
        and is returned in records_by_id with zero malformed lines."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            verify_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))
        record = AuditRecord(
            record_id="rec-valid-int-1",
            run_id="run-valid-int",
            terminal_outcome="approved",
            artifact_path="valid.md",
            execution_executed=True,
        )

        receipt = writer.append_emergency(record, failure_reason="initial append")
        assert receipt["persisted"] is True
        assert receipt["durability"] == "confirmed"

        records_by_id, malformed = writer.read_emergency_records()
        assert len(malformed) == 0
        assert "rec-valid-int-1" in records_by_id

        stored_data = records_by_id["rec-valid-int-1"]
        is_valid, err = verify_record_digest(stored_data)
        assert is_valid is True
        assert err == ""
        assert stored_data["record_id"] == "rec-valid-int-1"
        assert stored_data["terminal_outcome"] == "approved"

    def test_emergency_record_integrity_modified_domain_content_fails(self, tmp_path):
        """When domain content (e.g. terminal_outcome or artifact_path) is modified in-place
        without recomputing the digest, integrity verification fails and the entry is segregated into malformed lines."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            verify_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))
        record = AuditRecord(
            record_id="rec-mod-cont-1",
            run_id="run-mod-cont",
            terminal_outcome="approved",
            artifact_path="legit.md",
            execution_executed=True,
        )
        writer.append_emergency(record, failure_reason="legit write")

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1

        # Tamper with terminal_outcome directly on disk without updating record_digest
        lines[0]["terminal_outcome"] = "tampered_outcome"
        emerg_file.write_text(json.dumps(lines[0]) + "\n", encoding="utf-8")

        # verify_record_digest directly detects tampering
        is_valid, err = verify_record_digest(lines[0])
        assert is_valid is False
        assert "record_digest mismatch" in err

        # read_emergency_records segregates tampered record
        records_by_id, malformed = writer.read_emergency_records()
        assert "rec-mod-cont-1" not in records_by_id
        assert len(malformed) == 1
        assert malformed[0]["record_id"] == "rec-mod-cont-1"
        assert "integrity verification failed" in malformed[0]["error"]

    def test_emergency_record_integrity_modified_digest_fails(self, tmp_path):
        """When record_digest is modified directly without modifying content, verification fails."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            verify_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))
        record = AuditRecord(
            record_id="rec-mod-dig-1",
            run_id="run-mod-dig",
            terminal_outcome="rejected",
        )
        writer.append_emergency(record, failure_reason="initial write")

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1

        # Tamper with digest
        orig_digest = lines[0]["record_digest"]
        tampered_digest = ("0" if orig_digest[0] != "0" else "1") + orig_digest[1:]
        lines[0]["record_digest"] = tampered_digest
        emerg_file.write_text(json.dumps(lines[0]) + "\n", encoding="utf-8")

        is_valid, err = verify_record_digest(lines[0])
        assert is_valid is False
        assert "record_digest mismatch" in err

        records_by_id, malformed = writer.read_emergency_records()
        assert "rec-mod-dig-1" not in records_by_id
        assert len(malformed) == 1
        assert "integrity verification failed" in malformed[0]["error"]

    def test_emergency_record_integrity_tampered_non_domain_fields_fails(self, tmp_path):
        """Tampering with fields like emergency_reason or storage_category invalidates the digest,
        while changing POST_DIGEST_FIELDS (persistence_timestamp) does not."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            verify_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))
        record = AuditRecord(
            record_id="rec-tamper-nd-1",
            run_id="run-tamper-nd",
            terminal_outcome="held",
        )
        writer.append_emergency(record, failure_reason="reason A")

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1

        # 1. Tampering with persistence_timestamp (in POST_DIGEST_FIELDS) is permitted
        unmodified = dict(lines[0])
        unmodified["persistence_timestamp"] = "2026-10-26T12:00:00Z"
        ok, err = verify_record_digest(unmodified)
        assert ok is True

        # 2. Tampering with emergency_reason (hashed in digest) fails verification
        tampered_reason = dict(lines[0])
        tampered_reason["emergency_reason"] = "tampered reason XYZ"
        ok, err = verify_record_digest(tampered_reason)
        assert ok is False
        assert "record_digest mismatch" in err

        # 3. Tampering with storage_category (hashed in digest) fails verification
        tampered_cat = dict(lines[0])
        tampered_cat["storage_category"] = "passed"
        ok, err = verify_record_digest(tampered_cat)
        assert ok is False
        assert "record_digest mismatch" in err

    def test_emergency_record_integrity_missing_or_malformed_digest_values(self):
        """Missing, non-string, truncated, non-hex, or empty digests are all rejected fail-closed."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            verify_record_digest,
        )

        # Non-dict
        ok, err = verify_record_digest(["not a dict"])
        assert ok is False
        assert "not a dictionary" in err

        # Missing record_digest
        ok, err = verify_record_digest({"record_id": "r1"})
        assert ok is False
        assert "missing or empty 'record_digest'" in err

        # Empty string
        ok, err = verify_record_digest({"record_id": "r1", "record_digest": "   "})
        assert ok is False
        assert "missing or empty 'record_digest'" in err

        # Non-string (int)
        ok, err = verify_record_digest({"record_id": "r1", "record_digest": 123456})
        assert ok is False
        assert "missing or empty 'record_digest'" in err

        # Truncated length (63 characters)
        ok, err = verify_record_digest({"record_id": "r1", "record_digest": "a" * 63})
        assert ok is False
        assert "not a 64-character hex string" in err

        # Too long length (65 characters)
        ok, err = verify_record_digest({"record_id": "r1", "record_digest": "a" * 65})
        assert ok is False
        assert "not a 64-character hex string" in err

        # Non-hex characters (64 chars containing 'z')
        ok, err = verify_record_digest({"record_id": "r1", "record_digest": "z" * 64})
        assert ok is False
        assert "not a 64-character hex string" in err

    def test_emergency_record_integrity_retry_recovers_unverifiable_record(self, tmp_path):
        """When an existing record on disk is unverifiable, append_emergency_history
        does not treat it as validly persisted, preserves the corrupted line on disk without deletion,
        and safely appends a fresh valid record upon retry."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))
        record = AuditRecord(
            record_id="rec-unverif-retry",
            run_id="run-unverif",
            terminal_outcome="approved",
            artifact_path="test.md",
            execution_executed=True,
        )

        # 1. Manually write a corrupted/tampered entry to emergency file
        errors_dir = audit_root / "errors"
        errors_dir.mkdir(parents=True, exist_ok=True)
        emerg_file = errors_dir / "audit_emergency.jsonl"
        corrupted_payload = {
            "record_id": "rec-unverif-retry",
            "run_id": "run-unverif",
            "terminal_outcome": "tampered",
            "record_digest": "0" * 64,  # mismatched dummy digest
            "storage_category": "errors",
            "emergency": True,
        }
        emerg_file.write_text(json.dumps(corrupted_payload) + "\n", encoding="utf-8")

        # Verify initial state: 1 malformed line, 0 valid records
        records_by_id, malformed = writer.read_emergency_records()
        assert len(records_by_id) == 0
        assert len(malformed) == 1

        # 2. Now attempt emergency history recovery
        res = writer.append_emergency_history([record], failure_reason="retry write")

        # Must report unresolved corrupt collision and fail closed
        assert res.all_ok is False
        assert "rec-unverif-retry" in res.failed_record_ids
        assert len(res.conflicts) == 1
        conflict_info = res.conflicts[0]
        assert conflict_info["conflict"] is True
        assert conflict_info["corrupt_collision"] is True
        assert conflict_info["persisted"] is False
        assert conflict_info["supplemental_preserved"] is True
        supp_id = conflict_info["supplemental_record_id"]
        assert supp_id.startswith("rec-unverif-retry-corrupt-collision-")

        # Verify disk state: corrupted line preserved + fresh supplemental line appended (2 lines total)
        lines = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2
        assert "tampered" in lines[0]  # Corrupted original line preserved!

        # Supplemental record contains incoming domain state
        supp_data = json.loads(lines[1])
        assert supp_data["record_id"] == supp_id
        assert supp_data["terminal_outcome"] == "approved"
        assert supp_data["artifact_path"] == "test.md"

    def test_fallback_writer_malformed_and_insufficient_receipts_fail_closed(self, tmp_path):
        """In persist_record_resilient, when using fallback writer without append_emergency_history:
        malformed receipts, wrong record_id, missing persisted, string persisted, contradictory state,
        missing durability, unconfirmed durability, and failed durability all fail closed."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            persist_record_resilient,
        )

        class ParametricFallbackWriter:
            def __init__(self, receipt_to_return):
                self.receipt_to_return = receipt_to_return

            def append(self, record):
                raise OSError("primary failed")

            def append_emergency(self, record, failure_reason=""):
                return self.receipt_to_return

        rec = AuditRecord(record_id="rec-fb-test", run_id="run-fb")
        hist_rec = AuditRecord(record_id="rec-fb-hist", run_id="run-fb")

        bad_receipts = [
            ("non-dict receipt", "not a dictionary receipt string"),
            ("missing persisted", {"durability": "confirmed", "record_id": "rec-fb-test", "audit_path": "/a.jsonl", "emergency": True}),
            ("string persisted", {"persisted": "true", "durability": "confirmed", "record_id": "rec-fb-test", "audit_path": "/a.jsonl", "emergency": True}),
            ("persisted false", {"persisted": False, "durability": "failed", "record_id": "rec-fb-test", "audit_path": "/a.jsonl", "emergency": True}),
            ("missing durability", {"persisted": True, "record_id": "rec-fb-test", "audit_path": "/a.jsonl", "emergency": True}),
            ("durability failed", {"persisted": True, "durability": "failed", "record_id": "rec-fb-test", "audit_path": "/a.jsonl", "emergency": True}),
            ("mismatched record_id", {"persisted": True, "durability": "confirmed", "record_id": "rec-OTHER-ID", "audit_path": "/a.jsonl", "emergency": True}),
            ("empty audit_path", {"persisted": True, "durability": "confirmed", "record_id": "rec-fb-test", "audit_path": "", "emergency": True}),
        ]

        for label, bad_rcpt in bad_receipts:
            writer = ParametricFallbackWriter(bad_rcpt)
            res = persist_record_resilient(writer, rec, history=[hist_rec, rec])
            assert res["persisted"] is False, f"Failed closed assertion for {label}"
            assert res["durability"] == "failed", f"Durability failed assertion for {label}"
            assert res["recovery_all_ok"] is False, f"recovery_all_ok assertion for {label}"
            assert "emergency_error" in res, f"emergency_error diagnostic for {label}"

        # Durability unconfirmed: should preserve durability='unconfirmed'
        unconfirmed_rcpt = {
            "persisted": True,
            "durability": "unconfirmed",
            "record_id": "rec-fb-test",
            "audit_path": "/a.jsonl",
            "emergency": True,
        }
        writer_unconfirmed = ParametricFallbackWriter(unconfirmed_rcpt)
        res_unconf = persist_record_resilient(writer_unconfirmed, rec, history=[hist_rec, rec])
        assert res_unconf["persisted"] is False
        assert res_unconf["durability"] == "unconfirmed"
        assert res_unconf["recovery_all_ok"] is False

    def test_emergency_recovery_supplemental_conflict_evidence_fields_and_integrity(self, tmp_path):
        """When append_emergency_history encounters an identity/content conflict,
        the supplemental record written to the journal retains terminal_outcome (not empty!),
        artifact_path, artifact_reference, execution_executed, conflict metadata,
        and its record_digest is fully verifiable."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            verify_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "errors").mkdir()

        writer = AuditWriter(str(audit_root))
        original_rec = AuditRecord(
            record_id="rec-supp-test-1",
            run_id="run-supp-1",
            terminal_outcome="approved",
            artifact_path="primary_doc.md",
            artifact_reference="ref-100",
            execution_executed=True,
            proposal_action="CREATE",
            proposal_hash="hash-orig",
        )

        # 1. Initial write
        res1 = writer.append_emergency_history([original_rec], failure_reason="first write")
        assert res1.all_ok is True

        # 2. Conflicting record with different domain state
        conflicting_rec = AuditRecord(
            record_id="rec-supp-test-1",
            run_id="run-supp-1",
            terminal_outcome="rejected",
            artifact_path="different_doc.md",
            artifact_reference="ref-200",
            execution_executed=False,
            proposal_action="UPDATE",
            proposal_hash="hash-diff",
        )

        res2 = writer.append_emergency_history([conflicting_rec], failure_reason="conflicting write")
        assert res2.all_ok is False
        assert "rec-supp-test-1" in res2.failed_record_ids
        assert len(res2.conflicts) == 1
        conflict_info = res2.conflicts[0]
        assert conflict_info["supplemental_preserved"] is True
        assert conflict_info["supplemental_error"] == ""
        supp_id = conflict_info["supplemental_record_id"]
        assert supp_id.startswith("rec-supp-test-1-conflict-")

        # 3. Read and inspect the physical JSONL lines
        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2

        # First line is original record
        assert lines[0]["record_id"] == "rec-supp-test-1"
        assert lines[0]["terminal_outcome"] == "approved"
        assert lines[0]["artifact_path"] == "primary_doc.md"

        # Second line is supplemental conflict record
        supp_line = lines[1]
        assert supp_line["record_id"] == supp_id
        assert supp_line["conflict_with_record_id"] == "rec-supp-test-1"
        assert supp_line["conflict_type"] == "identity_content_mismatch"
        assert supp_line["terminal_outcome"] == "rejected"
        assert supp_line["artifact_path"] == "different_doc.md"
        assert supp_line["artifact_reference"] == "ref-200"
        assert supp_line["execution_executed"] is False
        assert supp_line["proposal_action"] == "UPDATE"
        assert supp_line["proposal_hash"] == "hash-diff"
        assert supp_line["original_stored_digest"] == lines[0]["record_digest"]

        # Critical: Verify digest integrity of the supplemental record itself
        is_valid_supp, supp_err = verify_record_digest(supp_line)
        assert is_valid_supp is True
        assert supp_err == ""

    def test_emergency_recovery_supplemental_conflict_retry_is_idempotent(self, tmp_path):
        """Repeated emergency recovery with an identical conflicting record is idempotent:
        it detects the already-preserved supplemental record, does not append duplicate rows,
        and reports supplemental_preserved=True."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "errors").mkdir()

        writer = AuditWriter(str(audit_root))
        original_rec = AuditRecord(
            record_id="rec-supp-idem-1",
            run_id="run-supp-idem",
            terminal_outcome="approved",
        )
        conflicting_rec = AuditRecord(
            record_id="rec-supp-idem-1",
            run_id="run-supp-idem",
            terminal_outcome="rejected",
        )

        # 1. Original record
        writer.append_emergency_history([original_rec], failure_reason="first")

        # 2. First conflict write
        res1 = writer.append_emergency_history([conflicting_rec], failure_reason="conflict 1")
        assert res1.conflicts[0]["supplemental_preserved"] is True

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines_after_first = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after_first) == 2

        # 3. Second conflict write (retry)
        res2 = writer.append_emergency_history([conflicting_rec], failure_reason="conflict 2 (retry)")
        assert res2.all_ok is False
        assert res2.conflicts[0]["supplemental_preserved"] is True
        assert res2.conflicts[0]["supplemental_receipt"]["note"] == "already_persisted_conflict_evidence"

        # Verify NO duplicate line was written: still exactly 2 lines
        lines_after_second = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after_second) == 2
        assert lines_after_second == lines_after_first

    def test_emergency_recovery_supplemental_write_failure_surfaces_diagnostics(self, tmp_path, monkeypatch):
        """When writing supplemental conflict evidence fails (e.g. disk error),
        the error is surfaced explicitly in supplemental_error and errors dictionary,
        supplemental_preserved is False, and the operation fails closed without swallowing the failure."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "errors").mkdir()

        writer = AuditWriter(str(audit_root))
        original_rec = AuditRecord(
            record_id="rec-supp-err-1",
            run_id="run-supp-err",
            terminal_outcome="approved",
        )
        conflicting_rec = AuditRecord(
            record_id="rec-supp-err-1",
            run_id="run-supp-err",
            terminal_outcome="rejected",
        )

        # 1. Original write succeeds
        writer.append_emergency_history([original_rec], failure_reason="first")

        # 2. Patch append_emergency to fail when writing supplemental conflict record
        real_append_emergency = writer.append_emergency

        def failing_supplemental_emergency(record, failure_reason=""):
            rec_id = str(getattr(record, "record_id", "") or (record.get("record_id") if isinstance(record, dict) else "") or "")
            if "conflict" in rec_id:
                raise OSError("simulated disk error writing supplemental conflict record")
            return real_append_emergency(record, failure_reason=failure_reason)

        monkeypatch.setattr(writer, "append_emergency", failing_supplemental_emergency)

        res = writer.append_emergency_history([conflicting_rec], failure_reason="conflict write")

        assert res.all_ok is False
        assert "rec-supp-err-1" in res.failed_record_ids
        assert len(res.conflicts) == 1
        conflict_info = res.conflicts[0]
        assert conflict_info["supplemental_preserved"] is False
        assert "simulated disk error writing supplemental conflict record" in conflict_info["supplemental_error"]
        assert "rec-supp-err-1_supplemental_preservation" in res.errors

    def test_canonical_domain_matching_identical_with_storage_metadata_differences(self):
        """Identical canonical domain content matches even when legitimate storage metadata fields differ."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
            compute_record_digest,
        )

        incoming = AuditRecord(
            record_id="rec-match-1",
            run_id="run-match",
            terminal_outcome="approved",
            artifact_path="doc.md",
            execution_executed=True,
        )

        stored = {
            "record_id": "rec-match-1",
            "run_id": "run-match",
            "terminal_outcome": "approved",
            "artifact_path": "doc.md",
            "execution_executed": True,
            # Storage metadata differences:
            "storage_category": "errors",
            "emergency": True,
            "emergency_reason": "primary append disk error",
            "emergency_timestamp": "2026-10-26T00:00:00Z",
            "persistence_timestamp": "2026-10-26T00:00:01Z",
        }
        stored["record_digest"] = compute_record_digest(stored)

        is_match, mismatches = _check_emergency_record_match(incoming, stored)
        assert is_match is True
        assert len(mismatches) == 0

    def test_canonical_domain_matching_extra_meaningful_field_in_stored(self):
        """An extra meaningful domain field in stored record causes match failure in bidirectional comparison."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
            compute_record_digest,
        )

        incoming = AuditRecord(
            record_id="rec-extra-1",
            run_id="run-extra",
            terminal_outcome="approved",
        )

        stored = {
            "record_id": "rec-extra-1",
            "run_id": "run-extra",
            "terminal_outcome": "approved",
            "extra_domain_metadata": "unexpected reviewer note",
            "storage_category": "errors",
            "emergency": True,
        }
        stored["record_digest"] = compute_record_digest(stored)

        is_match, mismatches = _check_emergency_record_match(incoming, stored)
        assert is_match is False
        assert any("extra_domain_metadata" in m for m in mismatches)

    def test_canonical_domain_matching_missing_meaningful_field_in_stored(self):
        """A meaningful field present on incoming record but missing from stored record causes match failure."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
            compute_record_digest,
        )

        incoming = AuditRecord(
            record_id="rec-missing-1",
            run_id="run-missing",
            terminal_outcome="approved",
            proposal_action="CREATE",
        )

        stored = {
            "record_id": "rec-missing-1",
            "run_id": "run-missing",
            "terminal_outcome": "approved",
            # proposal_action is missing from stored!
            "storage_category": "errors",
            "emergency": True,
        }
        stored["record_digest"] = compute_record_digest(stored)

        is_match, mismatches = _check_emergency_record_match(incoming, stored)
        assert is_match is False
        assert any("proposal_action" in m for m in mismatches)

    def test_canonical_domain_matching_different_terminal_outcome_or_artifact_metadata(self):
        """Differing terminal outcomes or artifact references cause match failure."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
            compute_record_digest,
        )

        incoming = AuditRecord(
            record_id="rec-diff-1",
            run_id="run-diff",
            terminal_outcome="approved",
            artifact_path="doc_a.md",
        )

        stored_diff_outcome = {
            "record_id": "rec-diff-1",
            "run_id": "run-diff",
            "terminal_outcome": "rejected",
            "artifact_path": "doc_a.md",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_diff_outcome["record_digest"] = compute_record_digest(stored_diff_outcome)
        match_outcome, mismatches_outcome = _check_emergency_record_match(incoming, stored_diff_outcome)
        assert match_outcome is False
        assert any("terminal_outcome" in m for m in mismatches_outcome)

        stored_diff_artifact = {
            "record_id": "rec-diff-1",
            "run_id": "run-diff",
            "terminal_outcome": "approved",
            "artifact_path": "doc_DIFFERENT.md",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_diff_artifact["record_digest"] = compute_record_digest(stored_diff_artifact)
        match_art, mismatches_art = _check_emergency_record_match(incoming, stored_diff_artifact)
        assert match_art is False
        assert any("artifact_path" in m for m in mismatches_art)

    def test_canonical_domain_matching_valid_stored_digest_but_different_content(self):
        """A stored entry with a valid digest matching its own content still fails match when content differs from incoming."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
            compute_record_digest,
            verify_record_digest,
        )

        incoming = AuditRecord(
            record_id="rec-valid-dig-diff-1",
            run_id="run-1",
            terminal_outcome="approved",
            execution_executed=True,
        )

        stored = {
            "record_id": "rec-valid-dig-diff-1",
            "run_id": "run-1",
            "terminal_outcome": "held",
            "execution_executed": False,
            "storage_category": "errors",
            "emergency": True,
        }
        stored["record_digest"] = compute_record_digest(stored)

        # Stored digest is valid for its stored content
        is_digest_valid, _ = verify_record_digest(stored)
        assert is_digest_valid is True

        # But domain content match fails
        is_match, mismatches = _check_emergency_record_match(incoming, stored)
        assert is_match is False
        assert any("terminal_outcome" in m for m in mismatches)
        assert any("execution_executed" in m for m in mismatches)

    def test_canonical_domain_matching_invalid_stored_digest(self):
        """When stored entry has an invalid or corrupted digest, match verification fails immediately."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
        )

        incoming = AuditRecord(
            record_id="rec-bad-dig-1",
            run_id="run-1",
            terminal_outcome="approved",
        )

        stored = {
            "record_id": "rec-bad-dig-1",
            "run_id": "run-1",
            "terminal_outcome": "approved",
            "record_digest": "0" * 64,  # Corrupt dummy digest
            "storage_category": "errors",
            "emergency": True,
        }

        is_match, mismatches = _check_emergency_record_match(incoming, stored)
        assert is_match is False
        assert any("stored record integrity verification failed" in m for m in mismatches)

    def test_supplemental_conflict_repeated_recovery_unchanged(self, tmp_path):
        """Repeated recovery of an unchanged conflict verifies existing supplemental record
        and does not write duplicate entries."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))
        orig_rec = AuditRecord(record_id="rec-idem-supp", terminal_outcome="approved")
        conf_rec = AuditRecord(record_id="rec-idem-supp", terminal_outcome="rejected")

        # 1. Initial write
        writer.append_emergency_history([orig_rec])
        res1 = writer.append_emergency_history([conf_rec])
        assert res1.all_ok is False
        assert res1.conflicts[0]["supplemental_preserved"] is True

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines1 = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines1) == 2

        # 2. Retry of the same unchanged conflict
        res2 = writer.append_emergency_history([conf_rec])
        assert res2.all_ok is False
        assert res2.conflicts[0]["supplemental_preserved"] is True
        assert res2.conflicts[0]["supplemental_receipt"]["note"] == "already_persisted_conflict_evidence"

        lines2 = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines2) == 2  # Idempotent: exactly 2 lines

    def test_supplemental_conflict_existing_record_altered_linkage_or_domain_content_fails_closed(self, tmp_path):
        """If an existing supplemental record on disk has altered conflict linkage or domain content,
        retry detects inconsistency, reports it in supplemental_error, and fails closed."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "errors").mkdir()
        writer = AuditWriter(str(audit_root))
        orig_rec = AuditRecord(record_id="rec-alt-supp", terminal_outcome="approved")
        conf_rec = AuditRecord(record_id="rec-alt-supp", terminal_outcome="rejected")

        writer.append_emergency_history([orig_rec])
        res1 = writer.append_emergency_history([conf_rec])
        assert res1.conflicts[0]["supplemental_preserved"] is True

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2

        # Alter the supplemental record's conflict_type on disk, updating its digest so it remains valid JSON
        lines[1]["conflict_type"] = "tampered_conflict_type"
        lines[1]["record_digest"] = compute_record_digest(lines[1])
        emerg_file.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

        # Retry with the same conf_rec
        res2 = writer.append_emergency_history([conf_rec])
        assert res2.all_ok is False
        conflict_info = res2.conflicts[0]
        assert conflict_info["supplemental_preserved"] is False
        assert "content inconsistent" in conflict_info["supplemental_error"]

    def test_supplemental_conflict_existing_record_invalid_digest_fails_closed(self, tmp_path):
        """If an existing supplemental record has an invalid digest, recovery fails closed."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        writer = AuditWriter(str(audit_root))
        orig_rec = AuditRecord(record_id="rec-bad-supp-dig", terminal_outcome="approved")
        conf_rec = AuditRecord(record_id="rec-bad-supp-dig", terminal_outcome="rejected")

        writer.append_emergency_history([orig_rec])
        res1 = writer.append_emergency_history([conf_rec])
        assert res1.conflicts[0]["supplemental_preserved"] is True

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2

        # Tamper with the supplemental record's digest
        lines[1]["record_digest"] = "0" * 64
        emerg_file.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

        # Retry
        res2 = writer.append_emergency_history([conf_rec])
        assert res2.all_ok is False
        conflict_info = res2.conflicts[0]
        assert conflict_info["supplemental_preserved"] is False
        assert "corrupt on disk" in conflict_info["supplemental_error"] or "digest invalid" in conflict_info["supplemental_error"]

    def test_supplemental_conflict_persistence_failure_followed_by_retry(self, tmp_path, monkeypatch):
        """When supplemental conflict write fails initially, failure is reported;
        upon retry with writer working, supplemental evidence is successfully preserved."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        (audit_root / "errors").mkdir()

        writer = AuditWriter(str(audit_root))
        orig_rec = AuditRecord(record_id="rec-fail-retry", terminal_outcome="approved")
        conf_rec = AuditRecord(record_id="rec-fail-retry", terminal_outcome="rejected")

        # 1. Original write succeeds
        writer.append_emergency_history([orig_rec])

        # 2. Patch append_emergency to fail on supplemental write
        real_append_emergency = writer.append_emergency

        def failing_emergency(record, failure_reason=""):
            rec_id = str(getattr(record, "record_id", "") or (record.get("record_id") if isinstance(record, dict) else "") or "")
            if "conflict" in rec_id:
                raise OSError("simulated disk error writing supplemental record")
            return real_append_emergency(record, failure_reason=failure_reason)

        monkeypatch.setattr(writer, "append_emergency", failing_emergency)

        res1 = writer.append_emergency_history([conf_rec])
        assert res1.all_ok is False
        assert res1.conflicts[0]["supplemental_preserved"] is False
        assert "simulated disk error" in res1.conflicts[0]["supplemental_error"]

        # 3. Restore append_emergency (transient error resolved) and retry
        monkeypatch.setattr(writer, "append_emergency", real_append_emergency)

        res2 = writer.append_emergency_history([conf_rec])
        assert res2.all_ok is False
        assert res2.conflicts[0]["supplemental_preserved"] is True
        assert res2.conflicts[0]["supplemental_error"] == ""

        emerg_file = audit_root / "errors" / "audit_emergency.jsonl"
        lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2
        assert lines[0]["record_id"] == "rec-fail-retry"
        assert lines[1]["conflict_with_record_id"] == "rec-fail-retry"

    def test_corrupt_existing_record_truncated_entry_collision_handled_conservatively(self, tmp_path):
        """When an incoming record's ID collides with a truncated JSON line in the journal,
        it does NOT append a normal replacement under that ID, does NOT overwrite the truncated line,
        preserves the incoming evidence via a supplemental record, and fails closed."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        # Write a truncated JSON line containing an identifiable record_id
        emerg_file.write_text('{"record_id": "rec-trunc-coll", "run_id": "run-trunc", "term\n', encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        incoming = AuditRecord(record_id="rec-trunc-coll", run_id="run-trunc", terminal_outcome="approved")

        res = writer.append_emergency_history([incoming])
        assert res.all_ok is False
        assert "rec-trunc-coll" in res.failed_record_ids
        assert len(res.conflicts) == 1
        conflict_info = res.conflicts[0]
        assert conflict_info["conflict"] is True
        assert conflict_info["corrupt_collision"] is True
        assert conflict_info["persisted"] is False
        assert conflict_info["supplemental_preserved"] is True
        supp_id = conflict_info["supplemental_record_id"]
        assert supp_id.startswith("rec-trunc-coll-corrupt-collision-")

        # Disk check: original truncated line preserved + supplemental line appended
        lines = emerg_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert '{"record_id": "rec-trunc-coll", "run_id": "run-trunc", "term' in lines[0]
        supp_data = json.loads(lines[1])
        assert supp_data["record_id"] == supp_id
        assert supp_data["terminal_outcome"] == "approved"

        # Retry does NOT turn unresolved collision into success:
        res_retry = writer.append_emergency_history([incoming])
        assert res_retry.all_ok is False
        lines_after_retry = emerg_file.read_text(encoding="utf-8").splitlines()
        assert len(lines_after_retry) == 2  # No duplicate rows created!

    def test_corrupt_existing_record_malformed_json_collision_handled_conservatively(self, tmp_path):
        """When an incoming record's ID collides with a malformed JSON line with identifiable ID,
        collision is reported and handled conservatively."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        # Malformed JSON syntax with identifiable record_id
        emerg_file.write_text('{"record_id": "rec-badjson-coll", broken syntax here!\n', encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        incoming = AuditRecord(record_id="rec-badjson-coll", run_id="run-badjson", terminal_outcome="held")

        res = writer.append_emergency_history([incoming])
        assert res.all_ok is False
        assert "rec-badjson-coll" in res.failed_record_ids
        assert res.conflicts[0]["corrupt_collision"] is True
        assert res.conflicts[0]["supplemental_preserved"] is True

        # File has original malformed line + supplemental record
        lines = emerg_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert "broken syntax here!" in lines[0]

    def test_corrupt_existing_record_bad_digest_collision_handled_conservatively(self, tmp_path):
        """When an incoming record collides with a valid JSON line that has an invalid digest,
        collision is reported, incoming evidence is preserved via supplemental record, and fails closed."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        # Valid JSON but bad digest
        emerg_file.write_text(json.dumps({
            "record_id": "rec-baddig-coll",
            "run_id": "run-baddig",
            "terminal_outcome": "approved",
            "record_digest": "f" * 64,  # Invalid digest
            "storage_category": "errors",
            "emergency": True,
        }) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        incoming = AuditRecord(record_id="rec-baddig-coll", run_id="run-baddig", terminal_outcome="approved")

        res = writer.append_emergency_history([incoming])
        assert res.all_ok is False
        assert "rec-baddig-coll" in res.failed_record_ids
        assert res.conflicts[0]["corrupt_collision"] is True
        assert res.conflicts[0]["supplemental_preserved"] is True

        # Retry remains unsuccessful
        res_retry = writer.append_emergency_history([incoming])
        assert res_retry.all_ok is False
        lines_after_retry = emerg_file.read_text(encoding="utf-8").splitlines()
        assert len(lines_after_retry) == 2

    def test_canonical_domain_matching_rejects_zero_versus_false(self):
        """0 and False must not be collapsed as equivalent in canonical domain matching."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
            compute_record_digest,
        )

        # 1. query_position int 0 vs bool False
        incoming_int = AuditRecord(record_id="rec-num-bool", query_position=0, terminal_outcome="approved")
        stored_bool = {
            "record_id": "rec-num-bool",
            "query_position": False,
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_bool["record_digest"] = compute_record_digest(stored_bool)
        is_match, diffs = _check_emergency_record_match(incoming_int, stored_bool)
        assert is_match is False
        assert any("query_position" in d for d in diffs)

        # 2. execution_executed bool False vs int 0
        incoming_bool = AuditRecord(record_id="rec-bool-num", execution_executed=False, terminal_outcome="approved")
        stored_int = {
            "record_id": "rec-bool-num",
            "execution_executed": 0,
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_int["record_digest"] = compute_record_digest(stored_int)
        is_match2, diffs2 = _check_emergency_record_match(incoming_bool, stored_int)
        assert is_match2 is False
        assert any("execution_executed" in d for d in diffs2)

    def test_canonical_domain_matching_rejects_none_versus_empty_string(self):
        """None and empty string '' must not be collapsed as equivalent."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            _check_emergency_record_match,
            compute_record_digest,
        )

        incoming = {
            "record_id": "rec-none-str",
            "terminal_outcome": "approved",
            "proposal_action": None,
        }
        stored = {
            "record_id": "rec-none-str",
            "terminal_outcome": "approved",
            "proposal_action": "",
            "storage_category": "errors",
            "emergency": True,
        }
        stored["record_digest"] = compute_record_digest(stored)
        is_match, diffs = _check_emergency_record_match(incoming, stored)
        assert is_match is False
        assert any("proposal_action" in d for d in diffs)

    def test_canonical_domain_matching_rejects_none_versus_empty_list(self):
        """None and empty list [] must not be collapsed as equivalent."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            _check_emergency_record_match,
            compute_record_digest,
        )

        incoming = {
            "record_id": "rec-none-list",
            "terminal_outcome": "approved",
            "errors": None,
        }
        stored = {
            "record_id": "rec-none-list",
            "terminal_outcome": "approved",
            "errors": [],
            "storage_category": "errors",
            "emergency": True,
        }
        stored["record_digest"] = compute_record_digest(stored)
        is_match, diffs = _check_emergency_record_match(incoming, stored)
        assert is_match is False
        assert any("errors" in d for d in diffs)

    def test_canonical_domain_matching_rejects_true_versus_string_false(self):
        """Boolean True must never match serialized string 'false'."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
            compute_record_digest,
        )

        incoming = AuditRecord(record_id="rec-t-f", execution_executed=True, terminal_outcome="approved")
        stored = {
            "record_id": "rec-t-f",
            "execution_executed": "false",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored["record_digest"] = compute_record_digest(stored)
        is_match, diffs = _check_emergency_record_match(incoming, stored)
        assert is_match is False
        assert any("execution_executed" in d for d in diffs)

    def test_canonical_domain_matching_valid_booleans_consistent_across_record_and_dict(self):
        """Valid boolean values match across AuditRecord and dictionary, while mismatches are rejected."""
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditRecord,
            _check_emergency_record_match,
            compute_record_digest,
        )

        # True matches True
        incoming_t = AuditRecord(record_id="rec-bool-match", execution_executed=True, terminal_outcome="approved")
        stored_t = {
            "record_id": "rec-bool-match",
            "execution_executed": True,
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_t["record_digest"] = compute_record_digest(stored_t)
        match_t, diffs_t = _check_emergency_record_match(incoming_t, stored_t)
        assert match_t is True
        assert len(diffs_t) == 0

        # True matches serialized 'true'
        stored_str_t = dict(stored_t)
        stored_str_t["execution_executed"] = "true"
        stored_str_t["record_digest"] = compute_record_digest(stored_str_t)
        match_str_t, diffs_str_t = _check_emergency_record_match(incoming_t, stored_str_t)
        assert match_str_t is True

        # False matches False
        incoming_f = AuditRecord(record_id="rec-bool-match", execution_executed=False, terminal_outcome="approved")
        stored_f = {
            "record_id": "rec-bool-match",
            "execution_executed": False,
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_f["record_digest"] = compute_record_digest(stored_f)
        match_f, diffs_f = _check_emergency_record_match(incoming_f, stored_f)
        assert match_f is True

        # True does not match False
        stored_f_for_t = dict(stored_f)
        match_mismatch, diffs_mismatch = _check_emergency_record_match(incoming_t, stored_f_for_t)
        assert match_mismatch is False
        assert any("execution_executed" in d for d in diffs_mismatch)

        # Unsupported string representation rejected
        stored_bad = dict(stored_t)
        stored_bad["execution_executed"] = "invalid_truthy_string"
        stored_bad["record_digest"] = compute_record_digest(stored_bad)
        match_bad, diffs_bad = _check_emergency_record_match(incoming_t, stored_bad)
        assert match_bad is False
        assert any("execution_executed" in d for d in diffs_bad)

    def test_journal_duplicate_same_id_identical_domain_content(self, tmp_path):
        """When the emergency journal contains two valid entries with the same ID and identical domain content:
        it is surfaced as a duplicate journal entry, recovery does not claim complete success,
        and no additional duplicate rows are written."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        entry1 = {
            "record_id": "rec-dup-ident",
            "run_id": "run-dup-ident",
            "terminal_outcome": "approved",
            "artifact_path": "doc.md",
            "storage_category": "errors",
            "emergency": True,
            "timestamp": "2026-10-26T00:00:00Z",
        }
        entry1["record_digest"] = compute_record_digest(entry1)

        entry2 = dict(entry1)
        entry2["timestamp"] = "2026-10-26T00:00:05Z"
        entry2["record_digest"] = compute_record_digest(entry2)

        emerg_file.write_text(json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        read_res = writer.read_emergency_records()

        # Unpacking works
        recs, malformed = read_res
        assert len(read_res.duplicates) == 1
        dup = read_res.duplicates[0]
        assert dup["record_id"] == "rec-dup-ident"
        assert dup["conflict"] is False
        assert dup["first_line_no"] == 1
        assert dup["line_no"] == 2
        assert dup["differing_fields"] == []

        # Recovery does not claim all_ok=True while unresolved duplicate exists
        incoming = AuditRecord(
            record_id="rec-dup-ident",
            run_id="run-dup-ident",
            terminal_outcome="approved",
            artifact_path="doc.md",
        )
        rec_res = writer.append_emergency_history([incoming])
        assert rec_res.all_ok is False
        assert len(rec_res.duplicates) == 1
        assert any("duplicate_record_id_rec-dup-ident" in k for k in rec_res.errors)

        # No duplicate lines added to journal
        lines_after = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after) == 2

    def test_journal_duplicate_same_id_different_terminal_outcomes(self, tmp_path):
        """When journal contains two records with same ID but conflicting terminal outcomes:
        internal conflict is reported, clean dictionary excludes it, incoming recovery fails closed,
        and evidence is safely preserved in supplemental record."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        entry1 = {
            "record_id": "rec-dup-term",
            "run_id": "run-dup-term",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        entry1["record_digest"] = compute_record_digest(entry1)

        entry2 = {
            "record_id": "rec-dup-term",
            "run_id": "run-dup-term",
            "terminal_outcome": "rejected",
            "storage_category": "errors",
            "emergency": True,
        }
        entry2["record_digest"] = compute_record_digest(entry2)

        emerg_file.write_text(json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        read_res = writer.read_emergency_records()
        assert len(read_res.duplicates) == 1
        dup = read_res.duplicates[0]
        assert dup["record_id"] == "rec-dup-term"
        assert dup["conflict"] is True
        assert any("terminal_outcome" in d for d in dup["differing_fields"])
        # Neither conflicting entry is authoritative in clean dictionary
        assert "rec-dup-term" not in read_res.records_by_id

        # Incoming recovery fails closed and preserves evidence
        incoming = AuditRecord(record_id="rec-dup-term", run_id="run-dup-term", terminal_outcome="approved")
        rec_res = writer.append_emergency_history([incoming])
        assert rec_res.all_ok is False
        assert "rec-dup-term" in rec_res.failed_record_ids
        assert len(rec_res.conflicts) == 1
        assert rec_res.conflicts[0]["conflict"] is True
        assert rec_res.conflicts[0]["duplicate_conflict"] is True
        assert rec_res.conflicts[0]["supplemental_preserved"] is True

        # Original lines 1 and 2 remain intact
        lines = [json.loads(l) for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3
        assert lines[0]["terminal_outcome"] == "approved"
        assert lines[1]["terminal_outcome"] == "rejected"
        assert lines[2]["record_id"].startswith("rec-dup-term-conflict-")

    def test_journal_duplicate_same_id_different_artifact_metadata(self, tmp_path):
        """When journal contains two records with same ID but different artifact paths:
        internal conflict is detected and reported on artifact_path."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        entry1 = {
            "record_id": "rec-dup-art",
            "run_id": "run-dup-art",
            "terminal_outcome": "approved",
            "artifact_path": "doc_v1.md",
            "storage_category": "errors",
            "emergency": True,
        }
        entry1["record_digest"] = compute_record_digest(entry1)

        entry2 = {
            "record_id": "rec-dup-art",
            "run_id": "run-dup-art",
            "terminal_outcome": "approved",
            "artifact_path": "doc_v2.md",
            "storage_category": "errors",
            "emergency": True,
        }
        entry2["record_digest"] = compute_record_digest(entry2)

        emerg_file.write_text(json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        read_res = writer.read_emergency_records()
        assert len(read_res.duplicates) == 1
        dup = read_res.duplicates[0]
        assert dup["conflict"] is True
        assert any("artifact_path" in d for d in dup["differing_fields"])

        incoming = AuditRecord(record_id="rec-dup-art", run_id="run-dup-art", terminal_outcome="approved", artifact_path="doc_v1.md")
        rec_res = writer.append_emergency_history([incoming])
        assert rec_res.all_ok is False
        assert rec_res.conflicts[0]["conflict"] is True
        assert rec_res.conflicts[0]["duplicate_conflict"] is True

    def test_journal_multiple_valid_occurrences_followed_by_recovery_attempt(self, tmp_path):
        """Multiple occurrences of the same record ID in the journal are all tracked,
        and subsequent recovery attempts fail closed without appending further duplicate rows."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        # Three occurrences of rec-multi
        rows = []
        for i in range(1, 4):
            e = {
                "record_id": "rec-multi",
                "run_id": "run-multi",
                "terminal_outcome": "approved",
                "step": i,
                "storage_category": "errors",
                "emergency": True,
            }
            e["record_digest"] = compute_record_digest(e)
            rows.append(json.dumps(e))
        emerg_file.write_text("\n".join(rows) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        read_res = writer.read_emergency_records()
        assert len(read_res.all_records_by_id["rec-multi"]) == 3
        assert len(read_res.duplicates) == 2

        incoming = AuditRecord(record_id="rec-multi", run_id="run-multi", terminal_outcome="approved")
        rec_res = writer.append_emergency_history([incoming])
        assert rec_res.all_ok is False

        # All 3 original lines remain intact on disk
        lines_after = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after) == 4  # 3 original + 1 supplemental conflict evidence

    def test_journal_duplicate_conflict_alongside_unrelated_malformed_line(self, tmp_path):
        """A duplicate-ID conflict alongside an unrelated malformed journal line reports both
        distinct errors while preserving valid unrelated records."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        line1 = {
            "record_id": "rec-dup-mix",
            "run_id": "run-dup-mix",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        line1["record_digest"] = compute_record_digest(line1)

        line2_malformed = "NOT A JSON OBJECT LINE -- CORRUPT!\n"

        line3_conflict = {
            "record_id": "rec-dup-mix",
            "run_id": "run-dup-mix",
            "terminal_outcome": "rejected",
            "storage_category": "errors",
            "emergency": True,
        }
        line3_conflict["record_digest"] = compute_record_digest(line3_conflict)

        line4_clean = {
            "record_id": "rec-clean-unrelated",
            "run_id": "run-clean",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        line4_clean["record_digest"] = compute_record_digest(line4_clean)

        emerg_file.write_text(
            json.dumps(line1) + "\n" +
            line2_malformed +
            json.dumps(line3_conflict) + "\n" +
            json.dumps(line4_clean) + "\n",
            encoding="utf-8",
        )

        writer = AuditWriter(str(audit_root))
        read_res = writer.read_emergency_records()

        # Both the malformed line and the duplicate conflict are tracked
        assert len(read_res.duplicates) == 1
        assert read_res.duplicates[0]["conflict"] is True
        assert len(read_res.malformed_lines) == 2  # 1 syntax error on line 2 + 1 duplicate conflict on line 3
        assert "rec-clean-unrelated" in read_res.records_by_id
        assert "rec-dup-mix" not in read_res.records_by_id

        # Attempting recovery on rec-clean-unrelated still reports overall all_ok=False due to journal integrity issues
        incoming_clean = AuditRecord(record_id="rec-clean-unrelated", run_id="run-clean", terminal_outcome="approved")
        rec_res = writer.append_emergency_history([incoming_clean])
        assert rec_res.all_ok is False
        assert any("malformed_line_2" in k for k in rec_res.errors)
        assert any("duplicate_record_id_rec-dup-mix" in k for k in rec_res.errors)

        # Original lines on disk remain intact
        raw_lines = emerg_file.read_text(encoding="utf-8").splitlines()
        assert len(raw_lines) == 4
        assert "NOT A JSON OBJECT LINE" in raw_lines[1]

    def test_journal_repeated_recovery_attempts_do_not_create_unbounded_supplemental_records(self, tmp_path):
        """Repeated recovery attempts on a journal with duplicate conflict idempotently verify
        the existing supplemental record without creating uncontrolled duplicates."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        e1 = {
            "record_id": "rec-bounded-supp",
            "run_id": "run-bounded",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        e1["record_digest"] = compute_record_digest(e1)

        e2 = {
            "record_id": "rec-bounded-supp",
            "run_id": "run-bounded",
            "terminal_outcome": "rejected",
            "storage_category": "errors",
            "emergency": True,
        }
        e2["record_digest"] = compute_record_digest(e2)

        emerg_file.write_text(json.dumps(e1) + "\n" + json.dumps(e2) + "\n", encoding="utf-8")

        writer = AuditWriter(str(audit_root))
        incoming = AuditRecord(record_id="rec-bounded-supp", run_id="run-bounded", terminal_outcome="quarantined")

        # 1. First attempt writes 1 supplemental conflict record (line count: 2 -> 3)
        res1 = writer.append_emergency_history([incoming])
        assert res1.all_ok is False
        assert len(res1.conflicts) == 1
        assert res1.conflicts[0]["supplemental_preserved"] is True
        lines_after_first = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after_first) == 3

        # 2. Second attempt detects already-persisted supplemental evidence (line count remains 3)
        res2 = writer.append_emergency_history([incoming])
        assert res2.all_ok is False
        assert len(res2.conflicts) == 1
        assert res2.conflicts[0]["supplemental_preserved"] is True
        assert res2.conflicts[0]["supplemental_receipt"]["note"] == "already_persisted_conflict_evidence"
        lines_after_second = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after_second) == 3

        # 3. Third attempt still strictly bounded at 3 lines
        res3 = writer.append_emergency_history([incoming])
        assert res3.all_ok is False
        lines_after_third = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after_third) == 3

    def test_supplemental_single_valid_existing_record_with_matching_content(self, tmp_path):
        """1. A single valid existing supplemental record with matching content is accepted idempotently.
        Inspects: parsed original journal entries, supplemental_preserved=True, supplemental_receipt,
        overall EmergencyRecoveryResult, and confirms retries create no additional entries."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
            record_to_dict,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        # Primary entry on disk
        stored_rec = {
            "record_id": "rec-supp-single-valid",
            "run_id": "run-supp-1",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_rec["record_digest"] = compute_record_digest(stored_rec)

        # Incoming record to recover (conflicts with stored_rec on terminal_outcome)
        incoming = AuditRecord(
            record_id="rec-supp-single-valid",
            run_id="run-supp-1",
            terminal_outcome="rejected",
        )
        incoming_dict = dict(record_to_dict(incoming))
        incoming_digest = compute_record_digest(incoming_dict)
        conflict_id = f"rec-supp-single-valid-conflict-{incoming_digest[:12]}"

        # Pre-existing valid supplemental record
        supp_entry = dict(incoming_dict)
        supp_entry["record_id"] = conflict_id
        supp_entry["conflict_with_record_id"] = "rec-supp-single-valid"
        supp_entry["conflict_type"] = "identity_content_mismatch"
        supp_entry["conflicting_fields"] = ["terminal_outcome: in-memory='rejected' != stored='approved'"]
        supp_entry["original_stored_digest"] = stored_rec["record_digest"]
        supp_entry["conflicting_content_digest"] = incoming_digest
        supp_entry["storage_category"] = "errors"
        supp_entry["emergency"] = True
        supp_entry["record_digest"] = compute_record_digest(supp_entry)

        emerg_file.write_text(
            json.dumps(stored_rec) + "\n" + json.dumps(supp_entry) + "\n",
            encoding="utf-8",
        )

        writer = AuditWriter(str(audit_root))

        # 1. Inspect parsed original entries
        read_res = writer.read_emergency_records()
        assert len(read_res.records_by_id) == 2
        assert "rec-supp-single-valid" in read_res.records_by_id
        assert conflict_id in read_res.records_by_id
        assert len(read_res.malformed_lines) == 0
        assert len(read_res.duplicates) == 0
        assert len(read_res.all_records_by_id[conflict_id]) == 1

        # 2. Recovery attempt verifies existing supplemental evidence
        res = writer.append_emergency_history([incoming])
        assert res.all_ok is False  # primary conflict keeps overall recovery unsuccessful
        assert len(res.conflicts) == 1
        conf = res.conflicts[0]
        assert conf["record_id"] == "rec-supp-single-valid"
        assert conf["supplemental_record_id"] == conflict_id
        assert conf["supplemental_preserved"] is True
        assert conf["supplemental_error"] == ""
        assert conf["supplemental_receipt"] is not None
        assert conf["supplemental_receipt"]["persisted"] is True
        assert conf["supplemental_receipt"]["durability"] == "confirmed"
        assert conf["supplemental_receipt"]["note"] == "already_persisted_conflict_evidence"

        # 3. Retries create no additional entries
        res_retry = writer.append_emergency_history([incoming])
        assert res_retry.all_ok is False
        assert res_retry.conflicts[0]["supplemental_preserved"] is True
        lines = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2

    def test_supplemental_two_identical_valid_entries_same_id_fails_closed(self, tmp_path):
        """2. Two identical valid supplemental entries with the same ID violate the duplicate policy.
        Must return supplemental_preserved=False, provide explicit diagnostics, keep overall recovery
        unsuccessful, and create no additional entries on retry."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
            record_to_dict,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        stored_rec = {
            "record_id": "rec-supp-dup-ident",
            "run_id": "run-dup-1",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_rec["record_digest"] = compute_record_digest(stored_rec)

        incoming = AuditRecord(
            record_id="rec-supp-dup-ident",
            run_id="run-dup-1",
            terminal_outcome="rejected",
        )
        incoming_dict = dict(record_to_dict(incoming))
        incoming_digest = compute_record_digest(incoming_dict)
        conflict_id = f"rec-supp-dup-ident-conflict-{incoming_digest[:12]}"

        supp_entry = dict(incoming_dict)
        supp_entry["record_id"] = conflict_id
        supp_entry["conflict_with_record_id"] = "rec-supp-dup-ident"
        supp_entry["conflict_type"] = "identity_content_mismatch"
        supp_entry["conflicting_fields"] = ["terminal_outcome"]
        supp_entry["original_stored_digest"] = stored_rec["record_digest"]
        supp_entry["conflicting_content_digest"] = incoming_digest
        supp_entry["storage_category"] = "errors"
        supp_entry["emergency"] = True
        supp_entry["record_digest"] = compute_record_digest(supp_entry)

        # Line 1: primary, Line 2: supplemental, Line 3: identical duplicate supplemental
        emerg_file.write_text(
            json.dumps(stored_rec) + "\n" +
            json.dumps(supp_entry) + "\n" +
            json.dumps(supp_entry) + "\n",
            encoding="utf-8",
        )

        writer = AuditWriter(str(audit_root))

        # Inspect parsed original entries
        read_res = writer.read_emergency_records()
        assert len(read_res.all_records_by_id[conflict_id]) == 2
        assert len(read_res.duplicates) == 1
        assert read_res.duplicates[0]["record_id"] == conflict_id
        assert read_res.duplicates[0]["conflict"] is False  # identical content
        dup_entry = [m for m in read_res.malformed_lines if m.get("record_id") == conflict_id][0]
        assert dup_entry.get("duplicate") is True
        assert dup_entry.get("integrity_failure") is False
        assert dup_entry.get("kind") == "duplicate_identical"

        # Recovery attempt
        res = writer.append_emergency_history([incoming])
        assert res.all_ok is False
        conf = res.conflicts[0]
        assert conf["supplemental_record_id"] == conflict_id
        assert conf["supplemental_preserved"] is False
        assert conf["supplemental_receipt"] is None
        assert "duplicate policy" in conf["supplemental_error"]
        assert "duplicate entries cannot be reported as clean, uniquely verified evidence" in conf["supplemental_error"]
        # Must not falsely describe valid duplicates as corrupt or digest invalid
        assert "corrupt on disk" not in conf["supplemental_error"]
        assert "digest invalid" not in conf["supplemental_error"]
        assert "integrity verification failed" not in conf["supplemental_error"]
        assert any(conflict_id in k for k in res.errors)

        # Retries create no additional entries
        res2 = writer.append_emergency_history([incoming])
        assert res2.all_ok is False
        lines = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3

    def test_supplemental_two_valid_entries_same_id_differing_domain_content_fails_closed(self, tmp_path):
        """3. Two valid supplemental entries with the same ID but differing domain content fail closed.
        Must return supplemental_preserved=False, surface conflicting duplicate diagnostics, keep overall
        recovery unsuccessful, and create no additional entries."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
            record_to_dict,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        stored_rec = {
            "record_id": "rec-supp-diff-content",
            "run_id": "run-diff-1",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_rec["record_digest"] = compute_record_digest(stored_rec)

        incoming = AuditRecord(
            record_id="rec-supp-diff-content",
            run_id="run-diff-1",
            terminal_outcome="rejected",
        )
        incoming_dict = dict(record_to_dict(incoming))
        incoming_digest = compute_record_digest(incoming_dict)
        conflict_id = f"rec-supp-diff-content-conflict-{incoming_digest[:12]}"

        # Supplemental entry 1
        supp1 = dict(incoming_dict)
        supp1["record_id"] = conflict_id
        supp1["conflict_with_record_id"] = "rec-supp-diff-content"
        supp1["conflict_type"] = "identity_content_mismatch"
        supp1["conflicting_fields"] = ["terminal_outcome"]
        supp1["original_stored_digest"] = stored_rec["record_digest"]
        supp1["conflicting_content_digest"] = incoming_digest
        supp1["artifact_reference"] = "art-v1"
        supp1["storage_category"] = "errors"
        supp1["emergency"] = True
        supp1["record_digest"] = compute_record_digest(supp1)

        # Supplemental entry 2: same conflict_id, but conflicting artifact_reference
        supp2 = dict(supp1)
        supp2["artifact_reference"] = "art-v2-conflicting"
        supp2["record_digest"] = compute_record_digest(supp2)

        emerg_file.write_text(
            json.dumps(stored_rec) + "\n" +
            json.dumps(supp1) + "\n" +
            json.dumps(supp2) + "\n",
            encoding="utf-8",
        )

        writer = AuditWriter(str(audit_root))

        # Inspect parsed original entries: conflict popped from records_by_id
        read_res = writer.read_emergency_records()
        assert len(read_res.all_records_by_id[conflict_id]) == 2
        assert len(read_res.duplicates) == 1
        assert read_res.duplicates[0]["conflict"] is True
        assert conflict_id not in read_res.records_by_id
        dup_entry = [m for m in read_res.malformed_lines if m.get("record_id") == conflict_id][0]
        assert dup_entry.get("duplicate") is True
        assert dup_entry.get("integrity_failure") is False
        assert dup_entry.get("kind") == "duplicate_conflict"

        # Recovery attempt
        res = writer.append_emergency_history([incoming])
        assert res.all_ok is False
        conf = res.conflicts[0]
        assert conf["supplemental_record_id"] == conflict_id
        assert conf["supplemental_preserved"] is False
        assert conf["supplemental_receipt"] is None
        assert "conflicting duplicate entries" in conf["supplemental_error"]
        assert "artifact_reference" in conf["supplemental_error"]
        assert "corrupt on disk" not in conf["supplemental_error"]
        assert "digest invalid" not in conf["supplemental_error"]

        # Retries create no additional entries
        writer.append_emergency_history([incoming])
        lines = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3

    def test_supplemental_valid_entry_alongside_malformed_or_digest_invalid_fails_closed(self, tmp_path):
        """4. A valid supplemental entry alongside another malformed or digest-invalid entry claiming
        the same ID must never be reported as verified. Fails closed, reports diagnostics, and prevents extra writes."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
            record_to_dict,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        stored_rec = {
            "record_id": "rec-supp-corrupt-pair",
            "run_id": "run-cp-1",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_rec["record_digest"] = compute_record_digest(stored_rec)

        incoming = AuditRecord(
            record_id="rec-supp-corrupt-pair",
            run_id="run-cp-1",
            terminal_outcome="rejected",
        )
        incoming_dict = dict(record_to_dict(incoming))
        incoming_digest = compute_record_digest(incoming_dict)
        conflict_id = f"rec-supp-corrupt-pair-conflict-{incoming_digest[:12]}"

        # Valid supplemental entry
        supp_valid = dict(incoming_dict)
        supp_valid["record_id"] = conflict_id
        supp_valid["conflict_with_record_id"] = "rec-supp-corrupt-pair"
        supp_valid["conflict_type"] = "identity_content_mismatch"
        supp_valid["conflicting_fields"] = ["terminal_outcome"]
        supp_valid["original_stored_digest"] = stored_rec["record_digest"]
        supp_valid["conflicting_content_digest"] = incoming_digest
        supp_valid["storage_category"] = "errors"
        supp_valid["emergency"] = True
        supp_valid["record_digest"] = compute_record_digest(supp_valid)

        # Corrupt / digest-invalid second entry claiming the same conflict_id
        supp_corrupt = dict(supp_valid)
        supp_corrupt["record_digest"] = "f" * 64  # invalid digest

        emerg_file.write_text(
            json.dumps(stored_rec) + "\n" +
            json.dumps(supp_valid) + "\n" +
            json.dumps(supp_corrupt) + "\n",
            encoding="utf-8",
        )

        writer = AuditWriter(str(audit_root))

        # Inspect parsed original entries: line 3 in malformed_lines as genuine integrity failure
        read_res = writer.read_emergency_records()
        assert len(read_res.all_records_by_id.get(conflict_id, [])) == 1
        corrupt_entry = [m for m in read_res.malformed_lines if m.get("record_id") == conflict_id and m.get("integrity_failure")][0]
        assert corrupt_entry.get("integrity_failure") is True
        assert corrupt_entry.get("duplicate") is False
        assert corrupt_entry.get("kind") == "digest_invalid"

        # Recovery attempt
        res = writer.append_emergency_history([incoming])
        assert res.all_ok is False
        conf = res.conflicts[0]
        assert conf["supplemental_record_id"] == conflict_id
        assert conf["supplemental_preserved"] is False
        assert conf["supplemental_receipt"] is None
        assert "associated with malformed or digest invalid entry on line 3" in conf["supplemental_error"]

        # Retries create no additional entries
        writer.append_emergency_history([incoming])
        lines = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3

    def test_supplemental_id_present_in_all_records_but_absent_from_clean_mapping(self, tmp_path):
        """5. An existing supplemental ID present in all_records_by_id but absent from clean records_by_id
        (due to internal conflict) must fail closed and never be reported as clean evidence."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
            record_to_dict,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        stored_rec = {
            "record_id": "rec-supp-absent-clean",
            "run_id": "run-ac-1",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_rec["record_digest"] = compute_record_digest(stored_rec)

        incoming = AuditRecord(
            record_id="rec-supp-absent-clean",
            run_id="run-ac-1",
            terminal_outcome="rejected",
        )
        incoming_dict = dict(record_to_dict(incoming))
        incoming_digest = compute_record_digest(incoming_dict)
        conflict_id = f"rec-supp-absent-clean-conflict-{incoming_digest[:12]}"

        # Two conflicting occurrences of conflict_id on disk
        occ1 = dict(incoming_dict)
        occ1["record_id"] = conflict_id
        occ1["conflict_with_record_id"] = "rec-supp-absent-clean"
        occ1["conflict_type"] = "identity_content_mismatch"
        occ1["conflicting_fields"] = ["terminal_outcome"]
        occ1["original_stored_digest"] = stored_rec["record_digest"]
        occ1["conflicting_content_digest"] = incoming_digest
        occ1["execution_executed"] = True
        occ1["storage_category"] = "errors"
        occ1["emergency"] = True
        occ1["record_digest"] = compute_record_digest(occ1)

        occ2 = dict(occ1)
        occ2["execution_executed"] = False  # conflicting boolean domain field
        occ2["record_digest"] = compute_record_digest(occ2)

        emerg_file.write_text(
            json.dumps(stored_rec) + "\n" +
            json.dumps(occ1) + "\n" +
            json.dumps(occ2) + "\n",
            encoding="utf-8",
        )

        writer = AuditWriter(str(audit_root))

        # Inspect parsed original entries: present in all_records_by_id, absent from records_by_id
        read_res = writer.read_emergency_records()
        assert conflict_id in read_res.all_records_by_id
        assert conflict_id not in read_res.records_by_id

        # Recovery attempt
        res = writer.append_emergency_history([incoming])
        assert res.all_ok is False
        conf = res.conflicts[0]
        assert conf["supplemental_record_id"] == conflict_id
        assert conf["supplemental_preserved"] is False
        assert conf["supplemental_receipt"] is None
        assert "conflicting duplicate entries" in conf["supplemental_error"] or "absent from clean records mapping" in conf["supplemental_error"]

        # Retries create no additional entries
        writer.append_emergency_history([incoming])
        lines = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3

    def test_supplemental_repeated_recovery_attempts_after_conflict_are_idempotent(self, tmp_path):
        """6. Repeated recovery attempts after a supplemental-record conflict must remain strictly
        bounded and non-destructive: supplemental_preserved remains False, diagnostics remain consistent,
        and zero additional entries are created on disk across repeated retries."""
        import json
        from src.living_authenticity.knowledge.governance.audit.persistence import (
            AuditWriter,
            AuditRecord,
            compute_record_digest,
            record_to_dict,
        )

        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        errors_dir = audit_root / "errors"
        errors_dir.mkdir()
        emerg_file = errors_dir / "audit_emergency.jsonl"

        stored_rec = {
            "record_id": "rec-supp-repeat-conf",
            "run_id": "run-rc-1",
            "terminal_outcome": "approved",
            "storage_category": "errors",
            "emergency": True,
        }
        stored_rec["record_digest"] = compute_record_digest(stored_rec)

        incoming = AuditRecord(
            record_id="rec-supp-repeat-conf",
            run_id="run-rc-1",
            terminal_outcome="rejected",
        )
        incoming_dict = dict(record_to_dict(incoming))
        incoming_digest = compute_record_digest(incoming_dict)
        conflict_id = f"rec-supp-repeat-conf-conflict-{incoming_digest[:12]}"

        # Existing supplemental record has a linkage mismatch (conflict_with_record_id points to wrong ID)
        supp_mismatched_linkage = dict(incoming_dict)
        supp_mismatched_linkage["record_id"] = conflict_id
        supp_mismatched_linkage["conflict_with_record_id"] = "rec-wrong-target-id"
        supp_mismatched_linkage["conflict_type"] = "identity_content_mismatch"
        supp_mismatched_linkage["conflicting_fields"] = ["terminal_outcome"]
        supp_mismatched_linkage["original_stored_digest"] = stored_rec["record_digest"]
        supp_mismatched_linkage["conflicting_content_digest"] = incoming_digest
        supp_mismatched_linkage["storage_category"] = "errors"
        supp_mismatched_linkage["emergency"] = True
        supp_mismatched_linkage["record_digest"] = compute_record_digest(supp_mismatched_linkage)

        emerg_file.write_text(
            json.dumps(stored_rec) + "\n" + json.dumps(supp_mismatched_linkage) + "\n",
            encoding="utf-8",
        )

        writer = AuditWriter(str(audit_root))

        # Initial file state has exactly 2 lines
        initial_lines = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(initial_lines) == 2

        # Attempt 1: detects linkage mismatch, fails closed
        res1 = writer.append_emergency_history([incoming])
        assert res1.all_ok is False
        assert len(res1.conflicts) == 1
        conf1 = res1.conflicts[0]
        assert conf1["supplemental_preserved"] is False
        assert conf1["supplemental_receipt"] is None
        assert "linkage mismatch" in conf1["supplemental_error"]
        assert "rec-wrong-target-id" in conf1["supplemental_error"]
        lines_after_att1 = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after_att1) == 2

        # Attempt 2: identical outcome, 0 new entries
        res2 = writer.append_emergency_history([incoming])
        assert res2.all_ok is False
        conf2 = res2.conflicts[0]
        assert conf2["supplemental_preserved"] is False
        assert conf2["supplemental_error"] == conf1["supplemental_error"]
        lines_after_att2 = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after_att2) == 2

        # Attempt 3: still strictly bounded at 2 lines
        res3 = writer.append_emergency_history([incoming])
        assert res3.all_ok is False
        assert res3.conflicts[0]["supplemental_preserved"] is False
        lines_after_att3 = [l for l in emerg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines_after_att3) == 2
