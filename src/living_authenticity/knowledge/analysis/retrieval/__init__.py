"""Retrieval package (optional vector exports resolved lazily)."""
from typing import TYPE_CHECKING

from .base_retriever import KnowledgeRetriever
from .candidate import RetrievedCandidate
from .result import RetrievalResult
from .retriever_registry import RetrieverRegistry
from .store import InMemoryKnowledgeStore, KnowledgeCorpus
from .token_overlap import (
    DefaultRetriever,
    TokenOverlapRetriever,
    normalize_query_text,
    tokenize,
)

# ``LanceDBVectorRetriever`` depends on the optional ``lancedb`` package
# through ``database.lancedb_manager``. It is exported lazily so that
# importing the retrieval package (and therefore the pipeline) does not
# require the optional vector-database dependency to be installed.
# ``TYPE_CHECKING`` gives static analysers a real import to resolve
# ``__all__`` against, while runtime keeps the lazy ``__getattr__`` path.
_VECTOR_EXPORTS = {"LanceDBVectorRetriever"}

if TYPE_CHECKING:
    from .lancedb_retriever import LanceDBVectorRetriever


def __getattr__(name):
    """Provide optional vector exports without an eager heavy import."""
    if name in _VECTOR_EXPORTS:
        from .lancedb_retriever import LanceDBVectorRetriever

        return LanceDBVectorRetriever
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = (
    "RetrievedCandidate",
    "RetrievalResult",
    "KnowledgeRetriever",
    "LanceDBVectorRetriever",
    "RetrieverRegistry",
    "KnowledgeCorpus",
    "InMemoryKnowledgeStore",
    "TokenOverlapRetriever",
    "DefaultRetriever",
    "normalize_query_text",
    "tokenize",
)
