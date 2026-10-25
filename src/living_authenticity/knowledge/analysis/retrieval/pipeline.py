"""The Five Retrieval Capabilities Pipeline.

Defines the five distinct, contract-separated capabilities of Retrieval:
1. Query Preparation (prepare_query):
   Prepares an appropriate search query from the input while preserving its
   intended meaning and relevant context.
2. Candidate Retrieval (retrieve_candidates):
   Retrieves potentially relevant knowledge candidates from the available corpus.
3. Relevance Assessment (assess_relevance):
   Assesses potential relevance using explicit, explainable evidence.
4. Candidate Identification (identify_candidates):
   Preserves stable candidate identity so similar candidates are not conflated
   or treated as identical to the input.
5. Retrieval Evidence Recording (record_evidence):
   Records why each candidate was retrieved and which evidence supports its inclusion.

These are real backend analytical steps that execute in strict sequence.
They propose nothing, decide nothing, and authorize nothing.
"""
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import re
from typing import Any
import unicodedata

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from .candidate import RetrievedCandidate
from .result import RetrievalResult

_WS_RE = re.compile(r"\s+")
_NON_TOKEN_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_query_text(text: str) -> str:
    """Normalize text for query matching.

    Applies Unicode NFC normalization, lowercasing, and ZWNJ-to-space mapping.
    """
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFC", text).lower()
    return normalized.replace("\u200c", " ")


def tokenize_query(text: str) -> tuple[str, ...]:
    """Extract distinct sorted alphabetic content tokens."""
    cleaned = _NON_TOKEN_RE.sub(" ", normalize_query_text(text))
    tokens = {
        token
        for token in _WS_RE.split(cleaned)
        if token and any(char.isalpha() for char in token)
    }
    return tuple(sorted(tokens))


def _extract_unit_text(unit: KnowledgeUnit) -> str:
    """Extract best available text for a unit."""
    return (
        unit.cleaned_text
        or unit.normalized_text
        or unit.meaning
        or unit.original_text
        or ""
    )


@dataclass(frozen=True)
class PreparedQuery:
    """Capability 1: Prepared search query preserving meaning and context."""

    query_unit_id: str
    query_source: str
    query_position: int
    raw_text: str
    normalized_text: str
    query_terms: tuple[str, ...]
    context_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RelevanceAssessment:
    """Capability 3: Explicit, explainable evidence of candidate relevance.

    ``similarity_score``: For token overlap / lexical strategies, this records the
    unnormalized raw count of shared content tokens (equal to float(overlap_score))
    measuring exact token overlap. For vector retrieval strategies, this records a
    normalized similarity score bounded in [0.0, 1.0] computed monotonically from
    vector distance as 1.0 / (1.0 + vector_distance).
    In all cases, similarity is an analytical metric only and must never be interpreted
    as certainty, truth, identity, or authorization.
    """

    candidate_unit_id: str
    candidate_source: str
    candidate_position: int
    candidate_text: str
    overlap_score: int
    matched_terms: tuple[str, ...]
    similarity_score: float = 0.0
    evidence_basis: str = ""
    strategy: str = ""
    vector_distance: float | None = None


# Capability 1: Query Preparation
def prepare_query(query: KnowledgeUnit) -> PreparedQuery:
    """Prepare a search query from the input unit while preserving meaning and context."""
    if not isinstance(query, KnowledgeUnit):
        raise TypeError(f"query must be a KnowledgeUnit, got {type(query).__name__}")
    raw = _extract_unit_text(query)
    normalized = normalize_query_text(raw)
    terms = tokenize_query(raw)
    context = {
        "source": query.source,
        "position": query.position,
        "id": query.id,
        "tags": tuple(getattr(query, "tags", ()) or ()),
        "raw_length": len(raw),
        "term_count": len(terms),
    }
    return PreparedQuery(
        query_unit_id=query.id,
        query_source=query.source,
        query_position=query.position,
        raw_text=raw,
        normalized_text=normalized,
        query_terms=terms,
        context_metadata=context,
    )


def candidate_identity_key(candidate: Any) -> str:
    """Return the canonical candidate identity key format: <source>:<position>:<unit_id>."""
    src = getattr(candidate, "candidate_source", None) or getattr(candidate, "source", "")
    pos = getattr(candidate, "candidate_position", None)
    if pos is None:
        pos = getattr(candidate, "position", 0)
    uid = getattr(candidate, "candidate_unit_id", None) or getattr(candidate, "unit_id", "")
    return f"{src}:{pos}:{uid}"


