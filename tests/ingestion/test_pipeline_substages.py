import pytest

from src.living_authenticity.knowledge.ingestion.chunking.chunker_registry import (
    ChunkerRegistry,
)
from src.living_authenticity.knowledge.ingestion.cleaning.cleaner import Cleaner
from src.living_authenticity.knowledge.ingestion.cleaning.normalizer import Normalizer
from src.living_authenticity.knowledge.ingestion.extraction.extractor_registry import (
    ExtractorRegistry,
)
from src.living_authenticity.knowledge.ingestion.metadata.extractor import MetadataExtractor
from src.living_authenticity.knowledge.ingestion.parser.obsidian_parser import ObsidianParser
from src.living_authenticity.knowledge.ingestion.pipeline import IngestionPipeline
from src.living_authenticity.knowledge.ingestion.readers.reader_registry import ReaderRegistry


def _pipeline(stage_observer=None) -> IngestionPipeline:
    return IngestionPipeline(
        reader_registry=ReaderRegistry(),
        chunker_registry=ChunkerRegistry(),
        extractor_registry=ExtractorRegistry(),
        cleaner=Cleaner(),
        metadata_extractor=MetadataExtractor(),
        parser=ObsidianParser(),
        normalizer=Normalizer(),
        stage_observer=stage_observer,
    )


