from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)

from .result import RetrievalResult


class KnowledgeRetriever(ABC):
    """Find existing knowledge relevant to one query knowledge unit.

    Retrieval is an analytical evidence-gathering stage. It must never:

    * decide duplicates, updates, merges, novelty, or relations;
    * calculate or authorize confidence;
    * approve or execute an action;
    * create, modify, move, or delete notes;
    * mutate the query unit or any corpus unit;
    * perform filesystem, network, or provider operations;
    * invent candidate identities, sources, or content.

    A retriever that cannot find anything meaningful must say so explicitly
    (a :class:`RetrievalResult` with no candidates and an explanatory note)
    rather than fabricating evidence.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable strategy name recorded on results and candidates."""
        raise NotImplementedError

    @property
    def cache(self) -> Any:
        """Optional temporary embedding cache attached to this retriever."""
        return None

    @abstractmethod
    def retrieve(
        self, query: KnowledgeUnit, corpus: Sequence[KnowledgeUnit]
    ) -> RetrievalResult:
        """Return existing-knowledge candidates relevant to ``query``."""
        raise NotImplementedError
