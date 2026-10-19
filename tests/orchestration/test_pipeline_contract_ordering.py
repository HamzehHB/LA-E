"""Focused tests for the authoritative pipeline contract and stage ordering.

Verifies:
1. Ingestion sequence:
   Reader Selection -> Reading -> Cleaning -> Normalization -> Parsing -> Chunking -> Metadata Extraction -> Knowledge Unit Extraction & Provenance Recording.
2. Downstream sequence:
   Retrieval -> Comparison -> Relation Analysis -> Core Analysis -> Classification -> Proposal Generation -> Confidence -> Knowledge Filter -> Mandatory LLM Synthesis -> Human Review -> Explicit Approval -> Revalidation -> Controlled Execution -> Audit.
3. Retrieval precedes Classification; Classification immediately precedes Proposal Generation.
4. Chunking operates on cleaned, normalized, parsed content (never raw text).
5. Pre-execution audit persistence failure halts safely before ControlledExecutor.
"""
from pathlib import Path
from unittest.mock import MagicMock

from src.living_authenticity.knowledge.ingestion.pipeline import IngestionPipeline
from src.living_authenticity.knowledge.ingestion.readers.reader_registry import ReaderRegistry
from src.living_authenticity.knowledge.ingestion.readers.chat_input_reader import ChatInputReader
from src.living_authenticity.knowledge.ingestion.chunking.chunker_registry import ChunkerRegistry
from src.living_authenticity.knowledge.ingestion.chunking.markdown_chunker import MarkdownChunker
from src.living_authenticity.knowledge.ingestion.extraction.extractor_registry import ExtractorRegistry
from src.living_authenticity.knowledge.ingestion.extraction.markdown_extractor import MarkdownKnowledgeExtractor
from src.living_authenticity.knowledge.ingestion.cleaning.cleaner import Cleaner
from src.living_authenticity.knowledge.ingestion.cleaning.normalizer import Normalizer
from src.living_authenticity.knowledge.ingestion.metadata.extractor import MetadataExtractor
from src.living_authenticity.knowledge.ingestion.parser.obsidian_parser import ObsidianParser
from src.living_authenticity.knowledge.orchestration import STAGE_ORDER, EvidenceFirstPipeline
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.input.processor import process_input
from src.living_authenticity.knowledge.integration.unit_loop import _govern_candidate
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment
from src.living_authenticity.knowledge.output.outcome import GeneratedNote
from src.living_authenticity.knowledge.governance.approval.outcome import ApprovalRequest, ApprovalOutcome


def _pipeline(stage_observer=None) -> IngestionPipeline:
    return IngestionPipeline(
        reader_registry=ReaderRegistry(),
        chunker_registry=ChunkerRegistry(),
        extractor_registry=ExtractorRegistry(),
        cleaner=Cleaner(),
        normalizer=Normalizer(),
        parser=ObsidianParser(),
        metadata_extractor=MetadataExtractor(),
        stage_observer=stage_observer,
    )


def test_ingestion_exact_substage_ordering():
    events = []
    pipe = _pipeline(stage_observer=lambda ev: events.append(ev))
    text = (
        "Observation:\n"
        "Testing that parsing strictly precedes chunking in the ingestion order.\n\n"
        "Relations:\n"
        "[[IngestionOrder]]\n"
    )
    eval_res = process_input(text, source_type="chat_input", run_id="run-order-test")
    assert eval_res.valid
    assert eval_res.provenance is not None

    result = pipe.ingest_text(text, kind="chat_input", input_provenance=eval_res.provenance)
    assert result.parsed is not None
    assert result.parsed.note_type == "Observation"
    assert len(result.chunks) >= 1

    ops = [e.get("operation") for e in events if e.get("stage") == "ingestion"]
    expected = [
        "reader_selection",
        "reading",
        "cleaning",
        "normalization",
        "parsing",
        "chunking",
        "metadata_extraction",
        "extraction",
    ]
    assert ops == expected
    # Explicit contract assertions
    assert ops.index("cleaning") < ops.index("normalization")
    assert ops.index("normalization") < ops.index("parsing")
    assert ops.index("parsing") < ops.index("chunking")
    assert ops.index("chunking") < ops.index("metadata_extraction")
    assert ops.index("metadata_extraction") < ops.index("extraction")


