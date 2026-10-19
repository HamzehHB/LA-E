"""Tests for live Ingestion process event reporting, timeline consistency, and audit continuity."""
import json
from pathlib import Path

import pytest

from src.living_authenticity.adapter import Launcher
from src.living_authenticity.knowledge.ingestion.pipeline import IngestionPipeline


def test_live_run_emits_granular_ingestion_events(tmp_path):
    audit_root = tmp_path / "audit"
    staging_root = tmp_path / "staging"
    audit_root.mkdir()
    staging_root.mkdir()

    launcher = Launcher(
        paths={
            "audit": {"root": str(audit_root), "passed": str(audit_root / "passed"), "failed": str(audit_root / "failed")},
            "staging": {"root": str(staging_root)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )

    valid_text = (
        "Observation:\n"
        "Researchers working under cognitive compression require reliable traceability "
        "and clear verification milestones across all stages of data ingestion and governance.\n\n"
        "Relations:\n"
        "[[Traceability]]\n"
        "[[Autonomy]]\n"
    )

    run_id = "run-ingestion-report-test"
    launcher._run_thread(
        run_id=run_id,
        input_text=valid_text,
        mode_value="examples",
    )

    events = launcher.events.since(0)
    ingestion_events = [e for e in events if e.get("stage") == "ingestion"]

    assert len(ingestion_events) >= 8

    # First and last ingestion events are lifecycle boundary milestones
    assert ingestion_events[0]["kind"] == "stage_started"
    assert ingestion_events[-1]["kind"] == "stage_completed"

    # Middle events are granular, structured operational status events
    status_events = [e for e in ingestion_events if e["kind"] == "status"]
    operations = [e.get("operation") for e in status_events]

    expected_ops = [
        "reader_selection",
        "reading",
        "cleaning",
        "normalization",
        "parsing",
        "chunking",
        "metadata_extraction",
        "extraction",
    ]
    assert operations == expected_ops
    assert operations.index("parsing") < operations.index("chunking")

    # Verify that all events have run_id stamped
    for ev in ingestion_events:
        assert ev.get("run_id") == run_id

    # Verify real counts and descriptions
    reader_ev = next(e for e in status_events if e.get("operation") == "reader_selection")
    assert "reader" in reader_ev
    assert reader_ev["reader"] == "ChatInputReader"

    reading_ev = next(e for e in status_events if e.get("operation") == "reading")
    assert reading_ev["raw_length"] == len(valid_text)

    chunking_ev = next(e for e in status_events if e.get("operation") == "chunking")
    assert chunking_ev["chunk_count"] >= 1

    parsing_ev = next(e for e in status_events if e.get("operation") == "parsing")
    assert parsing_ev["note_type"] == "Observation"

    extraction_ev = next(e for e in status_events if e.get("operation") == "extraction")
    assert extraction_ev["unit_count"] >= 1
    assert len(extraction_ev["unit_ids"]) >= 1


def test_unsupported_input_format_does_not_emit_ingestion_completion(tmp_path):
    launcher = Launcher(
        llm_config={"llm": {"provider": "disabled"}},
    )

    # Calling with unsupported source type fails at input boundary
    with pytest.raises(Exception):
        launcher.start_run(
            "Some valid text meeting minimum length boundaries for test.",
            source_type="file_upload",  # Unsupported source type
        )

    events = launcher.events.since(0)
    assert not any(e.get("stage") == "ingestion" for e in events)


def test_multi_unit_input_preserves_independent_provenance(tmp_path):
    audit_root = tmp_path / "audit"
    staging_root = tmp_path / "staging"
    audit_root.mkdir()
    staging_root.mkdir()

    launcher = Launcher(
        paths={
            "audit": {"root": str(audit_root)},
            "staging": {"root": str(staging_root)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )

    multi_unit_text = (
        "Observation:\n"
        "First distinct knowledge observation regarding cognitive continuity and empirical observation in research.\n\n"
        "Concept:\n"
        "Second independent knowledge concept describing structural stability in knowledge governance systems.\n"
    )

    run_id = "run-multi-unit-test"
    launcher._run_thread(
        run_id=run_id,
        input_text=multi_unit_text,
        mode_value="examples",
    )

    events = launcher.events.since(0)
    extraction_ev = next(e for e in events if e.get("operation") == "extraction")
    assert extraction_ev["unit_count"] == 2
    assert len(extraction_ev["unit_ids"]) == 2
    assert extraction_ev["unit_ids"][0] != extraction_ev["unit_ids"][1]


def test_audit_records_capture_ingestion_provenance(tmp_path):
    audit_root = tmp_path / "audit"
    staging_root = tmp_path / "staging"
    audit_root.mkdir()
    staging_root.mkdir()

    launcher = Launcher(
        paths={
            "audit": {"root": str(audit_root)},
            "staging": {"root": str(staging_root)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )

    valid_text = (
        "Observation:\n"
        "Verifying that audit traceability connects query units to their original chat input source correctly.\n\n"
        "Relations:\n"
        "[[Traceability]]\n"
    )

    run_id = "run-audit-continuity-test"
    launcher._run_thread(
        run_id=run_id,
        input_text=valid_text,
        mode_value="examples",
    )

    failed_log = audit_root / "failed" / "audit.jsonl"
    passed_log = audit_root / "passed" / "audit.jsonl"
    errors_log = audit_root / "errors" / "audit.jsonl"
    candidates = (errors_log, failed_log, passed_log)
    log_path = next((c for c in candidates if c.is_file()), None)
    assert log_path is not None, "At least one audit log candidate must exist"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    matching = [r for r in records if r.get("run_id") == run_id]
    assert len(matching) >= 1
    record = matching[0]

    import hashlib
    assert record["query_source"].startswith("chat_input:inp-")
    assert record["query_unit_id"].startswith("ku-")
    expected_hash = hashlib.sha256(valid_text.encode("utf-8")).hexdigest()
    assert record["input_hash"] == expected_hash
    assert record["input_hash_source"] == "input_provenance"
    assert record["query_source"] == f"chat_input:inp-{expected_hash[:12]}"
    assert any("query:ku-" in p for p in record.get("provenance", []))


def test_audit_trace_distinguishes_unit_fallback_hash():
    import hashlib
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.integration.unit_loop import _trace_for_audit

    unit_with_prov = KnowledgeUnit(
        id="ku-withprov",
        source="chat_input:inp-12345",
        position=0,
        original_text="Some text",
        cleaned_text="Some text",
        meaning="Some text",
        input_id="inp-12345",
        input_hash="hash-full-input-sha256",
    )
    trace_prov = _trace_for_audit(query=unit_with_prov)
    assert trace_prov["input_hash"] == "hash-full-input-sha256"
    assert trace_prov["input_hash_source"] == "input_provenance"

    unit_without_prov = KnowledgeUnit(
        id="ku-noprov",
        source="test.txt",
        position=0,
        original_text="Standalone text without provenance",
        cleaned_text="Standalone text without provenance",
        meaning="Standalone text without provenance",
    )
    trace_fallback = _trace_for_audit(query=unit_without_prov)
    expected_unit_hash = hashlib.sha256("Standalone text without provenance".encode("utf-8")).hexdigest()
    assert trace_fallback["input_hash"] == expected_unit_hash
    assert len(trace_fallback["input_hash"]) == 64
    assert trace_fallback["input_hash_source"] == "unit_content_fallback"