# Capability 2: Candidate Retrieval
def retrieve_candidates(
    prepared: PreparedQuery,
    corpus: Sequence[Any],
    strategy: str = "token_overlap",
    min_shared_terms: int = 1,
) -> tuple[KnowledgeUnit, ...]:
    """Retrieve potentially relevant knowledge candidates from the corpus (excluding self)."""
    if not isinstance(prepared, PreparedQuery):
        raise TypeError("prepared must be a PreparedQuery")
    if isinstance(min_shared_terms, bool) or not isinstance(min_shared_terms, int):
        raise TypeError("min_shared_terms must be an integer")
    if min_shared_terms < 1:
        raise ValueError("min_shared_terms must be at least 1")
    if strategy not in ("token_overlap", "lexical"):
        raise ValueError(f"unsupported retrieval strategy: {strategy!r}")
    if not prepared.query_terms:
        return ()

    query_term_set = set(prepared.query_terms)
    matched_units: list[KnowledgeUnit] = []

    for item in corpus:
        if not isinstance(item, KnowledgeUnit):
            raise TypeError(
                f"corpus must contain only KnowledgeUnit objects, got {type(item).__name__}"
            )
        # Invariant: Never retrieve the query unit as its own candidate
        if item.id == prepared.query_unit_id:
            continue
        unit_text = _extract_unit_text(item)
        cand_terms = tokenize_query(unit_text)
        shared = query_term_set.intersection(cand_terms)
        if len(shared) >= min_shared_terms:
            matched_units.append(item)

    return tuple(matched_units)


# Capability 3: Relevance Assessment
def assess_relevance(
    prepared: PreparedQuery,
    candidates: Sequence[KnowledgeUnit],
    strategy: str = "token_overlap",
) -> tuple[RelevanceAssessment, ...]:
    """Assess potential relevance of retrieved candidates using explicit, explainable evidence."""
    if not isinstance(prepared, PreparedQuery):
        raise TypeError("prepared must be a PreparedQuery")

    query_term_set = set(prepared.query_terms)
    assessments: list[RelevanceAssessment] = []

    for unit in candidates:
        if not isinstance(unit, KnowledgeUnit):
            continue
        unit_text = _extract_unit_text(unit)
        cand_terms = tokenize_query(unit_text)
        shared = tuple(sorted(query_term_set.intersection(cand_terms)))
        overlap_score = len(shared)
        evidence = (
            f"Shares {overlap_score} content token(s) with query: [{', '.join(shared)}]"
            if shared
            else "No overlapping tokens"
        )
        assessments.append(
            RelevanceAssessment(
                candidate_unit_id=unit.id,
                candidate_source=unit.source,
                candidate_position=unit.position,
                candidate_text=unit_text,
                overlap_score=overlap_score,
                matched_terms=shared,
                similarity_score=float(overlap_score),
                evidence_basis=evidence,
                strategy=strategy,
            )
        )

    return tuple(assessments)


# Capability 4: Candidate Identification
def identify_candidates(
    assessments: Sequence[RelevanceAssessment],
    max_candidates: int = 5,
    retriever_name: str = "token_overlap",
) -> tuple[RetrievedCandidate, ...]:
    """Preserve stable candidate identity and order without conflating candidates."""
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise TypeError("max_candidates must be an integer")
    if max_candidates <= 0:
        return ()

    # Deterministic ranking: score descending, then source, position, id
    sorted_assessments = sorted(
        assessments,
        key=lambda a: (
            -a.overlap_score,
            -a.similarity_score,
            a.candidate_source,
            a.candidate_position,
            a.candidate_unit_id,
        ),
    )
    seen_keys: set[str] = set()
    chosen: list[RetrievedCandidate] = []
    for a in sorted_assessments:
        canon_key = f"{a.candidate_source}:{a.candidate_position}:{a.candidate_unit_id}"
        if canon_key in seen_keys:
            continue
        seen_keys.add(canon_key)
        chosen.append(
            RetrievedCandidate(
                unit_id=a.candidate_unit_id,
                source=a.candidate_source,
                position=a.candidate_position,
                text=a.candidate_text,
                matched_terms=a.matched_terms,
                overlap_score=a.overlap_score,
                retriever=retriever_name,
                evidence_basis=a.evidence_basis,
                candidate_identity_key=canon_key,
            )
        )
        if len(chosen) >= max_candidates:
            break
    return tuple(chosen)