def test_chunking_operates_on_processed_and_parsed_content():
    captured_inputs = []

    class CapturingChunker:
        def split(self, text, parsed=None):
            captured_inputs.append({"text": text, "parsed": parsed})
            return ["chunk-1"]

    reader_reg = ReaderRegistry()
    reader_reg.register("custom_test", ChatInputReader())

    chunker_reg = ChunkerRegistry()
    chunker_reg.register("custom_test", CapturingChunker())

    extractor_reg = ExtractorRegistry()
    extractor_reg.register("custom_test", MarkdownKnowledgeExtractor())

    pipe = IngestionPipeline(
        reader_registry=reader_reg,
        chunker_registry=chunker_reg,
        extractor_registry=extractor_reg,
        cleaner=Cleaner(),
        normalizer=Normalizer(),
        parser=ObsidianParser(),
        metadata_extractor=MetadataExtractor(),
    )

    dirty_text = "Observation:   \r\nDirty input    with CRLF and irregular spaces.\r\n"
    pipe.ingest_text(dirty_text, kind="custom_test")

    assert len(captured_inputs) == 1
    chunk_call = captured_inputs[0]

    # Verify chunking received normalized text, NOT raw uncleaned text
    assert "\r" not in chunk_call["text"]
    assert "Dirty input    with" not in chunk_call["text"]
    assert "Dirty input with" in chunk_call["text"]

    # Verify chunking has access to the parsed output
    assert chunk_call["parsed"] is not None
    assert chunk_call["parsed"].note_type == "Observation"


def test_downstream_exact_stage_order():
    # Downstream sequence:
    # Retrieval -> Comparison -> Relation Analysis -> Core Analysis -> Classification -> Proposal -> Confidence -> Knowledge Filter
    assert STAGE_ORDER == (
        "ingestion",
        "retrieval",
        "comparison",
        "relation",
        "core",
        "classification",
        "proposal",
        "confidence",
        "knowledge_filter",
        "representation",
    )

    # Invariant 1: Retrieval MUST occur before Classification so retrieval is not biased by classification
    assert STAGE_ORDER.index("retrieval") < STAGE_ORDER.index("classification")

    # Invariant 2: Classification MUST immediately precede Proposal Generation
    assert STAGE_ORDER.index("classification") == STAGE_ORDER.index("proposal") - 1


def test_downstream_execution_trace_order():
    stage_events = []

    def observer(ev):
        if ev.get("kind") == "stage_started":
            stage_events.append(ev.get("stage"))

    pipe = EvidenceFirstPipeline(stage_observer=observer)
    unit = KnowledgeUnit(
        id="ku-downstream-test",
        source="inbox",
        original_text="Observation:\nA test note verifying downstream execution order.",
        meaning="Testing execution order",
        cleaned_text="Observation:\nA test note verifying downstream execution order.",
        normalized_text="Observation:\nA test note verifying downstream execution order.",
        context="",
        proposed_type="",
        position=0,
    )

    result = pipe.run_unit(unit, corpus=[], core_units=[])
    assert result.query_unit.id == "ku-downstream-test"

    expected_downstream = [
        "retrieval",
        "comparison",
        "relation",
        "core",
        "classification",
        "proposal",
        "confidence",
        "knowledge_filter",
        "representation",
    ]
    assert stage_events == expected_downstream
    assert stage_events.index("retrieval") < stage_events.index("classification")
    assert stage_events.index("classification") == stage_events.index("proposal") - 1


