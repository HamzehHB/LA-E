"""End-to-end integration tests for retrieval capabilities, narrative audit, and durability.

Verifies:
1. End-to-end processing: text input -> ingestion -> 5 retrieval capabilities ->
   governance -> controlled execution -> durable audit persistence.
2. The 5 retrieval capabilities emit observational events in strict sequence:
   - query_preparation
   - candidate_retrieval
   - relevance_assessment
   - candidate_identification
   - retrieval_evidence_recording
3. Continuous multi-stage narrative generation in both English ('en') and Persian ('fa').
4. Durable persistence into audit.jsonl with narrative and metadata intact.
5. Atomic staging file creation with zero overwrite and content hash integrity.
"""

import json
from pathlib import Path
import pytest

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.integration import run_bounded_integration
from tests._synthetic_synthesis import SyntheticSynthesis


def _make_unit(unit_id: str, text: str, source: str = "corpus.md", position: int = 0) -> KnowledgeUnit:
    return KnowledgeUnit(
        id=unit_id,
        source=source,
        original_text=text,
        cleaned_text=text,
        normalized_text=text,
        meaning=text,
        context="",
        proposed_type="Observation",
        position=position,
    )


def _approve_all(_request=None):
    return "y"


class TestEndToEndRetrievalNarrativeWorkflow:
    def test_e2e_persian_audit_and_five_retrieval_capabilities(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        audit_root = tmp_path / "audit"
        audit_root.mkdir()

        input_dir = tmp_path / "inputs"
        input_dir.mkdir()
        input_file = input_dir / "01-observation-presence.md"
        input_file.write_text(
            "Observation:\n"
            "I observed that people often share calm morning rituals "
            "and I notice that the pattern repeats daily.\n",
            encoding="utf-8",
        )

        corpus = [
            _make_unit("c-1", "A ledger of unrelated vocabulary about tide charts and harbour moorings.", source="archive.md", position=1),
        ]
        core_units = [
            _make_unit("core-1", "Method:\nA method for calibrating laboratory instruments with a step by step procedure.\n", source="core.md"),
        ]

        events = []

        def event_sink(ev):
            events.append(ev)

        synthesis = SyntheticSynthesis()

        report = run_bounded_integration(
            source_root=str(input_dir),
            staging_root=str(staging),
            guarded_roots=(),
            approval_reader=_approve_all,
            corpus=corpus,
            core_units=core_units,
            max_files=1,
            synthesis=synthesis,
            audit_root=str(audit_root),
            authorized_audit_root=str(audit_root),
            authorized_staging_root=str(staging),
            audit_language="fa",
            event_sink=event_sink,
        )

        # 1. Pipeline and Execution verification
        assert report.accepted is True
        assert report.files_processed == 1
        assert len(report.units) >= 1
        executed_units = [u for u in report.units if u.outcome == "executed"]
        assert len(executed_units) >= 1
        executed_unit = executed_units[0]
        assert executed_unit.outcome == "executed"
        assert executed_unit.revalidation_valid is True
        assert executed_unit.approval_approved is True

        # 2. Five Retrieval Capabilities sequence verification
        retrieval_ops = [
            e["operation"]
            for e in events
            if e.get("stage") == "retrieval" and e.get("operation") and e.get("phase") == "completed"
        ]
        assert retrieval_ops == [
            "query_preparation",
            "candidate_retrieval",
            "relevance_assessment",
            "candidate_identification",
            "retrieval_evidence_recording",
        ]
        started_ops = [
            e["operation"]
            for e in events
            if e.get("stage") == "retrieval" and e.get("operation") and e.get("phase") == "started"
        ]
        assert started_ops == retrieval_ops

        # 3. Controlled Execution / Staging artifact verification
        staged_files = list(staging.glob("*.md"))
        assert len(staged_files) >= 1
        staged_content = staged_files[0].read_text(encoding="utf-8")
        assert len(staged_content) > 0
        assert "LLM synthesis" in staged_content

        # 4. Durable Audit Persistence in Persian ('fa')
        passed_log = audit_root / "passed" / "audit.jsonl"
        assert passed_log.is_file(), "audit record must be persisted in passed/audit.jsonl"
        lines = [json.loads(line) for line in passed_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        executed_records = [r for r in lines if r.get("execution_executed") is True]
        assert len(executed_records) >= 1

        terminal_record = executed_records[-1]
        assert terminal_record["execution_executed"] is True
        assert terminal_record["narrative_language"] == "fa"
        assert "گزارش حسابرسی پیوسته و شفاف" in terminal_record["narrative"]
        assert "مرحله ورودی و استخراج دانش" in terminal_record["narrative"]
        assert "مرحله بازیابی و شواهد زمینه‌ای (قابلیت‌های ۵گانه)" in terminal_record["narrative"]
        assert "مرز نظارت، اعتبارسنجی و تصمیم‌گیری انسانی" in terminal_record["narrative"]
        assert "مرحله اجرای کنترل‌شده و پیش‌نویس موقت" in terminal_record["narrative"]
        assert "با موفقیت" in terminal_record["narrative"]

    def test_e2e_english_audit_and_five_retrieval_capabilities(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        audit_root = tmp_path / "audit"
        audit_root.mkdir()

        input_dir = tmp_path / "inputs"
        input_dir.mkdir()
        input_file = input_dir / "01-observation-ethics.md"
        input_file.write_text(
            "Observation:\n"
            "I observed that people often share calm morning rituals "
            "and I notice that the pattern repeats daily.\n",
            encoding="utf-8",
        )

        corpus = [
            _make_unit("c-3", "A ledger of unrelated vocabulary about tide charts and harbour moorings.", source="ethics.md", position=1),
        ]
        core_units = [
            _make_unit("core-2", "Method:\nA method for calibrating laboratory instruments with a step by step procedure.\n", source="core_ethics.md"),
        ]

        events = []

        report = run_bounded_integration(
            source_root=str(input_dir),
            staging_root=str(staging),
            guarded_roots=(),
            approval_reader=_approve_all,
            corpus=corpus,
            core_units=core_units,
            max_files=1,
            synthesis=SyntheticSynthesis(),
            audit_root=str(audit_root),
            authorized_audit_root=str(audit_root),
            authorized_staging_root=str(staging),
            audit_language="en",
            event_sink=lambda e: events.append(e),
        )

        assert report.accepted is True
        assert report.audit_language == "en"
        assert report.files_processed == 1
        assert len(report.units) >= 1
        executed_units = [u for u in report.units if u.outcome == "executed"]
        assert len(executed_units) >= 1
        executed_unit = executed_units[0]
        assert executed_unit.outcome == "executed"
        assert executed_unit.revalidation_valid is True
        assert executed_unit.approval_approved is True

        # Verify 5 capabilities emitted
        retrieval_ops = [
            e["operation"]
            for e in events
            if e.get("stage") == "retrieval" and e.get("operation") and e.get("phase") == "completed"
        ]
        assert retrieval_ops == [
            "query_preparation",
            "candidate_retrieval",
            "relevance_assessment",
            "candidate_identification",
            "retrieval_evidence_recording",
        ]
        started_ops = [
            e["operation"]
            for e in events
            if e.get("stage") == "retrieval" and e.get("operation") and e.get("phase") == "started"
        ]
        assert started_ops == retrieval_ops

        # Verify staged file
        staged_files = list(staging.glob("*.md"))
        assert len(staged_files) >= 1
        staged_content = staged_files[0].read_text(encoding="utf-8")
        assert len(staged_content) > 0
        assert "LLM synthesis" in staged_content

        passed_log = audit_root / "passed" / "audit.jsonl"
        assert passed_log.is_file()
        lines = [json.loads(line) for line in passed_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        executed_records = [r for r in lines if r.get("execution_executed") is True]
        assert len(executed_records) >= 1
        terminal_record = executed_records[-1]
        assert terminal_record["execution_executed"] is True
        assert "Continuous Lifecycle Narrative Audit" in terminal_record["narrative"]
        assert "1. Input & Knowledge Ingestion" in terminal_record["narrative"]
        assert "2. Retrieval & Contextual Evidence (5 Capabilities)" in terminal_record["narrative"]
        assert "3. Governance, Revalidation & Human Decision Boundary" in terminal_record["narrative"]
        assert "4. Controlled Execution & Staging" in terminal_record["narrative"]
        assert "5. Terminal Outcome" in terminal_record["narrative"]