# Capability 5: Retrieval Evidence Recording
def record_evidence(
    prepared: PreparedQuery,
    candidates: tuple[RetrievedCandidate, ...],
    corpus_size: int | None = None,
    strategy: str = "token_overlap",
    min_shared_terms: int = 1,
    metadata: dict | None = None,
) -> RetrievalResult:
    """Record why each candidate was retrieved and the limits/evidence of the search."""
    if not isinstance(prepared, PreparedQuery):
        raise TypeError("prepared must be a PreparedQuery")

    meta = dict(metadata) if metadata is not None else {}
    meta.setdefault(
        "capabilities",
        (
            "query_preparation",
            "candidate_retrieval",
            "relevance_assessment",
            "candidate_identification",
            "retrieval_evidence_recording",
        ),
    )
    meta.setdefault("query_terms_count", len(prepared.query_terms))
    meta.setdefault("candidates_count", len(candidates))
    meta.setdefault("corpus_size", corpus_size)
    if corpus_size is None:
        meta.setdefault("corpus_size_unknown", True)
        meta.setdefault("corpus_size_reporting", "unknown")

    if not prepared.query_terms:
        note = "Query has no analyzable content; no candidates retrieved."
    elif candidates:
        if corpus_size is not None:
            note = (
                f"Retrieved {len(candidates)} of {corpus_size} examined units as contextual evidence; "
                f"relevance is {strategy} evidence only and decides nothing."
            )
        else:
            note = (
                f"Retrieved {len(candidates)} units from an unmeasured collection as contextual evidence; "
                f"relevance is {strategy} evidence only and decides nothing."
            )
    else:
        note = (
            f"No corpus unit shares at least {min_shared_terms} distinct content tokens with the query."
        )

    return RetrievalResult(
        query_unit_id=prepared.query_unit_id,
        query_source=prepared.query_source,
        query_position=prepared.query_position,
        strategy=strategy,
        candidates=candidates,
        corpus_size=corpus_size,
        query_terms=prepared.query_terms,
        note=note,
        metadata=meta,
    )


SUPPORTED_COORDINATOR_STRATEGIES = frozenset({"token_overlap", "lexical"})