def test_audit_persistence_failure_safely_halts_before_execution(tmp_path):
    """If pre-execution audit persistence fails, ControlledExecutor MUST NOT run."""
    staged_dir = tmp_path / "staging"
    staged_dir.mkdir()

    query = KnowledgeUnit(
        id="ku-fail-audit",
        source="inbox",
        original_text="Test",
        meaning="Test",
    )
    proposal = Proposal(
        query_unit_id="ku-fail-audit",
        query_source="inbox",
        query_position=0,
        action="CREATE",
        title="Test Proposal",
        reason="Test",
        classification_type="Observation",
        destination="staging/test.md",
        evidence=(),
        provenance=("p1",),
    )
    confidence = ConfidenceAssessment(
        query_unit_id="ku-fail-audit",
        query_source="inbox",
        query_position=0,
        level="sufficiently_supported",
        basis="Test",
    )
    note = GeneratedNote(
        query_unit_id="ku-fail-audit",
        query_source="inbox",
        query_position=0,
        title="Test",
        body="Test note body",
        proposed_type="Observation",
        proposal_action="CREATE",
        confidence_level="HIGH",
        destination="staging/test.md",
        markdown="Test markdown",
    )

    # Gate that approves
    gate = MagicMock()
    req = ApprovalRequest(
        query_unit_id="ku-fail-audit",
        query_source="inbox",
        query_position=0,
        proposal_hash="hash123",
        proposal_action="CREATE",
        confidence_level="HIGH",
        destination="staging/test.md",
    )
    gate.request_approval.return_value = (
        req,
        "raw",
        ApprovalOutcome(
            query_unit_id="ku-fail-audit",
            proposal_hash="hash123",
            proposal_action="CREATE",
            approved=True,
            input_value="yes",
            reason="ok",
        ),
    )

    from src.living_authenticity.knowledge.governance.revalidation.outcome import RevalidationResult

    # Validator that says valid
    validator = MagicMock()
    validator.revalidate.return_value = RevalidationResult(valid=True, approval_bound=True)

    # Runner (ControlledExecutor) that should NEVER be called
    runner = MagicMock()

    # AuditWriter whose append fails
    failing_writer = MagicMock()
    failing_writer.append.side_effect = OSError("Disk write failed: permission denied")

    entries = []
    audits = []
    audit_receipts = []
    emitted = []

    _govern_candidate(
        child="test.md",
        query=query,
        proposal=proposal,
        confidence=confidence,
        note=note,
        gate=gate,
        validator=validator,
        runner=runner,
        pinned_staging=str(staged_dir),
        guarded=(),
        schema_version="1.0",
        approval_reader=lambda _: "yes",
        entries=entries,
        audits=audits,
        authorized_staging=str(staged_dir),
        run_id="run-audit-fail-test",
        audit_writer=failing_writer,
        audit_receipts=audit_receipts,
        event_sink=lambda ev: emitted.append(ev),
    )

    # ControlledExecutor must NOT have been called
    assert not runner.execute.called, "ControlledExecutor must NOT execute if pre-execution audit fails!"

    # Entry outcome must report audit_persistence_failed
    assert len(entries) == 1
    assert entries[0].outcome == "audit_persistence_failed"
    assert "audit persistence failed before execution" in entries[0].reason

    # Event stopped must be emitted
    stopped_ev = next((e for e in emitted if e.get("kind") == "stopped" and e.get("stage") == "audit_persistence"), None)
    assert stopped_ev is not None


