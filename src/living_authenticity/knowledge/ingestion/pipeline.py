from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import inspect
from pathlib import Path
import threading
from typing import Any

from src.living_authenticity.knowledge.ingestion.chunking.chunker_registry import (
    ChunkerRegistry,
)
from src.living_authenticity.knowledge.analysis.classification.base_classifier import (
    KnowledgeUnitClassifier,
)
from src.living_authenticity.knowledge.analysis.classification.result import (
    ClassificationResult,
)
from src.living_authenticity.knowledge.ingestion.cleaning.cleaner import Cleaner
from src.living_authenticity.knowledge.ingestion.cleaning.normalizer import Normalizer
from src.living_authenticity.knowledge.ingestion.extraction.extractor_registry import (
    ExtractorRegistry,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.ingestion.metadata.extractor import MetadataExtractor
from src.living_authenticity.knowledge.ingestion.parser.base_parser import BaseParser
from src.living_authenticity.knowledge.ingestion.parser.parsed_note import ParsedNote
from src.living_authenticity.knowledge.ingestion.readers.base_reader import BaseReader
from src.living_authenticity.knowledge.ingestion.readers.reader_registry import ReaderRegistry


@dataclass
class IngestionResult:
    """Analysis-only output of one file or text source. No authoritative write."""

    file_path: str
    raw_text: str
    cleaned_text: str
    normalized_text: str | None
    chunks: list[str]
    knowledge_units: list[KnowledgeUnit]
    parsed: ParsedNote | None
    metadata: dict
    classifications: list[ClassificationResult] = field(default_factory=list)


class IngestionPipeline:
    """Pipeline: Reader Selection -> Reading -> Cleaning -> Normalization -> Parsing -> Chunking -> Metadata Extraction -> Knowledge Unit Extraction & Provenance Recording.

    Reader Selection
        ↓
    Reading
        ↓
    Cleaning
        ↓
    Normalization (optional)
        ↓
    Parsing (optional)
        ↓
    Chunking
        ↓
    Metadata Extraction
        ↓
    Knowledge Unit Extraction & Provenance Recording

    The contractual runtime order places parsing and chunking over the
    cleaned and normalized representation so that chunking operates on parsed
    structure and extraction consumes that parsed structure to preserve semantic
    boundaries. Classification is NOT part
    of ingestion ordering in the integrated runtime: ``EvidenceFirstPipeline``
    constructs this pipeline with ``classifier=None`` and classifies each unit
    only after retrieval, comparison, relation analysis, and Core analysis.
    The optional ``classifier`` hook exists only for backward compatibility
    with pre-integration tests and must not be used to gate retrieval.

    This pipeline is strictly analysis-only: it never writes to staging,
    never mutates the vault, and never executes authoritative changes.
    """

    def __init__(
        self,
        reader_registry: ReaderRegistry,
        chunker_registry: ChunkerRegistry,
        extractor_registry: ExtractorRegistry,
        cleaner: Cleaner,
        metadata_extractor: MetadataExtractor,
        parser: BaseParser | None = None,
        normalizer: Normalizer | None = None,
        classifier: KnowledgeUnitClassifier | None = None,
        stage_observer: Callable | None = None,
    ):
        self.reader_registry = reader_registry
        self.chunker_registry = chunker_registry
        self.extractor_registry = extractor_registry
        self.cleaner = cleaner
        self.metadata_extractor = metadata_extractor
        self.parser = parser
        self.normalizer = normalizer
        self.classifier = classifier
        self.stage_observer = stage_observer
        self._local = threading.local()
        self._last_observer_errors: list[dict] = []
        self._lock = threading.Lock()

    @property
    def observer_errors(self) -> list[dict]:
        local_errs = getattr(self._local, "observer_errors", None)
        if local_errs is not None:
            return local_errs
        with self._lock:
            return list(self._last_observer_errors)

    @observer_errors.setter
    def observer_errors(self, value: list[dict]) -> None:
        self._local.observer_errors = value
        with self._lock:
            self._last_observer_errors = list(value)

    def _emit(
        self,
        observer: Callable | None,
        kind: str,
        message: str,
        operation: str = "",
        extra: dict | None = None,
        run_id: str = "",
        errors_collector: list[dict] | None = None,
    ) -> bool:
        obs = observer if observer is not None else self.stage_observer
        if obs is None:
            return False
        payload = {
            "kind": kind,
            "stage": "ingestion",
            "message": message,
            "operation": operation,
        }
        if run_id:
            payload["run_id"] = run_id
        if extra:
            for k, v in extra.items():
                if k != "kind":
                    payload[k] = v
        try:
            obs(payload)
            return True
        except Exception as exc:
            err = {
                "operation": operation,
                "kind": kind,
                "run_id": run_id,
                "error": str(exc) or type(exc).__name__,
            }
            if errors_collector is not None:
                errors_collector.append(err)
            else:
                self.observer_errors.append(err)
            return False

    def ingest(
        self,
        file_path: str,
        observer: Callable | None = None,
        input_provenance: object | None = None,
    ) -> IngestionResult:
        obs = observer if observer is not None else self.stage_observer
        run_errors: list[dict] = []
        self.observer_errors = run_errors
        prov_run_id = getattr(input_provenance, "run_id", "") if input_provenance else ""

        # Sub-stage 1: Reader Selection
        reader = self.reader_registry.get(file_path)
        reader_name = type(reader).__name__
        file_name = Path(file_path).name
        self._emit(
            obs,
            "status",
            f"Reader selected: {reader_name} for '{file_name}'",
            operation="reader_selection",
            extra={"reader": reader_name, "source": file_path},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        # Sub-stage 2: Reading
        raw_text = reader.read(file_path)
        self._emit(
            obs,
            "status",
            f"Reading completed: {len(raw_text)} characters read from '{file_name}'",
            operation="reading",
            extra={"raw_length": len(raw_text), "source": file_path},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        return self._process(
            raw_text,
            source=file_path,
            kind=file_path,
            reader=reader,
            observer=obs,
            input_provenance=input_provenance,
            run_errors=run_errors,
        )

    def ingest_text(
        self,
        text: str,
        source: str = "<text-input>",
        kind: str = "chat_input",
        observer: Callable | None = None,
        input_provenance: object | None = None,
    ) -> IngestionResult:
        """Process already-available text through the same stages as a file.

        This keeps terminal text input on the single existing ingestion
        path (reader selection, reading, cleaning, normalization, parsing,
        chunking, metadata extraction, knowledge unit extraction & provenance recording)
        without creating a temporary file and without a second parsing/extraction implementation.
        ``source`` is the inert provenance label; ``kind`` selects the
        registered reader, chunker, and extractor.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        obs = observer if observer is not None else self.stage_observer
        run_errors: list[dict] = []
        self.observer_errors = run_errors
        prov_run_id = getattr(input_provenance, "run_id", "") if input_provenance else ""

        # Sub-stage 1: Reader Selection
        reader_key = kind if kind else "chat_input"
        reader = self.reader_registry.get(reader_key)
        reader_name = type(reader).__name__
        self._emit(
            obs,
            "status",
            f"Reader selected: {reader_name} for format '{reader_key}'",
            operation="reader_selection",
            extra={"reader": reader_name, "format": reader_key},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        # Sub-stage 2: Reading
        raw_text = reader.read_text(text) if hasattr(reader, "read_text") else str(text)
        self._emit(
            obs,
            "status",
            f"Reading completed: {len(raw_text)} characters received",
            operation="reading",
            extra={"raw_length": len(raw_text)},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        return self._process(
            raw_text,
            source=source,
            kind=kind,
            reader=reader,
            observer=obs,
            input_provenance=input_provenance,
            run_errors=run_errors,
        )

    def _process(
        self,
        raw_text: str,
        source: str,
        kind: str,
        reader: BaseReader | None = None,
        observer: Callable | None = None,
        input_provenance: object | None = None,
        run_errors: list[dict] | None = None,
    ) -> IngestionResult:
        """Shared ingestion body over raw text (analysis-only, no writes)."""
        obs = observer if observer is not None else self.stage_observer
        if run_errors is None:
            run_errors = []
            self.observer_errors = run_errors
        prov_run_id = getattr(input_provenance, "run_id", "") if input_provenance else ""

        # Sub-stage 3: Cleaning
        cleaned_text = self.cleaner.clean(raw_text)
        self._emit(
            obs,
            "status",
            f"Cleaning completed: line endings and whitespace normalized ({len(cleaned_text)} characters)",
            operation="cleaning",
            extra={"cleaned_length": len(cleaned_text)},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        # Sub-stage 4: Normalization
        normalized_text = None
        if self.normalizer is not None:
            normalized_text = self.normalizer.normalize(cleaned_text)

        text_for_analysis = normalized_text if normalized_text is not None else cleaned_text
        self._emit(
            obs,
            "status",
            f"Normalization completed: Unicode & intra-line spaces refined ({len(text_for_analysis)} characters)",
            operation="normalization",
            extra={"normalized_length": len(text_for_analysis)},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        # Sub-stage 5: Parsing
        parsed = None
        if self.parser is not None:
            parsed = self.parser.parse(text_for_analysis)

        note_type = getattr(parsed, "note_type", "unparsed") if parsed else "unparsed"
        rel_count = len(getattr(parsed, "relations", ()) or ()) if parsed else 0
        tag_count = len(getattr(parsed, "tags", ()) or ()) if parsed else 0
        self._emit(
            obs,
            "status",
            f"Parsing completed: note structure identified (type: {note_type}, relations: {rel_count}, tags: {tag_count})",
            operation="parsing",
            extra={"note_type": str(note_type), "relations_count": rel_count, "tags_count": tag_count},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        # Sub-stage 6: Chunking
        chunker = self.chunker_registry.get(kind)
        accepts_parsed = False
        try:
            sig = inspect.signature(chunker.split)
            params = sig.parameters
            accepts_parsed = "parsed" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (ValueError, TypeError):
            accepts_parsed = False

        if accepts_parsed:
            chunks = chunker.split(text_for_analysis, parsed=parsed)
        else:
            chunks = chunker.split(text_for_analysis)
        chunker_name = type(chunker).__name__
        self._emit(
            obs,
            "status",
            f"Chunking completed: {len(chunks)} structural chunk(s) formed via {chunker_name}",
            operation="chunking",
            extra={"chunk_count": len(chunks), "chunker": chunker_name},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        # Sub-stage 7: Metadata Extraction
        reader_name = type(reader).__name__ if reader is not None else ""
        input_id = getattr(input_provenance, "input_id", "") if input_provenance else ""
        input_hash = getattr(input_provenance, "input_hash", "") if input_provenance else ""
        if not input_hash and raw_text:
            input_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

        metadata = self.metadata_extractor.extract(
            source,
            reader=reader_name,
            chunk_count=len(chunks),
            input_id=input_id,
            input_hash=input_hash,
        )

        self._emit(
            obs,
            "status",
            f"Metadata extraction completed for source '{source}'",
            operation="metadata_extraction",
            extra={"source": source, "reader": reader_name},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        # Sub-stage 8: Knowledge Unit Extraction & Provenance Recording
        extractor = self.extractor_registry.get(kind)
        accepts_extra = False
        accepts_parsed = True
        accepts_parser = True
        try:
            sig = inspect.signature(extractor.extract)
            params = sig.parameters
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            accepts_extra = (
                "metadata" in params
                or "input_provenance" in params
                or has_var_kw
            )
            accepts_parsed = "parsed" in params or has_var_kw
            accepts_parser = "parser" in params or has_var_kw
        except (ValueError, TypeError):
            accepts_extra = False
            accepts_parsed = True
            accepts_parser = True

        extract_kwargs: dict[str, Any] = {
            "source": source,
            "original_text": raw_text,
            "cleaned_text": cleaned_text,
            "normalized_text": normalized_text or cleaned_text,
        }
        if accepts_parsed:
            extract_kwargs["parsed"] = parsed
        if accepts_parser:
            extract_kwargs["parser"] = self.parser
        if accepts_extra:
            extract_kwargs["metadata"] = metadata
            extract_kwargs["input_provenance"] = input_provenance

        knowledge_units = extractor.extract(**extract_kwargs)

        for unit in knowledge_units:
            if not getattr(unit, "input_id", "") and input_id:
                unit.input_id = input_id
            if not getattr(unit, "input_hash", "") and input_hash:
                unit.input_hash = input_hash

        unit_ids = [u.id for u in knowledge_units]
        self._emit(
            obs,
            "status",
            f"Extraction completed: {len(knowledge_units)} knowledge unit(s) extracted with provenance",
            operation="extraction",
            extra={"unit_count": len(knowledge_units), "unit_ids": unit_ids},
            run_id=prov_run_id,
            errors_collector=run_errors,
        )

        classifications = []
        if self.classifier is not None:
            classifications = [
                self.classifier.classify(unit) for unit in knowledge_units
            ]

        if run_errors:
            metadata["observer_errors"] = list(run_errors)

        return IngestionResult(
            file_path=source,
            raw_text=raw_text,
            cleaned_text=cleaned_text,
            normalized_text=normalized_text,
            chunks=chunks,
            knowledge_units=knowledge_units,
            parsed=parsed,
            metadata=metadata,
            classifications=classifications,
        )
