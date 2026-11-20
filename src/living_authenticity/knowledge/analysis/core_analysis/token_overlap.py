"""Deterministic token-overlap Core relevance analysis logic."""

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit

from .base_analyzer import CoreRelevanceAnalyzer
from .outcome import CoreAnalysisResult

_WS_RE = re.compile(r"\s+")
_NON_TOKEN_RE = re.compile(r"[^\w\s]", re.UNICODE)

# Minimum distinct shared alphabetic tokens before a potentially_relevant
# observation is justified. Mirrors the comparison stage guard that a single
# shared word cannot establish relatedness.
_MIN_SHARED_TERMS = 2

# Minimum containment of the smaller token set before overlap counts as
# material rather than incidental shared vocabulary.
_MIN_CONTAINMENT = 0.3


def normalize_text(text: str) -> str:
    """Normalize text for token comparison (NFC, lowercase, ZWNJ-to-space)."""
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFC", text).lower()
    return normalized.replace(chr(0x200C), " ")


def analysis_tokens(text: str) -> tuple:
    """Return the distinct sorted alphabetic tokens of ``text``."""
    cleaned = _NON_TOKEN_RE.sub(" ", normalize_text(text))
    tokens = {
        token
        for token in _WS_RE.split(cleaned)
        if token and any(char.isalpha() for char in token)
    }
    return tuple(sorted(tokens))


def unit_text(unit: KnowledgeUnit) -> str:
    """Best available text of a unit for matching."""
    return (
        unit.cleaned_text
        or unit.normalized_text
        or unit.meaning
        or unit.original_text
        or ""
    )


class TokenOverlapCoreAnalyzer(CoreRelevanceAnalyzer):
    """Analyze Core relevance from explicit token-overlap evidence."""

    def __init__(self) -> None:
        self._name = "token_overlap_core_analysis"

    @property
    def name(self) -> str:
        return self._name

    def analyze(
        self, query: KnowledgeUnit, core_units: Sequence[Any], context: Any = None
    ) -> CoreAnalysisResult:
        if not isinstance(query, KnowledgeUnit):
            raise TypeError("query must be a KnowledgeUnit")
        if core_units is None:
            raise TypeError("core_units must be a sequence")
        cores = list(core_units)
        for item in cores:
            if not isinstance(item, KnowledgeUnit):
                raise TypeError("core_units must hold KnowledgeUnit objects")
        from src.living_authenticity.knowledge.vault.core_validator import verify_core_units_provenance
        verify_core_units_provenance(cores, context=context)
        examined = tuple(unit.id for unit in cores)
        query_text = unit_text(query)
        if not query_text.strip():
            return self._result(
                query, examined, "insufficient_evidence",
                "Query has no analyzable content; Core relevance is unresolved.",
                (), (),
            )
        query_tokens = set(analysis_tokens(query_text))
        if not query_tokens:
            return self._result(
                query, examined, "insufficient_evidence",
                "Query has no content tokens; Core relevance is unresolved.",
                (), (),
            )
        best_shared: tuple = ()
        best_core_id = ""
        for core in cores:
            core_text = unit_text(core)
            if not core_text.strip():
                continue
            shared = tuple(sorted(query_tokens & set(analysis_tokens(core_text))))
            if not shared:
                continue
            core_tokens = set(analysis_tokens(core_text))
            smaller = min(len(query_tokens), len(core_tokens))
            containment = len(shared) / smaller if smaller else 0.0
            if (
                len(shared) >= _MIN_SHARED_TERMS
                and containment >= _MIN_CONTAINMENT
                and len(shared) > len(best_shared)
            ):
                best_shared = shared
                best_core_id = core.id
        if best_shared:
            evidence = ("core_unit:" + best_core_id,) if best_core_id else ()
            return self._result(
                query, examined, "potentially_relevant",
                "Analytical observation only: shared vocabulary with approved "
                "Core unit " + best_core_id + ". This is not a Core change and "
                "does not establish support, conflict, or equivalence.",
                best_shared, evidence,
            )
        return self._result(
            query, examined, "no_observed_relevance",
            "No material token overlap with the supplied approved Core units; "
            "lexical overlap alone could not justify a stronger claim.",
            (), (),
        )

    def _result(self, query, examined, relevance, basis, shared, evidence):
        return CoreAnalysisResult(
            query_unit_id=query.id,
            query_source=query.source,
            query_position=query.position,
            strategy=self._name,
            relevance=relevance,
            basis=basis,
            shared_terms=shared,
            examined_core_ids=examined,
            note=(
                "Non-authoritative analytical observation for human review; "
                "Core remains human-controlled."
            ),
            is_authoritative=False,
            requires_human_review=True,
            evidence=evidence,
        )


class DefaultCoreAnalyzer(TokenOverlapCoreAnalyzer):
    """Default analyzer alias preserving token-overlap behavior."""