def test_non_create_proposal_actions_coerce_to_needs_review_and_never_execute():
    """Verify that unsupported proposal actions coerce conservatively and cannot execute."""
    from src.living_authenticity.knowledge.decision.proposal.outcome import PROPOSAL_ACTIONS, Proposal
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.llm.schema import LLMResult, CandidateOutput

    # 1. Structural coercion on direct construction
    p_update = Proposal(query_unit_id="ku-1", query_source="test", query_position=0, action="UPDATE")
    assert p_update.action == "NEEDS_REVIEW"

    p_link = Proposal(query_unit_id="ku-1", query_source="test", query_position=0, action="LINK")
    assert p_link.action == "NEEDS_REVIEW"

    p_merge = Proposal(query_unit_id="ku-1", query_source="test", query_position=0, action="MERGE")
    assert p_merge.action == "NEEDS_REVIEW"

    p_archive = Proposal(query_unit_id="ku-1", query_source="test", query_position=0, action="ARCHIVE")
    assert p_archive.action == "NEEDS_REVIEW"

    # Only CREATE, DO_NOT_IMPORT, and NEEDS_REVIEW are recognized actions
    assert set(PROPOSAL_ACTIONS) == {"CREATE", "DO_NOT_IMPORT", "NEEDS_REVIEW"}

    # 2. Execution prevention in unit governance loop
    query = KnowledgeUnit(id="ku-non-create", source="test", original_text="Non create test", meaning="Test")
    for blocked_action in ("NEEDS_REVIEW", "DO_NOT_IMPORT"):
        blocked_proposal = Proposal(
            query_unit_id=query.id,
            query_source=query.source,
            query_position=0,
            action=blocked_action,
        )
        unit_result = MagicMock(
            query_unit=query,
            proposal=blocked_proposal,
            confidence=None,
            classification=None,
            retrieval_result=None,
            comparisons=(),
            relation_result=None,
            core_analysis=None,
            filter_outcome=None,
        )

        mock_gate = MagicMock()
        mock_validator = MagicMock()
        mock_runner = MagicMock()
        mock_synthesis = MagicMock()
        mock_synthesis.synthesize.return_value = (
            LLMResult(candidates=(CandidateOutput(title="Draft", body="Body"),)),
            "prompt",
            "prompt-hash",
        )

        entries = []
        audits = []

        _handle_unit(
            child="test.md",
            unit_result=unit_result,
            gate=mock_gate,
            validator=mock_validator,
            runner=mock_runner,
            pinned_staging="/tmp/staging",
            guarded=(),
            schema_version="1.0",
            approval_reader=lambda _: "yes",
            entries=entries,
            audits=audits,
            synthesis=mock_synthesis,
        )

        # Gate, validator, and runner must NEVER be called for non-CREATE actions
        assert not mock_gate.request_approval.called
        assert not mock_validator.revalidate.called
        assert not mock_runner.execute.called

        # Candidate is held as non-executable
        assert len(entries) == 1
        assert entries[0].outcome == "held_non_executable"
        assert entries[0].approval_approved is False
        assert entries[0].proposal_action == blocked_action


def test_chunker_internal_typeerror_is_not_masked_or_retried():
    """An internal TypeError inside chunker.split MUST propagate cleanly and NOT be retried."""
    class BuggyChunker:
        def split(self, text, parsed=None):
            # Internal bug inside chunker logic: raises TypeError
            raise TypeError("internal bug inside chunker logic")

    reader_reg = ReaderRegistry()
    reader_reg.register("buggy", ChatInputReader())

    chunker_reg = ChunkerRegistry()
    chunker_reg.register("buggy", BuggyChunker())

    pipe = IngestionPipeline(
        reader_registry=reader_reg,
        chunker_registry=chunker_reg,
        extractor_registry=ExtractorRegistry(),
        cleaner=Cleaner(),
        normalizer=Normalizer(),
        parser=ObsidianParser(),
        metadata_extractor=MetadataExtractor(),
    )

    import pytest
    with pytest.raises(TypeError, match="internal bug inside chunker logic"):
        pipe.ingest_text("Observation:\nTesting error propagation.\n", kind="buggy")


def test_chunker_without_parsed_parameter_works_cleanly():
    """A legacy or third-party chunker with split(text) signature works without error."""
    class LegacyChunker:
        def split(self, text):
            return [text.strip()]

    reader_reg = ReaderRegistry()
    reader_reg.register("legacy", ChatInputReader())

    chunker_reg = ChunkerRegistry()
    chunker_reg.register("legacy", LegacyChunker())

    extractor_reg = ExtractorRegistry()
    extractor_reg.register("legacy", MarkdownKnowledgeExtractor())

    pipe = IngestionPipeline(
        reader_registry=reader_reg,
        chunker_registry=chunker_reg,
        extractor_registry=extractor_reg,
        cleaner=Cleaner(),
        normalizer=Normalizer(),
        parser=ObsidianParser(),
        metadata_extractor=MetadataExtractor(),
    )

    result = pipe.ingest_text("Observation:\nLegacy chunker test.\n", kind="legacy")
    assert len(result.chunks) == 1
    assert "Legacy chunker test." in result.chunks[0]


