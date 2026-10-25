"""Integrated evidence-first analytical pipeline (composition layer)."""
import inspect
from dataclasses import dataclass, field
from typing import Any
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
def _retriever_accepts_observer(retriever: Any) -> bool:
    """Safely check if retriever.retrieve accepts an observer parameter."""
    retrieve_fn = getattr(retriever, "retrieve", None)
    if not callable(retrieve_fn):
        return False
    try:
        sig = inspect.signature(retrieve_fn)
        for p in sig.parameters.values():
            if p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "observer":
                return True
        return False
    except (ValueError, TypeError):
        return False


class EvidenceFirstPipeline:
    def __init__(self, reader_registry=None, chunker_registry=None, extractor_registry=None,
                 cleaner=None, normalizer=None, parser=None, metadata_extractor=None,
                 retriever=None, comparator=None, relation_detector=None, core_analyzer=None,
                 classifier=None, proposal_builder=None, confidence_guard=None,
                 knowledge_filter=None, output_generator=None, destination: str = "",
                 stage_observer=None) -> None:
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
        # Optional observational hook only (default off): receives raw dicts
        # {"kind", "stage", "message"}. Never changes stage order, results,
        # or authorization; sink errors are swallowed.
        if stage_observer is not None and not callable(stage_observer):
            raise TypeError("stage_observer must be callable or None")
        self._stage_observer = stage_observer

        self.ingestion = IngestionPipeline(
            reader_registry=self.reader_registry, chunker_registry=self.chunker_registry,
            extractor_registry=self.extractor_registry, cleaner=self.cleaner,
            metadata_extractor=self.metadata_extractor, parser=self.parser,
            normalizer=self.normalizer, classifier=None,
            stage_observer=None)
        self.retriever: Any = retriever or DefaultRetriever()
        if hasattr(self.retriever, "set_observer") and callable(self.retriever.set_observer):
            try:
                self.retriever.set_observer(self._stage_observer)
            except Exception:
                pass
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

    def _observe(self, kind: str, stage: str, message: str = "") -> None:
        if self._stage_observer is None:
            return
        try:
            self._stage_observer(
                {"kind": kind, "stage": stage, "message": message})
        except Exception:
            pass
    @property
    def stage_order(self) -> tuple:
        return STAGE_ORDER
    def run_file(self, file_path: str, corpus=(), core_units=()) -> IntegratedResult:
        if not isinstance(file_path, str) or not file_path:
            raise TypeError("file_path must be a non-empty string")
        return self.run_ingestion(
            self.ingestion.ingest(file_path, observer=self._stage_observer), corpus, core_units)

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
        cache = getattr(self.retriever, "cache", None)
        if cache is not None:
            cache.cleanup()
            for r in unit_results:
                ret_res = getattr(r, "retrieval_result", None)
                if ret_res is not None and isinstance(ret_res.metadata, dict):
                    ret_res.metadata["cleaned_up"] = getattr(cache, "cleaned_up", True)
                    ret_res.metadata["cleanup_error"] = getattr(cache, "cleanup_error", None)
                    receipt_fn = getattr(cache, "receipt", None)
                    if callable(receipt_fn):
                        receipt = receipt_fn()
                        ret_res.metadata["cleanup_time"] = getattr(receipt, "cleanup_time_iso", "")
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
        uid = query.id if isinstance(query, KnowledgeUnit) else ""
        self._observe("stage_started", "retrieval", uid)
        if _retriever_accepts_observer(self.retriever):
            retrieval = self.retriever.retrieve(
                query, corpus_units, observer=self._stage_observer
            )
        else:
            retrieval = self.retriever.retrieve(query, corpus_units)
        self._observe("stage_completed", "retrieval", uid)
        if not isinstance(retrieval, RetrievalResult):
            raise TypeError("retriever must return a RetrievalResult")
        self._observe("stage_started", "comparison", uid)
        comparisons = tuple(self.comparator.compare(query, retrieval.candidates))
        self._observe("stage_completed", "comparison", uid)
        for item in comparisons:
            if not isinstance(item, ComparisonResult):
                raise TypeError("comparator must return ComparisonResult items")
        self._observe("stage_started", "relation", uid)
        relation = self.relation_detector.detect(query, comparisons)
        self._observe("stage_completed", "relation", uid)
        if not isinstance(relation, RelationDetectionResult):
            raise TypeError("detector must return a RelationDetectionResult")
        self._observe("stage_started", "core", uid)
        core = self.core_analyzer.analyze(query, cores)
        self._observe("stage_completed", "core", uid)
        if not isinstance(core, CoreAnalysisResult):
            raise TypeError("analyzer must return a CoreAnalysisResult")
        self._observe("stage_started", "classification", uid)
        classification = self.classifier.classify(query)
        self._observe("stage_completed", "classification", uid)
        if not isinstance(classification, ClassificationResult):
            raise TypeError("classifier must return a ClassificationResult")
        self._observe("stage_started", "proposal", uid)
        proposal = self.proposal_builder.build(query, classification=classification,
            retrieval=retrieval, comparisons=comparisons, relation=relation,
            core=core, destination=self.destination)
        self._observe("stage_completed", "proposal", uid)
        if not isinstance(proposal, Proposal):
            raise TypeError("proposal builder must return a Proposal")
        self._observe("stage_started", "confidence", uid)
        confidence = self.confidence_guard.assess(query, proposal, classification=classification,
            retrieval=retrieval, comparisons=comparisons, relation=relation, core=core)
        self._observe("stage_completed", "confidence", uid)
        if not isinstance(confidence, ConfidenceAssessment):
            raise TypeError("confidence guard must return a ConfidenceAssessment")
        self._observe("stage_started", "knowledge_filter", uid)
        filter_outcome = self.knowledge_filter.filter(query, proposal, classification=classification,
            retrieval=retrieval, comparisons=comparisons, relation=relation,
            core=core, confidence=confidence)
        self._observe("stage_completed", "knowledge_filter", uid)
        if not isinstance(filter_outcome, FilterOutcome):
            raise TypeError("knowledge filter must return a FilterOutcome")
        self._observe("stage_started", "representation", uid)
        generated = self.output_generator.generate(query, proposal=proposal,
            classification=classification, confidence=confidence)
        self._observe("stage_completed", "representation", uid)
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

