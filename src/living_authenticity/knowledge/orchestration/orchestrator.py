"""Integrated evidence-first analytical pipeline (composition layer)."""
from dataclasses import dataclass, field
from src.living_authenticity.knowledge.analysis.classification.result import ClassificationResult
from src.living_authenticity.knowledge.analysis.comparison.outcome import ComparisonResult
from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment
from src.living_authenticity.knowledge.analysis.core_analysis.outcome import CoreAnalysisResult
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.ingestion.pipeline import IngestionPipeline, IngestionResult
from src.living_authenticity.knowledge.output.outcome import GeneratedNote
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.analysis.relations.outcome import RelationDetectionResult
from src.living_authenticity.knowledge.analysis.retrieval.result import RetrievalResult
from src.living_authenticity.knowledge.decision.filter_outcome import FilterOutcome
from src.living_authenticity.knowledge.decision.knowledge_filter import KnowledgeFilter
STAGE_ORDER = ("ingestion", "retrieval", "comparison", "relation", "core",
    "classification", "proposal", "confidence", "knowledge_filter", "representation")
@dataclass(frozen=True, kw_only=True)
class IntegratedUnitResult:
    query_unit: KnowledgeUnit
    retrieval_result: RetrievalResult
    comparisons: tuple = ()
    relation_result: RelationDetectionResult
    core_analysis: CoreAnalysisResult
    classification: ClassificationResult
    proposal: Proposal
    confidence: ConfidenceAssessment
    filter_outcome: FilterOutcome
    generated_note: GeneratedNote
    final_outcome: str = "NEEDS_REVIEW"


@dataclass(frozen=True)
class IntegratedResult:
    source: str = ""
    ingestion: IngestionResult | None = None
    units: tuple = field(default_factory=tuple)
    note: str = ""
    @property
    def has_units(self) -> bool:
        return bool(self.units)