def test_chunker_with_kwargs_receives_parsed_ast():
    """A chunker split method with **kwargs receives parsed AST cleanly."""
    captured = {}

    class KwargsChunker:
        def split(self, text, **kwargs):
            captured["kwargs"] = kwargs
            return [text.strip()]

    reader_reg = ReaderRegistry()
    reader_reg.register("kwargs_kind", ChatInputReader())

    chunker_reg = ChunkerRegistry()
    chunker_reg.register("kwargs_kind", KwargsChunker())

    extractor_reg = ExtractorRegistry()
    extractor_reg.register("kwargs_kind", MarkdownKnowledgeExtractor())

    pipe = IngestionPipeline(
        reader_registry=reader_reg,
        chunker_registry=chunker_reg,
        extractor_registry=extractor_reg,
        cleaner=Cleaner(),
        normalizer=Normalizer(),
        parser=ObsidianParser(),
        metadata_extractor=MetadataExtractor(),
    )

    result = pipe.ingest_text("Observation:\nKwargs chunker.\n", kind="kwargs_kind")
    assert len(result.chunks) == 1
    assert "parsed" in captured["kwargs"]
    assert captured["kwargs"]["parsed"] is not None


def test_chunker_with_uninspectable_split_falls_back_to_single_argument():
    """A chunker whose split method raises ValueError or TypeError during signature inspection defaults to split(text)."""
    class MockUninspectableChunker:
        def __init__(self):
            self.calls = []

        def split(self, text):
            self.calls.append(text)
            return [text]

    chunker_inst = MockUninspectableChunker()
    from unittest.mock import patch

    reader_reg = ReaderRegistry()
    reader_reg.register("uninspectable", ChatInputReader())

    chunker_reg = ChunkerRegistry()
    chunker_reg.register("uninspectable", chunker_inst)

    extractor_reg = ExtractorRegistry()
    extractor_reg.register("uninspectable", MarkdownKnowledgeExtractor())

    pipe = IngestionPipeline(
        reader_registry=reader_reg,
        chunker_registry=chunker_reg,
        extractor_registry=extractor_reg,
        cleaner=Cleaner(),
        normalizer=Normalizer(),
        parser=ObsidianParser(),
        metadata_extractor=MetadataExtractor(),
    )

    with patch("inspect.signature", side_effect=ValueError("no signature found")):
        result = pipe.ingest_text("Observation:\nUninspectable signature.\n", kind="uninspectable")
        assert len(result.chunks) == 1
        assert len(chunker_inst.calls) == 1


def test_extractor_error_propagation_does_not_swallow_failures():
    """A genuine ValueError raised during knowledge unit extraction propagates directly."""
    class FailingExtractor:
        def extract(self, **kwargs):
            raise ValueError("corrupted extraction logic inside extractor")

    reader_reg = ReaderRegistry()
    reader_reg.register("failing_ext", ChatInputReader())

    chunker_reg = ChunkerRegistry()
    chunker_reg.register("failing_ext", MarkdownChunker())

    extractor_reg = ExtractorRegistry()
    extractor_reg.register("failing_ext", FailingExtractor())

    pipe = IngestionPipeline(
        reader_registry=reader_reg,
        chunker_registry=chunker_reg,
        extractor_registry=extractor_reg,
        cleaner=Cleaner(),
        normalizer=Normalizer(),
        parser=ObsidianParser(),
        metadata_extractor=MetadataExtractor(),
    )

    import pytest
    with pytest.raises(ValueError, match="corrupted extraction logic inside extractor"):
        pipe.ingest_text("Observation:\nTest failure propagation.\n", kind="failing_ext")


def test_unregistered_extractor_fails_at_registry_boundary():
    """An unregistered extractor format fails explicitly with ValueError at registry lookup."""
    reader_reg = ReaderRegistry()
    reader_reg.register(".custom", ChatInputReader())

    chunker_reg = ChunkerRegistry()
    from src.living_authenticity.knowledge.ingestion.chunking.markdown_chunker import MarkdownChunker
    chunker_reg.register(".custom", MarkdownChunker())

    # ExtractorRegistry does NOT have .custom registered
    extractor_reg = ExtractorRegistry()

    pipe = IngestionPipeline(
        reader_registry=reader_reg,
        chunker_registry=chunker_reg,
        extractor_registry=extractor_reg,
        cleaner=Cleaner(),
        normalizer=Normalizer(),
        parser=ObsidianParser(),
        metadata_extractor=MetadataExtractor(),
    )

    import pytest
    with pytest.raises(ValueError, match="No knowledge unit extractor registered for '.custom'"):
        pipe.ingest_text("Observation:\nCustom unregistered.\n", kind=".custom")