class TestPipelineSubstages:
    def test_ingest_text_executes_all_substages_in_order(self):
        events = []

        def observer(ev):
            events.append(ev)

        pipe = _pipeline(stage_observer=observer)
        text = (
            "Observation:\n"
            "Individuals experiencing sustained environmental pressure may lose "
            "the intuitive capability to appreciate simple calmness.\n\n"
            "Relations:\n"
            "[[Peace]]\n"
            "[[Survival]]\n"
        )
        result = pipe.ingest_text(
            text,
            source="chat_input:inp-test001",
            kind="text-input.md",
        )

        assert result.raw_text == text
        assert "Individuals experiencing sustained" in result.cleaned_text
        assert result.normalized_text is not None
        assert len(result.chunks) >= 1
        assert result.parsed is not None
        assert result.parsed.note_type == "Observation"
        assert len(result.knowledge_units) >= 1
        assert result.metadata["reader"] == "MarkdownReader"
        assert result.metadata["chunk_count"] == len(result.chunks)

        # Check emitted operations sequence
        operations = [e.get("operation") for e in events if e.get("stage") == "ingestion"]
        expected_sequence = [
            "reader_selection",
            "reading",
            "cleaning",
            "normalization",
            "parsing",
            "chunking",
            "metadata_extraction",
            "extraction",
        ]
        assert operations == expected_sequence
        assert operations.index("parsing") < operations.index("chunking")

        # Verify real counts, no fake monologues
        reading_ev = next(e for e in events if e.get("operation") == "reading")
        assert reading_ev["raw_length"] == len(text)
        assert "characters received" in reading_ev["message"]

        chunking_ev = next(e for e in events if e.get("operation") == "chunking")
        assert chunking_ev["chunk_count"] == len(result.chunks)

        parsing_ev = next(e for e in events if e.get("operation") == "parsing")
        assert parsing_ev["note_type"] == "Observation"

        extraction_ev = next(e for e in events if e.get("operation") == "extraction")
        assert extraction_ev["unit_count"] == len(result.knowledge_units)
        assert len(extraction_ev["unit_ids"]) == len(result.knowledge_units)

    def test_ingest_file_executes_substages_and_emits_events(self, tmp_path):
        events = []

        def observer(ev):
            events.append(ev)

        pipe = _pipeline(stage_observer=observer)
        note_file = tmp_path / "research_note.md"
        note_file.write_text(
            "Observation:\n"
            "Cognitive load accumulates significantly during context switches.\n\n"
            "Tags:\n"
            "#focus\n",
            encoding="utf-8",
        )

        result = pipe.ingest(str(note_file))
        assert result.metadata["file_name"] == "research_note.md"
        assert result.metadata["reader"] == "MarkdownReader"

        operations = [e.get("operation") for e in events if e.get("stage") == "ingestion"]
        assert operations == [
            "reader_selection",
            "reading",
            "cleaning",
            "normalization",
            "parsing",
            "chunking",
            "metadata_extraction",
            "extraction",
        ]
        assert operations.index("parsing") < operations.index("chunking")

    def test_unsupported_kind_fails_at_reader_selection_without_bypassing(self):
        events = []
        pipe = _pipeline(stage_observer=lambda ev: events.append(ev))

        with pytest.raises(ValueError, match="No reader registered"):
            pipe.ingest_text("Some text", source="chat_input:inp-bad", kind="unsupported.pdf")

        # Zero stages beyond reader selection executed
        operations = [e.get("operation") for e in events if e.get("stage") == "ingestion"]
        assert operations == []

    def test_provenance_linkage_in_metadata_and_units(self):
        pipe = _pipeline()
        text = "Observation:\nClarity emerges through rigorous verification.\n"
        source = "chat_input:inp-prov123"
        result = pipe.ingest_text(text, source=source, kind="text-input.md")

        assert result.metadata["source"] == source
        assert result.metadata["input_hash"] != ""
        for unit in result.knowledge_units:
            assert unit.source == source
            assert unit.id.startswith("ku-")
            assert "Clarity emerges" in unit.original_text

    def test_observer_failure_is_observable_without_halting_pipeline(self):
        """Observer failures are non-fatal telemetry events that must be recorded without altering execution."""
        def broken_observer(ev):
            raise RuntimeError("SSE client disconnected unexpectedly")

        pipe = _pipeline(stage_observer=broken_observer)
        text = "Observation:\nRobustness in the presence of observer failure.\n"
        result = pipe.ingest_text(text, source="chat_input:inp-obsfail", kind="chat_input")

        # Ingestion succeeds completely despite observer exceptions
        assert len(result.knowledge_units) == 1
        assert len(result.chunks) >= 1
        assert result.parsed is not None

        # Observer errors are recorded and observable
        assert len(pipe.observer_errors) >= 1
        assert any(e["operation"] == "reader_selection" for e in pipe.observer_errors)
        assert any("SSE client disconnected unexpectedly" in e["error"] for e in pipe.observer_errors)
        assert "observer_errors" in result.metadata
        assert len(result.metadata["observer_errors"]) == len(pipe.observer_errors)

    def test_knowledge_unit_preserves_input_provenance_and_metadata(self):
        """InputProvenance (input_id, input_hash) is preserved faithfully on KnowledgeUnits and metadata."""
        from src.living_authenticity.knowledge.input.processor import InputProvenance

        prov = InputProvenance(
            input_id="inp-exact-prov-456",
            input_hash="hash-exact-sha256-abcdef",
            source_type="chat_input",
            resolved_type="research_note",
            byte_length=120,
            char_length=100,
            timestamp="2026-10-19T05:00:00Z",
            operating_mode="examples",
            run_id="run-exact-789",
        )

        pipe = _pipeline()
        text = (
            "Observation:\n"
            "First unit regarding empirical testing and verification.\n\n"
            "Observation:\n"
            "Second unit regarding provenance propagation across layers.\n"
        )
        source = f"{prov.source_type}:{prov.input_id}"
        result = pipe.ingest_text(
            text,
            source=source,
            kind="chat_input",
            input_provenance=prov,
        )

        # Metadata records input provenance
        assert result.metadata["input_id"] == "inp-exact-prov-456"
        assert result.metadata["input_hash"] == "hash-exact-sha256-abcdef"
        assert result.metadata["source"] == "chat_input:inp-exact-prov-456"

        # Every extracted KnowledgeUnit preserves the exact input_id and input_hash
        assert len(result.knowledge_units) == 2
        for unit in result.knowledge_units:
            assert unit.input_id == "inp-exact-prov-456"
            assert unit.input_hash == "hash-exact-sha256-abcdef"
            assert unit.source == "chat_input:inp-exact-prov-456"
            assert unit.id.startswith("ku-")

    def test_observer_errors_concurrent_and_run_scoped(self):
        """Observer errors are isolated per run and per thread without cross-contamination."""
        import concurrent.futures

        pipe = _pipeline()

        def worker(thread_idx: int):
            def custom_broken_observer(ev):
                raise RuntimeError(f"Observer error from thread {thread_idx}")

            text = f"Observation:\nNote from thread {thread_idx}.\n"
            result = pipe.ingest_text(
                text,
                source=f"chat_input:inp-{thread_idx}",
                kind="chat_input",
                observer=custom_broken_observer,
            )
            return thread_idx, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, i) for i in (1, 2)]
            results = [f.result() for f in futures]

        for thread_idx, res in results:
            errors = res.metadata.get("observer_errors", [])
            assert len(errors) >= 1
            # All errors in this result must strictly originate from thread_idx
            for err in errors:
                assert f"thread {thread_idx}" in err["error"]
                assert f"thread {3 - thread_idx}" not in err["error"]

    def test_legacy_extractor_without_kwargs_works_and_populates_provenance(self):
        """Extractors with minimal legacy signatures extract cleanly and receive provenance."""
        from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
        from src.living_authenticity.knowledge.input.processor import InputProvenance

        class LegacyMinimalExtractor:
            """Legacy extractor that only accepts core positional arguments without kwargs."""
            def extract(self, source, original_text, cleaned_text, normalized_text):
                return [
                    KnowledgeUnit(
                        id="ku-legacy-001",
                        source=source,
                        position=0,
                        original_text=original_text,
                        cleaned_text=cleaned_text,
                        normalized_text=normalized_text,
                        meaning=cleaned_text,
                    )
                ]

        pipe = _pipeline()
        pipe.extractor_registry.register("legacy_format", LegacyMinimalExtractor())
        pipe.reader_registry.register("legacy_format", pipe.reader_registry.get("text"))
        pipe.chunker_registry.register("legacy_format", pipe.chunker_registry.get("text"))

        prov = InputProvenance(
            input_id="inp-legacy-123",
            input_hash="hash-legacy-sha256-456",
            source_type="chat_input",
            resolved_type="research_note",
            byte_length=50,
            char_length=40,
            timestamp="2026-10-19T05:00:00Z",
            operating_mode="examples",
            run_id="run-legacy-789",
        )

        result = pipe.ingest_text(
            "Observation:\nTesting legacy extractor compatibility.",
            source="chat_input:inp-legacy-123",
            kind="legacy_format",
            input_provenance=prov,
        )

        assert len(result.knowledge_units) == 1
        unit = result.knowledge_units[0]
        assert unit.input_id == "inp-legacy-123"
        assert unit.input_hash == "hash-legacy-sha256-456"

    def test_extractor_failure_propagates_unsuppressed(self):
        """Genuine exceptions during extraction are not swallowed by signature inspect blocks."""
        class FailingExtractor:
            def extract(self, **kwargs):
                raise ValueError("Genuine semantic extraction parser syntax error")

        pipe = _pipeline()
        pipe.extractor_registry.register("fail_fmt", FailingExtractor())
        pipe.reader_registry.register("fail_fmt", pipe.reader_registry.get("text"))
        pipe.chunker_registry.register("fail_fmt", pipe.chunker_registry.get("text"))

        with pytest.raises(ValueError, match="Genuine semantic extraction parser syntax error"):
            pipe.ingest_text("Some note", kind="fail_fmt")

    def test_in_memory_text_with_path_syntax_never_touches_filesystem(self):
        """In-memory text containing Windows/Unix paths is processed in-memory without filesystem calls."""
        pipe = _pipeline()
        path_text = "C:\\Windows\\System32\\drivers\\etc\\hosts\n/etc/passwd\nObservation:\nIn-memory path text.\n"
        result_chat = pipe.ingest_text(path_text, kind="chat_input")
        assert len(result_chat.knowledge_units) >= 1
        assert "C:\\Windows\\System32" in result_chat.raw_text

        result_txt = pipe.ingest_text(path_text, kind="text")
        assert len(result_txt.knowledge_units) >= 1
        assert "C:\\Windows\\System32" in result_txt.raw_text

    def test_readers_strip_bom_in_memory(self):
        """BaseReader and derived readers strip leading UTF-8 BOM in-memory without filesystem access."""
        from src.living_authenticity.knowledge.ingestion.readers.base_reader import BaseReader
        from src.living_authenticity.knowledge.ingestion.readers.chat_input_reader import ChatInputReader
        from src.living_authenticity.knowledge.ingestion.readers.markdown_reader import MarkdownReader
        from src.living_authenticity.knowledge.ingestion.readers.txt_reader import TXTReader

        class ConcreteBaseReader(BaseReader):
            def read(self, file_path: str) -> str:
                return ""

        raw = "\ufeffObservation:\nBOM check.\n"
        expected = "Observation:\nBOM check.\n"
        for reader in (ConcreteBaseReader(), ChatInputReader(), TXTReader(), MarkdownReader()):
            assert reader.read_text(raw) == expected