class EvidenceFirstPipeline:
    def __init__(self, reader_registry=None, chunker_registry=None, extractor_registry=None,
                 cleaner=None, normalizer=None, parser=None, metadata_extractor=None,
                 retriever=None, comparator=None, relation_detector=None, core_analyzer=None,
                 classifier=None, proposal_builder=None, confidence_guard=None,
                 knowledge_filter=None, output_generator=None, destination: str = "") -> None:
        from src.living_authenticity.knowledge.ingestion.chunking.chunker_registry import ChunkerRegistry as _CR
        from src.living_authenticity.knowledge.analysis.classification.rule_based_classifier import DefaultClassifier
        from src.living_authenticity.knowledge.ingestion.cleaning.cleaner import Cleaner as _Cleaner
        from src.living_authenticity.knowledge.ingestion.cleaning.normalizer import Normalizer as _Normalizer
        from src.living_authenticity.knowledge.analysis.comparison.token_overlap import DefaultComparator
        from src.living_authenticity.knowledge.decision.confidence.evidence_strength import DefaultConfidenceGuard
        from src.living_authenticity.knowledge.analysis.core_analysis.token_overlap import DefaultCoreAnalyzer
        from src.living_authenticity.knowledge.ingestion.extraction.extractor_registry import ExtractorRegistry as _ER
        from src.living_authenticity.knowledge.ingestion.metadata.extractor import MetadataExtractor as _ME
        from src.living_authenticity.knowledge.output.evidence_note import DefaultOutputGenerator
        from src.living_authenticity.knowledge.ingestion.parser.obsidian_parser import ObsidianParser
        from src.living_authenticity.knowledge.decision.proposal.builder import ProposalBuilder
        from src.living_authenticity.knowledge.ingestion.readers.reader_registry import ReaderRegistry
        from src.living_authenticity.knowledge.analysis.relations.token_overlap import DefaultDetector
        from src.living_authenticity.knowledge.analysis.retrieval.token_overlap import DefaultRetriever
        self.reader_registry = reader_registry or ReaderRegistry()
        self.chunker_registry = chunker_registry or _CR()
        self.extractor_registry = extractor_registry or _ER()
        self.cleaner = cleaner or _Cleaner()
        self.normalizer = normalizer if normalizer is not None else _Normalizer()
        self.parser = parser if parser is not None else ObsidianParser()
        self.metadata_extractor = metadata_extractor or _ME()
        self.ingestion = IngestionPipeline(
            reader_registry=self.reader_registry, chunker_registry=self.chunker_registry,
            extractor_registry=self.extractor_registry, cleaner=self.cleaner,
            metadata_extractor=self.metadata_extractor, parser=self.parser,
            normalizer=self.normalizer, classifier=None)
        self.retriever = retriever or DefaultRetriever()
        self.comparator = comparator or DefaultComparator()
        self.relation_detector = relation_detector or DefaultDetector()
        self.core_analyzer = core_analyzer or DefaultCoreAnalyzer()
        self.classifier = classifier or DefaultClassifier()
        self.proposal_builder = proposal_builder or ProposalBuilder()
        self.confidence_guard = confidence_guard or DefaultConfidenceGuard()
        self.knowledge_filter = knowledge_filter or KnowledgeFilter()
        self.output_generator = output_generator or DefaultOutputGenerator()
        if not isinstance(destination, str):
            raise TypeError("destination must be a string")
        self.destination = destination
    @property
    def stage_order(self) -> tuple:
        return STAGE_ORDER
    def run_file(self, file_path: str, corpus=(), core_units=()) -> IntegratedResult:
        if not isinstance(file_path, str) or not file_path:
            raise TypeError("file_path must be a non-empty string")
        return self.run_ingestion(
            self.ingestion.ingest(file_path), corpus, core_units)

    def run_ingestion(self, ingestion, corpus=(), core_units=()) -> IntegratedResult:
        """Run the analytical chain over one already-ingested result.

        Where input comes from (file, explicit directory, or terminal
        text) is decided by the caller before ingestion; every stage
        from Retrieval onward is identical for all of them, so the
        integration layer reuses this one chain instead of building a
        parallel path.
        """
        if not isinstance(ingestion, IngestionResult):
            raise TypeError("ingestion must be an IngestionResult")
        corpus_units = self._corpus_units(corpus)
        cores = list(core_units) if core_units is not None else []
        for item in cores:
            if not isinstance(item, KnowledgeUnit):
                raise TypeError("core_units must hold KnowledgeUnit objects")
        unit_results = tuple(self.run_unit(u, corpus_units, cores) for u in ingestion.knowledge_units)
        note = "analysis-only integrated result for human review; not approval or execution."
        if not unit_results:
            note = "no knowledge units extracted; nothing analyzed."
        return IntegratedResult(
            source=ingestion.file_path, ingestion=ingestion,
            units=unit_results, note=note)
    def run_unit(self, query: KnowledgeUnit, corpus=(), core_units=()) -> IntegratedUnitResult:
        if not isinstance(query, KnowledgeUnit):
            raise TypeError("query must be a KnowledgeUnit")
        corpus_units = self._corpus_units(corpus)
        cores = list(core_units) if core_units is not None else []
        for item in cores:
            if not isinstance(item, KnowledgeUnit):
                raise TypeError("core_units must hold KnowledgeUnit objects")
        retrieval = self.retriever.retrieve(query, corpus_units)
        if not isinstance(retrieval, RetrievalResult):
            raise TypeError("retriever must return a RetrievalResult")
        comparisons = tuple(self.comparator.compare(query, retrieval.candidates))
        for item in comparisons:
            if not isinstance(item, ComparisonResult):
                raise TypeError("comparator must return ComparisonResult items")
        relation = self.relation_detector.detect(query, comparisons)
        if not isinstance(relation, RelationDetectionResult):
            raise TypeError("detector must return a RelationDetectionResult")
        core = self.core_analyzer.analyze(query, cores)
        if not isinstance(core, CoreAnalysisResult):
            raise TypeError("analyzer must return a CoreAnalysisResult")
        classification = self.classifier.classify(query)
        if not isinstance(classification, ClassificationResult):
            raise TypeError("classifier must return a ClassificationResult")
        proposal = self.proposal_builder.build(query, classification=classification,
            retrieval=retrieval, comparisons=comparisons, relation=relation,
            core=core, destination=self.destination)
        if not isinstance(proposal, Proposal):
            raise TypeError("proposal builder must return a Proposal")
        confidence = self.confidence_guard.assess(query, proposal, classification=classification,
            retrieval=retrieval, comparisons=comparisons, relation=relation, core=core)
        if not isinstance(confidence, ConfidenceAssessment):
            raise TypeError("confidence guard must return a ConfidenceAssessment")
        filter_outcome = self.knowledge_filter.filter(query, proposal, classification=classification,
            retrieval=retrieval, comparisons=comparisons, relation=relation,
            core=core, confidence=confidence)
        if not isinstance(filter_outcome, FilterOutcome):
            raise TypeError("knowledge filter must return a FilterOutcome")
        generated = self.output_generator.generate(query, proposal=proposal,
            classification=classification, confidence=confidence)
        if not isinstance(generated, GeneratedNote):
            raise TypeError("output generator must return a GeneratedNote")
        return IntegratedUnitResult(query_unit=query, retrieval_result=retrieval,
            comparisons=comparisons, relation_result=relation, core_analysis=core,
            classification=classification, proposal=proposal, confidence=confidence,
            filter_outcome=filter_outcome, generated_note=generated,
            final_outcome=filter_outcome.recommended_action)
    @staticmethod
    def _corpus_units(corpus) -> list:
        if corpus is None:
            return []
        if hasattr(corpus, "list_units"):
            units = corpus.list_units()
        else:
            units = list(corpus)
        for item in units:
            if not isinstance(item, KnowledgeUnit):
                raise TypeError("corpus must hold KnowledgeUnit objects")
        return units