def test_reader_selection_contracts_across_input_modes(tmp_path):
    """Verify reader selection identity and truthfulness across all supported and unsupported modes."""
    pipe = _pipeline()

    # 1. Normal chat input (explicit chat_input format)
    res_chat = pipe.ingest_text("Observation:\nChat text.\n", kind="chat_input")
    assert res_chat.metadata["reader"] == "ChatInputReader"

    # 2. In-memory text default
    res_default = pipe.ingest_text("Observation:\nDefault text.\n")
    assert res_default.metadata["reader"] == "ChatInputReader"

    # 3. Direct text with markdown format specified
    res_md_text = pipe.ingest_text("Observation:\nMD text.\n", kind=".md")
    assert res_md_text.metadata["reader"] == "MarkdownReader"

    # 4. Direct text with txt format specified
    res_txt_text = pipe.ingest_text("Observation:\nTXT text.\n", kind=".txt")
    assert res_txt_text.metadata["reader"] == "TXTReader"

    # 5. Markdown file input
    md_file = tmp_path / "test_note.md"
    md_file.write_text("Observation:\nFile MD.\n", encoding="utf-8")
    res_md_file = pipe.ingest(str(md_file))
    assert res_md_file.metadata["reader"] == "MarkdownReader"
    assert res_md_file.metadata["file_name"] == "test_note.md"

    # 6. TXT file input
    txt_file = tmp_path / "test_doc.txt"
    txt_file.write_text("Observation:\nFile TXT.\n", encoding="utf-8")
    res_txt_file = pipe.ingest(str(txt_file))
    assert res_txt_file.metadata["reader"] == "TXTReader"
    assert res_txt_file.metadata["file_name"] == "test_doc.txt"

    # 7. Unsupported format fails fast without fallback
    import pytest
    with pytest.raises(ValueError, match="No reader registered"):
        pipe.ingest_text("Some text", kind="unsupported.pdf")

    with pytest.raises(ValueError, match="No reader registered"):
        pipe.ingest(str(tmp_path / "photo.png"))


def test_audit_lifecycle_execution_failure_and_receipt_tracking(tmp_path):
    """Verify audit storage categories and receipt tracking on execution & persistence failure."""
    from src.living_authenticity.knowledge.governance.audit.persistence import (
        audit_storage_category,
    )
    from src.living_authenticity.knowledge.integration.runner import _persist_audits

    # 1. Record with execution_executed=False lands in 'failed' category
    mock_failed_exec = MagicMock()
    mock_failed_exec.execution_executed = False
    mock_failed_exec.terminal_outcome = "rejected"
    assert audit_storage_category(mock_failed_exec) == "failed"

    # 2. Record with execution_executed=True lands in 'passed' category
    mock_passed_exec = MagicMock()
    mock_passed_exec.execution_executed = True
    assert audit_storage_category(mock_passed_exec) == "passed"

    # 3. Operational error record lands in 'errors' category
    mock_err = MagicMock()
    mock_err.execution_executed = False
    mock_err.terminal_outcome = "audit_persistence_failed"
    assert audit_storage_category(mock_err) == "errors"

    # 4. Final audit persistence failure is faithfully recorded in audit_receipts
    failing_writer = MagicMock()
    failing_writer.append.side_effect = OSError("Disk write failed: read-only filesystem")

    mock_rec = MagicMock()
    mock_rec.record_id = "rec-fail-test"
    receipts = []

    _persist_audits(failing_writer, [mock_rec], receipts)

    assert len(receipts) == 1
    assert receipts[0]["record_id"] == "rec-fail-test"
    assert receipts[0]["persisted"] is False
    assert "Disk write failed" in receipts[0]["reason"]
