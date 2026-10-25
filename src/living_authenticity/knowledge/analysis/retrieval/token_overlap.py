"""Deterministic token-overlap retrieval over existing knowledge units.

The retriever normalizes the query and each corpus unit (lowercase,
Unicode NFC, ZWNJ-to-space, punctuation-to-space tokenization) and counts
distinct shared alphabetic tokens. Units sharing at least
``min_shared_terms`` distinct tokens become candidates, ranked by overlap
score (descending), then by stable identity order (source, position,
unit id) so repeated runs return identical results.

Token overlap is an explicit textual-relevance signal only: it must never
be interpreted as confidence, certainty, identity, a decision about the
units, or authorization for any action. The implementation stays
provider-independent and explainable; no embedding model, vector database,
network call, or external service is involved.

Retrieved content is treated as inert data. Matched text is copied into
candidates for later stages to inspect; it is never executed, rendered as
instructions, or allowed to trigger filesystem, network, or provider
operations.
"""

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)

from .base_retriever import KnowledgeRetriever
from .candidate import RetrievedCandidate
from .result import RetrievalResult

_WS_RE = re.compile(r"\s+")
_NON_TOKEN_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_query_text(text: str) -> str:
    """Normalize text for token-overlap comparison.

    Unicode NFC normalization, lowercasing, and ZWNJ-to-space mapping keep
    Persian and mixed-language content comparable, mirroring the existing
    classifier convention so matching is ZWNJ-insensitive.
    """
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFC", text).lower()
    return normalized.replace("\u200c", " ")


def tokenize(text: str) -> tuple[str, ...]:
    """Return the distinct alphabetic tokens of ``text``, in sorted order."""
    cleaned = _NON_TOKEN_RE.sub(" ", normalize_query_text(text))
    tokens = {
        token
        for token in _WS_RE.split(cleaned)
        if token and any(char.isalpha() for char in token)
    }
    return tuple(sorted(tokens))


def _unit_text(unit: KnowledgeUnit) -> str:
    """Best available text of a unit for matching and evidence copies."""
    return (
        unit.cleaned_text
        or unit.normalized_text
        or unit.meaning
        or unit.original_text
        or ""
    )



from .pipeline import (
    PreparedQuery,
    RelevanceAssessment,
    RetrievalCoordinator,
    assess_relevance,
    identify_candidates,
    prepare_query,
    record_evidence,
    retrieve_candidates,
)


class TokenOverlapRetriever(KnowledgeRetriever):
    """Retrieve corpus units sharing content tokens with the query unit."""

    def __init__(
        self,
        min_shared_terms: int = 1,
        max_candidates: int = 5,
        observer: Any = None,
    ) -> None:
        if isinstance(min_shared_terms, bool) or not isinstance(min_shared_terms, int):
            raise TypeError("min_shared_terms must be an int")
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
            raise TypeError("max_candidates must be an int")
        if min_shared_terms < 1:
            raise ValueError("min_shared_terms must be at least 1")
        if max_candidates < 0:
            raise ValueError("max_candidates must be non-negative")
        if observer is not None and not callable(observer):
            raise TypeError("observer must be callable or None")
        self._min_shared_terms = min_shared_terms
        self._max_candidates = max_candidates
        self._name = "token_overlap"
        self._observer = observer

    def set_observer(self, observer: Any) -> None:
        if observer is not None and not callable(observer):
            raise TypeError("observer must be callable or None")
        self._observer = observer

    @property
    def name(self) -> str:
        return self._name

    @property
    def min_shared_terms(self) -> int:
        return self._min_shared_terms

    @property
    def max_candidates(self) -> int:
        return self._max_candidates

    @property
    def cache(self) -> None:
        """Token overlap retrieval does not use a temporary embedding cache."""
        return None

    def retrieve(
        self,
        query: KnowledgeUnit,
        corpus: Sequence[Any],
        observer: Any = None,
    ) -> RetrievalResult:
        if not isinstance(query, KnowledgeUnit):
            raise TypeError(
                "query must be a KnowledgeUnit, got " + type(query).__name__
            )
        eff_obs = observer if observer is not None else self._observer
        coordinator = RetrievalCoordinator(
            strategy=self._name,
            min_shared_terms=self._min_shared_terms,
            max_candidates=self._max_candidates,
            observer=eff_obs,
        )
        return coordinator.run(query, corpus)


# Short, stable alias used as the default retriever.
DefaultRetriever = TokenOverlapRetriever


def _matched_terms(query_terms: set[str], unit: KnowledgeUnit) -> tuple[str, ...]:
    return tuple(sorted(query_terms.intersection(tokenize(_unit_text(unit)))))