class RetrievalCoordinator:
    """Executes the five retrieval capabilities in sequence with observational hooks."""

    def __init__(
        self,
        strategy: str = "token_overlap",
        min_shared_terms: int = 1,
        max_candidates: int = 5,
        observer: Callable | None = None,
    ) -> None:
        if strategy not in SUPPORTED_COORDINATOR_STRATEGIES:
            raise ValueError(
                f"unsupported coordinator strategy: {strategy!r}. Supported strategies are: {sorted(SUPPORTED_COORDINATOR_STRATEGIES)}"
            )
        if isinstance(min_shared_terms, bool) or not isinstance(min_shared_terms, int):
            raise TypeError("min_shared_terms must be an integer")
        if min_shared_terms < 1:
            raise ValueError("min_shared_terms must be at least 1")
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
            raise TypeError("max_candidates must be an integer")
        if max_candidates < 0:
            raise ValueError("max_candidates must be non-negative")
        if observer is not None and not callable(observer):
            raise TypeError("observer must be callable or None")
        self.strategy = strategy
        self.min_shared_terms = min_shared_terms
        self.max_candidates = max_candidates
        self.observer = observer

    def _emit(
        self,
        operation: str,
        message: str,
        phase: str = "completed",
        extra: dict | None = None,
        run_id: str = "",
    ) -> None:
        if self.observer is None or not callable(self.observer):
            return
        payload = {
            "kind": "status",
            "stage": "retrieval",
            "operation": operation,
            "phase": phase,
            "message": message,
        }
        if run_id:
            payload["run_id"] = run_id
        if extra:
            payload.update(extra)
        try:
            self.observer(payload)
        except Exception as obs_exc:
            if not hasattr(self, "_telemetry_failures"):
                self._telemetry_failures = []
            self._telemetry_failures.append({
                "operation": operation,
                "phase": phase,
                "error": str(obs_exc) or type(obs_exc).__name__,
            })

    def run(
        self,
        query: KnowledgeUnit,
        corpus: Sequence[KnowledgeUnit],
        run_id: str = "",
    ) -> RetrievalResult:
        self._telemetry_failures = []
        # Capability 1: Query Preparation
        qid = getattr(query, "id", "")
        self._emit(
            "query_preparation",
            f"Preparing search query for unit '{qid}'",
            phase="started",
            run_id=run_id,
        )
        try:
            prepared = prepare_query(query)
        except Exception as exc:
            self._emit(
                "query_preparation",
                f"Query preparation failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise
        self._emit(
            "query_preparation",
            f"Query prepared: {len(prepared.query_terms)} terms extracted from '{prepared.query_unit_id}'",
            phase="completed",
            extra={"query_terms_count": len(prepared.query_terms)},
            run_id=run_id,
        )

        # Capability 2: Candidate Retrieval
        corpus_size = len(corpus)
        self._emit(
            "candidate_retrieval",
            f"Searching corpus of {corpus_size} items",
            phase="started",
            run_id=run_id,
        )
        try:
            raw_candidates = retrieve_candidates(
                prepared, corpus, self.strategy, self.min_shared_terms
            )
        except Exception as exc:
            self._emit(
                "candidate_retrieval",
                f"Candidate retrieval failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise
        self._emit(
            "candidate_retrieval",
            f"Candidate retrieval: {len(raw_candidates)} matching units found from {corpus_size} corpus items",
            phase="completed",
            extra={"matched_count": len(raw_candidates), "corpus_size": corpus_size},
            run_id=run_id,
        )

        # Capability 3: Relevance Assessment
        self._emit(
            "relevance_assessment",
            f"Evaluating relevance of {len(raw_candidates)} candidate units",
            phase="started",
            run_id=run_id,
        )
        try:
            assessments = assess_relevance(prepared, raw_candidates, self.strategy)
        except Exception as exc:
            self._emit(
                "relevance_assessment",
                f"Relevance assessment failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise
        self._emit(
            "relevance_assessment",
            f"Relevance assessment: {len(assessments)} candidates evaluated with explicit textual evidence",
            phase="completed",
            extra={"assessed_count": len(assessments)},
            run_id=run_id,
        )

        # Capability 4: Candidate Identification
        self._emit(
            "candidate_identification",
            f"Establishing stable candidate identities for {len(assessments)} assessments",
            phase="started",
            run_id=run_id,
        )
        try:
            identified = identify_candidates(
                assessments, self.max_candidates, self.strategy
            )
        except Exception as exc:
            self._emit(
                "candidate_identification",
                f"Candidate identification failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise
        self._emit(
            "candidate_identification",
            f"Candidate identification: {len(identified)} distinct, stable candidate identities established",
            phase="completed",
            extra={"candidate_count": len(identified)},
            run_id=run_id,
        )

        # Capability 5: Retrieval Evidence Recording
        self._emit(
            "retrieval_evidence_recording",
            "Recording retrieval evidence and boundaries into result",
            phase="started",
            run_id=run_id,
        )
        evidence_meta = {}
        if getattr(self, "_telemetry_failures", None):
            evidence_meta["telemetry_delivery_failures"] = list(self._telemetry_failures)
        try:
            result = record_evidence(
                prepared,
                identified,
                corpus_size,
                self.strategy,
                min_shared_terms=self.min_shared_terms,
                metadata=evidence_meta or None,
            )
        except Exception as exc:
            self._emit(
                "retrieval_evidence_recording",
                f"Retrieval evidence recording failed: {exc}",
                phase="failed",
                extra={"error": str(exc)},
                run_id=run_id,
            )
            raise
        self._emit(
            "retrieval_evidence_recording",
            f"Retrieval evidence recorded: {len(identified)} candidates bound into RetrievalResult",
            phase="completed",
            extra={"has_candidates": result.has_candidates},
            run_id=run_id,
        )
        if getattr(self, "_telemetry_failures", None):
            result.metadata["telemetry_delivery_failures"] = list(self._telemetry_failures)

        return result
