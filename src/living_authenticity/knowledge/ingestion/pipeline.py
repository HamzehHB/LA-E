from dataclasses import dataclass, field

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
from src.living_authenticity.knowledge.ingestion.readers.reader_registry import ReaderRegistry


@dataclass
class IngestionResult:
    """Analysis-only output of one file. No authoritative write."""

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
    """Pipeline: read, clean, normalize, chunk, parse, extract units, metadata.

    Reader
        ↓
    Cleaner
        ↓
    Normalizer (optional)
        ↓
    Chunker
        ↓
    Parser (optional)
        ↓
    Knowledge Unit Extraction
        ↓
    File metadata

    The contractual runtime order places chunking before parsing/extraction:
    parsing operates on the normalized representation shared with chunking,
    and extraction consumes that parsed structure. Classification is NOT part
    of ingestion ordering in the integrated runtime: ``EvidenceFirstPipeline``
    constructs this pipeline with ``classifier=None`` and classifies each unit
    only after retrieval, comparison, relation analysis, and Core analysis.
    The optional ``classifier`` hook exists only for backward compatibility
    with pre-integration tests and must not be used to gate retrieval.

    Extraction runs after parsing because ``MarkdownKnowledgeExtractor`` uses
    the parser's ``section_boundaries()`` helper to locate semantic section
    boundaries. The parser instance must therefore be available, and the
    ``parsed`` output is used as a guard so that section-based extraction is
    only applied when the parser successfully parsed the note.

    Classification is an analysis-only, proposal step. When a classifier is
    provided, one :class:`ClassificationResult` is produced per extracted
    :class:`KnowledgeUnit`; the units themselves are never mutated. Without a
    classifier the ``classifications`` list stays empty.

    This pipeline does not embed, store, or write authoritative knowledge.
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
    ):

        self.reader_registry = reader_registry
        self.chunker_registry = chunker_registry
        self.extractor_registry = extractor_registry
        self.cleaner = cleaner
        self.metadata_extractor = metadata_extractor
        self.parser = parser
        self.normalizer = normalizer
        self.classifier = classifier

    def ingest(self, file_path: str) -> IngestionResult:
        reader = self.reader_registry.get(file_path)
        raw_text = reader.read(file_path)
        return self._process(raw_text, source=file_path, kind=file_path)

    def ingest_text(self, text: str, source: str = "<text-input>",
                    kind: str = ".md") -> IngestionResult:
        """Process already-available text through the same stages as a file.

        This keeps terminal text input on the single existing ingestion
        path (cleaning, normalization, chunking, parsing, extraction,
        metadata) without creating a temporary file and without a second
        parsing/extraction implementation. ``source`` is the inert
        provenance label; ``kind`` selects the registered chunker and
        extractor exactly as a file extension would.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return self._process(text, source=source, kind=kind)

    def _process(self, raw_text: str, source: str, kind: str) -> IngestionResult:
        """Shared ingestion body over raw text (analysis-only, no writes)."""
        cleaned_text = self.cleaner.clean(raw_text)

        normalized_text = None
        if self.normalizer is not None:
            normalized_text = self.normalizer.normalize(cleaned_text)

        text_for_analysis = normalized_text if normalized_text is not None else cleaned_text

        chunker = self.chunker_registry.get(kind)
        chunks = chunker.split(text_for_analysis)

        parsed = None
        if self.parser is not None:
            parsed = self.parser.parse(text_for_analysis)

        knowledge_units: list[KnowledgeUnit] = []
        try:
            extractor = self.extractor_registry.get(kind)
            knowledge_units = extractor.extract(
                source=source,
                original_text=raw_text,
                cleaned_text=cleaned_text,
                normalized_text=normalized_text or cleaned_text,
                parsed=parsed,
                parser=self.parser,
            )
        except ValueError:
            knowledge_units = []

        classifications = []
        if self.classifier is not None:
            classifications = [
                self.classifier.classify(unit) for unit in knowledge_units
            ]

        metadata = self.metadata_extractor.extract(source)

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
